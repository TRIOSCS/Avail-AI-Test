"""
test_scoring_coverage.py — Tests for app/scoring.py

Covers:
- score_sighting: authorized=100, new vendor=baseline (35), known vendor=vendor_score
- score_sighting_v2: multi-factor scoring with trust, price, qty, freshness, completeness
"""

from app.scoring import NEW_VENDOR_BASELINE, score_sighting, score_sighting_v2


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

    def test_price_unknown_neutral(self):
        _, comp = score_sighting_v2(vendor_score=50.0, is_authorized=False)
        assert comp["price"] == 50.0

    def test_qty_full_coverage(self):
        _, comp = score_sighting_v2(
            vendor_score=50.0, is_authorized=False,
            qty_available=1000, target_qty=1000,
        )
        assert comp["qty"] == 100.0

    def test_qty_half_coverage(self):
        _, comp = score_sighting_v2(
            vendor_score=50.0, is_authorized=False,
            qty_available=500, target_qty=1000,
        )
        assert comp["qty"] == 50.0

    def test_freshness_zero_age(self):
        _, comp = score_sighting_v2(
            vendor_score=50.0, is_authorized=False, age_hours=0.0,
        )
        assert comp["freshness"] == 100.0

    def test_freshness_decays(self):
        _, comp = score_sighting_v2(
            vendor_score=50.0, is_authorized=False, age_hours=48.0,
        )
        assert comp["freshness"] < 100.0

    def test_completeness_all_fields(self):
        _, comp = score_sighting_v2(
            vendor_score=50.0, is_authorized=False,
            has_price=True, has_qty=True, has_lead_time=True, has_condition=True,
        )
        assert comp["completeness"] == 100.0

    def test_completeness_no_fields(self):
        _, comp = score_sighting_v2(
            vendor_score=50.0, is_authorized=False,
            has_price=False, has_qty=False, has_lead_time=False, has_condition=False,
        )
        assert comp["completeness"] == 0.0

    def test_total_weighted_sum(self):
        """Verify total = weighted sum of components."""
        total, comp = score_sighting_v2(
            vendor_score=80.0, is_authorized=False,
            unit_price=1.0, median_price=1.0,
            qty_available=1000, target_qty=1000,
            age_hours=0.0,
            has_price=True, has_qty=True, has_lead_time=True, has_condition=True,
        )
        expected = (
            comp["trust"] * 0.30
            + comp["price"] * 0.25
            + comp["qty"] * 0.20
            + comp["freshness"] * 0.15
            + comp["completeness"] * 0.10
        )
        assert abs(total - round(expected, 1)) < 0.2
