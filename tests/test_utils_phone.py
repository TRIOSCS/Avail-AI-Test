"""tests/test_utils_phone.py — Tests for app/utils/phone.py."""

import os

os.environ["TESTING"] = "1"

import pytest

from app.utils.phone import normalize_e164


class TestNormalizeE164:
    def test_none_returns_none(self):
        assert normalize_e164(None) is None

    def test_empty_string_returns_none(self):
        assert normalize_e164("") is None

    def test_whitespace_only_returns_none(self):
        assert normalize_e164("   ") is None

    def test_valid_us_10_digit(self):
        result = normalize_e164("4155551234")
        assert result == "+14155551234"

    def test_valid_us_with_parens(self):
        result = normalize_e164("(415) 555-1234")
        assert result == "+14155551234"

    def test_valid_us_with_dashes(self):
        result = normalize_e164("415-555-1234")
        assert result == "+14155551234"

    def test_valid_us_with_plus(self):
        result = normalize_e164("+14155551234")
        assert result == "+14155551234"

    def test_valid_uk_number(self):
        result = normalize_e164("+44 20 7946 0958")
        assert result is not None
        assert result.startswith("+44")

    def test_garbage_returns_none(self):
        assert normalize_e164("not a phone") is None

    def test_too_short_returns_none(self):
        assert normalize_e164("12345") is None

    def test_integer_input(self):
        result = normalize_e164(4155551234)
        assert result == "+14155551234"

    def test_strips_whitespace(self):
        result = normalize_e164("  4155551234  ")
        assert result == "+14155551234"

    def test_non_us_region(self):
        result = normalize_e164("07911 123456", default_region="GB")
        assert result is not None
        assert result.startswith("+44")

    def test_garbage_never_raises(self):
        # Should not raise for any input
        try:
            normalize_e164({"not": "a phone"})
        except Exception:
            pytest.fail("normalize_e164 raised unexpectedly")

    def test_none_never_raises(self):
        try:
            result = normalize_e164(None)
            assert result is None
        except Exception:
            pytest.fail("normalize_e164 raised for None")
