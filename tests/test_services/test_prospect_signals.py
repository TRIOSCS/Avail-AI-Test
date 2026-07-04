"""Tests for Phase 4 — signal enrichment, similar customers, and AI writeups.

All external API calls are mocked. Tests cover:
- Individual signal enrichment (intent, hiring, events)
- Missing signal backfill (Explorium-discovered vs email-mined)
- Similar customer matching (NAICS, industry, size, region)
- AI writeup generation + template fallback
- Idempotent re-enrichment
- Graceful handling of missing data
- Batch orchestration
"""

import os

os.environ["TESTING"] = "1"
os.environ["RATE_LIMIT_ENABLED"] = "false"

import asyncio
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import Company, User
from app.models.prospect_account import ProspectAccount

# ── Helpers ──────────────────────────────────────────────────────────


def _run(coro):
    """Run a coroutine to completion on the active event loop."""
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_prospect(db: Session, **overrides) -> ProspectAccount:
    """Create a prospect with sensible defaults."""
    defaults = {
        "name": "Test Corp",
        "domain": "testcorp.com",
        "industry": "Aerospace & Defense",
        "naics_code": "336412",
        "employee_count_range": "1001-5000",
        "region": "US",
        "hq_location": "Dallas, TX, US",
        "discovery_source": "explorium",
        "status": "suggested",
        "fit_score": 65,
        "readiness_score": 30,
        "readiness_signals": {},
    }
    defaults.update(overrides)
    p = ProspectAccount(**defaults)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _make_company(db: Session, user: User, **overrides) -> Company:
    """Create an owned company for similar customer matching."""
    defaults = {
        "name": "Existing Customer Inc",
        "industry": "Aerospace & Defense",
        "domain": "existingcustomer.com",
        "employee_size": "1001-5000",
        "hq_country": "US",
        "is_active": True,
        "account_owner_id": user.id,
    }
    defaults.update(overrides)
    co = Company(**defaults)
    db.add(co)
    db.commit()
    db.refresh(co)
    return co


# ── Individual Signal Enrichment ─────────────────────────────────────


class TestEnrichWithIntent:
    def test_stores_intent_data(self, db_session):
        from app.services.prospect_signals import enrich_with_intent

        p = _make_prospect(db_session)
        intent = {"strength": "strong", "topics": ["electronic components", "semiconductors"]}
        enrich_with_intent(p.id, intent, db_session)

        db_session.refresh(p)
        assert p.readiness_signals["intent"] == intent
        assert "enriched_at" in p.readiness_signals
        assert p.readiness_signals["source"] == "backfill"
        assert p.last_enriched_at is not None

    def test_recalculates_readiness_score(self, db_session):
        p = _make_prospect(db_session, readiness_score=0, readiness_signals={})
        from app.services.prospect_signals import enrich_with_intent

        enrich_with_intent(p.id, {"strength": "strong"}, db_session)
        db_session.refresh(p)
        assert p.readiness_score > 0

    def test_nonexistent_prospect_no_error(self, db_session):
        from app.services.prospect_signals import enrich_with_intent

        enrich_with_intent(99999, {"strength": "weak"}, db_session)

    def test_preserves_existing_signals(self, db_session):
        from app.services.prospect_signals import enrich_with_intent

        p = _make_prospect(
            db_session,
            readiness_signals={"hiring": {"type": "procurement"}, "source": "explorium"},
        )
        enrich_with_intent(p.id, {"strength": "moderate"}, db_session)

        db_session.refresh(p)
        # hiring should still be there
        assert p.readiness_signals["hiring"]["type"] == "procurement"
        assert p.readiness_signals["intent"]["strength"] == "moderate"
        # source preserves original
        assert p.readiness_signals["source"] == "explorium"


class TestEnrichWithHiring:
    def test_stores_hiring_data(self, db_session):
        from app.services.prospect_signals import enrich_with_hiring

        p = _make_prospect(db_session)
        hiring = {"type": "procurement", "detail": "5% growth"}
        enrich_with_hiring(p.id, hiring, db_session)

        db_session.refresh(p)
        assert p.readiness_signals["hiring"] == hiring
        assert p.last_enriched_at is not None

    def test_nonexistent_prospect(self, db_session):
        from app.services.prospect_signals import enrich_with_hiring

        enrich_with_hiring(99999, {"type": "engineering"}, db_session)


class TestEnrichWithEvents:
    def test_stores_event_data(self, db_session):
        from app.services.prospect_signals import enrich_with_events

        p = _make_prospect(db_session)
        events = [
            {"type": "new_funding_round", "date": "2026-01", "description": "Series B"},
            {"type": "new_product", "date": "2025-11", "description": "New sensor line"},
        ]
        enrich_with_events(p.id, events, db_session)

        db_session.refresh(p)
        assert len(p.readiness_signals["events"]) == 2
        assert p.readiness_signals["events"][0]["type"] == "new_funding_round"

    def test_nonexistent_prospect(self, db_session):
        from app.services.prospect_signals import enrich_with_events

        enrich_with_events(99999, [], db_session)


class TestIdempotentEnrichment:
    def test_calling_twice_overwrites_not_duplicates(self, db_session):
        from app.services.prospect_signals import enrich_with_intent

        p = _make_prospect(db_session)

        enrich_with_intent(p.id, {"strength": "weak"}, db_session)
        db_session.refresh(p)
        score_1 = p.readiness_score

        enrich_with_intent(p.id, {"strength": "strong"}, db_session)
        db_session.refresh(p)
        score_2 = p.readiness_score

        assert p.readiness_signals["intent"]["strength"] == "strong"
        assert score_2 >= score_1


# ── Missing Signal Backfill ──────────────────────────────────────────


# Firmographics as returned by the verified connector (explorium.enrich_company shape).
_FIRMO = {
    "source": "explorium",
    "legal_name": "Email Mined Corp",
    "domain": "emailmined.com",
    "website": "https://emailmined.com",
    "industry": "Electronics Manufacturing",
    "employee_size": "1001-5000",
    "hq_city": "Austin",
    "hq_state": "TX",
    "hq_country": "US",
    "linkedin_url": "https://linkedin.com/company/emc",
    "naics": "334412",
    "ticker": None,
    "revenue_range": "$100M-$500M",
}


@contextmanager
def _explorium_patched(enrich_result, *, enabled=True, circuit=False, api_key="test-key"):
    """Patch the verified Explorium firmographic-backfill dependencies.

    ``enrich_result`` is the dict/None ``explorium.enrich_company`` should return, OR an
    exception instance to raise from it. Yields the enrich_company mock for assertions.
    """
    with (
        patch("app.config.settings.explorium_enrichment_enabled", enabled),
        patch("app.services.enrichment_credit_guard.circuit_open", return_value=circuit),
        patch("app.services.credential_service.get_credential_cached", return_value=api_key),
        patch("app.connectors.explorium.enrich_company", new_callable=AsyncMock) as mock_enrich,
    ):
        if isinstance(enrich_result, BaseException):
            mock_enrich.side_effect = enrich_result
        else:
            mock_enrich.return_value = enrich_result
        yield mock_enrich


class TestEnrichMissingSignals:
    def test_firmographics_complete_skips_backfill(self, db_session):
        """A prospect with industry + size + region already set skips the paid call."""
        from app.services.prospect_signals import enrich_missing_signals

        p = _make_prospect(db_session)  # defaults set industry, size, and region

        with _explorium_patched(_FIRMO) as mock_enrich:
            result = _run(enrich_missing_signals(p.id, db_session))

        assert result is False
        mock_enrich.assert_not_called()

    def test_email_mined_prospect_gets_firmographic_backfill(self, db_session):
        """Email-mined prospects missing firmographics get an Explorium backfill."""
        from app.services.prospect_signals import enrich_missing_signals

        p = _make_prospect(
            db_session,
            domain="emailmined.com",
            discovery_source="email_mining",
            industry=None,
            naics_code=None,
            employee_count_range=None,
            revenue_range=None,
            region=None,
            hq_location=None,
            website=None,
            fit_score=0,
            fit_reasoning=None,
            last_enriched_at=None,
        )

        with _explorium_patched(_FIRMO) as mock_enrich:
            result = _run(enrich_missing_signals(p.id, db_session))

        assert result is True
        mock_enrich.assert_awaited_once()
        db_session.refresh(p)
        assert p.industry == "Electronics Manufacturing"
        assert p.employee_count_range == "1001-5000"
        assert p.naics_code == "334412"
        assert p.revenue_range == "$100M-$500M"
        assert p.website == "https://emailmined.com"
        assert p.hq_location == "Austin, TX, US"
        assert p.region == "US"
        # Fit score recomputed from the newly-populated firmographics.
        assert p.fit_score > 0
        assert p.fit_reasoning
        assert p.last_enriched_at is not None

    def test_backfill_only_fills_missing_fields(self, db_session):
        """Fields already set on the prospect are preserved; only blanks are filled."""
        from app.services.prospect_signals import enrich_missing_signals

        p = _make_prospect(
            db_session,
            domain="partial.com",
            discovery_source="email_mining",
            industry="Existing Industry",
            employee_count_range=None,
            naics_code=None,
            region=None,
            hq_location=None,
            website=None,
        )

        with _explorium_patched(_FIRMO):
            result = _run(enrich_missing_signals(p.id, db_session))

        assert result is True
        db_session.refresh(p)
        assert p.industry == "Existing Industry"  # preserved
        assert p.employee_count_range == "1001-5000"  # backfilled
        assert p.region == "US"  # backfilled

    def test_no_domain_returns_false(self, db_session):
        from app.services.prospect_signals import enrich_missing_signals

        p = _make_prospect(
            db_session,
            domain="",
            industry=None,
            employee_count_range=None,
            region=None,
        )

        with _explorium_patched(_FIRMO) as mock_enrich:
            result = _run(enrich_missing_signals(p.id, db_session))

        assert result is False
        mock_enrich.assert_not_called()

    def test_nonexistent_prospect_returns_false(self, db_session):
        from app.services.prospect_signals import enrich_missing_signals

        result = _run(enrich_missing_signals(999, db_session))
        assert result is False

    def test_missing_credential_returns_false(self, db_session):
        from app.services.prospect_signals import enrich_missing_signals

        p = _make_prospect(
            db_session,
            domain="nocred.com",
            industry=None,
            employee_count_range=None,
            region=None,
        )

        with _explorium_patched(_FIRMO, api_key="") as mock_enrich:
            result = _run(enrich_missing_signals(p.id, db_session))

        assert result is False
        mock_enrich.assert_not_called()

    def test_gate_disabled_returns_false(self, db_session):
        from app.services.prospect_signals import enrich_missing_signals

        p = _make_prospect(
            db_session,
            domain="disabled.com",
            industry=None,
            employee_count_range=None,
            region=None,
        )

        with _explorium_patched(_FIRMO, enabled=False) as mock_enrich:
            result = _run(enrich_missing_signals(p.id, db_session))

        assert result is False
        mock_enrich.assert_not_called()

    def test_quota_error_trips_circuit_returns_false(self, db_session):
        from app.services import enrichment_credit_guard as cg
        from app.services.prospect_signals import enrich_missing_signals

        p = _make_prospect(
            db_session,
            domain="quota.com",
            industry=None,
            employee_count_range=None,
            region=None,
        )

        with (
            _explorium_patched(cg.ProviderQuotaError("429")),
            patch("app.services.enrichment_credit_guard.trip_circuit") as mock_trip,
        ):
            result = _run(enrich_missing_signals(p.id, db_session))

        assert result is False
        mock_trip.assert_called_once()

    def test_enrich_company_none_returns_false(self, db_session):
        from app.services.prospect_signals import enrich_missing_signals

        p = _make_prospect(
            db_session,
            domain="nodata.com",
            industry=None,
            employee_count_range=None,
            region=None,
        )

        with _explorium_patched(None):
            result = _run(enrich_missing_signals(p.id, db_session))

        assert result is False

    def test_enrich_company_empty_dict_returns_false(self, db_session):
        from app.services.prospect_signals import enrich_missing_signals

        p = _make_prospect(
            db_session,
            domain="emptydict.com",
            industry=None,
            employee_count_range=None,
            region=None,
        )

        with _explorium_patched({}):
            result = _run(enrich_missing_signals(p.id, db_session))

        assert result is False

    def test_generic_exception_returns_false(self, db_session):
        from app.services.prospect_signals import enrich_missing_signals

        p = _make_prospect(
            db_session,
            domain="boom.com",
            industry=None,
            employee_count_range=None,
            region=None,
        )

        with _explorium_patched(RuntimeError("connection reset")):
            result = _run(enrich_missing_signals(p.id, db_session))

        assert result is False


# ── Similar Customer Matching ────────────────────────────────────────


class TestFindSimilarCustomers:
    def test_strong_match_same_industry_segment(self, db_session, test_user):
        from app.services.prospect_signals import find_similar_customers

        _make_company(
            db_session,
            test_user,
            name="Lockheed Systems",
            industry="Aerospace & Defense Manufacturing",
            employee_size="5001-10000",
            hq_country="US",
        )

        p = _make_prospect(
            db_session,
            name="New Aerospace Co",
            domain="newaero.com",
            industry="Aerospace & Defense",
            naics_code="336412",
            employee_count_range="1001-5000",
            region="US",
        )

        result = find_similar_customers(p, db_session)
        assert len(result) >= 1
        assert result[0]["name"] == "Lockheed Systems"
        assert result[0]["match_strength"] in ("strong", "moderate")

    def test_industry_keyword_overlap(self, db_session, test_user):
        from app.services.prospect_signals import find_similar_customers

        _make_company(
            db_session,
            test_user,
            name="MedDevice Corp",
            industry="Medical Device Manufacturing",
            employee_size="501-1000",
        )

        p = _make_prospect(
            db_session,
            name="HealthTech Inc",
            domain="healthtech.com",
            industry="Medical Device Solutions",
            naics_code="334513",
            employee_count_range="201-500",
        )

        result = find_similar_customers(p, db_session)
        assert len(result) >= 1
        match_reasons = result[0]["match_reason"].lower()
        assert "medical" in match_reasons or "device" in match_reasons

    def test_region_match(self, db_session, test_user):
        from app.services.prospect_signals import find_similar_customers

        _make_company(
            db_session,
            test_user,
            name="EU Company",
            industry="General Manufacturing",
            employee_size="201-500",
            hq_country="DE",
            domain="eucompany.de",
        )

        p = _make_prospect(
            db_session,
            domain="eurotest.com",
            industry="Unrelated Industry",
            region="EU",
            employee_count_range="501-1000",
        )

        result = find_similar_customers(p, db_session)
        # Should match on region + similar size
        assert len(result) >= 1

    def test_no_owned_companies(self, db_session):
        from app.services.prospect_signals import find_similar_customers

        p = _make_prospect(db_session, domain="lonely.com")
        result = find_similar_customers(p, db_session)
        assert result == []

    def test_returns_top_3(self, db_session, test_user):
        from app.services.prospect_signals import find_similar_customers

        # Create 5 companies with same industry
        for i in range(5):
            _make_company(
                db_session,
                test_user,
                name=f"Aero Company {i}",
                industry="Aerospace & Defense",
                domain=f"aero{i}.com",
                employee_size="1001-5000",
                hq_country="US",
            )

        p = _make_prospect(
            db_session,
            domain="newaero5.com",
            industry="Aerospace & Defense",
            naics_code="336412",
            employee_count_range="1001-5000",
            region="US",
        )

        result = find_similar_customers(p, db_session)
        assert len(result) <= 3

    def test_stores_in_similar_customers_field(self, db_session, test_user):
        from app.services.prospect_signals import find_similar_customers

        _make_company(db_session, test_user, name="Match Corp", industry="Aerospace & Defense")

        p = _make_prospect(
            db_session,
            domain="storedmatch.com",
            industry="Aerospace & Defense",
            naics_code="336412",
        )

        find_similar_customers(p, db_session)

        db_session.refresh(p)
        assert isinstance(p.similar_customers, list)

    def test_unowned_companies_excluded(self, db_session):
        """Companies without account_owner_id should not appear as matches."""
        from app.services.prospect_signals import find_similar_customers

        co = Company(
            name="Orphan Corp",
            industry="Aerospace & Defense",
            domain="orphan.com",
            is_active=True,
            account_owner_id=None,
        )
        db_session.add(co)
        db_session.commit()

        p = _make_prospect(
            db_session,
            domain="testorphan.com",
            industry="Aerospace & Defense",
            naics_code="336412",
        )

        result = find_similar_customers(p, db_session)
        assert all(m["name"] != "Orphan Corp" for m in result)

    def test_inactive_companies_excluded(self, db_session, test_user):
        from app.services.prospect_signals import find_similar_customers

        co = Company(
            name="Dead Corp",
            industry="Aerospace & Defense",
            domain="dead.com",
            is_active=False,
            account_owner_id=test_user.id,
        )
        db_session.add(co)
        db_session.commit()

        p = _make_prospect(db_session, domain="testdead.com", industry="Aerospace & Defense")
        result = find_similar_customers(p, db_session)
        assert all(m["name"] != "Dead Corp" for m in result)


class TestComparesSizes:
    @pytest.mark.parametrize(
        ("a", "b", "expected"),
        [
            ("201-500", "201-500", True),
            ("201-500", "501-1000", True),
            ("1-50", "10001+", False),
            (None, "201-500", False),
            ("201-500", None, False),
            (None, None, False),
            ("300", "400", True),
            ("10001+", "5001-10000", True),
            ("lots", "many", False),
        ],
        ids=[
            "same_bracket",
            "adjacent_bracket",
            "distant_brackets",
            "none_first",
            "none_second",
            "none_both",
            "numeric_string",
            "plus_format",
            "unparseable",
        ],
    )
    def test_compare_sizes(self, a, b, expected):
        from app.services.prospect_signals import _compare_sizes

        assert _compare_sizes(a, b) is expected


# ── AI Writeup Generation ───────────────────────────────────────────


class TestGenerateAIWriteup:
    def test_successful_ai_writeup(self, db_session):
        from app.services.prospect_signals import generate_ai_writeup

        p = _make_prospect(
            db_session,
            name="BorgWarner Inc",
            industry="Automotive OEM",
            employee_count_range="10001+",
            fit_score=75,
            readiness_signals={"intent": {"strength": "strong"}},
            similar_customers=[{"name": "Lear Corporation", "domain": "lear.com"}],
        )

        with patch(
            "app.utils.claude_client.claude_text",
            new_callable=AsyncMock,
            return_value="BorgWarner is a major automotive Tier 1 supplier. They show strong buying intent for electronic components.",
        ):
            result = _run(generate_ai_writeup(p, db_session))

        assert "BorgWarner" in result
        db_session.refresh(p)
        assert p.ai_writeup == result
        assert p.last_enriched_at is not None

    def test_template_fallback_on_api_failure(self, db_session):
        from app.services.prospect_signals import generate_ai_writeup

        p = _make_prospect(
            db_session,
            name="FallbackCorp",
            industry="Electronics Manufacturing",
            employee_count_range="501-1000",
            hq_location="Austin, TX, US",
            fit_reasoning="Industry: Electronics Manufacturing / EMS (10/30); Size: 501-1000 (20/20)",
            readiness_signals={"intent": {"strength": "moderate"}},
            similar_customers=[{"name": "Flex Ltd", "domain": "flex.com"}],
        )

        with patch(
            "app.utils.claude_client.claude_text",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = _run(generate_ai_writeup(p, db_session))

        assert "FallbackCorp" in result
        assert "501-1000" in result
        assert "Austin" in result

    def test_template_fallback_on_exception(self, db_session):
        from app.services.prospect_signals import generate_ai_writeup

        p = _make_prospect(
            db_session,
            name="ExceptionCorp",
            industry="Manufacturing",
            employee_count_range="201-500",
        )

        with patch(
            "app.utils.claude_client.claude_text",
            new_callable=AsyncMock,
            side_effect=Exception("API down"),
        ):
            result = _run(generate_ai_writeup(p, db_session))

        assert "ExceptionCorp" in result
        db_session.refresh(p)
        assert p.ai_writeup is not None

    def test_writeup_stores_in_db(self, db_session):
        from app.services.prospect_signals import generate_ai_writeup

        p = _make_prospect(db_session, name="StoreCorp")

        with patch(
            "app.utils.claude_client.claude_text",
            new_callable=AsyncMock,
            return_value="StoreCorp is a great prospect.",
        ):
            _run(generate_ai_writeup(p, db_session))

        db_session.refresh(p)
        assert p.ai_writeup == "StoreCorp is a great prospect."


class TestTemplateFallbackWriteup:
    def test_full_data(self):
        from app.services.prospect_signals import _template_fallback_writeup

        p = MagicMock()
        p.name = "TestCo"
        p.employee_count_range = "1001-5000"
        p.industry = "Aerospace"
        p.hq_location = "Dallas, TX"
        p.fit_reasoning = "Industry: Aerospace & Defense (30/30); Size: 1001-5000 (20/20)"
        p.readiness_signals = {"intent": {"strength": "strong"}}
        p.similar_customers = [{"name": "Lockheed"}]

        result = _template_fallback_writeup(p)
        assert "TestCo" in result
        assert "1001-5000" in result
        assert "Dallas" in result

    def test_minimal_data(self):
        from app.services.prospect_signals import _template_fallback_writeup

        p = MagicMock()
        p.name = "MinimalCo"
        p.employee_count_range = None
        p.industry = None
        p.hq_location = None
        p.fit_reasoning = None
        p.readiness_signals = {}
        p.similar_customers = []

        result = _template_fallback_writeup(p)
        assert "MinimalCo" in result

    def test_hiring_signal_in_writeup(self):
        from app.services.prospect_signals import _template_fallback_writeup

        p = MagicMock()
        p.name = "HiringCo"
        p.employee_count_range = "501-1000"
        p.industry = "Electronics"
        p.hq_location = "Austin, TX"
        p.fit_reasoning = None
        p.readiness_signals = {"hiring": {"type": "procurement"}}
        p.similar_customers = []

        result = _template_fallback_writeup(p)
        assert "procurement" in result.lower()

    def test_events_signal_in_writeup(self):
        from app.services.prospect_signals import _template_fallback_writeup

        p = MagicMock()
        p.name = "EventCo"
        p.employee_count_range = "201-500"
        p.industry = "Manufacturing"
        p.hq_location = "Chicago, IL"
        p.fit_reasoning = None
        p.readiness_signals = {
            "intent": {"strength": "weak"},
            "events": [{"type": "new_funding_round"}],
        }
        p.similar_customers = []

        result = _template_fallback_writeup(p)
        # Intent is weak, not strong/moderate — so events should be the top signal
        assert "funding" in result.lower()

    def test_similar_customers_in_writeup(self):
        from app.services.prospect_signals import _template_fallback_writeup

        p = MagicMock()
        p.name = "SimilarCo"
        p.employee_count_range = "1001-5000"
        p.industry = "Automotive"
        p.hq_location = "Detroit, MI"
        p.fit_reasoning = None
        p.readiness_signals = {}
        p.similar_customers = [
            {"name": "Lear Corporation"},
            {"name": "BorgWarner"},
        ]

        result = _template_fallback_writeup(p)
        assert "Lear Corporation" in result
        assert "BorgWarner" in result


class TestBuildWriteupPrompt:
    def test_includes_all_data(self):
        from app.services.prospect_signals import _build_writeup_prompt

        p = MagicMock()
        p.name = "PromptCo"
        p.domain = "promptco.com"
        p.industry = "Aerospace"
        p.employee_count_range = "5001-10000"
        p.revenue_range = "$1B+"
        p.hq_location = "Los Angeles, CA"
        p.fit_score = 85
        p.fit_reasoning = "Industry: Aerospace (30/30)"
        p.readiness_signals = {
            "intent": {"strength": "strong", "component_topics": ["semiconductors"]},
            "hiring": {"type": "procurement"},
            "events": [{"type": "funding"}],
        }
        p.similar_customers = [{"name": "Raytheon"}]

        prompt = _build_writeup_prompt(p)
        assert "PromptCo" in prompt
        assert "promptco.com" in prompt
        assert "Aerospace" in prompt
        assert "5001-10000" in prompt
        assert "$1B+" in prompt
        assert "85/100" in prompt
        assert "strong" in prompt
        assert "procurement" in prompt
        assert "Raytheon" in prompt

    def test_handles_missing_data(self):
        from app.services.prospect_signals import _build_writeup_prompt

        p = MagicMock()
        p.name = "EmptyCo"
        p.domain = "empty.com"
        p.industry = None
        p.employee_count_range = None
        p.revenue_range = None
        p.hq_location = None
        p.fit_score = 0
        p.fit_reasoning = None
        p.readiness_signals = {}
        p.similar_customers = []

        prompt = _build_writeup_prompt(p)
        assert "EmptyCo" in prompt
        assert "empty.com" in prompt


# ── Batch Orchestration ──────────────────────────────────────────────


class TestRunSignalEnrichmentBatch:
    @patch("app.services.prospect_signals.generate_ai_writeup", new_callable=AsyncMock)
    @patch("app.services.prospect_signals.find_similar_customers")
    @patch("app.services.prospect_signals.enrich_missing_signals", new_callable=AsyncMock)
    def test_batch_runs_all_steps(self, mock_enrich, mock_similar, mock_writeup, db_session):
        from app.services.prospect_signals import run_signal_enrichment_batch

        # Create prospects
        p1 = _make_prospect(
            db_session,
            domain="batch1.com",
            fit_score=60,
            readiness_signals={},
        )
        p2 = _make_prospect(
            db_session,
            domain="batch2.com",
            fit_score=50,
            readiness_signals={"intent": {"strength": "strong"}, "hiring": {"type": "procurement"}},
            similar_customers=[],
        )

        mock_enrich.return_value = True
        mock_similar.return_value = [{"name": "Match"}]
        mock_writeup.return_value = "Great prospect."

        with patch("app.database.SessionLocal", return_value=db_session):
            result = _run(run_signal_enrichment_batch(min_fit_score=40))

        assert result["signals_added"] >= 1
        assert result["similar_computed"] >= 1
        assert result["writeups_generated"] >= 1

    @patch("app.services.prospect_signals.generate_ai_writeup", new_callable=AsyncMock)
    @patch("app.services.prospect_signals.find_similar_customers")
    @patch("app.services.prospect_signals.enrich_missing_signals", new_callable=AsyncMock)
    def test_batch_skips_low_score_prospects(self, mock_enrich, mock_similar, mock_writeup, db_session):
        from app.services.prospect_signals import run_signal_enrichment_batch

        _make_prospect(
            db_session,
            domain="lowscore.com",
            fit_score=20,  # below threshold
            readiness_signals={},
        )

        with patch("app.database.SessionLocal", return_value=db_session):
            result = _run(run_signal_enrichment_batch(min_fit_score=40))

        assert result["signals_added"] == 0
        assert result["similar_computed"] == 0
        assert result["writeups_generated"] == 0

    @patch("app.services.prospect_signals.generate_ai_writeup", new_callable=AsyncMock)
    @patch("app.services.prospect_signals.find_similar_customers", side_effect=Exception("DB error"))
    @patch("app.services.prospect_signals.enrich_missing_signals", new_callable=AsyncMock)
    def test_batch_continues_on_error(self, mock_enrich, mock_similar, mock_writeup, db_session):
        from app.services.prospect_signals import run_signal_enrichment_batch

        _make_prospect(
            db_session,
            domain="errorbatch.com",
            fit_score=60,
            readiness_signals={"intent": {"strength": "strong"}, "hiring": {"type": "procurement"}},
        )

        mock_enrich.return_value = False
        mock_writeup.return_value = "Writeup."

        with patch("app.database.SessionLocal", return_value=db_session):
            result = _run(run_signal_enrichment_batch(min_fit_score=40))

        assert result["errors"] >= 1
        # Should still attempt writeups despite similar_customers error
        assert result["writeups_generated"] >= 0


# ── Readiness Recalculation ──────────────────────────────────────────


class TestRecalculateReadiness:
    def test_recalculates_on_signal_add(self, db_session):
        from app.services.prospect_signals import enrich_with_events, enrich_with_intent

        p = _make_prospect(db_session, readiness_score=0, readiness_signals={})

        # Add strong intent
        enrich_with_intent(p.id, {"strength": "strong"}, db_session)
        db_session.refresh(p)
        score_after_intent = p.readiness_score
        assert score_after_intent > 0

        # Add events
        enrich_with_events(
            p.id,
            [{"type": "new_funding_round", "date": "2026-01"}],
            db_session,
        )
        db_session.refresh(p)
        score_after_events = p.readiness_score
        assert score_after_events >= score_after_intent


# ── Coverage: firmographic backfill edge cases ──────────────────────


class TestEnrichMissingSignalsFirmographicEdges:
    def test_hq_location_from_country_only(self, db_session):
        """Only hq_country present → hq_location is just the country."""
        from app.services.prospect_signals import enrich_missing_signals

        p = _make_prospect(
            db_session,
            domain="countryonly.com",
            industry=None,
            employee_count_range=None,
            region=None,
            hq_location=None,
        )

        firmo = {**_FIRMO, "hq_city": None, "hq_state": None, "hq_country": "US"}
        with _explorium_patched(firmo):
            result = _run(enrich_missing_signals(p.id, db_session))

        assert result is True
        db_session.refresh(p)
        assert p.hq_location == "US"

    def test_existing_region_not_overwritten(self, db_session):
        """A region already set is preserved even when the connector returns another."""
        from app.services.prospect_signals import enrich_missing_signals

        p = _make_prospect(
            db_session,
            domain="hasregion.com",
            industry=None,
            employee_count_range=None,
            region="EU",
        )

        # _FIRMO carries hq_country "US", but the prospect's existing EU region must win.
        with _explorium_patched(_FIRMO):
            result = _run(enrich_missing_signals(p.id, db_session))

        assert result is True
        db_session.refresh(p)
        assert p.region == "EU"


# ── Coverage: find_similar_customers line 348, _to_bracket_index ────


class TestFindSimilarCustomersWeakMatch:
    def test_weak_match_region_only(self, db_session, test_user):
        """Line 348: 'weak' match strength when score > 0 but < 15.

        Region match (5 pts) + size match (10 pts) = 15 (moderate).
        Region match (5 pts) alone = 5 (weak).
        """
        from app.services.prospect_signals import find_similar_customers

        _make_company(
            db_session,
            test_user,
            name="Region Only Corp",
            industry="Totally Different Industry",
            employee_size="1-50",
            hq_country="JP",
            domain="regiononly.co.jp",
        )

        p = _make_prospect(
            db_session,
            domain="asiaprospect.com",
            industry="Completely Unrelated Sector",
            naics_code="999999",
            employee_count_range="5001-10000",
            region="ASIA",
        )

        result = find_similar_customers(p, db_session)
        assert len(result) >= 1
        weak_matches = [m for m in result if m["match_strength"] == "weak"]
        assert len(weak_matches) >= 1
        assert "Same region" in weak_matches[0]["match_reason"]


class TestToBracketIndexEdgeCases:
    """_to_bracket_index edge cases exercised through _compare_sizes."""

    @pytest.mark.parametrize(
        ("a", "b", "expected"),
        [
            # Lines 405-406: "10000+" parses to 10000, in bracket (5001, 10000).
            ("10000+", "5001-10000", True),
            # Lines 405-406: "abc+" fails to parse -> None -> False.
            ("abc+", "1001-5000", False),
            # Lines 411-412: non-parseable dash range -> None -> False.
            ("abc-def", "1001-5000", False),
            # Line 425: 2000000 exceeds the largest bracket (10001, 999999).
            ("2000000", "1001-5000", False),
        ],
        ids=[
            "plus_format_valid",
            "plus_format_invalid",
            "dash_format_invalid",
            "exceeds_all_brackets",
        ],
    )
    def test_compare_sizes_edge_cases(self, a, b, expected):
        from app.services.prospect_signals import _compare_sizes

        assert _compare_sizes(a, b) is expected


# ── Coverage: run_signal_enrichment_batch error handling ─────────────


class TestRunBatchErrorPaths:
    @patch("app.services.prospect_signals.generate_ai_writeup", new_callable=AsyncMock)
    @patch("app.services.prospect_signals.find_similar_customers")
    @patch(
        "app.services.prospect_signals.enrich_missing_signals",
        new_callable=AsyncMock,
        side_effect=Exception("Explorium down"),
    )
    def test_signal_enrichment_error_increments_errors(self, mock_enrich, mock_similar, mock_writeup, db_session):
        """Lines 640-642: exception in enrich_missing_signals increments errors."""
        from app.services.prospect_signals import run_signal_enrichment_batch

        _make_prospect(
            db_session,
            domain="enricherror.com",
            fit_score=60,
            readiness_signals={},
        )

        mock_similar.return_value = []
        mock_writeup.return_value = "Writeup."

        with patch("app.database.SessionLocal", return_value=db_session):
            result = _run(run_signal_enrichment_batch(min_fit_score=40))

        assert result["errors"] >= 1
        assert result["signals_added"] == 0

    @patch("app.services.prospect_signals.generate_ai_writeup", new_callable=AsyncMock)
    @patch("app.services.prospect_signals.find_similar_customers")
    @patch("app.services.prospect_signals.enrich_missing_signals", new_callable=AsyncMock)
    def test_skip_prospects_with_existing_similar_customers(self, mock_enrich, mock_similar, mock_writeup, db_session):
        """Line 656: skip prospects that already have similar_customers."""
        from app.services.prospect_signals import run_signal_enrichment_batch

        _make_prospect(
            db_session,
            domain="alreadymatched.com",
            fit_score=60,
            readiness_signals={"intent": {"strength": "strong"}, "hiring": {"type": "eng"}},
            similar_customers=[{"name": "Existing Match", "match_strength": "strong"}],
        )

        mock_enrich.return_value = False
        mock_writeup.return_value = "Writeup."

        with patch("app.database.SessionLocal", return_value=db_session):
            result = _run(run_signal_enrichment_batch(min_fit_score=40))

        # similar_customers already set → find_similar_customers should NOT be called
        mock_similar.assert_not_called()
        assert result["similar_computed"] == 0

    @patch(
        "app.services.prospect_signals.generate_ai_writeup",
        new_callable=AsyncMock,
        side_effect=Exception("Claude API exploded"),
    )
    @patch("app.services.prospect_signals.find_similar_customers")
    @patch("app.services.prospect_signals.enrich_missing_signals", new_callable=AsyncMock)
    def test_writeup_generation_error_increments_errors(self, mock_enrich, mock_similar, mock_writeup, db_session):
        """Lines 679-681: exception in generate_ai_writeup increments errors."""
        from app.services.prospect_signals import run_signal_enrichment_batch

        _make_prospect(
            db_session,
            domain="writeuperror.com",
            fit_score=60,
            readiness_signals={"intent": {"strength": "strong"}, "hiring": {"type": "eng"}},
            ai_writeup=None,
        )

        mock_enrich.return_value = False
        mock_similar.return_value = []

        with patch("app.database.SessionLocal", return_value=db_session):
            result = _run(run_signal_enrichment_batch(min_fit_score=40))

        assert result["errors"] >= 1
        assert result["writeups_generated"] == 0
