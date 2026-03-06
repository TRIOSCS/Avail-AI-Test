"""Tests for phone formatting utilities (E.164 and display).

Covers: US 10-digit, US 11-digit, international, extensions,
letters, empty strings, too-short numbers.
"""

import pytest

from app.utils.phone_utils import format_phone_display, format_phone_e164


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
