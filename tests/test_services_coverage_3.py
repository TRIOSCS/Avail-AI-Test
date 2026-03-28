"""Tests for medium-large service files at 0% coverage.

Covers:
  - app/services/prospect_discovery_explorium.py
  - app/services/unified_score_service.py
  - app/services/vendor_email_lookup.py
  - app/enrichment_service.py
  - app/services/prospect_signals.py

Called by: pytest
Depends on: conftest.py fixtures, unittest.mock
"""

import os

os.environ["TESTING"] = "1"

from datetime import date
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
    def test_string_passthrough(self):
        assert _normalize_size({"company_size": "201-500"}) == "201-500"

    def test_int_small(self):
        assert _normalize_size({"employee_count": 30}) == "1-50"

    def test_int_medium(self):
        assert _normalize_size({"estimated_num_employees": 100}) == "51-200"

    def test_int_201_500(self):
        assert _normalize_size({"company_size": 300}) == "201-500"

    def test_int_501_1000(self):
        assert _normalize_size({"company_size": 800}) == "501-1000"

    def test_int_1001_5000(self):
        assert _normalize_size({"company_size": 3000}) == "1001-5000"

    def test_int_5001_10000(self):
        assert _normalize_size({"company_size": 7000}) == "5001-10000"

    def test_int_large(self):
        assert _normalize_size({"company_size": 20000}) == "10001+"

    def test_none_returns_none(self):
        assert _normalize_size({}) is None


class TestBuildLocation:
    def test_full(self):
        assert _build_location({"city": "Dallas", "state": "TX", "country": "US"}) == "Dallas, TX, US"

    def test_partial(self):
        assert _build_location({"hq_city": "Berlin", "country_code": "DE"}) == "Berlin, DE"

    def test_empty(self):
        assert _build_location({}) is None


class TestDetectRegion:
    def test_us(self):
        assert _detect_region({"country_code": "US"}) == "US"
        assert _detect_region({"country_code": "USA"}) == "US"

    def test_eu(self):
        assert _detect_region({"country_code": "DE"}) == "EU"
        assert _detect_region({"country_code": "FR"}) == "EU"

    def test_asia(self):
        assert _detect_region({"country_code": "JP"}) == "Asia"
        assert _detect_region({"country_code": "TW"}) == "Asia"

    def test_other(self):
        assert _detect_region({"country_code": "BR"}) == "BR"

    def test_empty(self):
        assert _detect_region({}) is None


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
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        with (
            patch("app.services.prospect_discovery_explorium._get_api_key", return_value="key"),
            patch(
                "app.services.prospect_discovery_explorium.http.post", new_callable=AsyncMock, return_value=mock_resp
            ),
        ):
            result = await discover_companies_with_signals("aerospace_defense", "US")
            assert result == []

    @pytest.mark.asyncio
    async def test_successful_discovery(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "businesses": [{"company_name": "Test Corp", "domain": "test.com", "country_code": "US"}]
        }
        with (
            patch("app.services.prospect_discovery_explorium._get_api_key", return_value="key"),
            patch(
                "app.services.prospect_discovery_explorium.http.post", new_callable=AsyncMock, return_value=mock_resp
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
                "app.services.prospect_discovery_explorium.http.post",
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
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "businesses": [
                {"company_name": "Known Co", "domain": "known.com", "country_code": "US"},
                {"company_name": "New Co", "domain": "new.com", "country_code": "US"},
            ]
        }
        with (
            patch("app.services.prospect_discovery_explorium._get_api_key", return_value="key"),
            patch(
                "app.services.prospect_discovery_explorium.http.post", new_callable=AsyncMock, return_value=mock_resp
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
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"businesses": [{"company_name": "No Domain", "domain": ""}]}
        with (
            patch("app.services.prospect_discovery_explorium._get_api_key", return_value="key"),
            patch(
                "app.services.prospect_discovery_explorium.http.post", new_callable=AsyncMock, return_value=mock_resp
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
    def test_normal(self):
        assert _safe_pct(15, 30) == 50.0

    def test_max_clamp(self):
        assert _safe_pct(40, 30) == 100.0

    def test_min_clamp(self):
        assert _safe_pct(-5, 30) == 0.0

    def test_zero_max(self):
        assert _safe_pct(10, 0) == 0.0

    def test_full_score(self):
        assert _safe_pct(30, 30) == 100.0


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
# 3. vendor_email_lookup.py
# ═══════════════════════════════════════════════════════════════════════

from app.services.vendor_email_lookup import (
    build_inquiry_groups,
    find_vendors_for_parts,
)


class TestBuildInquiryGroups:
    def test_basic_grouping(self):
        vendor_results = {
            "LM317T": [
                {"vendor_name": "Arrow", "emails": ["sales@arrow.com"], "domain": "arrow.com"},
            ],
        }
        parts = [{"mpn": "LM317T", "qty": 100}]
        groups = build_inquiry_groups(vendor_results, parts)
        assert len(groups) == 1
        assert groups[0]["vendor_email"] == "sales@arrow.com"
        assert "LM317T" in groups[0]["subject"]
        assert "100 pcs" in groups[0]["body"]

    def test_multiple_parts_single_vendor(self):
        vendor_results = {
            "LM317T": [{"vendor_name": "Arrow", "emails": ["sales@arrow.com"]}],
            "LM7805": [{"vendor_name": "Arrow", "emails": ["sales@arrow.com"]}],
        }
        parts = [{"mpn": "LM317T", "qty": 100}, {"mpn": "LM7805", "qty": 200}]
        groups = build_inquiry_groups(vendor_results, parts)
        assert len(groups) == 1

    def test_multiple_vendors(self):
        vendor_results = {
            "LM317T": [
                {"vendor_name": "Arrow", "emails": ["sales@arrow.com"]},
                {"vendor_name": "Mouser", "emails": ["info@mouser.com"]},
            ],
        }
        parts = [{"mpn": "LM317T", "qty": 50}]
        groups = build_inquiry_groups(vendor_results, parts)
        assert len(groups) == 2

    def test_vendor_with_no_emails(self):
        vendor_results = {
            "LM317T": [{"vendor_name": "NoEmail Inc", "emails": []}],
        }
        parts = [{"mpn": "LM317T", "qty": 50}]
        groups = build_inquiry_groups(vendor_results, parts)
        assert len(groups) == 0

    def test_subject_truncation_many_parts(self):
        vendor_results = {f"PART{i}": [{"vendor_name": "Arrow", "emails": ["sales@arrow.com"]}] for i in range(5)}
        parts = [{"mpn": f"PART{i}", "qty": 10} for i in range(5)]
        groups = build_inquiry_groups(vendor_results, parts)
        assert len(groups) == 1
        assert "+ 2 more" in groups[0]["subject"]


class TestFindVendorsForParts:
    @pytest.mark.asyncio
    async def test_empty_mpn_list(self):
        db = MagicMock()
        result = await find_vendors_for_parts([], db, enrich_missing=False)
        assert result == {}

    @pytest.mark.asyncio
    async def test_with_vendors_found(self):
        db = MagicMock()
        with patch(
            "app.services.vendor_email_lookup._query_db_for_part",
            return_value=[
                {
                    "vendor_name": "Arrow",
                    "emails": ["sales@arrow.com"],
                    "phones": [],
                    "domain": "arrow.com",
                    "card_id": 1,
                    "sources": {"api"},
                    "qty_available": 500,
                    "unit_price": 0.50,
                    "currency": "USD",
                    "last_seen": "2026-03-01",
                    "sighting_count": 5,
                }
            ],
        ):
            result = await find_vendors_for_parts(["LM317T"], db, enrich_missing=False)
            assert "LM317T" in result
            assert len(result["LM317T"]) == 1

    @pytest.mark.asyncio
    async def test_empty_mpn_skipped(self):
        db = MagicMock()
        with patch("app.services.vendor_email_lookup._query_db_for_part", return_value=[]):
            result = await find_vendors_for_parts(["", "  "], db, enrich_missing=False)
            assert "" in result
            assert "  " in result

    @pytest.mark.asyncio
    async def test_enrichment_triggered(self):
        """When enrich_missing=True and vendors lack emails, enrichment runs."""
        db = MagicMock()
        vendor_no_email = {
            "vendor_name": "NoEmail",
            "emails": [],
            "phones": [],
            "domain": "noemail.com",
            "card_id": 1,
            "sources": {"api"},
            "qty_available": 100,
            "unit_price": 1.0,
            "currency": "USD",
            "last_seen": None,
            "sighting_count": 1,
        }
        call_count = [0]

        def mock_query_db(mpn, db):
            call_count[0] += 1
            if call_count[0] <= 1:
                return [vendor_no_email]
            return [dict(vendor_no_email, emails=["found@noemail.com"])]

        with (
            patch("app.services.vendor_email_lookup._query_db_for_part", side_effect=mock_query_db),
            patch("app.services.vendor_email_lookup._enrich_vendors_batch", new_callable=AsyncMock),
            patch("app.services.vendor_email_lookup.normalize_vendor_name", return_value="noemail"),
        ):
            result = await find_vendors_for_parts(["LM317T"], db, enrich_missing=True)
            assert "LM317T" in result


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
    def test_basic(self):
        assert _clean_domain("https://www.example.com/page") == "example.com"

    def test_http(self):
        assert _clean_domain("http://example.com") == "example.com"

    def test_trailing_dot(self):
        assert _clean_domain("example.com.") == "example.com"

    def test_trailing_slash(self):
        assert _clean_domain("example.com/") == "example.com"

    def test_uppercase(self):
        assert _clean_domain("EXAMPLE.COM") == "example.com"


class TestNameLooksSuspicious:
    def test_normal_name(self):
        assert _name_looks_suspicious("Acme Electronics") is False

    def test_suspicious_no_vowels(self):
        assert _name_looks_suspicious("Xylmnk Corp") is True

    def test_acronym_preserved(self):
        assert _name_looks_suspicious("IBM Corp") is False

    def test_short_words_ignored(self):
        assert _name_looks_suspicious("AB CD") is False

    def test_empty(self):
        assert _name_looks_suspicious("") is False


class TestTitleCasePreserveAcronyms:
    def test_normal(self):
        assert _title_case_preserve_acronyms("acme electronics") == "Acme Electronics"

    def test_acronyms(self):
        assert _title_case_preserve_acronyms("ibm corp") == "IBM CORP"

    def test_mixed(self):
        assert _title_case_preserve_acronyms("texas instruments ti") == "Texas Instruments TI"

    def test_empty(self):
        assert _title_case_preserve_acronyms("") == ""


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
    async def test_explorium_then_ai_fallback(self):
        with (
            patch(
                "app.enrichment_service.normalize_company_input",
                new_callable=AsyncMock,
                return_value=("Acme", "acme.com"),
            ),
            patch("app.cache.intel_cache.get_cached", return_value=None),
            patch("app.enrichment_service._explorium_find_company", new_callable=AsyncMock, return_value=None),
            patch(
                "app.enrichment_service._ai_find_company",
                new_callable=AsyncMock,
                return_value={
                    "legal_name": "Acme Corp",
                    "industry": "Electronics",
                    "source": "ai",
                },
            ),
            patch("app.cache.intel_cache.set_cached"),
        ):
            result = await enrich_entity("acme.com", "Acme")
            assert result["source"] == "ai"

    @pytest.mark.asyncio
    async def test_no_providers_return_empty(self):
        with (
            patch(
                "app.enrichment_service.normalize_company_input",
                new_callable=AsyncMock,
                return_value=("Acme", "acme.com"),
            ),
            patch("app.cache.intel_cache.get_cached", return_value=None),
            patch("app.enrichment_service._explorium_find_company", new_callable=AsyncMock, return_value=None),
            patch("app.enrichment_service._ai_find_company", new_callable=AsyncMock, return_value=None),
            patch("app.cache.intel_cache.set_cached"),
        ):
            result = await enrich_entity("acme.com", "Acme")
            assert result["domain"] == "acme.com"
            assert result["legal_name"] is None


class TestFindSuggestedContacts:
    @pytest.mark.asyncio
    async def test_deduplication_by_email(self):
        with (
            patch(
                "app.enrichment_service._explorium_find_contacts",
                new_callable=AsyncMock,
                return_value=[
                    {
                        "full_name": "John Doe",
                        "title": "Procurement Manager",
                        "email": "john@acme.com",
                        "phone": None,
                        "linkedin_url": None,
                        "location": None,
                        "company": "Acme",
                    },
                ],
            ),
            patch(
                "app.enrichment_service._ai_find_contacts",
                new_callable=AsyncMock,
                return_value=[
                    {
                        "full_name": "John Doe",
                        "title": "Procurement Manager",
                        "email": "john@acme.com",
                        "phone": None,
                        "linkedin_url": None,
                        "location": None,
                        "company": "Acme",
                    },
                ],
            ),
        ):
            result = await find_suggested_contacts("acme.com", "Acme")
            assert len(result) == 1

    @pytest.mark.asyncio
    async def test_filters_irrelevant_titles(self):
        with (
            patch(
                "app.enrichment_service._explorium_find_contacts",
                new_callable=AsyncMock,
                return_value=[
                    {
                        "full_name": "Jane Smith",
                        "title": "Procurement Director",
                        "email": "jane@acme.com",
                        "phone": None,
                        "linkedin_url": None,
                        "location": None,
                        "company": "Acme",
                    },
                    {
                        "full_name": "Bob Intern",
                        "title": "Janitor",
                        "email": "bob@acme.com",
                        "phone": None,
                        "linkedin_url": None,
                        "location": None,
                        "company": "Acme",
                    },
                ],
            ),
            patch("app.enrichment_service._ai_find_contacts", new_callable=AsyncMock, return_value=[]),
        ):
            result = await find_suggested_contacts("acme.com")
            # Jane should pass (procurement), Bob filtered (janitor)
            assert len(result) == 1
            assert result[0]["full_name"] == "Jane Smith"

    @pytest.mark.asyncio
    async def test_keeps_all_if_filter_removes_everything(self):
        with (
            patch(
                "app.enrichment_service._explorium_find_contacts",
                new_callable=AsyncMock,
                return_value=[
                    {
                        "full_name": "Bob",
                        "title": "Janitor",
                        "email": "bob@acme.com",
                        "phone": None,
                        "linkedin_url": None,
                        "location": None,
                        "company": "Acme",
                    },
                ],
            ),
            patch("app.enrichment_service._ai_find_contacts", new_callable=AsyncMock, return_value=[]),
        ):
            result = await find_suggested_contacts("acme.com")
            # All filtered = return unfiltered
            assert len(result) == 1

    @pytest.mark.asyncio
    async def test_handles_provider_exception(self):
        with (
            patch(
                "app.enrichment_service._explorium_find_contacts",
                new_callable=AsyncMock,
                side_effect=Exception("API down"),
            ),
            patch(
                "app.enrichment_service._ai_find_contacts",
                new_callable=AsyncMock,
                return_value=[
                    {
                        "full_name": "Jane",
                        "title": "Buyer",
                        "email": "jane@acme.com",
                        "phone": None,
                        "linkedin_url": None,
                        "location": None,
                        "company": "Acme",
                    },
                ],
            ),
        ):
            result = await find_suggested_contacts("acme.com")
            assert len(result) == 1


# ═══════════════════════════════════════════════════════════════════════
# 5. prospect_signals.py
# ═══════════════════════════════════════════════════════════════════════

from app.services.prospect_signals import (
    _build_writeup_prompt,
    _compare_sizes,
    _template_fallback_writeup,
    enrich_with_events,
    enrich_with_hiring,
    enrich_with_intent,
    find_similar_customers,
    generate_ai_writeup,
)


class TestCompareSizes:
    def test_same_bracket(self):
        assert _compare_sizes("201-500", "300") is True

    def test_adjacent_bracket(self):
        assert _compare_sizes("201-500", "501-1000") is True

    def test_far_brackets(self):
        assert _compare_sizes("1-50", "5001-10000") is False

    def test_plus_notation(self):
        assert _compare_sizes("10001+", "5001-10000") is True

    def test_none_values(self):
        assert _compare_sizes(None, "200") is False
        assert _compare_sizes("200", None) is False

    def test_invalid_string(self):
        assert _compare_sizes("unknown", "200") is False


class TestEnrichWithIntent:
    def test_stores_intent(self, db_session):
        prospect = MagicMock()
        prospect.readiness_signals = {}
        prospect.name = "Test Co"

        with (
            patch(
                "app.services.prospect_signals.db.get"
                if False
                else "app.services.prospect_signals.calculate_readiness_score",
                return_value=(50, {}),
            ),
        ):
            db = MagicMock()
            db.get.return_value = prospect
            intent_data = {"strength": "strong", "topics": ["semiconductors"]}
            enrich_with_intent(1, intent_data, db)
            assert prospect.readiness_signals["intent"] == intent_data
            db.commit.assert_called_once()

    def test_prospect_not_found(self, db_session):
        db = MagicMock()
        db.get.return_value = None
        enrich_with_intent(999, {}, db)
        db.commit.assert_not_called()


class TestEnrichWithHiring:
    def test_stores_hiring(self):
        prospect = MagicMock()
        prospect.readiness_signals = {}
        prospect.name = "Test Co"

        with patch("app.services.prospect_signals.calculate_readiness_score", return_value=(40, {})):
            db = MagicMock()
            db.get.return_value = prospect
            enrich_with_hiring(1, {"type": "procurement", "detail": 5}, db)
            assert prospect.readiness_signals["hiring"]["type"] == "procurement"
            db.commit.assert_called_once()

    def test_prospect_not_found(self):
        db = MagicMock()
        db.get.return_value = None
        enrich_with_hiring(999, {}, db)
        db.commit.assert_not_called()


class TestEnrichWithEvents:
    def test_stores_events(self):
        prospect = MagicMock()
        prospect.readiness_signals = {}
        prospect.name = "Test Co"

        with patch("app.services.prospect_signals.calculate_readiness_score", return_value=(30, {})):
            db = MagicMock()
            db.get.return_value = prospect
            events = [{"type": "funding", "date": "2026-01-01", "description": "Series B"}]
            enrich_with_events(1, events, db)
            assert prospect.readiness_signals["events"] == events
            db.commit.assert_called_once()

    def test_prospect_not_found(self):
        db = MagicMock()
        db.get.return_value = None
        enrich_with_events(999, [], db)
        db.commit.assert_not_called()


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
    @pytest.mark.asyncio
    async def test_prospect_not_found(self):
        from app.services.prospect_signals import enrich_missing_signals

        db = MagicMock()
        db.get.return_value = None
        result = await enrich_missing_signals(999, db)
        assert result is False

    @pytest.mark.asyncio
    async def test_already_has_signals(self):
        from app.services.prospect_signals import enrich_missing_signals

        prospect = MagicMock()
        prospect.readiness_signals = {
            "intent": {"strength": "strong"},
            "hiring": {"type": "procurement"},
        }
        db = MagicMock()
        db.get.return_value = prospect
        result = await enrich_missing_signals(1, db)
        assert result is False

    @pytest.mark.asyncio
    async def test_no_api_key(self):
        from app.services.prospect_signals import enrich_missing_signals

        prospect = MagicMock()
        prospect.readiness_signals = {}
        prospect.domain = "test.com"
        db = MagicMock()
        db.get.return_value = prospect

        with patch("app.services.prospect_discovery_explorium._get_api_key", return_value=""):
            # Need to patch the lazy import
            with patch.dict("sys.modules", {}):
                result = await enrich_missing_signals(1, db)
                assert result is False

    @pytest.mark.asyncio
    async def test_no_domain(self):
        from app.services.prospect_signals import enrich_missing_signals

        prospect = MagicMock()
        prospect.readiness_signals = {}
        prospect.domain = ""
        db = MagicMock()
        db.get.return_value = prospect

        with patch("app.services.prospect_discovery_explorium._get_api_key", return_value="key"):
            result = await enrich_missing_signals(1, db)
            assert result is False

    @pytest.mark.asyncio
    async def test_successful_backfill(self):
        from app.services.prospect_signals import enrich_missing_signals

        prospect = MagicMock()
        prospect.readiness_signals = {}
        prospect.domain = "test.com"
        prospect.name = "Test Co"
        prospect.id = 1
        db = MagicMock()
        db.get.return_value = prospect

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "businesses": [
                {
                    "business_intent_topics": ["electronic components", "semiconductor sourcing", "circuit boards"],
                    "workforce_trends": {"procurement": 5},
                    "recent_events": [{"type": "funding", "date": "2026-01-01", "description": "Series B"}],
                }
            ]
        }

        with (
            patch("app.services.prospect_discovery_explorium._get_api_key", return_value="key"),
            patch("app.http_client.http.post", new_callable=AsyncMock, return_value=mock_resp),
            patch("app.services.prospect_signals.calculate_readiness_score", return_value=(60, {})),
        ):
            result = await enrich_missing_signals(1, db)
            assert result is True
            db.commit.assert_called()

    @pytest.mark.asyncio
    async def test_api_failure(self):
        from app.services.prospect_signals import enrich_missing_signals

        prospect = MagicMock()
        prospect.readiness_signals = {}
        prospect.domain = "test.com"
        prospect.id = 1
        db = MagicMock()
        db.get.return_value = prospect

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Server Error"

        with (
            patch("app.services.prospect_discovery_explorium._get_api_key", return_value="key"),
            patch("app.http_client.http.post", new_callable=AsyncMock, return_value=mock_resp),
        ):
            result = await enrich_missing_signals(1, db)
            assert result is False

    @pytest.mark.asyncio
    async def test_exception_handling(self):
        from app.services.prospect_signals import enrich_missing_signals

        prospect = MagicMock()
        prospect.readiness_signals = {}
        prospect.domain = "test.com"
        prospect.id = 1
        db = MagicMock()
        db.get.return_value = prospect

        with (
            patch("app.services.prospect_discovery_explorium._get_api_key", return_value="key"),
            patch("app.http_client.http.post", new_callable=AsyncMock, side_effect=Exception("Network error")),
        ):
            result = await enrich_missing_signals(1, db)
            assert result is False
