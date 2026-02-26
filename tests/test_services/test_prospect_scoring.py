"""Tests for prospect scoring service — deterministic, no API calls."""

import os

os.environ["TESTING"] = "1"
os.environ["RATE_LIMIT_ENABLED"] = "false"

import pytest

from app.services.prospect_scoring import (
    ALL_NAICS_CODES,
    ICP_SEGMENTS,
    apply_historical_bonus,
    calculate_composite_score,
    calculate_fit_score,
    calculate_readiness_score,
    classify_readiness,
    match_industry_segment,
    score_company_size,
)

# ── Fixtures ─────────────────────────────────────────────────────────

# Perfect ICP match: Raytheon-like aerospace, large, multi-region, procurement
PERFECT_FIT = {
    "name": "Raytheon Sensors",
    "industry": "Aerospace & Defense",
    "naics_code": "336412",
    "employee_count_range": "5001-10000",
    "region": "Global",
    "has_procurement_staff": True,
    "uses_brokers": True,
}

# Good mid-market EMS company
GOOD_MID_MARKET = {
    "name": "Midwest PCB Assembly",
    "industry": "Electronics Manufacturing Services",
    "naics_code": "334418",
    "employee_count_range": "201-500",
    "region": "US",
    "has_procurement_staff": True,
    "uses_brokers": None,
}

# Poor fit: restaurant chain, small, no procurement
POOR_FIT = {
    "name": "Joe's Diner Chain",
    "industry": "Food Services",
    "naics_code": "722511",
    "employee_count_range": "11-50",
    "region": "US",
    "has_procurement_staff": False,
    "uses_brokers": False,
}

# Minimal data — missing most fields
SPARSE_DATA = {
    "name": "Mystery Corp",
}

# SF-imported prospect with rich history
SF_WITH_HISTORY = {
    "name": "Legacy Aero Inc",
    "industry": "Aerospace",
    "naics_code": "336413",
    "employee_count_range": "1001-5000",
    "region": "US",
    "has_procurement_staff": True,
    "uses_brokers": True,
}
SF_HISTORY_CONTEXT = {
    "quote_count": 47,
    "bought_before": True,
    "quoted_before": True,
    "last_activity": "2025-08-15",
    "total_revenue": 125000.50,
    "years_active": 3,
}

# SF-imported prospect with no history
SF_NO_HISTORY = {
    "name": "Imported Co",
    "industry": "Manufacturing",
    "naics_code": None,
    "employee_count_range": None,
    "region": None,
}

# High readiness signals
HIGH_READINESS_SIGNALS = {
    "intent": {"strength": "strong", "topics": ["electronic components", "semiconductors"]},
    "events": [
        {"type": "new_funding_round", "date": "2026-01"},
        {"type": "new_product_launch", "date": "2025-11"},
    ],
    "hiring": {"type": "procurement", "count": 5},
    "new_procurement_hire": True,
    "contacts_verified_count": 5,
}

# Low readiness — no signals at all
LOW_READINESS_SIGNALS = {}

# Moderate readiness
MODERATE_READINESS_SIGNALS = {
    "intent": {"strength": "moderate"},
    "events": [],
    "hiring": {"type": "engineering"},
    "new_procurement_hire": False,
    "contacts_verified_count": 2,
}


# ── Fit Score Tests ──────────────────────────────────────────────────


class TestFitScore:
    """Test calculate_fit_score with various prospect profiles."""

    def test_perfect_icp_match(self):
        """Raytheon-like: should score near maximum."""
        score, reasoning = calculate_fit_score(PERFECT_FIT)
        assert 85 <= score <= 100
        assert "Aerospace" in reasoning
        assert "exact match" in reasoning.lower() or "336412" in reasoning

    def test_good_mid_market(self):
        """Mid-market EMS: strong match — exact NAICS + procurement staff."""
        score, reasoning = calculate_fit_score(GOOD_MID_MARKET)
        assert 75 <= score <= 95
        assert "Electronics" in reasoning or "EMS" in reasoning or "334418" in reasoning

    def test_poor_fit(self):
        """Restaurant chain: should score very low."""
        score, reasoning = calculate_fit_score(POOR_FIT)
        assert score <= 30
        assert "no" in reasoning.lower() or "0/" in reasoning

    def test_sparse_data_neutral(self):
        """Missing data returns neutral, not zero."""
        score, reasoning = calculate_fit_score(SPARSE_DATA)
        # Neutral across all categories should land in 30-50 range
        assert 25 <= score <= 55
        assert score > 0, "Missing data must not produce zero"

    def test_score_range_0_100(self):
        """Score is always within bounds."""
        for data in [PERFECT_FIT, GOOD_MID_MARKET, POOR_FIT, SPARSE_DATA]:
            score, _ = calculate_fit_score(data)
            assert 0 <= score <= 100

    def test_deterministic(self):
        """Same input always produces same output."""
        s1, r1 = calculate_fit_score(PERFECT_FIT)
        s2, r2 = calculate_fit_score(PERFECT_FIT)
        assert s1 == s2
        assert r1 == r2

    def test_reasoning_includes_all_signals(self):
        """Reasoning text covers all scoring dimensions."""
        _, reasoning = calculate_fit_score(PERFECT_FIT)
        assert "Industry" in reasoning
        assert "Size" in reasoning
        assert "Procurement" in reasoning or "procurement" in reasoning
        assert "NAICS" in reasoning
        assert "Geography" in reasoning or "geography" in reasoning
        assert "Broker" in reasoning or "broker" in reasoning


# ── Readiness Score Tests ────────────────────────────────────────────


class TestReadinessScore:
    """Test calculate_readiness_score with various signal combinations."""

    def test_high_readiness(self):
        """All signals firing: should score near maximum."""
        score, breakdown = calculate_readiness_score(
            {"name": "Hot Prospect"}, HIGH_READINESS_SIGNALS
        )
        assert score >= 80
        assert breakdown["intent"]["score"] == 35
        assert breakdown["events"]["score"] >= 20
        assert breakdown["hiring"]["score"] == 20
        assert breakdown["contacts"]["score"] == 10

    def test_low_readiness(self):
        """No signals at all: low scores, mostly neutral."""
        score, breakdown = calculate_readiness_score(
            {"name": "Cold Prospect"}, LOW_READINESS_SIGNALS
        )
        assert score <= 30
        assert breakdown["events"]["score"] == 0

    def test_moderate_readiness(self):
        """Mixed signals."""
        score, breakdown = calculate_readiness_score(
            {"name": "Warm Prospect"}, MODERATE_READINESS_SIGNALS
        )
        assert 30 <= score <= 65
        assert breakdown["intent"]["score"] == 20
        assert breakdown["hiring"]["score"] == 15

    def test_score_range_0_100(self):
        """Score always within bounds."""
        for signals in [HIGH_READINESS_SIGNALS, LOW_READINESS_SIGNALS, MODERATE_READINESS_SIGNALS]:
            score, _ = calculate_readiness_score({"name": "Test"}, signals)
            assert 0 <= score <= 100

    def test_breakdown_structure(self):
        """Breakdown dict has expected keys and sub-keys."""
        _, breakdown = calculate_readiness_score(
            {"name": "Test"}, HIGH_READINESS_SIGNALS
        )
        for key in ["intent", "events", "hiring", "new_procurement_hire", "contacts"]:
            assert key in breakdown
            assert "score" in breakdown[key]
            assert "max" in breakdown[key]
            assert "detail" in breakdown[key]

    def test_deterministic(self):
        """Same input = same output."""
        s1, b1 = calculate_readiness_score({"name": "X"}, HIGH_READINESS_SIGNALS)
        s2, b2 = calculate_readiness_score({"name": "X"}, HIGH_READINESS_SIGNALS)
        assert s1 == s2
        assert b1 == b2

    def test_events_cap_at_25(self):
        """Multiple events still cap at 25."""
        many_events = {
            "events": [
                {"type": "new_funding_round"},
                {"type": "new_product_launch"},
                {"type": "expansion"},
                {"type": "acquisition"},
            ],
        }
        _, breakdown = calculate_readiness_score({"name": "Test"}, many_events)
        assert breakdown["events"]["score"] <= 25

    def test_intent_strength_levels(self):
        """Each intent strength level maps correctly."""
        for strength, expected in [("strong", 35), ("moderate", 20), ("weak", 10), ("none", 0)]:
            _, breakdown = calculate_readiness_score(
                {"name": "Test"},
                {"intent": {"strength": strength}},
            )
            assert breakdown["intent"]["score"] == expected

    def test_contact_quality_tiers(self):
        """Verified contact count maps to correct score."""
        # 3+ verified = 10
        _, b = calculate_readiness_score({"name": "T"}, {"contacts_verified_count": 5})
        assert b["contacts"]["score"] == 10

        # 1-2 verified = 7
        _, b = calculate_readiness_score({"name": "T"}, {"contacts_verified_count": 1})
        assert b["contacts"]["score"] == 7

        # Unverified only = 3
        _, b = calculate_readiness_score(
            {"name": "T"}, {"contacts_verified_count": 0, "contacts_unverified_count": 2}
        )
        assert b["contacts"]["score"] == 3

        # None = 0
        _, b = calculate_readiness_score({"name": "T"}, {"contacts_verified_count": 0})
        assert b["contacts"]["score"] == 0


# ── Classification Tests ─────────────────────────────────────────────


class TestClassifyReadiness:
    """Test readiness classification tiers."""

    def test_call_now(self):
        assert classify_readiness(70) == "call_now"
        assert classify_readiness(100) == "call_now"
        assert classify_readiness(85) == "call_now"

    def test_nurture(self):
        assert classify_readiness(40) == "nurture"
        assert classify_readiness(69) == "nurture"
        assert classify_readiness(55) == "nurture"

    def test_monitor(self):
        assert classify_readiness(0) == "monitor"
        assert classify_readiness(39) == "monitor"
        assert classify_readiness(10) == "monitor"


# ── Composite Score Tests ────────────────────────────────────────────


class TestCompositeScore:
    """Test weighted composite score calculation."""

    def test_basic_calculation(self):
        """60% fit + 40% readiness."""
        score = calculate_composite_score(80, 60)
        # 80*0.6 + 60*0.4 = 48 + 24 = 72
        assert score == 72.0

    def test_perfect_scores(self):
        score = calculate_composite_score(100, 100)
        assert score == 100.0

    def test_zero_scores(self):
        score = calculate_composite_score(0, 0)
        assert score == 0.0

    def test_fit_weighted_higher(self):
        """Fit matters more than readiness."""
        high_fit = calculate_composite_score(80, 20)
        high_readiness = calculate_composite_score(20, 80)
        assert high_fit > high_readiness


# ── Historical Bonus Tests ───────────────────────────────────────────


class TestHistoricalBonus:
    """Test SF prospect historical context bonuses."""

    def test_full_history_bonus(self):
        """Prospect with buy + quote + recent activity gets max bonus."""
        fit, readiness = apply_historical_bonus(60, 40, SF_HISTORY_CONTEXT)
        assert fit == 75  # 60 + 15 (bought)
        assert readiness == 55  # 40 + 10 (recent) + 5 (high quote count)

    def test_quoted_only_bonus(self):
        """Quoted but never bought — smaller fit bonus."""
        ctx = {"quoted_before": True, "quote_count": 5}
        fit, readiness = apply_historical_bonus(60, 40, ctx)
        assert fit == 70  # 60 + 10 (quoted)
        assert readiness == 40  # no recent activity, low quote count

    def test_inferred_quoted_from_count(self):
        """quote_count > 0 infers quoted_before even if not explicitly set."""
        ctx = {"quote_count": 3}
        fit, readiness = apply_historical_bonus(60, 40, ctx)
        assert fit == 70  # 60 + 10 (inferred quoted)

    def test_empty_context_no_change(self):
        """Empty dict or None returns scores unchanged."""
        fit, readiness = apply_historical_bonus(60, 40, {})
        assert fit == 60
        assert readiness == 40

        fit, readiness = apply_historical_bonus(60, 40, None)
        assert fit == 60
        assert readiness == 40

    def test_no_history_sf_import(self):
        """SF import with just sf_account_id but no interaction data."""
        ctx = {"sf_account_id": "SF-001"}
        fit, readiness = apply_historical_bonus(60, 40, ctx)
        assert fit == 60
        assert readiness == 40

    def test_caps_at_100(self):
        """Bonus doesn't push scores over 100."""
        fit, readiness = apply_historical_bonus(95, 95, SF_HISTORY_CONTEXT)
        assert fit == 100
        assert readiness == 100

    def test_recent_activity_bonus(self):
        """Activity within 2 years gives readiness bonus."""
        ctx = {"last_activity": "2025-01-01"}
        _, readiness = apply_historical_bonus(60, 40, ctx)
        assert readiness == 50  # +10

    def test_old_activity_no_bonus(self):
        """Activity older than 2 years gives no bonus."""
        ctx = {"last_activity": "2020-06-15"}
        _, readiness = apply_historical_bonus(60, 40, ctx)
        assert readiness == 40

    def test_high_quote_count_bonus(self):
        """Quote count >20 gives readiness bonus."""
        ctx = {"quote_count": 25}
        fit, readiness = apply_historical_bonus(60, 40, ctx)
        assert fit == 70  # +10 inferred quoted
        assert readiness == 45  # +5 high quote count


# ── Industry Matching Tests ──────────────────────────────────────────


class TestMatchIndustry:
    """Test industry segment matching logic."""

    def test_exact_naics_match(self):
        """Exact NAICS code gets full 30 points."""
        segment, score = match_industry_segment(None, "336412")
        assert score == 30
        assert segment == "Aerospace & Defense"

    def test_naics_4digit_match(self):
        """4-digit NAICS prefix gets 20 points."""
        segment, score = match_industry_segment(None, "336499")
        assert score == 20

    def test_industry_keyword_match(self):
        """Industry keyword match gets 20 points."""
        segment, score = match_industry_segment("Aerospace Manufacturing", None)
        assert score == 20
        assert segment == "Aerospace & Defense"

    def test_no_match(self):
        """No match returns neutral."""
        segment, score = match_industry_segment("Restaurant Services", "722511")
        assert segment is None
        assert score == 10  # neutral

    def test_empty_inputs_neutral(self):
        """None/None returns neutral score."""
        segment, score = match_industry_segment(None, None)
        assert segment is None
        assert score == 10

    def test_all_segments_have_matches(self):
        """Every ICP segment can be matched via its NAICS codes."""
        for key, seg in ICP_SEGMENTS.items():
            for code in seg["naics_codes"]:
                segment, score = match_industry_segment(None, code)
                assert score == 30, f"NAICS {code} should score 30 for {key}"
                assert segment == seg["name"]


# ── Company Size Tests ───────────────────────────────────────────────


class TestScoreCompanySize:
    """Test employee count range scoring."""

    def test_sweet_spot(self):
        """500-10000 = max 20 points."""
        assert score_company_size("501-1000") == 20
        assert score_company_size("5001-10000") == 20

    def test_mid_range(self):
        """200-499 = 15 points."""
        assert score_company_size("201-500") == 15

    def test_large(self):
        """10001+ = 15 points."""
        assert score_company_size("10001+") == 15

    def test_small(self):
        """50-199 = 10 points."""
        assert score_company_size("51-200") == 10

    def test_very_small(self):
        """<50 = 5 points."""
        assert score_company_size("1-50") == 5

    def test_unknown_neutral(self):
        """None/empty = neutral 10."""
        assert score_company_size(None) == 10
        assert score_company_size("") == 10

    def test_comma_format(self):
        """Handles comma-separated numbers."""
        assert score_company_size("1,001-5,000") == 20

    def test_exact_number(self):
        """Single number (not range)."""
        assert score_company_size("500") == 20
        assert score_company_size("50") == 10


# ── Edge Case Tests ──────────────────────────────────────────────────


class TestEdgeCases:
    """Edge cases that should not crash."""

    def test_fit_empty_dict(self):
        score, _ = calculate_fit_score({})
        assert 0 <= score <= 100

    def test_readiness_empty_signals(self):
        score, _ = calculate_readiness_score({}, {})
        assert 0 <= score <= 100

    def test_readiness_malformed_intent(self):
        """Intent as a string instead of dict."""
        score, breakdown = calculate_readiness_score(
            {"name": "Test"}, {"intent": "strong"}
        )
        assert 0 <= score <= 100

    def test_readiness_malformed_events(self):
        """Events as a string instead of list."""
        score, _ = calculate_readiness_score(
            {"name": "Test"}, {"events": "some event"}
        )
        assert 0 <= score <= 100

    def test_historical_malformed_quote_count(self):
        """Non-numeric quote_count."""
        fit, readiness = apply_historical_bonus(60, 40, {"quote_count": "many"})
        assert fit == 60
        assert readiness == 40

    def test_fit_with_none_values(self):
        """Explicitly None values in all fields."""
        data = {
            "name": "None Corp",
            "industry": None,
            "naics_code": None,
            "employee_count_range": None,
            "region": None,
            "has_procurement_staff": None,
            "uses_brokers": None,
        }
        score, _ = calculate_fit_score(data)
        assert 25 <= score <= 55, "All-None should give neutral scores"

    def test_composite_negative_inputs(self):
        """Negative inputs (shouldn't happen, but be safe)."""
        score = calculate_composite_score(-10, -20)
        assert score == -14.0  # just math, no clamping needed at composite level
