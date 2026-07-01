"""Tests for app/utils/normalization_helpers.py — phone, country, state, encoding.

Targets uncovered lines in normalize_phone_e164, normalize_country,
normalize_us_state, and fix_encoding.

Called by: pytest
Depends on: app/utils/normalization_helpers.py
"""

import os

os.environ["TESTING"] = "1"

import pytest

from app.utils.normalization_helpers import (
    fix_encoding,
    normalize_country,
    normalize_phone_e164,
    normalize_us_state,
)


class TestNormalizePhoneE164:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (None, None),
            ("", None),
            ("   ", None),  # whitespace only
            ("123", None),  # too short
            ("5551234567", "+15551234567"),  # NANP 10 digits
            ("15551234567", "+15551234567"),  # NANP with leading 1
            ("(555) 123-4567", "+15551234567"),  # formatted NANP
            ("+442079460958", "+442079460958"),  # with plus prefix
            ("+44 20 7946 0958", "+442079460958"),  # UK number with plus
            # 12 digits, not NANP (doesn't start with 1 followed by 10) -> +{digits}
            ("442079460958", "+442079460958"),
            ("5551234", None),  # 7 digits — invalid E.164 partial, rejected (canonical normalizer)
            ("(555) 123-4567 ext. 100", "+15551234567"),  # extension stripped
            ("555-123-4567 x200", "+15551234567"),  # extension x format stripped
            ("1-800-555-0100", "+18005550100"),  # dashes stripped
        ],
    )
    def test_normalize(self, raw, expected):
        assert normalize_phone_e164(raw) == expected

    def test_8_digit_partial_rejected(self):
        # An 8-digit number is an invalid E.164 partial (US needs 10) — the canonical
        # normalizer rejects it rather than emitting a garbage "+155512345".
        assert normalize_phone_e164("55512345") is None


class TestNormalizeCountry:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (None, None),
            ("", None),
            ("   ", None),  # whitespace only
            ("United States", "US"),  # full name
            ("USA", "US"),  # abbreviation
            ("DE", "DE"),  # two-letter code passthrough
            ("Germany", "DE"),  # full name
            ("Deutschland", "DE"),
            ("UK", "GB"),  # abbreviation
            ("Japan", "JP"),
            ("China", "CN"),
            ("Atlantis", "Atlantis"),  # unknown — returned as-is (don't lose data)
            ("united states", "US"),  # case-insensitive lookup
            ("UNITED STATES", "US"),  # case-insensitive lookup
        ],
    )
    def test_normalize(self, raw, expected):
        assert normalize_country(raw) == expected


class TestNormalizeUSState:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (None, None),
            ("", None),
            ("   ", None),  # whitespace only
            ("California", "CA"),  # full name
            ("CA", "CA"),  # abbreviation
            ("ca", "CA"),  # lowercase abbreviation
            ("New York", "NY"),  # full name
            ("TX", "TX"),  # abbreviation
            ("DC", "DC"),  # abbreviation
            ("District of Columbia", "DC"),  # full name
            ("Narnia", "Narnia"),  # unknown — returned as-is
            ("Puerto Rico", "PR"),  # territory
        ],
    )
    def test_normalize(self, raw, expected):
        assert normalize_us_state(raw) == expected


class TestFixEncoding:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (None, None),
            ("", ""),  # empty string returned unchanged
            ("Arrow Electronics", "Arrow Electronics"),  # clean text unchanged
            ("int?l", "Int'l"),  # "int?l" should become "Int'l"
            ("INT?L", "Int'l"),  # uppercase
            ("Normal ASCII text with no issues", "Normal ASCII text with no issues"),  # no mojibake
        ],
    )
    def test_normalize(self, raw, expected):
        assert fix_encoding(raw) == expected

    def test_mojibake_right_single_quote(self):
        # mojibake -> '
        corrupted = "â"
        result = fix_encoding(f"It{corrupted}s available")
        assert "'" in result

    def test_mojibake_left_single_quote(self):
        corrupted = "â"
        result = fix_encoding(f"{corrupted}Hello")
        assert "'" in result
