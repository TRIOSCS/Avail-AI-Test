"""Tests for phone formatting utilities (E.164 and display).

Covers: US 10-digit, US 11-digit, international, extensions,
letters, empty strings, too-short numbers, 7-9 digit ambiguous,
12+ digit international, _guess_country_code_len branches.
"""

from app.utils.phone_utils import _guess_country_code_len, format_phone_display, format_phone_e164


class TestFormatPhoneE164:
    def test_us_10_digit(self):
        assert format_phone_e164("4155551234") == "+14155551234"

    def test_us_already_e164(self):
        assert format_phone_e164("+14155551234") == "+14155551234"

    def test_us_formatted(self):
        assert format_phone_e164("(415) 555-1234") == "+14155551234"

    def test_us_11_digit(self):
        assert format_phone_e164("14155551234") == "+14155551234"

    def test_international_hk(self):
        assert format_phone_e164("+852 9876 5432") == "+85298765432"

    def test_international_uk(self):
        assert format_phone_e164("+44 20 7946 0958") == "+442079460958"

    def test_too_short(self):
        assert format_phone_e164("12345") is None

    def test_letters(self):
        assert format_phone_e164("call john") is None

    def test_empty(self):
        assert format_phone_e164("") is None

    def test_none(self):
        assert format_phone_e164(None) is None

    def test_with_extension(self):
        assert format_phone_e164("(415) 555-1234 ext 100") == "+14155551234"

    def test_with_x_extension(self):
        assert format_phone_e164("4155551234 x200") == "+14155551234"

    def test_dashes_and_dots(self):
        assert format_phone_e164("415.555.1234") == "+14155551234"

    def test_spaces(self):
        assert format_phone_e164("415 555 1234") == "+14155551234"

    def test_mixed_formatting(self):
        assert format_phone_e164("  +1 (415) 555-1234  ") == "+14155551234"


class TestFormatPhoneDisplay:
    def test_us_raw(self):
        assert format_phone_display("4155551234") == "(415) 555-1234"

    def test_us_e164(self):
        assert format_phone_display("+14155551234") == "(415) 555-1234"

    def test_us_formatted_input(self):
        assert format_phone_display("(415) 555-1234") == "(415) 555-1234"

    def test_international_hk(self):
        result = format_phone_display("+852 9876 5432")
        assert result.startswith("+852")
        assert "9876" in result

    def test_unparseable_returns_raw(self):
        assert format_phone_display("12345") == "12345"

    def test_letters_returns_raw(self):
        assert format_phone_display("call john") == "call john"

    def test_empty(self):
        assert format_phone_display("") == ""

    def test_none(self):
        assert format_phone_display(None) == ""


class TestFormatPhoneE164EdgeCases:
    """Cover lines 51-58: 7-9 digits without + returns None, 12+ digits returns +digits."""

    def test_7_digits_no_plus_returns_none(self):
        assert format_phone_e164("5551234") is None

    def test_8_digits_no_plus_returns_none(self):
        assert format_phone_e164("55512345") is None

    def test_9_digits_no_plus_returns_none(self):
        assert format_phone_e164("555123456") is None

    def test_7_digits_with_plus_returns_e164(self):
        """7 digits WITH a + prefix should work (international short number)."""
        result = format_phone_e164("+5551234")
        assert result == "+5551234"

    def test_12_digits_no_plus_returns_international(self):
        result = format_phone_e164("861012345678")
        assert result == "+861012345678"

    def test_13_digits_no_plus_returns_international(self):
        result = format_phone_e164("8521234567890")
        assert result == "+8521234567890"

    def test_14_digits_no_plus_returns_international(self):
        result = format_phone_e164("44207946095800")
        assert result == "+44207946095800"

    def test_11_digits_not_starting_with_1_returns_none(self):
        """11 digits not starting with 1 — falls through to line 58 (return None)."""
        result = format_phone_e164("44207946095")
        assert result is None


class TestGuessCountryCodeLen:
    """Cover lines 99, 103, 109: the three branch returns in _guess_country_code_len."""

    def test_1_digit_us_canada(self):
        assert _guess_country_code_len("14155551234") == 1

    def test_2_digit_uk(self):
        assert _guess_country_code_len("442079460958") == 2

    def test_2_digit_germany(self):
        assert _guess_country_code_len("4930123456") == 2

    def test_2_digit_china(self):
        assert _guess_country_code_len("861012345678") == 2

    def test_2_digit_india(self):
        assert _guess_country_code_len("919876543210") == 2

    def test_2_digit_singapore(self):
        assert _guess_country_code_len("6598765432") == 2

    def test_3_digit_hong_kong(self):
        assert _guess_country_code_len("85298765432") == 3

    def test_3_digit_ireland(self):
        assert _guess_country_code_len("353861234567") == 3

    def test_3_digit_israel(self):
        assert _guess_country_code_len("972501234567") == 3

    def test_3_digit_uae(self):
        assert _guess_country_code_len("971501234567") == 3

    def test_default_2_digit_for_unknown_prefix(self):
        """Number not matching any known prefix -> default return 2."""
        assert _guess_country_code_len("7912345678") == 2

    def test_default_2_digit_nigeria(self):
        assert _guess_country_code_len("2341234567") == 2


class TestFormatPhoneDisplayInternational:
    """Integration: format_phone_display with various CC lengths."""

    def test_display_hong_kong(self):
        result = format_phone_display("+85298765432")
        assert result == "+852 9876 5432"

    def test_display_uk(self):
        result = format_phone_display("+442079460958")
        assert result == "+44 2079 4609 58"

    def test_display_unknown_country_code(self):
        # 12+ digits with + prefix to avoid US 10-digit path
        result = format_phone_display("+79123456789")
        # CC default 2 ("79"), rest="123456789" -> "1234 5678 9"
        assert result == "+79 1234 5678 9"
