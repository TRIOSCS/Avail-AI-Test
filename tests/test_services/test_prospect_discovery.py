"""Tests for all three discovery services — Explorium, Apollo people, email mining.

All external API calls are mocked. Tests cover normalization, dedup, scoring
integration, rate limiting, batch orchestration, graceful degradation, and
edge cases.
"""

import os

os.environ["TESTING"] = "1"
os.environ["RATE_LIMIT_ENABLED"] = "false"

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import Company, User
from app.models.prospect_account import ProspectAccount
from app.schemas.prospect_account import ProspectAccountCreate

# ── Explorium Fixtures ───────────────────────────────────────────────

EXPLORIUM_RAW_RESULT = {
    "company_name": "Raytheon Sensors Inc",
    "domain": "www.raytheon-sensors.com",
    "website": "https://raytheon-sensors.com",
    "industry": "Aerospace & Defense",
    "primary_naics_code": "336412",
    "company_size": "5001-10000",
    "annual_revenue": "$1B+",
    "city": "Tucson",
    "state": "AZ",
    "country_code": "US",
    "description": "Sensor-heavy defense manufacturer",
    "business_intent_topics": [
        "electronic components",
        "semiconductors",
        "procurement solutions",
        "mil-spec components",
    ],
    "workforce_trends": {"procurement": 5, "engineering": 10},
    "recent_events": [
        {"type": "new_funding_round", "date": "2026-01", "description": "Series B"},
    ],
}

EXPLORIUM_MINIMAL_RESULT = {
    "name": "Small Corp",
    "domain": "smallcorp.com",
    "industry": "Manufacturing",
}

EXPLORIUM_NO_DOMAIN = {
    "company_name": "Ghost Corp",
    "industry": "Unknown",
}


# ── Explorium Tests ──────────────────────────────────────────────────


class TestExploriumNormalization:
    """Test normalize_explorium_result mapping."""

    def test_full_result(self):
        from app.services.prospect_discovery_explorium import normalize_explorium_result

        result = normalize_explorium_result(EXPLORIUM_RAW_RESULT, "aerospace_defense")

        assert result["name"] == "Raytheon Sensors Inc"
        assert result["domain"] == "raytheon-sensors.com"  # stripped www.
        assert result["industry"] == "Aerospace & Defense"
        assert result["naics_code"] == "336412"
        assert result["employee_count_range"] == "5001-10000"
        assert result["hq_location"] == "Tucson, AZ, US"
        assert result["region"] == "US"

    def test_intent_signals_extracted(self):
        from app.services.prospect_discovery_explorium import normalize_explorium_result

        result = normalize_explorium_result(EXPLORIUM_RAW_RESULT, "aerospace_defense")

        assert result["intent"]["strength"] in ("strong", "moderate")
        assert len(result["intent"]["topics"]) > 0
        assert len(result["intent"]["component_topics"]) >= 1

    def test_hiring_signals_extracted(self):
        from app.services.prospect_discovery_explorium import normalize_explorium_result

        result = normalize_explorium_result(EXPLORIUM_RAW_RESULT, "aerospace_defense")

        assert result["hiring"]["type"] == "procurement"

    def test_events_extracted(self):
        from app.services.prospect_discovery_explorium import normalize_explorium_result

        result = normalize_explorium_result(EXPLORIUM_RAW_RESULT, "aerospace_defense")

        assert len(result["events"]) == 1
        assert result["events"][0]["type"] == "new_funding_round"

    def test_minimal_result(self):
        from app.services.prospect_discovery_explorium import normalize_explorium_result

        result = normalize_explorium_result(EXPLORIUM_MINIMAL_RESULT, "ems_electronics")

        assert result["name"] == "Small Corp"
        assert result["domain"] == "smallcorp.com"
        assert result["intent"] == {}
        assert result["hiring"] == {}
        assert result["events"] == []

    def test_no_domain(self):
        from app.services.prospect_discovery_explorium import normalize_explorium_result

        result = normalize_explorium_result(EXPLORIUM_NO_DOMAIN, "automotive")

        assert result["domain"] == ""
        assert result["name"] == "Ghost Corp"

    def test_numeric_employee_count(self):
        from app.services.prospect_discovery_explorium import normalize_explorium_result

        raw = {"name": "Test", "domain": "test.com", "estimated_num_employees": 2500}
        result = normalize_explorium_result(raw, "ems_electronics")
        assert result["employee_count_range"] == "1001-5000"

    def test_string_events(self):
        """Events as strings instead of dicts."""
        from app.services.prospect_discovery_explorium import normalize_explorium_result

        raw = {
            "name": "Test",
            "domain": "test.com",
            "recent_events": ["funding round", "product launch"],
        }
        result = normalize_explorium_result(raw, "ems_electronics")
        assert len(result["events"]) == 2
        assert result["events"][0]["type"] == "funding round"

    def test_region_detection(self):
        from app.services.prospect_discovery_explorium import normalize_explorium_result

        for cc, expected in [("US", "US"), ("DE", "EU"), ("JP", "Asia"), ("BR", "BR")]:
            raw = {"name": "Test", "domain": "t.com", "country_code": cc}
            r = normalize_explorium_result(raw, "automotive")
            assert r["region"] == expected, f"country_code={cc} should map to {expected}"


class TestExploriumDiscovery:
    """Test discover_companies_with_signals API call."""

    @pytest.mark.asyncio
    async def test_no_api_key(self):
        from app.services.prospect_discovery_explorium import discover_companies_with_signals

        with patch("app.services.prospect_discovery_explorium._get_api_key", return_value=""):
            results = await discover_companies_with_signals("aerospace_defense", "US")
            assert results == []

    @pytest.mark.asyncio
    async def test_unknown_segment(self):
        from app.services.prospect_discovery_explorium import discover_companies_with_signals

        with patch("app.services.prospect_discovery_explorium._get_api_key", return_value="key"):
            results = await discover_companies_with_signals("nonexistent", "US")
            assert results == []

    @pytest.mark.asyncio
    async def test_successful_search(self):
        from app.services.prospect_discovery_explorium import discover_companies_with_signals

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"businesses": [EXPLORIUM_RAW_RESULT]}

        with patch("app.services.prospect_discovery_explorium._get_api_key", return_value="key"):
            with patch("app.services.prospect_discovery_explorium.http") as mock_http:
                mock_http.post = AsyncMock(return_value=mock_resp)
                results = await discover_companies_with_signals("aerospace_defense", "US")

        assert len(results) == 1
        assert results[0]["domain"] == "raytheon-sensors.com"

    @pytest.mark.asyncio
    async def test_api_error_returns_empty(self):
        from app.services.prospect_discovery_explorium import discover_companies_with_signals

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"

        with patch("app.services.prospect_discovery_explorium._get_api_key", return_value="key"):
            with patch("app.services.prospect_discovery_explorium.http") as mock_http:
                mock_http.post = AsyncMock(return_value=mock_resp)
                results = await discover_companies_with_signals("aerospace_defense", "US")

        assert results == []

    @pytest.mark.asyncio
    async def test_exception_returns_empty(self):
        from app.services.prospect_discovery_explorium import discover_companies_with_signals

        with patch("app.services.prospect_discovery_explorium._get_api_key", return_value="key"):
            with patch("app.services.prospect_discovery_explorium.http") as mock_http:
                mock_http.post = AsyncMock(side_effect=Exception("timeout"))
                results = await discover_companies_with_signals("aerospace_defense", "US")

        assert results == []


class TestExploriumBatch:
    """Test batch orchestration with dedup."""

    @pytest.mark.asyncio
    async def test_dedup_existing_domains(self):
        from app.services.prospect_discovery_explorium import run_explorium_discovery_batch

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"businesses": [EXPLORIUM_RAW_RESULT]}

        existing = {"raytheon-sensors.com"}  # already in pool

        with patch("app.services.prospect_discovery_explorium._get_api_key", return_value="key"):
            with patch("app.services.prospect_discovery_explorium.http") as mock_http:
                mock_http.post = AsyncMock(return_value=mock_resp)
                results = await run_explorium_discovery_batch("test-batch", existing)

        assert len(results) == 0  # deduped away

    @pytest.mark.asyncio
    async def test_no_api_key_skips_batch(self):
        from app.services.prospect_discovery_explorium import run_explorium_discovery_batch

        with patch("app.services.prospect_discovery_explorium._get_api_key", return_value=""):
            results = await run_explorium_discovery_batch("test-batch")

        assert results == []

    @pytest.mark.asyncio
    async def test_batch_returns_prospect_creates(self):
        from app.services.prospect_discovery_explorium import run_explorium_discovery_batch

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"businesses": [EXPLORIUM_RAW_RESULT]}

        with patch("app.services.prospect_discovery_explorium._get_api_key", return_value="key"):
            with patch("app.services.prospect_discovery_explorium.http") as mock_http:
                mock_http.post = AsyncMock(return_value=mock_resp)
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    results = await run_explorium_discovery_batch("test-batch", set())

        assert len(results) > 0
        assert all(isinstance(r, ProspectAccountCreate) for r in results)
        assert results[0].domain == "raytheon-sensors.com"
        assert results[0].discovery_source == "explorium"


# ── Apollo People Tests ──────────────────────────────────────────────


class TestApolloCheckPeople:
    """Test check_people_signals."""

    @pytest.mark.asyncio
    async def test_no_api_key(self):
        from app.services.prospect_discovery_apollo import check_people_signals

        with patch("app.services.prospect_discovery_apollo._get_api_key", return_value=""):
            result = await check_people_signals("example.com")

        assert result["has_procurement_staff"] is None
        assert result["contact_count"] == 0

    @pytest.mark.asyncio
    async def test_found_procurement_staff(self):
        from app.services.prospect_discovery_apollo import check_people_signals

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "people": [
                {
                    "first_name": "Jane",
                    "last_name": "Buyer",
                    "title": "VP Procurement",
                    "email": "jane@example.com",
                    "linkedin_url": "https://linkedin.com/in/jane",
                    "seniority": "vp",
                },
                {
                    "first_name": "Bob",
                    "last_name": "Supply",
                    "title": "Supply Chain Manager",
                },
            ],
        }

        with patch("app.services.prospect_discovery_apollo._get_api_key", return_value="key"):
            with patch("app.services.prospect_discovery_apollo.http") as mock_http:
                mock_http.post = AsyncMock(return_value=mock_resp)
                result = await check_people_signals("example.com")

        assert result["has_procurement_staff"] is True
        assert result["contact_count"] == 2
        assert len(result["sample_contacts"]) == 2
        assert result["sample_contacts"][0]["name"] == "Jane Buyer"

    @pytest.mark.asyncio
    async def test_no_procurement_staff(self):
        from app.services.prospect_discovery_apollo import check_people_signals

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"people": []}

        with patch("app.services.prospect_discovery_apollo._get_api_key", return_value="key"):
            with patch("app.services.prospect_discovery_apollo.http") as mock_http:
                mock_http.post = AsyncMock(return_value=mock_resp)
                result = await check_people_signals("example.com")

        assert result["has_procurement_staff"] is False
        assert result["contact_count"] == 0

    @pytest.mark.asyncio
    async def test_api_error(self):
        from app.services.prospect_discovery_apollo import check_people_signals

        mock_resp = MagicMock()
        mock_resp.status_code = 429

        with patch("app.services.prospect_discovery_apollo._get_api_key", return_value="key"):
            with patch("app.services.prospect_discovery_apollo.http") as mock_http:
                mock_http.post = AsyncMock(return_value=mock_resp)
                result = await check_people_signals("example.com")

        assert result["has_procurement_staff"] is None


class TestApolloBatch:
    """Test run_people_check_batch."""

    @pytest.mark.asyncio
    async def test_no_api_key(self):
        from app.services.prospect_discovery_apollo import run_people_check_batch

        with patch("app.services.prospect_discovery_apollo._get_api_key", return_value=""):
            result = await run_people_check_batch([], MagicMock())

        assert result == {"checked": 0, "has_staff": 0, "no_staff": 0, "errors": 0}

    @pytest.mark.asyncio
    async def test_batch_updates_prospects(self, db_session: Session):
        from app.services.prospect_discovery_apollo import run_people_check_batch

        # Create a prospect to check
        pa = ProspectAccount(
            name="Batch Test Co",
            domain="batchtest.com",
            discovery_source="explorium",
        )
        db_session.add(pa)
        db_session.commit()
        db_session.refresh(pa)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "people": [{"first_name": "Jane", "last_name": "Doe", "title": "Buyer"}],
        }

        with patch("app.services.prospect_discovery_apollo._get_api_key", return_value="key"):
            with patch("app.services.prospect_discovery_apollo.http") as mock_http:
                mock_http.post = AsyncMock(return_value=mock_resp)
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    result = await run_people_check_batch([pa.id], db_session)

        assert result["checked"] == 1
        assert result["has_staff"] == 1

        # Verify prospect was updated
        db_session.refresh(pa)
        assert pa.enrichment_data.get("apollo_people", {}).get("has_procurement_staff") is True


class TestApolloCompanyEnrich:
    """Test enrich_company_apollo fallback."""

    @pytest.mark.asyncio
    async def test_successful_enrich(self):
        from app.services.prospect_discovery_apollo import enrich_company_apollo

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "organization": {
                "name": "Test Corp",
                "primary_domain": "testcorp.com",
                "website_url": "https://testcorp.com",
                "industry": "Electronics",
                "estimated_num_employees": 500,
                "city": "Austin",
                "state": "TX",
                "country": "United States",
                "short_description": "Electronics manufacturer",
            },
        }

        with patch("app.services.prospect_discovery_apollo._get_api_key", return_value="key"):
            with patch("app.services.prospect_discovery_apollo.http") as mock_http:
                mock_http.get = AsyncMock(return_value=mock_resp)
                result = await enrich_company_apollo("testcorp.com")

        assert result["name"] == "Test Corp"
        assert result["industry"] == "Electronics"
        assert result["employee_count_range"] == "201-500"
        assert result["region"] == "US"

    @pytest.mark.asyncio
    async def test_no_api_key(self):
        from app.services.prospect_discovery_apollo import enrich_company_apollo

        with patch("app.services.prospect_discovery_apollo._get_api_key", return_value=""):
            result = await enrich_company_apollo("example.com")

        assert result is None

    @pytest.mark.asyncio
    async def test_not_found(self):
        from app.services.prospect_discovery_apollo import enrich_company_apollo

        mock_resp = MagicMock()
        mock_resp.status_code = 404

        with patch("app.services.prospect_discovery_apollo._get_api_key", return_value="key"):
            with patch("app.services.prospect_discovery_apollo.http") as mock_http:
                mock_http.get = AsyncMock(return_value=mock_resp)
                result = await enrich_company_apollo("unknown.com")

        assert result is None


# ── Email Mining Tests ───────────────────────────────────────────────


class TestEmailMining:
    """Test email domain mining."""

    @pytest.mark.asyncio
    async def test_mine_unknown_domains(self, db_session: Session):
        from app.services.prospect_discovery_email import mine_unknown_domains

        # Create a known customer
        user = User(email="u@test.com", name="User", role="buyer", azure_id="az-1")
        db_session.add(user)
        db_session.flush()
        customer = Company(name="Known Inc", domain="known.com", is_active=True, account_owner_id=user.id)
        db_session.add(customer)
        db_session.commit()

        # Mock Graph client
        mock_graph = AsyncMock()
        mock_graph.list_messages.return_value = [
            # Unknown domain — should be captured (appears 3 times)
            {"from": {"emailAddress": {"address": "alice@newcorp.com", "name": "Alice"}}},
            {"from": {"emailAddress": {"address": "bob@newcorp.com", "name": "Bob"}}},
            {"from": {"emailAddress": {"address": "charlie@newcorp.com", "name": "Charlie"}}},
            # Known customer — should be filtered
            {"from": {"emailAddress": {"address": "vendor@known.com", "name": "Vendor"}}},
            {"from": {"emailAddress": {"address": "vendor@known.com", "name": "Vendor"}}},
            # Freemail — should be filtered
            {"from": {"emailAddress": {"address": "spam@gmail.com", "name": "Spam"}}},
            {"from": {"emailAddress": {"address": "spam@gmail.com", "name": "Spam"}}},
            # Internal — should be filtered
            {"from": {"emailAddress": {"address": "team@trioscs.com", "name": "Team"}}},
            # Single email (below threshold) — should be filtered
            {"from": {"emailAddress": {"address": "once@onetime.com", "name": "Once"}}},
        ]

        results = await mine_unknown_domains(mock_graph, db_session, days_back=90)

        assert len(results) == 1
        assert results[0]["domain"] == "newcorp.com"
        assert results[0]["email_count"] == 3
        assert len(results[0]["sample_senders"]) == 3

    @pytest.mark.asyncio
    async def test_mine_filters_existing_prospects(self, db_session: Session):
        from app.services.prospect_discovery_email import mine_unknown_domains

        # Pre-existing prospect (from Explorium)
        pa = ProspectAccount(
            name="Already Found",
            domain="alreadyfound.com",
            discovery_source="explorium",
        )
        db_session.add(pa)
        db_session.commit()

        mock_graph = AsyncMock()
        mock_graph.list_messages.return_value = [
            {"from": {"emailAddress": {"address": "a@alreadyfound.com", "name": "A"}}},
            {"from": {"emailAddress": {"address": "b@alreadyfound.com", "name": "B"}}},
        ]

        results = await mine_unknown_domains(mock_graph, db_session, days_back=90)
        assert len(results) == 0  # filtered because domain is in prospect_accounts

    @pytest.mark.asyncio
    async def test_mine_graph_error(self, db_session: Session):
        from app.services.prospect_discovery_email import mine_unknown_domains

        mock_graph = AsyncMock()
        mock_graph.list_messages.side_effect = Exception("Graph API down")

        results = await mine_unknown_domains(mock_graph, db_session)
        assert results == []


class TestEmailEnrichment:
    """Test enrich_email_domains."""

    @pytest.mark.asyncio
    async def test_enrich_with_explorium(self):
        from app.services.prospect_discovery_email import enrich_email_domains

        mock_enrich = AsyncMock(
            return_value={
                "name": "New Corp",
                "domain": "newcorp.com",
                "industry": "Electronics",
                "employee_count_range": "201-500",
                "region": "US",
            }
        )

        domains = [{"domain": "newcorp.com", "email_count": 5, "sample_senders": []}]
        results = await enrich_email_domains(domains, enrich_fn=mock_enrich)

        assert len(results) == 1
        assert results[0].domain == "newcorp.com"
        assert results[0].discovery_source == "email_history"

    @pytest.mark.asyncio
    async def test_enrich_fallback_to_apollo(self):
        from app.services.prospect_discovery_email import enrich_email_domains

        mock_explorium = AsyncMock(return_value=None)  # primary fails
        mock_apollo = AsyncMock(
            return_value={
                "name": "Apollo Found",
                "domain": "apollofound.com",
                "industry": "Manufacturing",
            }
        )

        domains = [{"domain": "apollofound.com", "email_count": 3, "sample_senders": []}]
        results = await enrich_email_domains(domains, enrich_fn=mock_explorium, apollo_enrich_fn=mock_apollo)

        assert len(results) == 1
        assert results[0].name == "Apollo Found"

    @pytest.mark.asyncio
    async def test_enrich_skip_when_no_data(self):
        from app.services.prospect_discovery_email import enrich_email_domains

        mock_enrich = AsyncMock(return_value=None)

        domains = [{"domain": "unknown.com", "email_count": 2, "sample_senders": []}]
        results = await enrich_email_domains(domains, enrich_fn=mock_enrich)

        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_enrich_explorium_exception_falls_through(self):
        from app.services.prospect_discovery_email import enrich_email_domains

        mock_explorium = AsyncMock(side_effect=Exception("API timeout"))
        mock_apollo = AsyncMock(
            return_value={
                "name": "Fallback Corp",
                "domain": "fallback.com",
            }
        )

        domains = [{"domain": "fallback.com", "email_count": 4, "sample_senders": []}]
        results = await enrich_email_domains(domains, enrich_fn=mock_explorium, apollo_enrich_fn=mock_apollo)

        assert len(results) == 1
        assert results[0].name == "Fallback Corp"


class TestEmailMiningBatch:
    """Test full pipeline orchestration."""

    @pytest.mark.asyncio
    async def test_full_pipeline(self, db_session: Session):
        from app.services.prospect_discovery_email import run_email_mining_batch

        mock_graph = AsyncMock()
        mock_graph.list_messages.return_value = [
            {"from": {"emailAddress": {"address": "a@newbiz.com", "name": "A"}}},
            {"from": {"emailAddress": {"address": "b@newbiz.com", "name": "B"}}},
        ]

        mock_enrich = AsyncMock(
            return_value={
                "name": "New Biz",
                "domain": "newbiz.com",
                "industry": "Electronics",
            }
        )

        results = await run_email_mining_batch(
            "test-email-batch",
            mock_graph,
            db_session,
            enrich_fn=mock_enrich,
        )

        assert len(results) == 1
        assert results[0].domain == "newbiz.com"
        assert results[0].discovery_source == "email_history"

    @pytest.mark.asyncio
    async def test_no_unknown_domains(self, db_session: Session):
        from app.services.prospect_discovery_email import run_email_mining_batch

        mock_graph = AsyncMock()
        mock_graph.list_messages.return_value = []

        results = await run_email_mining_batch("empty-batch", mock_graph, db_session)

        assert results == []


# ── Dedup Logic Tests ────────────────────────────────────────────────


class TestDedupLogic:
    """Test dedup across discovery services."""

    def test_normalize_domain(self):
        from app.services.prospect_discovery_email import _normalize_domain

        assert _normalize_domain("user@Example.COM") == "example.com"
        assert _normalize_domain("user@www.example.com") == "example.com"
        assert _normalize_domain("") is None
        assert _normalize_domain(None) is None
        assert _normalize_domain("no-at-sign") is None

    @pytest.mark.asyncio
    async def test_explorium_dedup_within_batch(self):
        """Same domain from multiple segment searches is deduped."""
        from app.services.prospect_discovery_explorium import run_explorium_discovery_batch

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        # Same company appears in every search
        mock_resp.json.return_value = {"businesses": [EXPLORIUM_RAW_RESULT]}

        with patch("app.services.prospect_discovery_explorium._get_api_key", return_value="key"):
            with patch("app.services.prospect_discovery_explorium.http") as mock_http:
                mock_http.post = AsyncMock(return_value=mock_resp)
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    results = await run_explorium_discovery_batch("dedup-batch", set())

        # Should only appear once despite 12 searches (4 segments x 3 regions)
        domains = [r.domain for r in results]
        assert domains.count("raytheon-sensors.com") == 1


# ── Graceful Degradation Tests ───────────────────────────────────────


class TestGracefulDegradation:
    """Services don't crash when APIs are down."""

    @pytest.mark.asyncio
    async def test_explorium_down_returns_empty(self):
        from app.services.prospect_discovery_explorium import run_explorium_discovery_batch

        with patch("app.services.prospect_discovery_explorium._get_api_key", return_value="key"):
            with patch("app.services.prospect_discovery_explorium.http") as mock_http:
                mock_http.post = AsyncMock(side_effect=Exception("Connection refused"))
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    results = await run_explorium_discovery_batch("fail-batch", set())

        assert results == []

    @pytest.mark.asyncio
    async def test_apollo_down_returns_none(self):
        from app.services.prospect_discovery_apollo import check_people_signals

        with patch("app.services.prospect_discovery_apollo._get_api_key", return_value="key"):
            with patch("app.services.prospect_discovery_apollo.http") as mock_http:
                mock_http.post = AsyncMock(side_effect=Exception("timeout"))
                result = await check_people_signals("example.com")

        assert result["has_procurement_staff"] is None

    @pytest.mark.asyncio
    async def test_email_mining_survives_graph_failure(self, db_session: Session):
        from app.services.prospect_discovery_email import run_email_mining_batch

        mock_graph = AsyncMock()
        mock_graph.list_messages.side_effect = Exception("Graph API 503")

        results = await run_email_mining_batch("crash-batch", mock_graph, db_session)

        assert results == []  # empty, not crashed


# ── Apollo Helper Function Coverage ─────────────────────────────────


class TestApolloFullName:
    """Coverage for _full_name edge cases."""

    def test_both_names(self):
        from app.services.prospect_discovery_apollo import _full_name

        assert _full_name({"first_name": "Jane", "last_name": "Doe"}) == "Jane Doe"

    def test_only_first_name(self):
        from app.services.prospect_discovery_apollo import _full_name

        assert _full_name({"first_name": "Jane"}) == "Jane"

    def test_only_last_name(self):
        from app.services.prospect_discovery_apollo import _full_name

        assert _full_name({"last_name": "Doe"}) == "Doe"

    def test_only_name_field(self):
        from app.services.prospect_discovery_apollo import _full_name

        assert _full_name({"name": "Jane Doe"}) == "Jane Doe"

    def test_all_empty(self):
        from app.services.prospect_discovery_apollo import _full_name

        assert _full_name({}) == "Unknown"

    def test_none_values(self):
        from app.services.prospect_discovery_apollo import _full_name

        assert _full_name({"first_name": None, "last_name": None}) == "Unknown"

    def test_whitespace_only(self):
        from app.services.prospect_discovery_apollo import _full_name

        assert _full_name({"first_name": "  ", "last_name": "  "}) == "Unknown"


class TestApolloFormatSize:
    """Coverage for _format_size employee count bucketing."""

    def test_none(self):
        from app.services.prospect_discovery_apollo import _format_size

        assert _format_size(None) is None

    def test_small(self):
        from app.services.prospect_discovery_apollo import _format_size

        assert _format_size(10) == "1-50"
        assert _format_size(50) == "1-50"

    def test_medium_small(self):
        from app.services.prospect_discovery_apollo import _format_size

        assert _format_size(51) == "51-200"
        assert _format_size(200) == "51-200"

    def test_medium(self):
        from app.services.prospect_discovery_apollo import _format_size

        assert _format_size(201) == "201-500"
        assert _format_size(500) == "201-500"

    def test_medium_large(self):
        from app.services.prospect_discovery_apollo import _format_size

        assert _format_size(501) == "501-1000"
        assert _format_size(1000) == "501-1000"

    def test_large(self):
        from app.services.prospect_discovery_apollo import _format_size

        assert _format_size(1001) == "1001-5000"
        assert _format_size(5000) == "1001-5000"

    def test_very_large(self):
        from app.services.prospect_discovery_apollo import _format_size

        assert _format_size(5001) == "5001-10000"
        assert _format_size(10000) == "5001-10000"

    def test_enterprise(self):
        from app.services.prospect_discovery_apollo import _format_size

        assert _format_size(10001) == "10001+"
        assert _format_size(100000) == "10001+"

    def test_string_input(self):
        from app.services.prospect_discovery_apollo import _format_size

        assert _format_size("500") == "201-500"

    def test_invalid_string(self):
        from app.services.prospect_discovery_apollo import _format_size

        assert _format_size("unknown") == "unknown"

    def test_empty_string(self):
        from app.services.prospect_discovery_apollo import _format_size

        assert _format_size("") is None

    def test_zero(self):
        from app.services.prospect_discovery_apollo import _format_size

        assert _format_size(0) == "1-50"


class TestApolloDetectRegion:
    """Coverage for _detect_region_from_country."""

    def test_us_variants(self):
        from app.services.prospect_discovery_apollo import _detect_region_from_country

        assert _detect_region_from_country("US") == "US"
        assert _detect_region_from_country("USA") == "US"
        assert _detect_region_from_country("United States") == "US"

    def test_eu_countries(self):
        from app.services.prospect_discovery_apollo import _detect_region_from_country

        for country in [
            "Germany",
            "UK",
            "United Kingdom",
            "France",
            "Netherlands",
            "Sweden",
            "Italy",
            "Spain",
            "Switzerland",
            "Austria",
            "Belgium",
        ]:
            assert _detect_region_from_country(country) == "EU", f"{country} should be EU"

    def test_asian_countries(self):
        from app.services.prospect_discovery_apollo import _detect_region_from_country

        for country in ["China", "Japan", "South Korea", "Taiwan", "Singapore", "India"]:
            assert _detect_region_from_country(country) == "Asia", f"{country} should be Asia"

    def test_unknown_country(self):
        from app.services.prospect_discovery_apollo import _detect_region_from_country

        assert _detect_region_from_country("Brazil") is None
        assert _detect_region_from_country("Australia") is None

    def test_none(self):
        from app.services.prospect_discovery_apollo import _detect_region_from_country

        assert _detect_region_from_country(None) is None

    def test_case_insensitive(self):
        from app.services.prospect_discovery_apollo import _detect_region_from_country

        assert _detect_region_from_country("germany") == "EU"
        assert _detect_region_from_country("JAPAN") == "Asia"


class TestApolloCompanyEnrichEdgeCases:
    """Additional edge cases for enrich_company_apollo."""

    @pytest.mark.asyncio
    async def test_no_organization_in_response(self):
        from app.services.prospect_discovery_apollo import enrich_company_apollo

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"organization": None}

        with patch("app.services.prospect_discovery_apollo._get_api_key", return_value="key"):
            with patch("app.services.prospect_discovery_apollo.http") as mock_http:
                mock_http.get = AsyncMock(return_value=mock_resp)
                result = await enrich_company_apollo("ghost.com")

        assert result is None

    @pytest.mark.asyncio
    async def test_exception_returns_none(self):
        from app.services.prospect_discovery_apollo import enrich_company_apollo

        with patch("app.services.prospect_discovery_apollo._get_api_key", return_value="key"):
            with patch("app.services.prospect_discovery_apollo.http") as mock_http:
                mock_http.get = AsyncMock(side_effect=Exception("connection timeout"))
                result = await enrich_company_apollo("timeout.com")

        assert result is None

    @pytest.mark.asyncio
    async def test_missing_fields_in_org(self):
        """Organization response with minimal fields."""
        from app.services.prospect_discovery_apollo import enrich_company_apollo

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "organization": {
                "name": "Minimal Corp",
                # No domain, no employees, no location
            },
        }

        with patch("app.services.prospect_discovery_apollo._get_api_key", return_value="key"):
            with patch("app.services.prospect_discovery_apollo.http") as mock_http:
                mock_http.get = AsyncMock(return_value=mock_resp)
                result = await enrich_company_apollo("minimal.com")

        assert result is not None
        assert result["name"] == "Minimal Corp"
        assert result["domain"] == "minimal.com"  # falls back to input domain
        assert result["employee_count_range"] is None
        assert result["region"] is None
        assert result["hq_location"] is None

    @pytest.mark.asyncio
    async def test_partial_location(self):
        """Organization with only city, no state/country."""
        from app.services.prospect_discovery_apollo import enrich_company_apollo

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "organization": {
                "name": "City Only Corp",
                "city": "Austin",
            },
        }

        with patch("app.services.prospect_discovery_apollo._get_api_key", return_value="key"):
            with patch("app.services.prospect_discovery_apollo.http") as mock_http:
                mock_http.get = AsyncMock(return_value=mock_resp)
                result = await enrich_company_apollo("cityonly.com")

        assert result["hq_location"] == "Austin"


class TestApolloBatchEdgeCases:
    """Edge cases for run_people_check_batch."""

    @pytest.mark.asyncio
    async def test_batch_skips_missing_prospect(self, db_session):
        from app.services.prospect_discovery_apollo import run_people_check_batch

        with patch("app.services.prospect_discovery_apollo._get_api_key", return_value="key"):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await run_people_check_batch([99999], db_session)

        assert result["checked"] == 0

    @pytest.mark.asyncio
    async def test_batch_skips_prospect_without_domain(self, db_session):
        from app.services.prospect_discovery_apollo import run_people_check_batch

        pa = ProspectAccount(name="No Domain", domain="", discovery_source="manual")
        db_session.add(pa)
        db_session.commit()
        db_session.refresh(pa)

        with patch("app.services.prospect_discovery_apollo._get_api_key", return_value="key"):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await run_people_check_batch([pa.id], db_session)

        assert result["checked"] == 0

    @pytest.mark.asyncio
    async def test_batch_handles_check_exception(self, db_session):
        from app.services.prospect_discovery_apollo import run_people_check_batch

        pa = ProspectAccount(name="Error Corp", domain="errorcorp.com", discovery_source="explorium")
        db_session.add(pa)
        db_session.commit()
        db_session.refresh(pa)

        with patch("app.services.prospect_discovery_apollo._get_api_key", return_value="key"):
            with patch(
                "app.services.prospect_discovery_apollo.check_people_signals",
                new_callable=AsyncMock,
                side_effect=Exception("API crashed"),
            ):
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    result = await run_people_check_batch([pa.id], db_session)

        assert result["errors"] == 1

    @pytest.mark.asyncio
    async def test_batch_no_staff_result(self, db_session):
        from app.services.prospect_discovery_apollo import run_people_check_batch

        pa = ProspectAccount(name="No Staff Corp", domain="nostaff.com", discovery_source="explorium")
        db_session.add(pa)
        db_session.commit()
        db_session.refresh(pa)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"people": []}

        with patch("app.services.prospect_discovery_apollo._get_api_key", return_value="key"):
            with patch("app.services.prospect_discovery_apollo.http") as mock_http:
                mock_http.post = AsyncMock(return_value=mock_resp)
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    result = await run_people_check_batch([pa.id], db_session)

        assert result["no_staff"] == 1


# ── Explorium Coverage Gap Tests ────────────────────────────────────


class TestExploriumCoverageGaps:
    """Tests for uncovered branches in prospect_discovery_explorium."""

    def test_get_api_key_returns_empty(self):
        """Line 78: _get_api_key returns empty when attribute is missing."""
        from app.services.prospect_discovery_explorium import _get_api_key

        with patch("app.services.prospect_discovery_explorium.settings") as mock_s:
            del mock_s.explorium_api_key  # make getattr return ""
            result = _get_api_key()
        assert result == ""

    def test_businesses_not_list(self):
        """Line 133: businesses is not a list, gets reset to []."""
        from app.services.prospect_discovery_explorium import discover_companies_with_signals

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"businesses": "not-a-list"}

        async def run():
            with patch("app.services.prospect_discovery_explorium._get_api_key", return_value="key"):
                with patch("app.services.prospect_discovery_explorium.http") as mock_http:
                    mock_http.post = AsyncMock(return_value=mock_resp)
                    return await discover_companies_with_signals("aerospace_defense", "US")

        results = asyncio.get_event_loop().run_until_complete(run())
        assert results == []

    def test_intent_moderate_strength(self):
        """Lines 185-186: component_topics between 1 and 3 gives moderate."""
        from app.services.prospect_discovery_explorium import normalize_explorium_result

        raw = {
            "name": "Test",
            "domain": "test.com",
            "business_intent_topics": ["electronic components", "unrelated stuff"],
        }
        result = normalize_explorium_result(raw, "ems_electronics")
        assert result["intent"]["strength"] == "moderate"

    def test_intent_weak_strength(self):
        """Lines 187-188: no component_topics gives weak."""
        from app.services.prospect_discovery_explorium import normalize_explorium_result

        raw = {
            "name": "Test",
            "domain": "test.com",
            "business_intent_topics": ["food delivery", "retail", "fashion"],
        }
        result = normalize_explorium_result(raw, "ems_electronics")
        assert result["intent"]["strength"] == "weak"

    def test_hiring_engineering_type(self):
        """Line 212: engineering growth path."""
        from app.services.prospect_discovery_explorium import normalize_explorium_result

        raw = {
            "name": "Test",
            "domain": "test.com",
            "workforce_trends": {"engineering": 10, "procurement": 0},
        }
        result = normalize_explorium_result(raw, "ems_electronics")
        assert result["hiring"]["type"] == "engineering"

    def test_hiring_workforce_not_dict(self):
        """Line 216: workforce_trends is not a dict."""
        from app.services.prospect_discovery_explorium import normalize_explorium_result

        raw = {
            "name": "Test",
            "domain": "test.com",
            "workforce_trends": "not-a-dict",
        }
        result = normalize_explorium_result(raw, "ems_electronics")
        assert result["hiring"] == {}

    def test_normalize_size_all_ranges(self):
        """Lines 246-258: all size bracket paths in _normalize_size."""
        from app.services.prospect_discovery_explorium import _normalize_size

        assert _normalize_size({"company_size": 30}) == "1-50"
        assert _normalize_size({"company_size": 100}) == "51-200"
        assert _normalize_size({"company_size": 300}) == "201-500"
        assert _normalize_size({"company_size": 800}) == "501-1000"
        assert _normalize_size({"company_size": 3000}) == "1001-5000"
        assert _normalize_size({"company_size": 7000}) == "5001-10000"
        assert _normalize_size({"company_size": 20000}) == "10001+"

    @pytest.mark.asyncio
    async def test_batch_skips_empty_domain(self):
        """Line 320: results with empty domain are skipped."""
        from app.services.prospect_discovery_explorium import run_explorium_discovery_batch

        no_domain_result = {
            "company_name": "No Domain Corp",
            "industry": "Unknown",
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"businesses": [no_domain_result]}

        with patch("app.services.prospect_discovery_explorium._get_api_key", return_value="key"):
            with patch("app.services.prospect_discovery_explorium.http") as mock_http:
                mock_http.post = AsyncMock(return_value=mock_resp)
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    results = await run_explorium_discovery_batch("test-batch", set())

        assert len(results) == 0
