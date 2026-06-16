"""test_scoring.py — Tests for sighting scoring and lead classification.

Called by: pytest
Depends on: app.scoring
"""

import pytest

from app.scoring import (
    MISSING_DATA_SCORE,
    NEW_VENDOR_BASELINE,
    WEAK_LEAD_THRESHOLD,
    classify_lead,
    confidence_color,
    explain_lead,
    is_weak_lead,
    score_sighting,
    score_sighting_v2,
    score_unified,
)

# ---------------------------------------------------------------------------
# score_sighting
# ---------------------------------------------------------------------------


class TestScoreSighting:
    @pytest.mark.parametrize(
        ("vendor_score", "is_authorized", "expected"),
        [
            pytest.param(50.0, True, 100.0, id="authorized_returns_100"),
            pytest.param(None, True, 100.0, id="authorized_ignores_vendor_score"),
            pytest.param(None, False, NEW_VENDOR_BASELINE, id="none_vendor_score_returns_baseline"),
            pytest.param(72.456, False, 72.5, id="specific_vendor_score_rounded"),
            pytest.param(0.0, False, 0.0, id="zero_vendor_score"),
            pytest.param(100.0, False, 100.0, id="full_vendor_score"),
        ],
    )
    def test_score_sighting(self, vendor_score, is_authorized, expected):
        assert score_sighting(vendor_score, is_authorized=is_authorized) == expected


# ---------------------------------------------------------------------------
# score_sighting_v2
# ---------------------------------------------------------------------------


class TestScoreSightingV2:
    def test_authorized_gets_trust_95(self):
        total, comp = score_sighting_v2(None, is_authorized=True)
        assert comp["trust"] == 95.0

    def test_vendor_score_used_for_trust(self):
        _, comp = score_sighting_v2(80.0, is_authorized=False)
        assert comp["trust"] == 80.0

    def test_no_vendor_score_uses_baseline(self):
        _, comp = score_sighting_v2(None, is_authorized=False)
        assert comp["trust"] == NEW_VENDOR_BASELINE

    def test_missing_price_penalty(self):
        _, comp = score_sighting_v2(50.0, False)
        assert comp["price"] == MISSING_DATA_SCORE

    def test_missing_qty_penalty(self):
        _, comp = score_sighting_v2(50.0, False)
        assert comp["qty"] == MISSING_DATA_SCORE

    def test_good_price_ratio(self):
        # median=10, unit=5 → ratio=2.0 → price_f = min(100, 2.0*50) = 100
        _, comp = score_sighting_v2(50.0, False, unit_price=5.0, median_price=10.0)
        assert comp["price"] == 100.0

    def test_bad_price_ratio(self):
        # median=5, unit=50 → ratio=0.1 → price_f = 0.1*50 = 5.0
        _, comp = score_sighting_v2(50.0, False, unit_price=50.0, median_price=5.0)
        assert comp["price"] == 5.0

    def test_full_qty_coverage(self):
        _, comp = score_sighting_v2(50.0, False, qty_available=1000, target_qty=1000)
        assert comp["qty"] == 100.0

    def test_partial_qty_coverage(self):
        _, comp = score_sighting_v2(50.0, False, qty_available=500, target_qty=1000)
        assert comp["qty"] == 50.0

    def test_qty_available_no_target(self):
        _, comp = score_sighting_v2(50.0, False, qty_available=100)
        assert comp["qty"] == 60.0

    def test_excess_qty_capped(self):
        _, comp = score_sighting_v2(50.0, False, qty_available=5000, target_qty=1000)
        assert comp["qty"] == 100.0

    def test_fresh_data_high_freshness(self):
        _, comp = score_sighting_v2(50.0, False, age_hours=0.0)
        assert comp["freshness"] == 100.0

    def test_old_data_low_freshness(self):
        # age_hours=480 (20 days) → 100 - (480/24)*5 = 100 - 100 = 0
        _, comp = score_sighting_v2(50.0, False, age_hours=480.0)
        assert comp["freshness"] == 0.0

    def test_very_old_data_clamped_zero(self):
        _, comp = score_sighting_v2(50.0, False, age_hours=9999.0)
        assert comp["freshness"] == 0.0

    def test_no_age_data_penalty(self):
        _, comp = score_sighting_v2(50.0, False)
        assert comp["freshness"] == MISSING_DATA_SCORE

    def test_all_fields_completeness_100(self):
        _, comp = score_sighting_v2(
            50.0,
            False,
            has_price=True,
            has_qty=True,
            has_lead_time=True,
            has_condition=True,
        )
        assert comp["completeness"] == 100.0

    def test_no_fields_completeness_0(self):
        _, comp = score_sighting_v2(50.0, False)
        assert comp["completeness"] == 0.0

    def test_two_fields_completeness_50(self):
        _, comp = score_sighting_v2(50.0, False, has_price=True, has_qty=True)
        assert comp["completeness"] == 50.0

    def test_total_is_weighted_sum(self):
        total, comp = score_sighting_v2(
            50.0,
            False,
            unit_price=10.0,
            median_price=10.0,
            qty_available=1000,
            target_qty=1000,
            age_hours=0.0,
            has_price=True,
            has_qty=True,
            has_lead_time=True,
            has_condition=True,
        )
        expected = (
            comp["trust"] * 0.30
            + comp["price"] * 0.25
            + comp["qty"] * 0.20
            + comp["freshness"] * 0.15
            + comp["completeness"] * 0.10
        )
        assert total == round(expected, 1)

    def test_returns_tuple(self):
        result = score_sighting_v2(50.0, False)
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], float)
        assert isinstance(result[1], dict)


# ---------------------------------------------------------------------------
# classify_lead
# ---------------------------------------------------------------------------


class TestClassifyLead:
    @pytest.mark.parametrize(
        ("score", "kwargs", "expected"),
        [
            pytest.param(
                50, {"is_authorized": True, "has_price": True}, "strong", id="authorized_with_price_is_strong"
            ),
            pytest.param(55, {"has_price": True, "has_qty": True}, "strong", id="high_score_two_actionable_is_strong"),
            pytest.param(
                60,
                {"has_price": True, "has_qty": True, "has_contact": True},
                "strong",
                id="high_score_three_actionable_is_strong",
            ),
            pytest.param(40, {"has_price": True}, "moderate", id="mid_score_one_actionable_is_moderate"),
            pytest.param(40, {}, "weak", id="mid_score_no_actionable_is_weak"),
            pytest.param(35, {"evidence_tier": "T1"}, "moderate", id="t1_tier_score_35_is_moderate"),
            pytest.param(35, {"evidence_tier": "T2"}, "moderate", id="t2_tier_score_35_is_moderate"),
            pytest.param(20, {"evidence_tier": "T1"}, "weak", id="t1_tier_low_score_is_weak"),
            pytest.param(35, {"evidence_tier": "T3"}, "weak", id="t3_tier_not_promoted"),
            pytest.param(10, {}, "weak", id="low_score_nothing_is_weak"),
            pytest.param(55, {"has_price": True, "has_qty": True}, "strong", id="boundary_score_55_two_actionable"),
            pytest.param(
                54,
                {"has_price": True, "has_qty": True},
                "moderate",
                id="boundary_score_54_two_actionable_not_strong",
            ),
            pytest.param(35, {"evidence_tier": "t1"}, "moderate", id="tier_case_insensitive"),
            pytest.param(35, {"evidence_tier": None}, "weak", id="none_evidence_tier"),
        ],
    )
    def test_classify_lead(self, score, kwargs, expected):
        assert classify_lead(score, **kwargs) == expected

    def test_authorized_without_price_not_auto_strong(self):
        # Authorized but no price — falls through to other rules
        result = classify_lead(30, is_authorized=True, has_price=False)
        assert result in ("moderate", "weak")


# ---------------------------------------------------------------------------
# explain_lead
# ---------------------------------------------------------------------------


class TestExplainLead:
    @pytest.mark.parametrize(
        ("args", "kwargs", "contains", "excludes"),
        [
            pytest.param(
                ("Digi-Key",),
                {"is_authorized": True},
                ["authorized distributor", "Digi-Key"],
                [],
                id="authorized_vendor",
            ),
            pytest.param(("Acme",), {"vendor_score": 70.0}, ["proven vendor"], [], id="proven_vendor"),
            pytest.param(("NewCo",), {"vendor_score": 40.0}, ["developing vendor"], [], id="developing_vendor"),
            pytest.param((None,), {}, ["Unknown vendor"], [], id="unknown_vendor"),
            pytest.param(
                ("BadCo",), {"vendor_score": 10.0}, ["BadCo"], ["proven", "developing"], id="low_score_vendor"
            ),
            pytest.param(
                ("X",), {"unit_price": 1.50, "qty_available": 5000}, ["5,000 pcs", "$1.50"], [], id="price_and_qty"
            ),
            pytest.param(("X",), {"unit_price": 0.0523, "qty_available": 100}, ["$0.0523"], [], id="sub_dollar_price"),
            pytest.param(("X",), {"qty_available": 1000}, ["1,000 pcs", "no price listed"], [], id="qty_no_price"),
            pytest.param(("X",), {"unit_price": 5.00}, ["$5.00", "qty unknown"], [], id="price_no_qty"),
            pytest.param(
                ("X",), {"unit_price": 8.0, "median_price": 10.0}, ["below market"], [], id="below_market_price"
            ),
            pytest.param(
                ("X",), {"unit_price": 15.0, "median_price": 10.0}, ["above market"], [], id="above_market_price"
            ),
            pytest.param(
                ("X",),
                {"unit_price": 1.0, "qty_available": 1000, "target_qty": 500},
                ["covers full order qty"],
                [],
                id="full_order_coverage",
            ),
            pytest.param(
                ("X",),
                {"unit_price": 1.0, "qty_available": 600, "target_qty": 1000},
                ["covers 60% of order qty"],
                [],
                id="partial_order_coverage",
            ),
            pytest.param(("X",), {"has_contact": True}, ["contact info available"], [], id="contact_info_available"),
            pytest.param(
                ("X",),
                {"has_contact": False, "is_authorized": False},
                ["no contact info"],
                [],
                id="no_contact_not_authorized",
            ),
            pytest.param(("X",), {"age_days": 45}, ["45 days old"], [], id="old_data_warning"),
            pytest.param(("X",), {"age_days": 10}, [], ["days old"], id="recent_data_no_warning"),
        ],
    )
    def test_explain_lead(self, args, kwargs, contains, excludes):
        result = explain_lead(*args, **kwargs)
        for substring in contains:
            assert substring in result
        for substring in excludes:
            assert substring not in result


# ---------------------------------------------------------------------------
# is_weak_lead
# ---------------------------------------------------------------------------


class TestIsWeakLead:
    @pytest.mark.parametrize(
        ("score", "kwargs", "expected"),
        [
            pytest.param(0, {"is_authorized": True}, False, id="authorized_never_weak"),
            pytest.param(20, {"evidence_tier": "T1", "has_price": True}, False, id="t1_with_price_not_weak"),
            pytest.param(20, {"evidence_tier": "T2", "has_qty": True}, False, id="t2_with_qty_not_weak"),
            # T1 but no price/qty — falls through, below threshold with no data
            pytest.param(20, {"evidence_tier": "T1"}, True, id="t1_no_data_is_weak"),
            pytest.param(WEAK_LEAD_THRESHOLD - 1, {}, True, id="below_threshold_no_data_is_weak"),
            pytest.param(WEAK_LEAD_THRESHOLD, {}, False, id="at_threshold_no_data_not_weak"),
            pytest.param(10, {"has_price": True}, False, id="below_threshold_with_price_not_weak"),
            pytest.param(10, {"has_qty": True}, False, id="below_threshold_with_qty_not_weak"),
            pytest.param(50, {}, False, id="above_threshold_no_data_not_weak"),
        ],
    )
    def test_is_weak_lead(self, score, kwargs, expected):
        assert is_weak_lead(score, **kwargs) is expected


# ---------------------------------------------------------------------------
# confidence_color
# ---------------------------------------------------------------------------


class TestConfidenceColor:
    @pytest.mark.parametrize(
        ("score", "expected"),
        [
            pytest.param(75, "green", id="green_at_75"),
            pytest.param(90, "green", id="green_above_75"),
            pytest.param(50, "amber", id="amber_at_50"),
            pytest.param(74, "amber", id="amber_at_74"),
            pytest.param(49, "red", id="red_at_49"),
            pytest.param(0, "red", id="red_at_0"),
            pytest.param(100, "green", id="green_at_100"),
        ],
    )
    def test_confidence_color(self, score, expected):
        assert confidence_color(score) == expected


# ---------------------------------------------------------------------------
# score_unified
# ---------------------------------------------------------------------------


class TestScoreUnified:
    def test_live_source_maps_70_95(self):
        result = score_unified(
            "live_api",
            vendor_score=80.0,
            is_authorized=True,
            has_price=True,
            has_qty=True,
            has_lead_time=True,
            has_condition=True,
            unit_price=10.0,
            median_price=10.0,
            qty_available=1000,
            target_qty=1000,
            age_hours=0.0,
        )
        assert 70 <= result["confidence_pct"] <= 95
        assert result["source_badge"] == "Live Stock"
        assert result["confidence_color"] == "green"
        assert "trust" in result["components"]

    def test_live_source_minimum_70(self):
        # Worst-case live: all penalties
        result = score_unified("nexar")
        assert result["confidence_pct"] >= 70

    def test_live_source_maximum_95(self):
        result = score_unified(
            "digikey",
            is_authorized=True,
            unit_price=5.0,
            median_price=10.0,
            qty_available=5000,
            target_qty=100,
            age_hours=0.0,
            has_price=True,
            has_qty=True,
            has_lead_time=True,
            has_condition=True,
        )
        assert result["confidence_pct"] <= 95

    def test_historical_base_80(self):
        result = score_unified("historical", age_hours=0.0)
        assert result["source_badge"] == "Historical"
        assert result["confidence_pct"] == 80
        assert result["components"]["base"] == 80.0

    def test_historical_age_decay(self):
        # 2 months old → decay = 5 * 2 = 10, base = 70
        result = score_unified("historical", age_hours=1440.0)
        assert result["score"] < 80.0
        assert result["components"]["age_decay"] > 0

    def test_historical_repeat_boost(self):
        result = score_unified("historical", age_hours=0.0, repeat_sighting_count=3)
        assert result["score"] == 86.0
        assert result["components"]["repeat_boost"] == 6.0

    def test_historical_repeat_boost_capped(self):
        result = score_unified("historical", age_hours=0.0, repeat_sighting_count=100)
        assert result["components"]["repeat_boost"] == 10.0

    def test_vendor_affinity(self):
        result = score_unified("vendor_affinity", claude_confidence=0.85)
        assert result["source_badge"] == "Vendor Match"
        assert result["confidence_pct"] == 85
        assert result["components"]["claude_confidence"] == 0.85

    def test_vendor_affinity_no_confidence(self):
        result = score_unified("vendor_affinity")
        assert result["confidence_pct"] == 0
        assert result["confidence_color"] == "red"

    def test_ai_live_web_capped_at_60(self):
        result = score_unified("ai_live_web", claude_confidence=0.95)
        assert result["confidence_pct"] <= 60
        assert result["source_badge"] == "AI Found"
        assert result["components"]["capped_at"] == 60

    def test_ai_live_web_below_cap(self):
        result = score_unified("ai_live_web", claude_confidence=0.40)
        assert result["confidence_pct"] == 40

    def test_unknown_source_fallback(self):
        # The code checks `st not in (...)` so unknown goes to live path
        # Actually, any source not in the three special types goes to live
        result = score_unified("some_random_source")
        assert result["source_badge"] == "Live Stock"

    def test_none_source_type(self):
        result = score_unified(None)
        assert result["source_badge"] == "Live Stock"

    def test_historical_very_old_clamped(self):
        # Very old data: base goes negative, clamped at 0
        result = score_unified("historical", age_hours=72000.0)
        assert result["score"] >= 0.0
        assert result["confidence_pct"] >= 0

    def test_result_keys(self):
        result = score_unified("live_api")
        assert set(result.keys()) == {
            "score",
            "source_badge",
            "confidence_pct",
            "confidence_color",
            "components",
        }
