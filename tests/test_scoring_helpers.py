"""
test_scoring_helpers.py -- Tests for app/services/scoring_helpers.py and app/scoring.py

Covers month_range() with various edge cases and score_sighting_v2() range validation.

Called by: pytest
Depends on: app/services/scoring_helpers.py, app/scoring.py
"""

from datetime import UTC, date, datetime

import pytest

from app.scoring import score_sighting_v2
from app.services.scoring_helpers import month_range


class TestMonthRange:
    @pytest.mark.parametrize(
        ("input_date", "expected_start", "expected_end"),
        [
            pytest.param(
                date(2026, 3, 15),
                datetime(2026, 3, 1, tzinfo=UTC),
                datetime(2026, 4, 1, tzinfo=UTC),
                id="normal_month",
            ),
            pytest.param(
                date(2026, 12, 25),
                datetime(2026, 12, 1, tzinfo=UTC),
                datetime(2027, 1, 1, tzinfo=UTC),
                id="december_rolls_to_january",
            ),
            pytest.param(
                date(2026, 1, 10),
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2026, 2, 1, tzinfo=UTC),
                id="january",
            ),
            pytest.param(
                date(2026, 2, 28),
                datetime(2026, 2, 1, tzinfo=UTC),
                datetime(2026, 3, 1, tzinfo=UTC),
                id="february",
            ),
            pytest.param(
                date(2026, 6, 1),
                datetime(2026, 6, 1, tzinfo=UTC),
                datetime(2026, 7, 1, tzinfo=UTC),
                id="first_day_of_month",
            ),
        ],
    )
    def test_month_range(self, input_date, expected_start, expected_end):
        """month_range returns start/end of the month for various dates."""
        start, end = month_range(input_date)
        assert start == expected_start
        assert end == expected_end

    def test_returns_utc_aware(self):
        """Both datetimes are UTC-aware."""
        start, end = month_range(date(2026, 5, 15))
        assert start.tzinfo == UTC
        assert end.tzinfo == UTC


class TestScoreSightingV2Range:
    """Verify score_sighting_v2 returns 0-100, never multiplied by 100."""

    def test_score_range_with_full_data(self):
        """Score with all data present stays within 0-100."""
        score, components = score_sighting_v2(
            vendor_score=80.0,
            is_authorized=False,
            unit_price=1.50,
            median_price=2.00,
            qty_available=1000,
            target_qty=500,
            age_hours=12.0,
            has_price=True,
            has_qty=True,
            has_lead_time=True,
            has_condition=True,
        )
        assert 0 <= score <= 100, f"Score {score} outside 0-100 range"
        for name, val in components.items():
            assert 0 <= val <= 100, f"Component {name}={val} outside 0-100"

    def test_score_range_authorized_vendor(self):
        """Authorized vendor gets high score, still within 0-100."""
        score, components = score_sighting_v2(
            vendor_score=None,
            is_authorized=True,
            has_price=True,
            has_qty=True,
            has_lead_time=True,
            has_condition=True,
        )
        assert 0 <= score <= 100, f"Score {score} outside 0-100 range"
        assert score >= 50, "Authorized vendor should score well"

    def test_score_range_minimal_data(self):
        """Score with no optional data stays within 0-100."""
        score, components = score_sighting_v2(
            vendor_score=None,
            is_authorized=False,
        )
        assert 0 <= score <= 100, f"Score {score} outside 0-100 range"

    def test_score_not_multiplied_by_100(self):
        """Catch the bug where score * 100 produces values like 9350%.

        score_sighting_v2 already returns 0-100, so displaying as s.score|int (not
        s.score * 100) is correct.
        """
        score, _ = score_sighting_v2(
            vendor_score=93.5,
            is_authorized=False,
            unit_price=1.00,
            median_price=1.00,
            qty_available=100,
            target_qty=100,
            age_hours=1.0,
            has_price=True,
            has_qty=True,
            has_lead_time=True,
            has_condition=True,
        )
        # If someone did score * 100, a 93.5 vendor_score input could
        # produce a display like "9350%" — this must never happen
        assert score <= 100, f"Score {score} exceeds 100 — display bug!"
        assert score > 0
