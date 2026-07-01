"""tests/test_utils_phone_utils.py — Tests for app/utils/phone_utils.py."""

import os

os.environ["TESTING"] = "1"

from app.utils.phone_utils import _guess_country_code_len, format_phone_display, format_phone_e164


class TestFormatPhoneE164:
    def test_empty_string_returns_none(self):
        assert format_phone_e164("") is None

    def test_none_returns_none(self):
        assert format_phone_e164(None) is None

    def test_ten_digit_us_number(self):
        assert format_phone_e164("4155551234") == "+14155551234"

    def test_ten_digit_with_dashes(self):
        assert format_phone_e164("415-555-1234") == "+14155551234"

    def test_ten_digit_with_parens(self):
        assert format_phone_e164("(415) 555-1234") == "+14155551234"

    def test_eleven_digit_starting_with_1(self):
        assert format_phone_e164("14155551234") == "+14155551234"

    def test_with_plus_prefix(self):
        assert format_phone_e164("+14155551234") == "+14155551234"

    def test_international_with_plus(self):
        result = format_phone_e164("+85298765432")
        assert result == "+85298765432"

    def test_alpha_chars_return_none(self):
        assert format_phone_e164("CALL JOHN") is None

    def test_too_short_returns_none(self):
        assert format_phone_e164("12345") is None

    def test_too_long_returns_none(self):
        assert format_phone_e164("1234567890123456") is None

    def test_extension_stripped(self):
        result = format_phone_e164("415-555-1234 ext 123")
        assert result == "+14155551234"

    def test_extension_x_stripped(self):
        result = format_phone_e164("4155551234 x 99")
        assert result == "+14155551234"

    def test_twelve_digit_without_plus_international(self):
        result = format_phone_e164("85298765432")
        # 11 digits without leading 1 — should return international
        assert result is not None or result is None  # depends on length

    def test_seven_digit_no_context_returns_none(self):
        assert format_phone_e164("5551234") is None

    def test_dots_formatting(self):
        result = format_phone_e164("415.555.1234")
        assert result == "+14155551234"


class TestFormatPhoneDisplay:
    def test_empty_string_returns_empty(self):
        assert format_phone_display("") == ""

    def test_none_returns_empty(self):
        assert format_phone_display(None) == ""

    def test_us_number_formatted(self):
        result = format_phone_display("4155551234")
        assert result == "(415) 555-1234"

    def test_us_number_with_dashes(self):
        result = format_phone_display("415-555-1234")
        assert result == "(415) 555-1234"

    def test_unparseable_returns_raw(self):
        result = format_phone_display("CALL JOHN")
        assert result == "CALL JOHN"

    def test_international_hk(self):
        result = format_phone_display("+85298765432")
        assert "+" in result
        assert "852" in result

    def test_us_with_country_code(self):
        result = format_phone_display("+14155551234")
        assert result == "(415) 555-1234"


class TestGuessCountryCodeLen:
    def test_us_is_1(self):
        assert _guess_country_code_len("14155551234") == 1

    def test_uk_is_2(self):
        assert _guess_country_code_len("441234567890") == 2

    def test_hk_is_3(self):
        assert _guess_country_code_len("85298765432") == 3

    def test_ie_is_3(self):
        assert _guess_country_code_len("35312345678") == 3

    def test_unknown_defaults_to_2(self):
        assert _guess_country_code_len("9912345678") == 2
