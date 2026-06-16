"""Tests for phone formatting utilities (E.164 and display).

Covers: US 10-digit, US 11-digit, international, extensions,
letters, empty strings, too-short numbers, 7-9 digit ambiguous,
12+ digit international, _guess_country_code_len branches.
"""

import pytest

from app.utils.phone_utils import _guess_country_code_len, format_phone_display, format_phone_e164


class TestFormatPhoneE164:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("4155551234", "+14155551234"),
            ("+14155551234", "+14155551234"),
            ("(415) 555-1234", "+14155551234"),
            ("14155551234", "+14155551234"),
            ("+852 9876 5432", "+85298765432"),
            ("+44 20 7946 0958", "+442079460958"),
            ("12345", None),
            ("call john", None),
            ("", None),
            (None, None),
            ("(415) 555-1234 ext 100", "+14155551234"),
            ("4155551234 x200", "+14155551234"),
            ("415.555.1234", "+14155551234"),
            ("415 555 1234", "+14155551234"),
            ("  +1 (415) 555-1234  ", "+14155551234"),
        ],
        ids=[
            "us_10_digit",
            "us_already_e164",
            "us_formatted",
            "us_11_digit",
            "international_hk",
            "international_uk",
            "too_short",
            "letters",
            "empty",
            "none",
            "with_extension",
            "with_x_extension",
            "dashes_and_dots",
            "spaces",
            "mixed_formatting",
        ],
    )
    def test_format_phone_e164(self, raw, expected):
        assert format_phone_e164(raw) == expected


class TestFormatPhoneDisplay:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("4155551234", "(415) 555-1234"),
            ("+14155551234", "(415) 555-1234"),
            ("(415) 555-1234", "(415) 555-1234"),
            ("12345", "12345"),
            ("call john", "call john"),
            ("", ""),
            (None, ""),
        ],
        ids=[
            "us_raw",
            "us_e164",
            "us_formatted_input",
            "unparseable_returns_raw",
            "letters_returns_raw",
            "empty",
            "none",
        ],
    )
    def test_format_phone_display(self, raw, expected):
        assert format_phone_display(raw) == expected

    def test_international_hk(self):
        result = format_phone_display("+852 9876 5432")
        assert result.startswith("+852")
        assert "9876" in result


class TestFormatPhoneE164EdgeCases:
    """Cover lines 51-58: 7-9 digits without + returns None, 12+ digits returns +digits."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("5551234", None),
            ("55512345", None),
            ("555123456", None),
            # 7 digits WITH a + prefix should work (international short number).
            ("+5551234", "+5551234"),
            ("861012345678", "+861012345678"),
            ("8521234567890", "+8521234567890"),
            ("44207946095800", "+44207946095800"),
            # 11 digits not starting with 1 — falls through to line 58 (return None).
            ("44207946095", None),
        ],
        ids=[
            "7_digits_no_plus_returns_none",
            "8_digits_no_plus_returns_none",
            "9_digits_no_plus_returns_none",
            "7_digits_with_plus_returns_e164",
            "12_digits_no_plus_returns_international",
            "13_digits_no_plus_returns_international",
            "14_digits_no_plus_returns_international",
            "11_digits_not_starting_with_1_returns_none",
        ],
    )
    def test_format_phone_e164_edge_cases(self, raw, expected):
        assert format_phone_e164(raw) == expected

    def test_more_than_15_digits_returns_none(self):
        """E.164 caps at 15 digits — longer strings are not phone numbers (and would
        overflow the String(100) phone snapshot columns downstream)."""
        assert format_phone_e164("1234567890123456") is None
        assert format_phone_e164("+" + "9" * 30) is None


class TestGuessCountryCodeLen:
    """Cover lines 99, 103, 109: the three branch returns in _guess_country_code_len."""

    @pytest.mark.parametrize(
        ("digits", "expected"),
        [
            ("14155551234", 1),
            ("442079460958", 2),
            ("4930123456", 2),
            ("861012345678", 2),
            ("919876543210", 2),
            ("6598765432", 2),
            ("85298765432", 3),
            ("353861234567", 3),
            ("972501234567", 3),
            ("971501234567", 3),
            # Number not matching any known prefix -> default return 2.
            ("7912345678", 2),
            ("2341234567", 2),
        ],
        ids=[
            "1_digit_us_canada",
            "2_digit_uk",
            "2_digit_germany",
            "2_digit_china",
            "2_digit_india",
            "2_digit_singapore",
            "3_digit_hong_kong",
            "3_digit_ireland",
            "3_digit_israel",
            "3_digit_uae",
            "default_2_digit_for_unknown_prefix",
            "default_2_digit_nigeria",
        ],
    )
    def test_guess_country_code_len(self, digits, expected):
        assert _guess_country_code_len(digits) == expected


class TestFormatPhoneDisplayInternational:
    """Integration: format_phone_display with various CC lengths."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("+85298765432", "+852 9876 5432"),
            ("+442079460958", "+44 2079 4609 58"),
            # 12+ digits with + prefix to avoid US 10-digit path.
            # CC default 2 ("79"), rest="123456789" -> "1234 5678 9"
            ("+79123456789", "+79 1234 5678 9"),
        ],
        ids=["hong_kong", "uk", "unknown_country_code"],
    )
    def test_format_phone_display_international(self, raw, expected):
        assert format_phone_display(raw) == expected
