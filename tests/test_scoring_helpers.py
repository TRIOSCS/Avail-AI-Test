"""
test_scoring_helpers.py -- Tests for app/services/scoring_helpers.py

Covers month_range() with various edge cases.

Called by: pytest
Depends on: app/services/scoring_helpers.py
"""

from datetime import date, datetime, timezone

from app.services.scoring_helpers import month_range


class TestMonthRange:
    def test_normal_month(self):
        """Normal month returns start/end of month."""
        start, end = month_range(date(2026, 3, 15))
        assert start == datetime(2026, 3, 1, tzinfo=timezone.utc)
        assert end == datetime(2026, 4, 1, tzinfo=timezone.utc)

    def test_december_rolls_to_january(self):
        """December rolls over to January of next year."""
        start, end = month_range(date(2026, 12, 25))
        assert start == datetime(2026, 12, 1, tzinfo=timezone.utc)
        assert end == datetime(2027, 1, 1, tzinfo=timezone.utc)

    def test_january(self):
        """January returns Jan 1 to Feb 1."""
        start, end = month_range(date(2026, 1, 10))
        assert start == datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert end == datetime(2026, 2, 1, tzinfo=timezone.utc)

    def test_february(self):
        """February returns Feb 1 to Mar 1."""
        start, end = month_range(date(2026, 2, 28))
        assert start == datetime(2026, 2, 1, tzinfo=timezone.utc)
        assert end == datetime(2026, 3, 1, tzinfo=timezone.utc)

    def test_first_day_of_month(self):
        """First day of month should still work."""
        start, end = month_range(date(2026, 6, 1))
        assert start == datetime(2026, 6, 1, tzinfo=timezone.utc)
        assert end == datetime(2026, 7, 1, tzinfo=timezone.utc)

    def test_returns_utc_aware(self):
        """Both datetimes are UTC-aware."""
        start, end = month_range(date(2026, 5, 15))
        assert start.tzinfo == timezone.utc
        assert end.tzinfo == timezone.utc
