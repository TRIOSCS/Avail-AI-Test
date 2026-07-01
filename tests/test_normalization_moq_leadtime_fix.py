"""Regression tests for normalize_moq trailing qualifiers and compact-day lead times.

What it does: pins the two correctness fixes in app/utils/normalization.py —
(1) normalize_moq must parse MOQ strings whose numeric token is followed by a
trailing qualifier word ("10K minimum", "500 pcs"); (2) normalize_lead_time must
read compact day shorthand ("30d", "5d") as days, not weeks.
Called by: pytest.
Depends on: app.utils.normalization.
"""

import pytest

from app.utils.normalization import normalize_lead_time, normalize_moq


class TestNormalizeMoqTrailingQualifier:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            # The documented example that previously returned None because
            # "minimum" ends with "m" and tripped the 1e6 branch.
            pytest.param("10K minimum", 10000, id="k_trailing_minimum"),
            pytest.param("500 pcs", 500, id="trailing_pcs"),
            pytest.param("1000 units", 1000, id="trailing_units"),
            pytest.param("250 each", 250, id="trailing_each"),
            pytest.param("2K min", 2000, id="k_trailing_min"),
            # Existing behaviour must still hold.
            pytest.param("10K", 10000, id="k_suffix_plain"),
            pytest.param("MOQ: 500", 500, id="moq_prefix"),
            pytest.param("Minimum 100", 100, id="min_prefix"),
            pytest.param(500, 500, id="plain_int"),
        ],
    )
    def test_normalize_moq(self, value, expected):
        assert normalize_moq(value) == expected


class TestNormalizeLeadTimeCompactDays:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param("30d", 30, id="compact_30d"),
            pytest.param("5d", 5, id="compact_5d"),
            pytest.param("14 d", 14, id="spaced_d"),
            # Existing behaviour must still hold.
            pytest.param("30 days", 30, id="days_word"),
            pytest.param("6", 42, id="ambiguous_small_weeks"),
            pytest.param("4-6 weeks", 35, id="weeks_range"),
            pytest.param("90", 90, id="ambiguous_large_days"),
        ],
    )
    def test_normalize_lead_time(self, value, expected):
        assert normalize_lead_time(value) == expected
