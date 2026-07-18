"""Tests for medium-large service files at 0% coverage.

Covers:
  - app/services/prospect_discovery_explorium.py
  - app/services/unified_score_service.py
  - app/enrichment_service.py
  - app/services/prospect_signals.py

Called by: pytest
Depends on: conftest.py fixtures, unittest.mock
"""

import os

os.environ["TESTING"] = "1"

from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ═══════════════════════════════════════════════════════════════════════
# 1. prospect_discovery_explorium.py
# ═══════════════════════════════════════════════════════════════════════
from app.services.prospect_discovery_explorium import (
    _build_location,
    _detect_region,
    _normalize_size,
    discover_companies_with_signals,
    normalize_explorium_result,
    run_explorium_discovery_batch,
)


class TestNormalizeSize:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param({"company_size": "201-500"}, "201-500", id="string_passthrough"),
            pytest.param({"employee_count": 30}, "1-50", id="int_small"),
            pytest.param({"estimated_num_employees": 100}, "51-200", id="int_medium"),
            pytest.param({"company_size": 300}, "201-500", id="int_201_500"),
            pytest.param({"company_size": 800}, "501-1000", id="int_501_1000"),
            pytest.param({"company_size": 3000}, "1001-5000", id="int_1001_5000"),
            pytest.param({"company_size": 7000}, "5001-10000", id="int_5001_10000"),
            pytest.param({"company_size": 20000}, "10001+", id="int_large"),
            pytest.param({}, None, id="none_returns_none"),
        ],
    )
    def test_normalize_size(self, raw, expected):
        assert _normalize_size(raw) == expected


class TestBuildLocation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param({"city": "Dallas", "state": "TX", "country": "US"}, "Dallas, TX, US", id="full"),
            pytest.param({"hq_city": "Berlin", "country_code": "DE"}, "Berlin, DE", id="partial"),
            pytest.param({}, None, id="empty"),
        ],
    )
    def test_build_location(self, raw, expected):
        assert _build_location(raw) == expected


class TestDetectRegion:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param({"country_code": "US"}, "US", id="us"),
            pytest.param({"country_code": "USA"}, "US", id="usa"),
            pytest.param({"country_code": "DE"}, "EU", id="eu_de"),
            pytest.param({"country_code": "FR"}, "EU", id="eu_fr"),
            pytest.param({"country_code": "JP"}, "Asia", id="asia_jp"),
            pytest.param({"country_code": "TW"}, "Asia", id="asia_tw"),
            pytest.param({"country_code": "BR"}, "BR", id="other"),
            pytest.param({}, None, id="empty"),
        ],
    )
    def test_detect_region(self, raw, expected):
        assert _detect_region(raw) == expected


class TestNormalizeExploriumResult:
    def test_basic_normalization(self):
        raw = {
            "company_name": "Acme Corp",
            "domain": "www.acme.com",
            "website": "https://acme.com",
            "industry": "Electronics",
            "company_size": 500,
            "country_code": "US",
            "city": "Dallas",
            "state": "TX",
        }
        result = normalize_explorium_result(raw, "ems_electronics")
        assert result["name"] == "Acme Corp"
        assert result["domain"] == "acme.com"  # www stripped
        assert result["segment_key"] == "ems_electronics"
        assert result["discovery_source"] == "explorium"
        assert result["employee_count_range"] == "201-500"
        assert result["hq_location"] == "Dallas, TX, US"
        assert result["region"] == "US"

    def test_strong_intent(self):
        raw = {
            "name": "Test Co",
            "domain": "test.com",
            "business_intent_topics": [
                "electronic components",
                "semiconductor sourcing",
                "circuit board procurement",
            ],
        }
        result = normalize_explorium_result(raw, "aerospace_defense")
        assert result["intent"]["strength"] == "strong"
        assert len(result["intent"]["component_topics"]) == 3

    def test_moderate_intent(self):
        raw = {
            "name": "Test Co",
            "domain": "test.com",
            "business_intent_topics": ["electronic components", "cloud computing"],
        }
        result = normalize_explorium_result(raw, "automotive")
        assert result["intent"]["strength"] == "moderate"

    def test_weak_intent(self):
        raw = {
            "name": "Test Co",
            "domain": "test.com",
            "business_intent_topics": ["cloud computing", "machine learning"],
        }
        result = normalize_explorium_result(raw, "automotive")
        assert result["intent"]["strength"] == "weak"

    def test_no_intent(self):
        raw = {"name": "Test Co", "domain": "test.com"}
        result = normalize_explorium_result(raw, "automotive")
        assert result["intent"] == {}

    def test_procurement_hiring(self):
        raw = {
            "name": "Test Co",
            "domain": "test.com",
            "workforce_trends": {"procurement": 5},
        }
        result = normalize_explorium_result(raw, "automotive")
        assert result["hiring"]["type"] == "procurement"

    def test_engineering_hiring(self):
        raw = {
            "name": "Test Co",
            "domain": "test.com",
            "workforce_trends": {"engineering": 3},
        }
        result = normalize_explorium_result(raw, "automotive")
        assert result["hiring"]["type"] == "engineering"

    def test_hiring_string_growth(self):
        raw = {
            "name": "Test Co",
            "domain": "test.com",
            "workforce_trends": {"procurement": "Strong growth in Q1"},
        }
        result = normalize_explorium_result(raw, "automotive")
        assert result["hiring"]["type"] == "procurement"

    def test_no_hiring(self):
        raw = {
            "name": "Test Co",
            "domain": "test.com",
            "workforce_trends": {"procurement": 0},
        }
        result = normalize_explorium_result(raw, "automotive")
        assert result["hiring"] == {}

    def test_events_dict(self):
        raw = {
            "name": "Test Co",
            "domain": "test.com",
            "recent_events": [
                {"type": "funding", "date": "2026-01-01", "description": "Series B"},
            ],
        }
        result = normalize_explorium_result(raw, "automotive")
        assert len(result["events"]) == 1
        assert result["events"][0]["type"] == "funding"

    def test_events_string(self):
        raw = {
            "name": "Test Co",
            "domain": "test.com",
            "recent_events": ["M&A announcement"],
        }
        result = normalize_explorium_result(raw, "automotive")
        assert len(result["events"]) == 1
        assert result["events"][0]["type"] == "M&A announcement"

    def test_enrichment_raw_preserved(self):
        raw = {"name": "Test", "domain": "test.com", "custom_field": "value"}
        result = normalize_explorium_result(raw, "automotive")
        assert result["enrichment_raw"] == raw


class TestDiscoverCompaniesWithSignals:
    @pytest.mark.asyncio
    async def test_no_api_key(self):
        with patch("app.services.prospect_discovery_explorium._get_api_key", return_value=""):
            result = await discover_companies_with_signals("aerospace_defense", "US")
            assert result == []

    @pytest.mark.asyncio
    async def test_unknown_segment(self):
        with patch("app.services.prospect_discovery_explorium._get_api_key", return_value="key"):
            result = await discover_companies_with_signals("nonexistent_segment", "US")
            assert result == []

    @pytest.mark.asyncio
    async def test_api_error(self):
        # Connector degrades transport/HTTP errors to [] → discovery yields [].
        with (
            patch("app.services.prospect_discovery_explorium._get_api_key", return_value="key"),
            patch(
                "app.services.prospect_discovery_explorium.explorium.discover_businesses",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            result = await discover_companies_with_signals("aerospace_defense", "US")
            assert result == []

    @pytest.mark.asyncio
    async def test_successful_discovery(self):
        with (
            patch("app.services.prospect_discovery_explorium._get_api_key", return_value="key"),
            patch(
                "app.services.prospect_discovery_explorium.explorium.discover_businesses",
                new_callable=AsyncMock,
                return_value=[{"company_name": "Test Corp", "domain": "test.com", "country_code": "US"}],
            ),
        ):
            result = await discover_companies_with_signals("aerospace_defense", "US")
            assert len(result) == 1
            assert result[0]["name"] == "Test Corp"

    @pytest.mark.asyncio
    async def test_exception_returns_empty(self):
        with (
            patch("app.services.prospect_discovery_explorium._get_api_key", return_value="key"),
            patch(
                "app.services.prospect_discovery_explorium.explorium.discover_businesses",
                new_callable=AsyncMock,
                side_effect=Exception("timeout"),
            ),
        ):
            result = await discover_companies_with_signals("aerospace_defense", "US")
            assert result == []


class TestRunExploriumDiscoveryBatch:
    @pytest.mark.asyncio
    async def test_no_api_key_skips(self):
        with patch("app.services.prospect_discovery_explorium._get_api_key", return_value=""):
            result = await run_explorium_discovery_batch("batch-001")
            assert result == []

    @pytest.mark.asyncio
    async def test_dedup_known_domains(self):
        """Domains in existing_domains should be skipped."""
        with (
            patch("app.services.prospect_discovery_explorium._get_api_key", return_value="key"),
            patch(
                "app.services.prospect_discovery_explorium.explorium.discover_businesses",
                new_callable=AsyncMock,
                return_value=[
                    {"company_name": "Known Co", "domain": "known.com", "country_code": "US"},
                    {"company_name": "New Co", "domain": "new.com", "country_code": "US"},
                ],
            ),
            patch("app.services.prospect_discovery_explorium.calculate_fit_score", return_value=(70, "Good fit")),
            patch("app.services.prospect_discovery_explorium.calculate_readiness_score", return_value=(50, {})),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await run_explorium_discovery_batch("batch-002", existing_domains={"known.com"})
            domains = {p.domain for p in result}
            assert "known.com" not in domains
            assert "new.com" in domains

    @pytest.mark.asyncio
    async def test_skips_empty_domains(self):
        with (
            patch("app.services.prospect_discovery_explorium._get_api_key", return_value="key"),
            patch(
                "app.services.prospect_discovery_explorium.explorium.discover_businesses",
                new_callable=AsyncMock,
                return_value=[{"company_name": "No Domain", "domain": ""}],
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await run_explorium_discovery_batch("batch-003")
            assert result == []


# ═══════════════════════════════════════════════════════════════════════
# 2. unified_score_service.py
# ═══════════════════════════════════════════════════════════════════════

from app.services.unified_score_service import (
    CATEGORY_WEIGHTS,
    _buyer_categories,
    _merge_trader_categories,
    _safe_pct,
    _sales_categories,
    _weighted_score,
    compute_all_unified_scores,
    get_scoring_info,
    get_unified_leaderboard,
)


class TestSafePct:
    @pytest.mark.parametrize(
        ("value", "max_value", "expected"),
        [
            pytest.param(15, 30, 50.0, id="normal"),
            pytest.param(40, 30, 100.0, id="max_clamp"),
            pytest.param(-5, 30, 0.0, id="min_clamp"),
            pytest.param(10, 0, 0.0, id="zero_max"),
            pytest.param(30, 30, 100.0, id="full_score"),
        ],
    )
    def test_safe_pct(self, value, max_value, expected):
        assert _safe_pct(value, max_value) == expected


class TestBuyerCategories:
    def test_basic(self):
        snap = MagicMock()
        snap.b1_score = 8
        snap.b3_score = 7
        snap.b4_score = 9
        snap.o1_score = 6
        snap.o2_score = 5
        snap.o3_score = 8
        snap.o4_score = 7
        snap.o5_score = 9
        cats = _buyer_categories(snap)
        # Execution = (b1+b4+o1)/30 * 100 = (8+9+6)/30 * 100 = 76.67
        assert abs(cats["execution"] - 76.67) < 0.1
        # Follow-Through = (b3+o2)/20 * 100 = (7+5)/20 * 100 = 60.0
        assert cats["followthrough"] == 60.0
        # Closing = (o3+o4)/20 * 100 = (8+7)/20 * 100 = 75.0
        assert cats["closing"] == 75.0
        # Depth = o5/10 * 100 = 90.0
        assert cats["depth"] == 90.0

    def test_none_scores(self):
        snap = MagicMock()
        snap.b1_score = None
        snap.b3_score = None
        snap.b4_score = None
        snap.o1_score = None
        snap.o2_score = None
        snap.o3_score = None
        snap.o4_score = None
        snap.o5_score = None
        cats = _buyer_categories(snap)
        assert all(v == 0.0 for v in cats.values())


class TestSalesCategories:
    def test_basic(self):
        snap = MagicMock()
        snap.b2_score = 7
        snap.b3_score = 6
        snap.b4_score = 8
        snap.o1_score = 9
        snap.o2_score = 5
        snap.o3_score = 7
        snap.o4_score = 4
        snap.o5_score = 8
        cats = _sales_categories(snap)
        # Execution = (b2+b4+o3)/30 * 100 = (7+8+7)/30 * 100 = 73.33
        assert abs(cats["execution"] - 73.33) < 0.1
        # Closing = (o1+o2)/20 * 100 = (9+5)/20 * 100 = 70.0
        assert cats["closing"] == 70.0


class TestMergeTraderCategories:
    def test_both_present(self):
        buyer = {"execution": 80, "followthrough": 60, "closing": 70, "depth": 90}
        sales = {"execution": 60, "followthrough": 80, "closing": 50, "depth": 70}
        merged = _merge_trader_categories(buyer, sales)
        assert merged["execution"] == 70.0
        assert merged["followthrough"] == 70.0

    def test_buyer_only(self):
        buyer = {"execution": 80, "followthrough": 60, "closing": 70, "depth": 90}
        merged = _merge_trader_categories(buyer, None)
        assert merged == buyer

    def test_sales_only(self):
        sales = {"execution": 60, "followthrough": 80, "closing": 50, "depth": 70}
        merged = _merge_trader_categories(None, sales)
        assert merged == sales

    def test_neither(self):
        merged = _merge_trader_categories(None, None)
        assert all(v == 0.0 for v in merged.values())


class TestWeightedScore:
    def test_all_100(self):
        cats = {k: 100.0 for k in CATEGORY_WEIGHTS}
        assert _weighted_score(cats) == 100.0

    def test_all_zero(self):
        cats = {k: 0.0 for k in CATEGORY_WEIGHTS}
        assert _weighted_score(cats) == 0.0

    def test_mixed(self):
        cats = {"execution": 50, "followthrough": 50, "closing": 50, "depth": 50}
        assert _weighted_score(cats) == 50.0


class TestComputeAllUnifiedScores:
    def test_no_users(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = []
        result = compute_all_unified_scores(db, date(2026, 3, 1))
        assert result["computed"] == 0
        assert result["saved"] == 0

    def test_with_buyer_user(self):
        """Test scoring a buyer user with AvailScore data."""
        db = MagicMock()
        user = MagicMock()
        user.id = 1
        user.name = "Test Buyer"
        user.email = "buyer@trioscs.com"
        user.role = "buyer"
        user.is_active = True

        # AvailScoreSnapshot mock
        avail_snap = MagicMock()
        avail_snap.user_id = 1
        avail_snap.role_type = "buyer"
        avail_snap.total_score = 75
        for metric in ("b1", "b2", "b3", "b4", "b5", "o1", "o2", "o3", "o4", "o5"):
            setattr(avail_snap, f"{metric}_score", 7.5)

        # Setup db.query chains
        def query_side_effect(model):
            mock_q = MagicMock()
            if model.__name__ == "User":
                mock_q.filter.return_value.all.return_value = [user]
            elif model.__name__ == "AvailScoreSnapshot":
                mock_q.filter.return_value.all.return_value = [avail_snap]
            elif model.__name__ == "MultiplierScoreSnapshot":
                mock_q.filter.return_value.all.return_value = []
            elif model.__name__ == "UnifiedScoreSnapshot":
                mock_q.filter.return_value.first.return_value = None
            return mock_q

        db.query.side_effect = query_side_effect

        with patch("app.services.unified_score_service._refresh_blurbs"):
            result = compute_all_unified_scores(db, date(2026, 3, 1))
            assert result["computed"] == 1
            assert result["saved"] == 1
            db.commit.assert_called()


class TestGetScoringInfo:
    def test_returns_categories(self):
        info = get_scoring_info()
        assert len(info["categories"]) == 4
        assert info["total_range"] == "0-100"
        assert "bonus_tiers" in info
        assert "multiplier_points" in info


class TestGetUnifiedLeaderboard:
    def test_empty(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []
        result = get_unified_leaderboard(db, date(2026, 3, 1))
        assert result["month"] == "2026-03-01"
        assert result["entries"] == []


# ═══════════════════════════════════════════════════════════════════════
# 4. enrichment_service.py
# ═══════════════════════════════════════════════════════════════════════

from app.enrichment_service import (
    _clean_domain,
    _name_looks_suspicious,
    _title_case_preserve_acronyms,
    apply_enrichment_to_company,
    apply_enrichment_to_vendor,
    enrich_entity,
    find_suggested_contacts,
    normalize_company_input,
    normalize_company_output,
)


class TestCleanDomain:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("https://www.example.com/page", "example.com", id="basic"),
            pytest.param("http://example.com", "example.com", id="http"),
            pytest.param("example.com.", "example.com", id="trailing_dot"),
            pytest.param("example.com/", "example.com", id="trailing_slash"),
            pytest.param("EXAMPLE.COM", "example.com", id="uppercase"),
        ],
    )
    def test_clean_domain(self, raw, expected):
        assert _clean_domain(raw) == expected


class TestNameLooksSuspicious:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            pytest.param("Acme Electronics", False, id="normal_name"),
            pytest.param("Xylmnk Corp", True, id="suspicious_no_vowels"),
            pytest.param("IBM Corp", False, id="acronym_preserved"),
            pytest.param("AB CD", False, id="short_words_ignored"),
            pytest.param("", False, id="empty"),
        ],
    )
    def test_name_looks_suspicious(self, name, expected):
        assert _name_looks_suspicious(name) is expected


class TestTitleCasePreserveAcronyms:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("acme electronics", "Acme Electronics", id="normal"),
            pytest.param("ibm corp", "IBM CORP", id="acronyms"),
            pytest.param("texas instruments ti", "Texas Instruments TI", id="mixed"),
            pytest.param("", "", id="empty"),
        ],
    )
    def test_title_case_preserve_acronyms(self, raw, expected):
        assert _title_case_preserve_acronyms(raw) == expected


class TestNormalizeCompanyInput:
    @pytest.mark.asyncio
    async def test_basic_cleanup(self):
        with patch("app.enrichment_service.get_credential_cached", return_value=None):
            name, domain = await normalize_company_input("  Acme Corp  ", "https://www.acme.com/path")
            assert name == "Acme Corp"
            assert domain == "acme.com"

    @pytest.mark.asyncio
    async def test_empty_inputs(self):
        with patch("app.enrichment_service.get_credential_cached", return_value=None):
            name, domain = await normalize_company_input("", "")
            assert name == ""
            assert domain == ""

    @pytest.mark.asyncio
    async def test_suspicious_name_triggers_ai(self):
        with (
            patch("app.enrichment_service.get_credential_cached", return_value="sk-fake-key"),
            patch("app.enrichment_service.claude_text", new_callable=AsyncMock, return_value="Xylm Corp"),
            patch("app.enrichment_service._name_looks_suspicious", return_value=True),
        ):
            name, domain = await normalize_company_input("Xylmnk Corp", "xylm.com")
            assert name == "Xylm Corp"


class TestNormalizeCompanyOutput:
    def test_full_normalization(self):
        data = {
            "legal_name": "acme electronics llc",
            "domain": "HTTPS://WWW.ACME.COM/page",
            "industry": "  electronic components  ",
            "employee_size": "5000 employees",
            "hq_city": "  dallas  ",
            "hq_state": "tx",
            "hq_country": "US",
            "website": "acme.com",
            "linkedin_url": "linkedin.com/company/acme",
        }
        out = normalize_company_output(data)
        assert out["legal_name"] == "Acme Electronics LLC"
        assert out["domain"] == "acme.com"
        assert out["industry"] == "Electronic Components"
        assert out["employee_size"] == "5,000+"
        assert out["hq_city"] == "Dallas"
        assert out["hq_state"] == "TX"
        assert out["hq_country"] == "United States"
        assert out["website"].startswith("https://")
        assert out["linkedin_url"].startswith("https://")

    def test_employee_size_range(self):
        out = normalize_company_output({"employee_size": "51-200"})
        assert out["employee_size"] == "51-200"

    def test_employee_size_with_plus(self):
        out = normalize_company_output({"employee_size": "500+"})
        assert out["employee_size"] == "500+"

    def test_unknown_country(self):
        out = normalize_company_output({"hq_country": "XY"})
        assert out["hq_country"] == "Xy"

    def test_empty_fields(self):
        out = normalize_company_output({})
        assert out == {}


class TestApplyEnrichmentToCompany:
    def test_sets_empty_fields(self):
        company = MagicMock()
        company.domain = None
        company.linkedin_url = None
        company.legal_name = None
        company.industry = None
        company.employee_size = None
        company.hq_city = None
        company.hq_state = None
        company.hq_country = None
        company.website = None

        data = {
            "domain": "acme.com",
            "linkedin_url": "https://linkedin.com/company/acme",
            "legal_name": "Acme Corp",
            "industry": "Electronics",
            "source": "explorium",
        }
        updated = apply_enrichment_to_company(company, data)
        assert "domain" in updated
        assert "linkedin_url" in updated
        assert "legal_name" in updated
        assert "industry" in updated
        assert company.enrichment_source == "explorium"

    def test_does_not_overwrite_existing(self):
        company = MagicMock()
        company.domain = "existing.com"
        company.linkedin_url = None
        company.legal_name = "Existing Name"
        company.industry = None
        company.employee_size = None
        company.hq_city = None
        company.hq_state = None
        company.hq_country = None
        company.website = "https://existing.com"

        data = {
            "domain": "new.com",
            "legal_name": "New Name",
            "website": "https://new.com",
        }
        updated = apply_enrichment_to_company(company, data)
        assert "domain" not in updated  # already had domain
        assert "legal_name" not in updated
        assert "website" not in updated

    def test_no_updates_no_timestamp(self):
        company = MagicMock()
        company.domain = "acme.com"
        company.linkedin_url = "url"
        company.legal_name = "Name"
        company.industry = "Ind"
        company.employee_size = "50"
        company.hq_city = "Dallas"
        company.hq_state = "TX"
        company.hq_country = "US"
        company.website = "https://acme.com"

        updated = apply_enrichment_to_company(company, {"domain": "acme.com"})
        assert updated == []


class TestApplyEnrichmentToVendor:
    def test_sets_empty_fields(self):
        card = MagicMock()
        card.linkedin_url = None
        card.legal_name = None
        card.industry = None
        card.employee_size = None
        card.hq_city = None
        card.hq_state = None
        card.hq_country = None
        card.domain = None
        card.website = None

        data = {
            "domain": "vendor.com",
            "linkedin_url": "https://linkedin.com/company/vendor",
            "legal_name": "Vendor Inc",
            "website": "https://vendor.com",
            "source": "ai",
        }
        updated = apply_enrichment_to_vendor(card, data)
        assert "domain" in updated
        assert "website" in updated
        assert "legal_name" in updated
        assert card.enrichment_source == "ai"


class TestEnrichEntity:
    @pytest.mark.asyncio
    async def test_cached_result(self):
        mock_get_cached = MagicMock(return_value={"legal_name": "Acme Corp", "source": "cache"})
        mock_set_cached = MagicMock()
        with (
            patch(
                "app.enrichment_service.normalize_company_input",
                new_callable=AsyncMock,
                return_value=("Acme", "acme.com"),
            ),
            patch("app.cache.intel_cache.get_cached", mock_get_cached),
            patch("app.cache.intel_cache.set_cached", mock_set_cached),
        ):
            result = await enrich_entity("acme.com", "Acme")
            assert result["legal_name"] == "Acme Corp"

    @pytest.mark.asyncio
    async def test_router_returns_ai_result(self):
        """Task 9: enrich_entity now delegates to enrichment_router.gather_company.
        Patch the router to return an AI result and verify it is blended."""
        from app.services import enrichment_router

        async def fake_gather(domain, name=""):
            return [{"source": "ai", "legal_name": "Acme Corp", "industry": "Electronics"}]

        with (
            patch(
                "app.enrichment_service.normalize_company_input",
                new_callable=AsyncMock,
                return_value=("Acme", "acme.com"),
            ),
            patch("app.cache.intel_cache.get_cached", return_value=None),
            patch("app.cache.intel_cache.set_cached"),
            patch.object(enrichment_router, "gather_company", fake_gather),
        ):
            result = await enrich_entity("acme.com", "Acme")
            assert "ai" in result["source"]

    @pytest.mark.asyncio
    async def test_no_providers_return_empty(self):
        """Task 9: when gather_company returns nothing, result is domain-only dict."""
        from app.services import enrichment_router

        async def empty_gather(domain, name=""):
            return []

        with (
            patch(
                "app.enrichment_service.normalize_company_input",
                new_callable=AsyncMock,
                return_value=("Acme", "acme.com"),
            ),
            patch("app.cache.intel_cache.get_cached", return_value=None),
            patch("app.cache.intel_cache.set_cached"),
            patch.object(enrichment_router, "gather_company", empty_gather),
        ):
            result = await enrich_entity("acme.com", "Acme")
            assert result["domain"] == "acme.com"
            # legal_name may be absent or None when no providers contributed data
            assert result.get("legal_name") is None or isinstance(result.get("legal_name"), str)


class TestFindSuggestedContacts:
    """Task 9: find_suggested_contacts delegates to enrichment_router.gather_contacts.

    Tests patch gather_contacts at the router level instead of the old internal
    _explorium_find_contacts / _ai_find_contacts.
    """

    @pytest.mark.asyncio
    async def test_deduplication_by_email(self):
        """Same-email contacts from different sources are deduplicated by
        blend_contacts."""
        from app.services import enrichment_router

        contact = {
            "full_name": "John Doe",
            "title": "Procurement Manager",
            "email": "john@acme.com",
            "phone": None,
            "linkedin_url": None,
            "location": None,
            "company": "Acme",
        }
        # Two sources, same email → should deduplicate
        explorium_c = {**contact, "source": "explorium"}
        ai_c = {**contact, "source": "ai"}

        async def fake_gather(domain, name, title_filter, limit):
            return [explorium_c, ai_c]

        with patch.object(enrichment_router, "gather_contacts", fake_gather):
            result = await find_suggested_contacts("acme.com", "Acme")
            assert len(result) == 1

    @pytest.mark.asyncio
    async def test_filters_irrelevant_titles(self):
        """_is_relevant keeps relevant titles; irrelevant ones are filtered."""
        from app.services import enrichment_router

        async def fake_gather(domain, name, title_filter, limit):
            return [
                {
                    "source": "explorium",
                    "full_name": "Jane Smith",
                    "title": "Procurement Director",
                    "email": "jane@acme.com",
                    "phone": None,
                    "linkedin_url": None,
                    "location": None,
                    "company": "Acme",
                },
                {
                    "source": "explorium",
                    "full_name": "Bob Intern",
                    "title": "Janitor",
                    "email": "bob@acme.com",
                    "phone": None,
                    "linkedin_url": None,
                    "location": None,
                    "company": "Acme",
                },
            ]

        with patch.object(enrichment_router, "gather_contacts", fake_gather):
            result = await find_suggested_contacts("acme.com")
            # Jane should pass (procurement), Bob filtered (janitor)
            assert len(result) == 1
            assert result[0]["full_name"] == "Jane Smith"

    @pytest.mark.asyncio
    async def test_keeps_all_if_filter_removes_everything(self):
        """When all contacts fail _is_relevant, unfiltered list is returned."""
        from app.services import enrichment_router

        async def fake_gather(domain, name, title_filter, limit):
            return [
                {
                    "source": "explorium",
                    "full_name": "Bob",
                    "title": "Janitor",
                    "email": "bob@acme.com",
                    "phone": None,
                    "linkedin_url": None,
                    "location": None,
                    "company": "Acme",
                },
            ]

        with patch.object(enrichment_router, "gather_contacts", fake_gather):
            result = await find_suggested_contacts("acme.com")
            # All filtered = return unfiltered
            assert len(result) == 1

    @pytest.mark.asyncio
    async def test_gather_returns_single_valid_contact(self):
        """When gather returns a valid contact, it is blended and returned."""
        from app.services import enrichment_router

        async def fake_gather(domain, name, title_filter, limit):
            return [
                {
                    "source": "ai",
                    "full_name": "Jane",
                    "title": "Buyer",
                    "email": "jane@acme.com",
                    "phone": None,
                    "linkedin_url": None,
                    "location": None,
                    "company": "Acme",
                },
            ]

        with patch.object(enrichment_router, "gather_contacts", fake_gather):
            result = await find_suggested_contacts("acme.com")
            assert len(result) == 1


# ═══════════════════════════════════════════════════════════════════════
# 5. prospect_signals.py
# ═══════════════════════════════════════════════════════════════════════

from app.services.prospect_signals import (
    _build_writeup_prompt,
    _compare_sizes,
    _template_fallback_writeup,
    find_similar_customers,
    generate_ai_writeup,
)


class TestCompareSizes:
    @pytest.mark.parametrize(
        ("size_a", "size_b", "expected"),
        [
            pytest.param("201-500", "300", True, id="same_bracket"),
            pytest.param("201-500", "501-1000", True, id="adjacent_bracket"),
            pytest.param("1-50", "5001-10000", False, id="far_brackets"),
            pytest.param("10001+", "5001-10000", True, id="plus_notation"),
            pytest.param(None, "200", False, id="none_first"),
            pytest.param("200", None, False, id="none_second"),
            pytest.param("unknown", "200", False, id="invalid_string"),
        ],
    )
    def test_compare_sizes(self, size_a, size_b, expected):
        assert _compare_sizes(size_a, size_b) is expected


class TestFindSimilarCustomers:
    def test_no_companies(self):
        prospect = MagicMock()
        prospect.naics_code = "336412"
        prospect.industry = "Aerospace"
        prospect.employee_count_range = "201-500"
        prospect.region = "US"
        prospect.id = 1

        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = []
        result = find_similar_customers(prospect, db)
        assert result == []
        db.commit.assert_called()

    def test_industry_overlap_match(self):
        prospect = MagicMock()
        prospect.naics_code = None
        prospect.industry = "Electronic Components Manufacturing"
        prospect.employee_count_range = "201-500"
        prospect.region = "US"
        prospect.id = 1

        company = MagicMock()
        company.name = "Similar Corp"
        company.domain = "similar.com"
        company.industry = "Electronic Components Distribution"
        company.employee_size = "201-500"
        company.hq_country = "US"
        company.account_owner_id = 1
        company.is_active = True

        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [company]
        result = find_similar_customers(prospect, db)
        assert len(result) >= 1
        assert result[0]["name"] == "Similar Corp"
        assert "match_strength" in result[0]

    def test_region_match_eu(self):
        prospect = MagicMock()
        prospect.naics_code = None
        prospect.industry = None
        prospect.employee_count_range = "201-500"
        prospect.region = "EU"
        prospect.id = 1

        company = MagicMock()
        company.name = "EU Corp"
        company.domain = "eucorp.com"
        company.industry = None
        company.employee_size = "201-500"
        company.hq_country = "Germany"
        company.account_owner_id = 1
        company.is_active = True

        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [company]
        result = find_similar_customers(prospect, db)
        assert len(result) >= 1

    def test_top_3_limit(self):
        prospect = MagicMock()
        prospect.naics_code = None
        prospect.industry = "Electronics"
        prospect.employee_count_range = "201-500"
        prospect.region = "US"
        prospect.id = 1

        companies = []
        for i in range(5):
            c = MagicMock()
            c.name = f"Company {i}"
            c.domain = f"co{i}.com"
            c.industry = "Electronics Manufacturing"
            c.employee_size = "201-500"
            c.hq_country = "US"
            c.account_owner_id = 1
            c.is_active = True
            companies.append(c)

        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = companies
        result = find_similar_customers(prospect, db)
        assert len(result) <= 3


class TestBuildWriteupPrompt:
    def test_basic_prompt(self):
        prospect = MagicMock()
        prospect.name = "Test Corp"
        prospect.domain = "test.com"
        prospect.industry = "Electronics"
        prospect.employee_count_range = "201-500"
        prospect.revenue_range = "$10M-$50M"
        prospect.hq_location = "Dallas, TX"
        prospect.fit_score = 85
        prospect.fit_reasoning = "Industry: Aerospace segment"
        prospect.readiness_signals = {
            "intent": {"strength": "strong", "component_topics": ["semiconductors"]},
            "hiring": {"type": "procurement"},
            "events": [{"type": "funding"}],
        }
        prospect.similar_customers = [{"name": "Similar Co"}]

        prompt = _build_writeup_prompt(prospect)
        assert "Test Corp" in prompt
        assert "Electronics" in prompt
        assert "201-500" in prompt
        assert "semiconductors" in prompt
        assert "procurement" in prompt
        assert "funding" in prompt
        assert "Similar Co" in prompt

    def test_minimal_prompt(self):
        prospect = MagicMock()
        prospect.name = "Minimal"
        prospect.domain = "minimal.com"
        prospect.industry = None
        prospect.employee_count_range = None
        prospect.revenue_range = None
        prospect.hq_location = None
        prospect.fit_score = None
        prospect.fit_reasoning = None
        prospect.readiness_signals = {}
        prospect.similar_customers = []

        prompt = _build_writeup_prompt(prospect)
        assert "Minimal" in prompt
        assert "minimal.com" in prompt


class TestTemplateFallbackWriteup:
    def test_full_data(self):
        prospect = MagicMock()
        prospect.name = "Acme Corp"
        prospect.employee_count_range = "201-500"
        prospect.industry = "Electronic Components"
        prospect.hq_location = "Dallas, TX"
        prospect.fit_reasoning = "Industry: Aerospace segment; Size: good"
        prospect.readiness_signals = {
            "intent": {"strength": "strong"},
            "events": [{"type": "funding"}],
            "hiring": {"type": "procurement"},
        }
        prospect.similar_customers = [{"name": "Similar Co"}]

        writeup = _template_fallback_writeup(prospect)
        assert "Acme Corp" in writeup
        assert "201-500" in writeup
        assert "Electronic Components" in writeup
        assert "intent" in writeup.lower() or "sourcing" in writeup.lower()

    def test_minimal_data(self):
        prospect = MagicMock()
        prospect.name = "Basic Corp"
        prospect.employee_count_range = None
        prospect.industry = None
        prospect.hq_location = None
        prospect.fit_reasoning = None
        prospect.readiness_signals = {}
        prospect.similar_customers = []

        writeup = _template_fallback_writeup(prospect)
        assert "Basic Corp" in writeup

    def test_events_signal(self):
        prospect = MagicMock()
        prospect.name = "Events Corp"
        prospect.employee_count_range = None
        prospect.industry = None
        prospect.hq_location = None
        prospect.fit_reasoning = None
        prospect.readiness_signals = {
            "intent": {},
            "events": [{"type": "funding_round"}],
        }
        prospect.similar_customers = []

        writeup = _template_fallback_writeup(prospect)
        assert "funding_round" in writeup

    def test_hiring_signal(self):
        prospect = MagicMock()
        prospect.name = "Hiring Corp"
        prospect.employee_count_range = None
        prospect.industry = None
        prospect.hq_location = None
        prospect.fit_reasoning = None
        prospect.readiness_signals = {
            "intent": {},
            "hiring": {"type": "engineering"},
        }
        prospect.similar_customers = []

        writeup = _template_fallback_writeup(prospect)
        assert "engineering" in writeup


class TestGenerateAiWriteup:
    @pytest.mark.asyncio
    async def test_claude_success(self):
        prospect = MagicMock()
        prospect.name = "Test Corp"
        prospect.domain = "test.com"
        prospect.industry = None
        prospect.employee_count_range = None
        prospect.revenue_range = None
        prospect.hq_location = None
        prospect.fit_score = None
        prospect.fit_reasoning = None
        prospect.readiness_signals = {}
        prospect.similar_customers = []
        prospect.id = 1

        db = MagicMock()

        with patch(
            "app.utils.claude_client.claude_text",
            new_callable=AsyncMock,
            return_value="Test Corp is a promising prospect.",
        ):
            result = await generate_ai_writeup(prospect, db)
            assert result == "Test Corp is a promising prospect."
            assert prospect.ai_writeup == result
            db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_claude_failure_uses_template(self):
        prospect = MagicMock()
        prospect.name = "Fallback Corp"
        prospect.domain = "fallback.com"
        prospect.industry = "Electronics"
        prospect.employee_count_range = "201-500"
        prospect.revenue_range = None
        prospect.hq_location = "Austin, TX"
        prospect.fit_score = None
        prospect.fit_reasoning = None
        prospect.readiness_signals = {}
        prospect.similar_customers = []
        prospect.id = 2

        db = MagicMock()

        with patch("app.utils.claude_client.claude_text", new_callable=AsyncMock, side_effect=Exception("API down")):
            result = await generate_ai_writeup(prospect, db)
            assert "Fallback Corp" in result
            assert prospect.ai_writeup == result
            db.commit.assert_called_once()


class TestEnrichMissingSignals:
    @staticmethod
    def _prospect(**overrides):
        """A MagicMock prospect with empty firmographics (backfill proceeds)."""
        prospect = MagicMock()
        prospect.id = 1
        prospect.name = "Test Co"
        prospect.domain = "test.com"
        prospect.industry = None
        prospect.employee_count_range = None
        prospect.region = None
        prospect.naics_code = None
        prospect.revenue_range = None
        prospect.website = None
        prospect.hq_location = None
        prospect.last_enriched_at = None
        for key, value in overrides.items():
            setattr(prospect, key, value)
        return prospect

    @pytest.mark.asyncio
    async def test_prospect_not_found(self):
        from app.services.prospect_signals import enrich_missing_signals

        db = MagicMock()
        db.get.return_value = None
        result = await enrich_missing_signals(999, db)
        assert result is False

    @pytest.mark.asyncio
    async def test_firmographics_complete_skips(self):
        from app.services.prospect_signals import enrich_missing_signals

        prospect = self._prospect(industry="Electronics", employee_count_range="1001-5000", region="US")
        db = MagicMock()
        db.get.return_value = prospect

        with patch("app.connectors.explorium.enrich_company", new_callable=AsyncMock) as mock_enrich:
            result = await enrich_missing_signals(1, db)

        assert result is False
        mock_enrich.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_domain(self):
        from app.services.prospect_signals import enrich_missing_signals

        prospect = self._prospect(domain="")
        db = MagicMock()
        db.get.return_value = prospect
        result = await enrich_missing_signals(1, db)
        assert result is False

    @pytest.mark.asyncio
    async def test_no_credential(self):
        from app.services.prospect_signals import enrich_missing_signals

        prospect = self._prospect()
        db = MagicMock()
        db.get.return_value = prospect

        with (
            patch("app.config.settings.explorium_enrichment_enabled", True),
            patch("app.services.enrichment_credit_guard.circuit_open", return_value=False),
            patch("app.services.credential_service.get_credential_cached", return_value=""),
            patch("app.connectors.explorium.enrich_company", new_callable=AsyncMock) as mock_enrich,
        ):
            result = await enrich_missing_signals(1, db)

        assert result is False
        mock_enrich.assert_not_called()

    @pytest.mark.asyncio
    async def test_successful_backfill(self):
        from app.services.prospect_signals import enrich_missing_signals

        prospect = self._prospect()
        db = MagicMock()
        db.get.return_value = prospect

        firmo = {
            "source": "explorium",
            "legal_name": "Test Co",
            "industry": "Electronics Manufacturing",
            "employee_size": "1001-5000",
            "hq_city": "Austin",
            "hq_state": "TX",
            "hq_country": "US",
            "naics": "334412",
            "revenue_range": "$100M-$500M",
            "website": "https://test.com",
        }

        with (
            patch("app.config.settings.explorium_enrichment_enabled", True),
            patch("app.services.enrichment_credit_guard.circuit_open", return_value=False),
            patch("app.services.credential_service.get_credential_cached", return_value="key"),
            patch("app.connectors.explorium.enrich_company", new_callable=AsyncMock, return_value=firmo),
            patch("app.services.prospect_signals.calculate_fit_score", return_value=(72, "Good fit")),
        ):
            result = await enrich_missing_signals(1, db)

        assert result is True
        assert prospect.industry == "Electronics Manufacturing"
        assert prospect.employee_count_range == "1001-5000"
        assert prospect.hq_location == "Austin, TX, US"
        assert prospect.region == "US"
        assert prospect.fit_score == 72
        db.commit.assert_called()

    @pytest.mark.asyncio
    async def test_enrich_company_returns_none(self):
        from app.services.prospect_signals import enrich_missing_signals

        prospect = self._prospect()
        db = MagicMock()
        db.get.return_value = prospect

        with (
            patch("app.config.settings.explorium_enrichment_enabled", True),
            patch("app.services.enrichment_credit_guard.circuit_open", return_value=False),
            patch("app.services.credential_service.get_credential_cached", return_value="key"),
            patch("app.connectors.explorium.enrich_company", new_callable=AsyncMock, return_value=None),
        ):
            result = await enrich_missing_signals(1, db)

        assert result is False
        db.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_enrich_company_returns_empty_dict(self):
        from app.services.prospect_signals import enrich_missing_signals

        prospect = self._prospect()
        db = MagicMock()
        db.get.return_value = prospect

        with (
            patch("app.config.settings.explorium_enrichment_enabled", True),
            patch("app.services.enrichment_credit_guard.circuit_open", return_value=False),
            patch("app.services.credential_service.get_credential_cached", return_value="key"),
            patch("app.connectors.explorium.enrich_company", new_callable=AsyncMock, return_value={}),
        ):
            result = await enrich_missing_signals(1, db)

        assert result is False

    @pytest.mark.asyncio
    async def test_quota_error_trips_circuit(self):
        from app.services import enrichment_credit_guard as cg
        from app.services.prospect_signals import enrich_missing_signals

        prospect = self._prospect()
        db = MagicMock()
        db.get.return_value = prospect

        with (
            patch("app.config.settings.explorium_enrichment_enabled", True),
            patch("app.services.enrichment_credit_guard.circuit_open", return_value=False),
            patch("app.services.credential_service.get_credential_cached", return_value="key"),
            patch(
                "app.connectors.explorium.enrich_company",
                new_callable=AsyncMock,
                side_effect=cg.ProviderQuotaError("429"),
            ),
            patch("app.services.enrichment_credit_guard.trip_circuit") as mock_trip,
        ):
            result = await enrich_missing_signals(1, db)

        assert result is False
        mock_trip.assert_called_once()

    @pytest.mark.asyncio
    async def test_exception_handling(self):
        from app.services.prospect_signals import enrich_missing_signals

        prospect = self._prospect()
        db = MagicMock()
        db.get.return_value = prospect

        with (
            patch("app.config.settings.explorium_enrichment_enabled", True),
            patch("app.services.enrichment_credit_guard.circuit_open", return_value=False),
            patch("app.services.credential_service.get_credential_cached", return_value="key"),
            patch(
                "app.connectors.explorium.enrich_company",
                new_callable=AsyncMock,
                side_effect=RuntimeError("Network error"),
            ),
        ):
            result = await enrich_missing_signals(1, db)

        assert result is False
        db.commit.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# Additional coverage tests
# ═══════════════════════════════════════════════════════════════════════


class TestComputeUnifiedScoresTraderAndSales:
    """Additional unified score tests for trader and sales roles."""

    def test_trader_merges_buyer_and_sales(self):
        from app.constants import UserRole

        db = MagicMock()
        user = MagicMock()
        user.id = 1
        user.name = "Test Trader"
        user.email = "trader@trioscs.com"
        user.role = UserRole.TRADER
        user.is_active = True

        buyer_snap = MagicMock()
        buyer_snap.user_id = 1
        buyer_snap.role_type = "buyer"
        buyer_snap.total_score = 70
        for metric in ("b1", "b2", "b3", "b4", "b5", "o1", "o2", "o3", "o4", "o5"):
            setattr(buyer_snap, f"{metric}_score", 7.0)

        sales_snap = MagicMock()
        sales_snap.user_id = 1
        sales_snap.role_type = "sales"
        sales_snap.total_score = 60
        for metric in ("b1", "b2", "b3", "b4", "b5", "o1", "o2", "o3", "o4", "o5"):
            setattr(sales_snap, f"{metric}_score", 6.0)

        def query_side_effect(model):
            mock_q = MagicMock()
            if model.__name__ == "User":
                mock_q.filter.return_value.all.return_value = [user]
            elif model.__name__ == "AvailScoreSnapshot":
                mock_q.filter.return_value.all.return_value = [buyer_snap, sales_snap]
            elif model.__name__ == "MultiplierScoreSnapshot":
                mock_q.filter.return_value.all.return_value = []
            elif model.__name__ == "UnifiedScoreSnapshot":
                mock_q.filter.return_value.first.return_value = None
            return mock_q

        db.query.side_effect = query_side_effect

        with patch("app.services.unified_score_service._refresh_blurbs"):
            result = compute_all_unified_scores(db, date(2026, 3, 1))
            assert result["computed"] == 1

    def test_sales_user(self):
        from app.constants import UserRole

        db = MagicMock()
        user = MagicMock()
        user.id = 2
        user.name = "Test Sales"
        user.email = "sales@trioscs.com"
        user.role = UserRole.SALES
        user.is_active = True

        sales_snap = MagicMock()
        sales_snap.user_id = 2
        sales_snap.role_type = "sales"
        sales_snap.total_score = 65
        for metric in ("b1", "b2", "b3", "b4", "b5", "o1", "o2", "o3", "o4", "o5"):
            setattr(sales_snap, f"{metric}_score", 6.5)

        def query_side_effect(model):
            mock_q = MagicMock()
            if model.__name__ == "User":
                mock_q.filter.return_value.all.return_value = [user]
            elif model.__name__ == "AvailScoreSnapshot":
                mock_q.filter.return_value.all.return_value = [sales_snap]
            elif model.__name__ == "MultiplierScoreSnapshot":
                mock_q.filter.return_value.all.return_value = []
            elif model.__name__ == "UnifiedScoreSnapshot":
                mock_q.filter.return_value.first.return_value = None
            return mock_q

        db.query.side_effect = query_side_effect

        with patch("app.services.unified_score_service._refresh_blurbs"):
            result = compute_all_unified_scores(db, date(2026, 3, 1))
            assert result["computed"] == 1

    def test_existing_snapshot_updated(self):
        """When a UnifiedScoreSnapshot already exists, it should be updated."""
        db = MagicMock()
        user = MagicMock()
        user.id = 1
        user.name = "Existing User"
        user.email = "existing@trioscs.com"
        user.role = "buyer"
        user.is_active = True

        avail_snap = MagicMock()
        avail_snap.user_id = 1
        avail_snap.role_type = "buyer"
        avail_snap.total_score = 80
        for metric in ("b1", "b2", "b3", "b4", "b5", "o1", "o2", "o3", "o4", "o5"):
            setattr(avail_snap, f"{metric}_score", 8.0)

        existing_unified = MagicMock()

        def query_side_effect(model):
            mock_q = MagicMock()
            if model.__name__ == "User":
                mock_q.filter.return_value.all.return_value = [user]
            elif model.__name__ == "AvailScoreSnapshot":
                mock_q.filter.return_value.all.return_value = [avail_snap]
            elif model.__name__ == "MultiplierScoreSnapshot":
                mock_q.filter.return_value.all.return_value = []
            elif model.__name__ == "UnifiedScoreSnapshot":
                mock_q.filter.return_value.first.return_value = existing_unified
            return mock_q

        db.query.side_effect = query_side_effect

        with patch("app.services.unified_score_service._refresh_blurbs"):
            result = compute_all_unified_scores(db, date(2026, 3, 1))
            assert result["saved"] == 1

    def test_user_with_no_snapshots_skipped(self):
        """Users without any AvailScoreSnapshot data are skipped."""
        db = MagicMock()
        user = MagicMock()
        user.id = 1
        user.name = "No Data User"
        user.email = "nodata@trioscs.com"
        user.role = "buyer"
        user.is_active = True

        def query_side_effect(model):
            mock_q = MagicMock()
            if model.__name__ == "User":
                mock_q.filter.return_value.all.return_value = [user]
            elif model.__name__ == "AvailScoreSnapshot":
                mock_q.filter.return_value.all.return_value = []
            elif model.__name__ == "MultiplierScoreSnapshot":
                mock_q.filter.return_value.all.return_value = []
            elif model.__name__ == "UnifiedScoreSnapshot":
                mock_q.filter.return_value.first.return_value = None
            return mock_q

        db.query.side_effect = query_side_effect

        with patch("app.services.unified_score_service._refresh_blurbs"):
            result = compute_all_unified_scores(db, date(2026, 3, 1))
            assert result["computed"] == 0
            assert result["saved"] == 0


class TestRefreshBlurbs:
    def test_fresh_blurb_skipped(self):
        """Blurbs generated recently (< 2 hours) should be skipped."""
        from app.services.unified_score_service import _refresh_blurbs

        db = MagicMock()
        snap = MagicMock()
        snap.ai_blurb_generated_at = datetime.now(UTC)
        db.query.return_value.filter.return_value.first.return_value = snap

        results = [
            {
                "user_id": 1,
                "user_name": "Test",
                "primary_role": "buyer",
                "cats": {"execution": 80, "followthrough": 70, "closing": 60, "depth": 50},
                "score": 70,
                "rank": 1,
            }
        ]
        _refresh_blurbs(db, date(2026, 3, 1), results)
        db.commit.assert_called()

    def test_stale_blurb_regenerated(self):
        """Blurbs older than 2 hours should be regenerated."""
        from app.services.unified_score_service import _refresh_blurbs

        db = MagicMock()
        snap = MagicMock()
        snap.ai_blurb_generated_at = datetime.now(UTC) - timedelta(hours=3)
        db.query.return_value.filter.return_value.first.return_value = snap

        results = [
            {
                "user_id": 1,
                "user_name": "Test",
                "primary_role": "buyer",
                "cats": {"execution": 80, "followthrough": 70, "closing": 60, "depth": 50},
                "score": 70,
                "rank": 1,
            }
        ]

        with patch(
            "app.services.unified_score_service._generate_blurb",
            return_value={"strength": "Good!", "improvement": "Work harder"},
        ):
            _refresh_blurbs(db, date(2026, 3, 1), results)
            assert snap.ai_blurb_strength == "Good!"
            assert snap.ai_blurb_improvement == "Work harder"

    def test_blurb_generation_failure(self):
        """Failed blurb generation should not crash."""
        from app.services.unified_score_service import _refresh_blurbs

        db = MagicMock()
        snap = MagicMock()
        snap.ai_blurb_generated_at = None
        db.query.return_value.filter.return_value.first.return_value = snap

        results = [
            {
                "user_id": 1,
                "user_name": "Test",
                "primary_role": "buyer",
                "cats": {"execution": 80, "followthrough": 70, "closing": 60, "depth": 50},
                "score": 70,
                "rank": 1,
            }
        ]

        with patch("app.services.unified_score_service._generate_blurb", side_effect=Exception("API error")):
            _refresh_blurbs(db, date(2026, 3, 1), results)
            db.commit.assert_called()


class TestGetUnifiedLeaderboardWithData:
    def test_with_entries(self):
        db = MagicMock()

        snap = MagicMock()
        snap.user_id = 1
        snap.primary_role = "buyer"
        snap.unified_score = 85.0
        snap.rank = 1
        snap.prospecting_pct = 0.0
        snap.execution_pct = 90.0
        snap.followthrough_pct = 80.0
        snap.closing_pct = 85.0
        snap.depth_pct = 70.0
        snap.ai_blurb_strength = "Great execution!"
        snap.ai_blurb_improvement = "Improve depth."
        snap.avail_score_buyer = 75
        snap.avail_score_sales = None
        snap.multiplier_points_buyer = 120
        snap.multiplier_points_sales = None

        user = MagicMock()
        user.id = 1
        user.name = "Test User"
        user.role = "buyer"

        def query_side_effect(model):
            mock_q = MagicMock()
            if model.__name__ == "UnifiedScoreSnapshot":
                mock_q.filter.return_value.order_by.return_value.all.return_value = [snap]
            elif model.__name__ == "AvailScoreSnapshot":
                mock_q.filter.return_value.all.return_value = []
            elif model.__name__ == "MultiplierScoreSnapshot":
                mock_q.filter.return_value.all.return_value = []
            elif model.__name__ == "User":
                mock_q.filter.return_value.all.return_value = [user]
            return mock_q

        db.query.side_effect = query_side_effect

        result = get_unified_leaderboard(db, date(2026, 3, 1))
        assert len(result["entries"]) == 1
        entry = result["entries"][0]
        assert entry["user_name"] == "Test User"
        assert entry["unified_score"] == 85.0


class TestEnrichmentServiceProviders:
    """Test the Explorium and AI provider functions.

    Task 9: _explorium_find_company and _explorium_find_contacts were DELETED from
    enrichment_service.py. Their logic now lives in app.connectors.explorium.
    The Explorium tests below test the connector directly.
    """

    @pytest.mark.asyncio
    async def test_explorium_find_company_no_key(self):
        """enrich_company returns None when business_id match returns no results."""
        from app.connectors.explorium import enrich_company

        match_resp = MagicMock()
        match_resp.status_code = 200
        match_resp.json.return_value = {"data": {"matched_businesses": []}}

        with patch("app.connectors.explorium.http.post", new_callable=AsyncMock, return_value=match_resp):
            result = await enrich_company("acme.com", "Acme", "")
            assert result is None

    @pytest.mark.asyncio
    async def test_explorium_find_company_success(self):
        """enrich_company match+enrich pipeline returns parsed firmographic dict."""
        from app.connectors.explorium import enrich_company

        match_resp = MagicMock()
        match_resp.status_code = 200
        match_resp.json.return_value = {"data": {"matched_businesses": [{"business_id": "biz-1"}]}}

        enrich_resp = MagicMock()
        enrich_resp.status_code = 200
        enrich_resp.json.return_value = {
            "data": {
                "name": "Acme Corp",
                "linkedin_industry_category": "Electronics",
                "number_of_employees_range": {"min": 201, "max": 500},
            }
        }

        async def side(url, **kwargs):
            return match_resp if "match" in url else enrich_resp

        with patch("app.connectors.explorium.http.post", side_effect=side):
            result = await enrich_company("acme.com", "Acme", "fake-key")
            assert result["source"] == "explorium"
            assert result["legal_name"] == "Acme Corp"

    @pytest.mark.asyncio
    async def test_explorium_find_company_http_error(self):
        """Non-200 from /businesses/match returns None."""
        from app.connectors.explorium import enrich_company

        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch("app.connectors.explorium.http.post", new_callable=AsyncMock, return_value=mock_resp):
            result = await enrich_company("acme.com", "Acme", "fake-key")
            assert result is None

    @pytest.mark.asyncio
    async def test_explorium_find_contacts_no_key(self):
        """search_contacts with no business_id match returns empty list."""
        from app.connectors.explorium import search_contacts

        match_resp = MagicMock()
        match_resp.status_code = 200
        match_resp.json.return_value = {"data": {"matched_businesses": []}}

        with patch("app.connectors.explorium.http.post", new_callable=AsyncMock, return_value=match_resp):
            result = await search_contacts("acme.com", "Acme", "", "", 5)
            assert result == []

    @pytest.mark.asyncio
    async def test_explorium_find_contacts_success(self):
        """search_contacts pipeline returns contacts from connector."""
        from app.connectors.explorium import search_contacts

        match_resp = MagicMock()
        match_resp.status_code = 200
        match_resp.json.return_value = {"data": {"matched_businesses": [{"business_id": "biz-1"}]}}

        prospects_resp = MagicMock()
        prospects_resp.status_code = 200
        prospects_resp.json.return_value = {
            "data": [
                {
                    "prospect_id": "p1",
                    "full_name": "John Doe",
                    "job_title": "Procurement Manager",
                    "linkedin": "https://linkedin.com/in/johndoe",
                    "city": "Dallas",
                    "company_name": "Acme Corp",
                }
            ]
        }

        ci_resp = MagicMock()
        ci_resp.status_code = 200
        ci_resp.json.return_value = {
            "data": {
                "professional_email": "john@acme.com",
                "professional_email_status": "valid",
                "mobile_phone": "+1-555-0100",
            }
        }

        async def side(url, **kwargs):
            if "match" in url:
                return match_resp
            if "prospects/contacts" in url:
                return ci_resp
            return prospects_resp

        with patch("app.connectors.explorium.http.post", side_effect=side):
            result = await search_contacts("acme.com", "Acme", "fake-key", "procurement", 5)
            assert len(result) == 1
            assert result[0]["full_name"] == "John Doe"

    @pytest.mark.asyncio
    async def test_ai_find_company_no_key(self):
        from app.enrichment_service import _ai_find_company

        with patch("app.enrichment_service.get_credential_cached", return_value=None):
            result = await _ai_find_company("acme.com")
            assert result is None

    @pytest.mark.asyncio
    async def test_ai_find_company_success(self):
        from app.enrichment_service import _ai_find_company

        with (
            patch("app.enrichment_service.get_credential_cached", return_value="fake-key"),
            patch(
                "app.enrichment_service.claude_json",
                new_callable=AsyncMock,
                return_value={
                    "legal_name": "Acme Corp",
                    "industry": "Electronics",
                    "employee_size": "201-500",
                    "hq_city": "Dallas",
                    "hq_state": "TX",
                    "hq_country": "US",
                    "website": "https://acme.com",
                    "linkedin_url": "https://linkedin.com/company/acme",
                },
            ),
        ):
            result = await _ai_find_company("acme.com", "Acme")
            assert result["source"] == "ai"
            assert result["legal_name"] == "Acme Corp"


class TestSignalEnrichmentBatch:
    @pytest.mark.asyncio
    async def test_batch_orchestration(self):
        from app.services.prospect_signals import run_signal_enrichment_batch

        mock_prospect = MagicMock()
        mock_prospect.id = 1
        mock_prospect.readiness_signals = {}
        mock_prospect.similar_customers = None
        mock_prospect.ai_writeup = None
        mock_prospect.name = "Test Co"
        mock_prospect.domain = "test.com"
        mock_prospect.industry = None
        mock_prospect.employee_count_range = None
        mock_prospect.revenue_range = None
        mock_prospect.hq_location = None
        mock_prospect.fit_score = 50
        mock_prospect.fit_reasoning = None

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [mock_prospect]

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.services.prospect_signals.enrich_missing_signals", new_callable=AsyncMock, return_value=True),
            patch("app.services.prospect_signals.find_similar_customers", return_value=[]),
            patch(
                "app.services.prospect_signals.generate_ai_writeup",
                new_callable=AsyncMock,
                return_value="Great prospect",
            ),
        ):
            result = await run_signal_enrichment_batch(min_fit_score=40)
            assert result["signals_added"] >= 0
            assert result["errors"] >= 0
