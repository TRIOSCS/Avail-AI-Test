"""tests/test_utils_normalization_helpers_ext.py — Tests for app/utils/normalization_helpers.py."""

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

    def test_empty_returns_none(self):
        assert normalize_phone_e164("") is None

    def test_ten_digit_us(self):
        assert normalize_phone_e164("5551234567") == "+15551234567"

    def test_parens_format(self):
        assert normalize_phone_e164("(555) 123-4567") == "+15551234567"

    def test_dashes_format(self):
        assert normalize_phone_e164("555-123-4567") == "+15551234567"

    def test_eleven_digit_with_1(self):
        assert normalize_phone_e164("15551234567") == "+15551234567"

    def test_international_with_plus(self):
        result = normalize_phone_e164("+442079460958")
        assert result == "+442079460958"

    def test_extension_stripped(self):
        result = normalize_phone_e164("555-123-4567 ext 99")
        assert result == "+15551234567"

    def test_extension_x_stripped(self):
        result = normalize_phone_e164("5551234567 x 100")
        assert result == "+15551234567"

    def test_too_short_returns_none(self):
        assert normalize_phone_e164("123") is None

    def test_international_11plus_digits(self):
        result = normalize_phone_e164("85298765432")
        assert result is not None
        assert result.startswith("+")


class TestNormalizeCountry:
    def test_none_returns_none(self):
        assert normalize_country(None) is None

    def test_empty_returns_none(self):
        assert normalize_country("") is None

    def test_us_full_name(self):
        assert normalize_country("United States") == "US"

    def test_usa_abbreviation(self):
        assert normalize_country("USA") == "US"

    def test_iso_code_passthrough(self):
        assert normalize_country("DE") == "DE"

    def test_lowercase_iso(self):
        assert normalize_country("de") == "DE"

    def test_full_country_name(self):
        assert normalize_country("Germany") == "DE"

    def test_uk(self):
        assert normalize_country("United Kingdom") == "GB"

    def test_uk_abbrev(self):
        assert normalize_country("UK") == "GB"

    def test_china(self):
        assert normalize_country("China") == "CN"

    def test_prc(self):
        assert normalize_country("PRC") == "CN"

    def test_unknown_returns_original(self):
        result = normalize_country("Wakanda")
        assert result == "Wakanda"

    def test_singapore(self):
        assert normalize_country("Singapore") == "SG"

    def test_hong_kong(self):
        assert normalize_country("Hong Kong") == "HK"


class TestNormalizeUsState:
    def test_none_returns_none(self):
        assert normalize_us_state(None) is None

    def test_empty_returns_none(self):
        assert normalize_us_state("") is None

    def test_full_name_california(self):
        assert normalize_us_state("California") == "CA"

    def test_full_name_new_york(self):
        assert normalize_us_state("New York") == "NY"

    def test_code_tx_passthrough(self):
        assert normalize_us_state("TX") == "TX"

    def test_lowercase_code(self):
        assert normalize_us_state("ca") == "CA"

    def test_dc(self):
        assert normalize_us_state("District of Columbia") == "DC"

    def test_unknown_returns_original(self):
        result = normalize_us_state("Narnia")
        assert result == "Narnia"

    def test_texas_full(self):
        assert normalize_us_state("Texas") == "TX"

    def test_florida_full(self):
        assert normalize_us_state("Florida") == "FL"


class TestFixEncoding:
    def test_none_returns_none(self):
        assert fix_encoding(None) is None

    def test_empty_returns_empty(self):
        assert fix_encoding("") == ""

    def test_clean_text_unchanged(self):
        assert fix_encoding("Hello World") == "Hello World"

    def test_intl_corruption_fixed(self):
        result = fix_encoding("int?l")
        assert result == "Int'l"

    def test_intl_uppercase_corruption_fixed(self):
        result = fix_encoding("INT?L")
        assert result == "Int'l"

    def test_mojibake_apostrophe(self):
        bad = "â"
        result = fix_encoding(f"don{bad}t")
        assert "'" in result

    def test_mojibake_em_dash(self):
        bad = "â"
        result = fix_encoding(f"a{bad}b")
        assert "—" in result
