"""Tests for app/utils/normalization_helpers.py — phone, country, state, encoding.

Targets uncovered lines in normalize_phone_e164, normalize_country,
normalize_us_state, and fix_encoding.

Called by: pytest
Depends on: app/utils/normalization_helpers.py
"""

import os

os.environ["TESTING"] = "1"

from app.utils.normalization_helpers import (
    fix_encoding,
    normalize_country,
    normalize_phone_e164,
    normalize_us_state,
)


class TestNormalizePhoneE164:
    def test_none_returns_none(self):
        assert normalize_phone_e164(None) is None

    def test_empty_string_returns_none(self):
        assert normalize_phone_e164("") is None

    def test_whitespace_only_returns_none(self):
        assert normalize_phone_e164("   ") is None

    def test_too_short_returns_none(self):
        assert normalize_phone_e164("123") is None

    def test_nanp_10_digits(self):
        assert normalize_phone_e164("5551234567") == "+15551234567"

    def test_nanp_with_leading_1(self):
        assert normalize_phone_e164("15551234567") == "+15551234567"

    def test_formatted_nanp(self):
        assert normalize_phone_e164("(555) 123-4567") == "+15551234567"

    def test_with_plus_prefix(self):
        assert normalize_phone_e164("+442079460958") == "+442079460958"

    def test_uk_number_with_plus(self):
        assert normalize_phone_e164("+44 20 7946 0958") == "+442079460958"

    def test_11_plus_digits_without_plus(self):
        # 12 digits, not NANP (doesn't start with 1 followed by 10) → +{digits}
        assert normalize_phone_e164("442079460958") == "+442079460958"

    def test_7_to_9_digits_assumed_us_domestic(self):
        # 7 digits — assume US domestic
        result = normalize_phone_e164("5551234")
        assert result == "+15551234"

    def test_8_digit_number_assumed_us(self):
        result = normalize_phone_e164("55512345")
        assert result is not None
        assert result.startswith("+1")

    def test_extension_stripped(self):
        result = normalize_phone_e164("(555) 123-4567 ext. 100")
        assert result == "+15551234567"

    def test_extension_x_format_stripped(self):
        result = normalize_phone_e164("555-123-4567 x200")
        assert result == "+15551234567"

    def test_dashes_stripped(self):
        assert normalize_phone_e164("1-800-555-0100") == "+18005550100"


class TestNormalizeCountry:
    def test_none_returns_none(self):
        assert normalize_country(None) is None

    def test_empty_string_returns_none(self):
        assert normalize_country("") is None

    def test_whitespace_only_returns_none(self):
        assert normalize_country("   ") is None

    def test_full_name_united_states(self):
        assert normalize_country("United States") == "US"

    def test_abbreviation_usa(self):
        assert normalize_country("USA") == "US"

    def test_two_letter_code_passthrough(self):
        assert normalize_country("DE") == "DE"

    def test_germany_full_name(self):
        assert normalize_country("Germany") == "DE"

    def test_deutschland(self):
        assert normalize_country("Deutschland") == "DE"

    def test_uk_abbreviation(self):
        assert normalize_country("UK") == "GB"

    def test_japan(self):
        assert normalize_country("Japan") == "JP"

    def test_china(self):
        assert normalize_country("China") == "CN"

    def test_unknown_country_returned_unchanged(self):
        # Unknown country string — returned as-is (don't lose data)
        result = normalize_country("Atlantis")
        assert result == "Atlantis"

    def test_case_insensitive_lookup(self):
        assert normalize_country("united states") == "US"
        assert normalize_country("UNITED STATES") == "US"


class TestNormalizeUSState:
    def test_none_returns_none(self):
        assert normalize_us_state(None) is None

    def test_empty_string_returns_none(self):
        assert normalize_us_state("") is None

    def test_whitespace_only_returns_none(self):
        assert normalize_us_state("   ") is None

    def test_full_name_california(self):
        assert normalize_us_state("California") == "CA"

    def test_abbreviation_ca(self):
        assert normalize_us_state("CA") == "CA"

    def test_lowercase_abbreviation(self):
        assert normalize_us_state("ca") == "CA"

    def test_new_york_full(self):
        assert normalize_us_state("New York") == "NY"

    def test_texas_abbreviation(self):
        assert normalize_us_state("TX") == "TX"

    def test_dc_abbreviation(self):
        assert normalize_us_state("DC") == "DC"

    def test_district_of_columbia_full(self):
        assert normalize_us_state("District of Columbia") == "DC"

    def test_unknown_state_returned_unchanged(self):
        # Unknown state string — returned as-is
        result = normalize_us_state("Narnia")
        assert result == "Narnia"

    def test_territory_puerto_rico(self):
        assert normalize_us_state("Puerto Rico") == "PR"


class TestFixEncoding:
    def test_none_returns_none(self):
        assert fix_encoding(None) is None

    def test_empty_string_returned_unchanged(self):
        assert fix_encoding("") == ""

    def test_clean_text_unchanged(self):
        assert fix_encoding("Arrow Electronics") == "Arrow Electronics"

    def test_int_l_pattern(self):
        # "int?l" should become "Int'l"
        assert fix_encoding("int?l") == "Int'l"

    def test_int_l_uppercase(self):
        assert fix_encoding("INT?L") == "Int'l"

    def test_mojibake_right_single_quote(self):
        # â → '
        corrupted = "â"
        result = fix_encoding(f"It{corrupted}s available")
        assert "'" in result

    def test_mojibake_left_single_quote(self):
        corrupted = "â"
        result = fix_encoding(f"{corrupted}Hello")
        assert "'" in result

    def test_no_mojibake_unchanged(self):
        text = "Normal ASCII text with no issues"
        assert fix_encoding(text) == text
