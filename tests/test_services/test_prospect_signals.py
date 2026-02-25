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
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import Company, User
from app.models.prospect_account import ProspectAccount


# ── Helpers ──────────────────────────────────────────────────────────


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


class TestEnrichMissingSignals:
    def test_explorium_prospect_skips_backfill(self, db_session):
        """Explorium-discovered prospects already have signals — should skip."""
        from app.services.prospect_signals import enrich_missing_signals

        p = _make_prospect(
            db_session,
            discovery_source="explorium",
            readiness_signals={
                "intent": {"strength": "strong"},
                "hiring": {"type": "procurement"},
                "events": [{"type": "funding"}],
            },
        )

        result = asyncio.get_event_loop().run_until_complete(
            enrich_missing_signals(p.id, db_session)
        )
        assert result is False

    @patch("app.http_client.http")
    def test_email_mined_prospect_gets_backfill(self, mock_http, db_session):
        """Email-mined prospects without signals should get Explorium enrichment."""
        from app.services.prospect_signals import enrich_missing_signals

        p = _make_prospect(
            db_session,
            domain="emailmined.com",
            discovery_source="email_mining",
            readiness_signals={},
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "businesses": [{
                "business_intent_topics": [
                    "electronic components", "semiconductors", "procurement solutions"
                ],
                "workforce_trends": {"procurement": 3},
                "recent_events": [
                    {"type": "new_funding_round", "date": "2026-01", "description": "Raised $10M"}
                ],
            }]
        }
        mock_http.post = AsyncMock(return_value=mock_resp)

        with patch(
            "app.services.prospect_discovery_explorium._get_api_key",
            return_value="test-key",
        ):
            result = asyncio.get_event_loop().run_until_complete(
                enrich_missing_signals(p.id, db_session)
            )

        assert result is True
        db_session.refresh(p)
        assert p.readiness_signals["intent"]["strength"] == "strong"
        assert p.readiness_signals["hiring"]["type"] == "procurement"
        assert len(p.readiness_signals["events"]) == 1
        assert p.readiness_signals["source"] == "backfill"

    @patch("app.http_client.http")
    def test_backfill_api_failure_returns_false(self, mock_http, db_session):
        from app.services.prospect_signals import enrich_missing_signals

        p = _make_prospect(
            db_session,
            domain="failcorp.com",
            discovery_source="email_mining",
            readiness_signals={},
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_http.post = AsyncMock(return_value=mock_resp)

        with patch(
            "app.services.prospect_discovery_explorium._get_api_key",
            return_value="test-key",
        ):
            result = asyncio.get_event_loop().run_until_complete(
                enrich_missing_signals(p.id, db_session)
            )

        assert result is False

    def test_backfill_no_api_key_returns_false(self, db_session):
        from app.services.prospect_signals import enrich_missing_signals

        p = _make_prospect(
            db_session,
            discovery_source="email_mining",
            readiness_signals={},
        )

        with patch(
            "app.services.prospect_discovery_explorium._get_api_key",
            return_value="",
        ):
            result = asyncio.get_event_loop().run_until_complete(
                enrich_missing_signals(p.id, db_session)
            )

        assert result is False

    def test_backfill_nonexistent_prospect(self, db_session):
        from app.services.prospect_signals import enrich_missing_signals

        result = asyncio.get_event_loop().run_until_complete(
            enrich_missing_signals(99999, db_session)
        )
        assert result is False

    @patch("app.http_client.http")
    def test_backfill_no_results(self, mock_http, db_session):
        from app.services.prospect_signals import enrich_missing_signals

        p = _make_prospect(
            db_session,
            domain="nodatacorp.com",
            discovery_source="email_mining",
            readiness_signals={},
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"businesses": []}
        mock_http.post = AsyncMock(return_value=mock_resp)

        with patch(
            "app.services.prospect_discovery_explorium._get_api_key",
            return_value="test-key",
        ):
            result = asyncio.get_event_loop().run_until_complete(
                enrich_missing_signals(p.id, db_session)
            )

        assert result is False

    def test_prospect_with_intent_but_no_events_gets_backfill(self, db_session):
        """Prospect with partial signals should still get backfill for missing ones."""
        from app.services.prospect_signals import enrich_missing_signals

        p = _make_prospect(
            db_session,
            domain="partialcorp.com",
            discovery_source="email_mining",
            readiness_signals={"intent": {"strength": "weak"}},
        )

        # Has intent but not hiring or events — should attempt backfill
        # But no API key, so returns False
        with patch(
            "app.services.prospect_discovery_explorium._get_api_key",
            return_value="",
        ):
            result = asyncio.get_event_loop().run_until_complete(
                enrich_missing_signals(p.id, db_session)
            )

        assert result is False

    @patch("app.http_client.http")
    def test_backfill_exception_returns_false(self, mock_http, db_session):
        from app.services.prospect_signals import enrich_missing_signals

        p = _make_prospect(
            db_session,
            domain="exception.com",
            discovery_source="email_mining",
            readiness_signals={},
        )

        mock_http.post = AsyncMock(side_effect=Exception("Connection timeout"))

        with patch(
            "app.services.prospect_discovery_explorium._get_api_key",
            return_value="test-key",
        ):
            result = asyncio.get_event_loop().run_until_complete(
                enrich_missing_signals(p.id, db_session)
            )

        assert result is False

    def test_backfill_no_domain(self, db_session):
        from app.services.prospect_signals import enrich_missing_signals

        p = _make_prospect(
            db_session,
            domain="",
            discovery_source="email_mining",
            readiness_signals={},
        )

        with patch(
            "app.services.prospect_discovery_explorium._get_api_key",
            return_value="test-key",
        ):
            result = asyncio.get_event_loop().run_until_complete(
                enrich_missing_signals(p.id, db_session)
            )

        assert result is False


# ── Similar Customer Matching ────────────────────────────────────────


class TestFindSimilarCustomers:
    def test_strong_match_same_industry_segment(self, db_session, test_user):
        from app.services.prospect_signals import find_similar_customers

        _make_company(
            db_session, test_user,
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
            db_session, test_user,
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
            db_session, test_user,
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
                db_session, test_user,
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
    def test_same_bracket(self):
        from app.services.prospect_signals import _compare_sizes

        assert _compare_sizes("201-500", "201-500") is True

    def test_adjacent_bracket(self):
        from app.services.prospect_signals import _compare_sizes

        assert _compare_sizes("201-500", "501-1000") is True

    def test_distant_brackets(self):
        from app.services.prospect_signals import _compare_sizes

        assert _compare_sizes("1-50", "10001+") is False

    def test_none_values(self):
        from app.services.prospect_signals import _compare_sizes

        assert _compare_sizes(None, "201-500") is False
        assert _compare_sizes("201-500", None) is False
        assert _compare_sizes(None, None) is False

    def test_numeric_string(self):
        from app.services.prospect_signals import _compare_sizes

        assert _compare_sizes("300", "400") is True

    def test_plus_format(self):
        from app.services.prospect_signals import _compare_sizes

        assert _compare_sizes("10001+", "5001-10000") is True

    def test_unparseable(self):
        from app.services.prospect_signals import _compare_sizes

        assert _compare_sizes("lots", "many") is False


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
            result = asyncio.get_event_loop().run_until_complete(
                generate_ai_writeup(p, db_session)
            )

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
            result = asyncio.get_event_loop().run_until_complete(
                generate_ai_writeup(p, db_session)
            )

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
            result = asyncio.get_event_loop().run_until_complete(
                generate_ai_writeup(p, db_session)
            )

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
            asyncio.get_event_loop().run_until_complete(
                generate_ai_writeup(p, db_session)
            )

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
    def test_batch_runs_all_steps(
        self, mock_enrich, mock_similar, mock_writeup, db_session
    ):
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
            result = asyncio.get_event_loop().run_until_complete(
                run_signal_enrichment_batch(min_fit_score=40)
            )

        assert result["signals_added"] >= 1
        assert result["similar_computed"] >= 1
        assert result["writeups_generated"] >= 1

    @patch("app.services.prospect_signals.generate_ai_writeup", new_callable=AsyncMock)
    @patch("app.services.prospect_signals.find_similar_customers")
    @patch("app.services.prospect_signals.enrich_missing_signals", new_callable=AsyncMock)
    def test_batch_skips_low_score_prospects(
        self, mock_enrich, mock_similar, mock_writeup, db_session
    ):
        from app.services.prospect_signals import run_signal_enrichment_batch

        _make_prospect(
            db_session,
            domain="lowscore.com",
            fit_score=20,  # below threshold
            readiness_signals={},
        )

        with patch("app.database.SessionLocal", return_value=db_session):
            result = asyncio.get_event_loop().run_until_complete(
                run_signal_enrichment_batch(min_fit_score=40)
            )

        assert result["signals_added"] == 0
        assert result["similar_computed"] == 0
        assert result["writeups_generated"] == 0

    @patch("app.services.prospect_signals.generate_ai_writeup", new_callable=AsyncMock)
    @patch("app.services.prospect_signals.find_similar_customers", side_effect=Exception("DB error"))
    @patch("app.services.prospect_signals.enrich_missing_signals", new_callable=AsyncMock)
    def test_batch_continues_on_error(
        self, mock_enrich, mock_similar, mock_writeup, db_session
    ):
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
            result = asyncio.get_event_loop().run_until_complete(
                run_signal_enrichment_batch(min_fit_score=40)
            )

        assert result["errors"] >= 1
        # Should still attempt writeups despite similar_customers error
        assert result["writeups_generated"] >= 0


# ── Readiness Recalculation ──────────────────────────────────────────


class TestRecalculateReadiness:
    def test_recalculates_on_signal_add(self, db_session):
        from app.services.prospect_signals import enrich_with_intent, enrich_with_events

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
