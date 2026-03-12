"""
test_scoring_coverage.py — Tests for app/scoring.py

Covers:
- score_sighting: authorized=100, new vendor=baseline (35), known vendor=vendor_score
- score_sighting_v2: multi-factor scoring with penalized missing data
- classify_lead: strong/moderate/weak classification for buyer usefulness
- explain_lead: plain-English lead explanations
- is_weak_lead: filtering threshold for noise suppression
"""

from app.scoring import (
    MISSING_DATA_SCORE,
    NEW_VENDOR_BASELINE,
    WEAK_LEAD_THRESHOLD,
    classify_lead,
    explain_lead,
    is_weak_lead,
    score_sighting,
    score_sighting_v2,
)


class TestScoreSighting:
    def test_authorized_always_100(self):
        assert score_sighting(vendor_score=50.0, is_authorized=True) == 100.0

    def test_authorized_with_none_score(self):
        assert score_sighting(vendor_score=None, is_authorized=True) == 100.0

    def test_no_score_returns_baseline_not_zero(self):
        """New vendors get a baseline score so they appear in results, not buried at 0."""
        result = score_sighting(vendor_score=None, is_authorized=False)
        assert result == NEW_VENDOR_BASELINE
        assert result > 0, "New vendors must not score zero"

    def test_vendor_score_returned_rounded(self):
        """When vendor_score is set and not authorized, return rounded score."""
        assert score_sighting(vendor_score=85.678, is_authorized=False) == 85.7

    def test_zero_vendor_score(self):
        assert score_sighting(vendor_score=0.0, is_authorized=False) == 0.0

    def test_full_score(self):
        assert score_sighting(vendor_score=100.0, is_authorized=False) == 100.0

    def test_new_vendor_baseline_above_stale_historical(self):
        """Baseline must beat 90-day-old historical data (which scores ~30)."""
        assert NEW_VENDOR_BASELINE > 30


class TestScoreSightingV2:
    """Multi-factor scoring returns (total, components dict)."""

    def test_returns_tuple(self):
        total, components = score_sighting_v2(vendor_score=None, is_authorized=False)
        assert isinstance(total, float)
        assert isinstance(components, dict)
        assert set(components.keys()) == {"trust", "price", "qty", "freshness", "completeness"}

    def test_authorized_high_trust(self):
        total, comp = score_sighting_v2(vendor_score=None, is_authorized=True)
        assert comp["trust"] == 95.0

    def test_new_vendor_baseline_trust(self):
        _, comp = score_sighting_v2(vendor_score=None, is_authorized=False)
        assert comp["trust"] == NEW_VENDOR_BASELINE

    def test_known_vendor_trust(self):
        _, comp = score_sighting_v2(vendor_score=72.0, is_authorized=False)
        assert comp["trust"] == 72.0

    def test_price_below_median_scores_high(self):
        """Price half of median should score ~100."""
        _, comp = score_sighting_v2(
            vendor_score=50.0,
            is_authorized=False,
            unit_price=0.50,
            median_price=1.00,
        )
        assert comp["price"] == 100.0

    def test_price_at_median_scores_50(self):
        _, comp = score_sighting_v2(
            vendor_score=50.0,
            is_authorized=False,
            unit_price=1.00,
            median_price=1.00,
        )
        assert comp["price"] == 50.0

    def test_price_unknown_penalized(self):
        """Missing price is penalized, not neutral — buyers can't act without price."""
        _, comp = score_sighting_v2(vendor_score=50.0, is_authorized=False)
        assert comp["price"] == MISSING_DATA_SCORE
        assert comp["price"] < 50.0

    def test_qty_full_coverage(self):
        _, comp = score_sighting_v2(
            vendor_score=50.0,
            is_authorized=False,
            qty_available=1000,
            target_qty=1000,
        )
        assert comp["qty"] == 100.0

    def test_qty_half_coverage(self):
        _, comp = score_sighting_v2(
            vendor_score=50.0,
            is_authorized=False,
            qty_available=500,
            target_qty=1000,
        )
        assert comp["qty"] == 50.0

    def test_qty_available_no_target(self):
        """Has stock but no target qty — better than unknown, less than full coverage."""
        _, comp = score_sighting_v2(
            vendor_score=50.0,
            is_authorized=False,
            qty_available=100,
        )
        assert comp["qty"] == 60.0

    def test_qty_unknown_penalized(self):
        """Missing qty is penalized — buyers need to know stock levels."""
        _, comp = score_sighting_v2(vendor_score=50.0, is_authorized=False)
        assert comp["qty"] == MISSING_DATA_SCORE
        assert comp["qty"] < 50.0

    def test_freshness_zero_age(self):
        _, comp = score_sighting_v2(
            vendor_score=50.0,
            is_authorized=False,
            age_hours=0.0,
        )
        assert comp["freshness"] == 100.0

    def test_freshness_decays(self):
        _, comp = score_sighting_v2(
            vendor_score=50.0,
            is_authorized=False,
            age_hours=48.0,
        )
        assert comp["freshness"] < 100.0

    def test_freshness_unknown_penalized(self):
        """Missing freshness data is penalized."""
        _, comp = score_sighting_v2(vendor_score=50.0, is_authorized=False)
        assert comp["freshness"] == MISSING_DATA_SCORE

    def test_completeness_all_fields(self):
        _, comp = score_sighting_v2(
            vendor_score=50.0,
            is_authorized=False,
            has_price=True,
            has_qty=True,
            has_lead_time=True,
            has_condition=True,
        )
        assert comp["completeness"] == 100.0

    def test_completeness_no_fields(self):
        _, comp = score_sighting_v2(
            vendor_score=50.0,
            is_authorized=False,
            has_price=False,
            has_qty=False,
            has_lead_time=False,
            has_condition=False,
        )
        assert comp["completeness"] == 0.0

    def test_total_weighted_sum(self):
        """Verify total = weighted sum of components."""
        total, comp = score_sighting_v2(
            vendor_score=80.0,
            is_authorized=False,
            unit_price=1.0,
            median_price=1.0,
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
        assert abs(total - round(expected, 1)) < 0.2

    def test_junk_lead_scores_low(self):
        """A lead with no price, no qty, unknown vendor should score well below threshold."""
        total, _ = score_sighting_v2(
            vendor_score=None,
            is_authorized=False,
        )
        assert total < 30, f"Empty lead scored {total}, should be below 30"

    def test_strong_lead_scores_high(self):
        """Authorized distributor with all data should score near 100."""
        total, _ = score_sighting_v2(
            vendor_score=None,
            is_authorized=True,
            unit_price=1.0,
            median_price=1.5,
            qty_available=1000,
            target_qty=500,
            age_hours=0.0,
            has_price=True,
            has_qty=True,
            has_lead_time=True,
            has_condition=True,
        )
        assert total > 85, f"Strong lead scored {total}, should be above 85"

    def test_strong_lead_beats_junk_lead(self):
        """Strong leads must always outscore junk leads by a wide margin."""
        strong, _ = score_sighting_v2(
            vendor_score=80.0, is_authorized=False,
            unit_price=1.0, median_price=1.0,
            qty_available=1000, target_qty=1000,
            age_hours=0.0,
            has_price=True, has_qty=True, has_lead_time=True, has_condition=True,
        )
        junk, _ = score_sighting_v2(vendor_score=None, is_authorized=False)
        assert strong - junk > 30, "Strong leads must clearly separate from noise"


class TestClassifyLead:
    """Lead classification: strong/moderate/weak from buyer perspective."""

    def test_authorized_with_price_is_strong(self):
        assert classify_lead(score=80, is_authorized=True, has_price=True) == "strong"

    def test_authorized_no_price_not_auto_strong(self):
        """Even authorized, without price it needs score + fields to be strong."""
        result = classify_lead(score=80, is_authorized=True, has_price=False, has_qty=True, has_contact=True)
        assert result == "strong"

    def test_high_score_with_data_is_strong(self):
        assert classify_lead(score=60, has_price=True, has_qty=True) == "strong"

    def test_moderate_score_with_one_field(self):
        assert classify_lead(score=45, has_price=True) == "moderate"

    def test_t2_source_moderate_floor(self):
        """T2 direct API sources get a moderate floor if score is decent."""
        assert classify_lead(score=36, evidence_tier="T2") == "moderate"

    def test_low_score_no_data_is_weak(self):
        assert classify_lead(score=20) == "weak"

    def test_low_score_with_price_only(self):
        """Low score but has a price — still moderate because buyer can evaluate."""
        assert classify_lead(score=40, has_price=True) == "moderate"

    def test_zero_score_is_weak(self):
        assert classify_lead(score=0) == "weak"


class TestExplainLead:
    """Lead explanations: human-readable strings for buyers."""

    def test_authorized_distributor_label(self):
        result = explain_lead(vendor_name="DigiKey", is_authorized=True)
        assert "authorized distributor" in result.lower()

    def test_proven_vendor_label(self):
        result = explain_lead(vendor_name="Arrow", vendor_score=80.0)
        assert "proven" in result.lower()
        assert "80" in result

    def test_price_and_qty(self):
        result = explain_lead(
            vendor_name="TestVendor",
            unit_price=2.50,
            qty_available=1000,
        )
        assert "1,000" in result
        assert "$2.50" in result

    def test_qty_only(self):
        result = explain_lead(vendor_name="TestVendor", qty_available=500)
        assert "500" in result
        assert "no price" in result.lower()

    def test_price_only(self):
        result = explain_lead(vendor_name="TestVendor", unit_price=1.25)
        assert "$1.25" in result
        assert "qty unknown" in result.lower()

    def test_below_market_price(self):
        result = explain_lead(
            vendor_name="TestVendor",
            unit_price=0.80,
            median_price=1.00,
        )
        assert "below market" in result.lower()

    def test_above_market_price(self):
        result = explain_lead(
            vendor_name="TestVendor",
            unit_price=1.50,
            median_price=1.00,
        )
        assert "above market" in result.lower()

    def test_full_coverage(self):
        result = explain_lead(
            vendor_name="TestVendor",
            qty_available=1000,
            target_qty=500,
        )
        assert "full order qty" in result.lower()

    def test_partial_coverage(self):
        result = explain_lead(
            vendor_name="TestVendor",
            qty_available=300,
            target_qty=500,
        )
        assert "60%" in result

    def test_contact_info_noted(self):
        result = explain_lead(vendor_name="TestVendor", has_contact=True)
        assert "contact info available" in result.lower()

    def test_no_contact_noted(self):
        result = explain_lead(vendor_name="TestVendor", has_contact=False)
        assert "no contact info" in result.lower()

    def test_stale_data_flagged(self):
        result = explain_lead(vendor_name="TestVendor", age_days=45)
        assert "45 days old" in result

    def test_sub_dollar_price_format(self):
        """Prices under $1 use 4 decimal places for precision."""
        result = explain_lead(vendor_name="TestVendor", unit_price=0.0025, qty_available=5000)
        assert "$0.0025" in result


class TestIsWeakLead:
    """Weak lead filtering: noise suppression for buyers."""

    def test_authorized_never_weak(self):
        assert is_weak_lead(score=5, is_authorized=True) is False

    def test_t1_with_price_not_weak(self):
        assert is_weak_lead(score=20, has_price=True, evidence_tier="T1") is False

    def test_t2_with_qty_not_weak(self):
        assert is_weak_lead(score=15, has_qty=True, evidence_tier="T2") is False

    def test_low_score_no_data_is_weak(self):
        assert is_weak_lead(score=20) is True

    def test_below_threshold_no_data_marketplace(self):
        assert is_weak_lead(score=20, evidence_tier="T3") is True

    def test_above_threshold_not_weak(self):
        """Leads above threshold are not filtered even without price/qty."""
        assert is_weak_lead(score=35) is False

    def test_low_score_with_price_not_weak(self):
        """Has a price — buyer can evaluate, so don't filter."""
        assert is_weak_lead(score=20, has_price=True) is False

    def test_low_score_with_qty_not_weak(self):
        assert is_weak_lead(score=20, has_qty=True) is False

    def test_threshold_value(self):
        """Threshold should be 30 — allows decent leads through."""
        assert WEAK_LEAD_THRESHOLD == 30.0
