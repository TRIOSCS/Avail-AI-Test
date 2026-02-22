"""
test_scoring_coverage.py — Tests for app/scoring.py

Covers uncovered line 14: score_sighting with vendor_score value.
"""

from app.scoring import score_sighting


class TestScoreSighting:
    def test_authorized_always_100(self):
        assert score_sighting(vendor_score=50.0, is_authorized=True) == 100.0

    def test_authorized_with_none_score(self):
        assert score_sighting(vendor_score=None, is_authorized=True) == 100.0

    def test_no_score_returns_zero(self):
        assert score_sighting(vendor_score=None, is_authorized=False) == 0.0

    def test_vendor_score_returned_rounded(self):
        """Line 14: when vendor_score is set and not authorized, return rounded score."""
        assert score_sighting(vendor_score=85.678, is_authorized=False) == 85.7

    def test_zero_vendor_score(self):
        assert score_sighting(vendor_score=0.0, is_authorized=False) == 0.0

    def test_full_score(self):
        assert score_sighting(vendor_score=100.0, is_authorized=False) == 100.0
