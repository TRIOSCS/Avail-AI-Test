"""tests/test_currency.py — Tests for app/utils/currency.py (to_usd, FX_TO_USD).

Called by: pytest
Depends on: app.utils.currency
"""

import pytest

from app.utils.currency import FX_TO_USD, to_usd


class TestToUsd:
    def test_amount_none_returns_none(self):
        assert to_usd(None, "EUR") is None

    def test_amount_none_ignores_currency(self):
        assert to_usd(None, None) is None

    @pytest.mark.parametrize("currency", [None, "", "  "])
    def test_missing_currency_assumes_usd(self, currency):
        """Unknown/blank currency assumes the amount is already USD (prior behavior
        before FX conversion existed)."""
        assert to_usd(10.0, currency) == 10.0

    def test_usd_currency_is_unchanged(self):
        assert to_usd(10.0, "USD") == 10.0

    def test_unrecognized_currency_assumes_usd(self):
        """A currency code not in FX_TO_USD degrades gracefully to a no-op conversion
        rather than raising or dropping the price."""
        assert to_usd(10.0, "XYZ") == 10.0

    def test_lowercase_currency_is_normalized(self):
        assert to_usd(100.0, "eur") == pytest.approx(100.0 * FX_TO_USD["EUR"])

    def test_whitespace_currency_is_stripped(self):
        assert to_usd(100.0, "  jpy  ") == pytest.approx(100.0 * FX_TO_USD["JPY"])

    def test_eur_conversion(self):
        assert to_usd(100.0, "EUR") == pytest.approx(100.0 * FX_TO_USD["EUR"])

    def test_jpy_conversion_is_much_smaller(self):
        """100 JPY should convert to well under 1 USD (sanity check the rate table isn't
        accidentally inverted)."""
        result = to_usd(100.0, "JPY")
        assert result is not None
        assert 0 < result < 1.0

    def test_zero_amount(self):
        assert to_usd(0.0, "EUR") == 0.0

    def test_every_fx_rate_is_positive(self):
        """A zero or negative rate would corrupt every price comparison that uses it."""
        for currency, rate in FX_TO_USD.items():
            assert rate > 0, f"{currency} has a non-positive rate"

    def test_usd_rate_is_exactly_one(self):
        assert FX_TO_USD["USD"] == 1.0
