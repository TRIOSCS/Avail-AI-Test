"""test_enrichment_spend.py — Tests for app/management/enrichment_spend.py.

Covers: _tier_cost (pure arithmetic), collect (counter accumulation),
        render (zero calls, single tier, multiple tiers), main (argparse paths).

Called by: pytest autodiscovery
Depends on: app.cache.intel_cache.get_count (mocked)
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _zero_counts():
    return {
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "web_searches": 0,
    }


def _counts(
    calls=10, input_tokens=1_000_000, output_tokens=500_000, cache_read_tokens=0, cache_write_tokens=0, web_searches=0
):
    return {
        "calls": calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "web_searches": web_searches,
    }


# ---------------------------------------------------------------------------
# _tier_cost
# ---------------------------------------------------------------------------


class TestTierCost:
    def setup_method(self):
        from app.management.enrichment_spend import _tier_cost

        self._tier_cost = _tier_cost

    def test_basic_arithmetic(self):
        c = _counts(
            input_tokens=1_000_000, output_tokens=1_000_000, cache_read_tokens=0, cache_write_tokens=0, web_searches=0
        )
        # fast tier: in=1.0, out=5.0
        # cost = (1M * 1.0 + 0 + 0 + 1M * 5.0) / 1M = 6.0
        result = self._tier_cost(c, in_rate=1.0, out_rate=5.0)
        assert abs(result - 6.0) < 1e-9

    def test_zero_tokens_returns_zero(self):
        c = _zero_counts()
        result = self._tier_cost(c, in_rate=3.0, out_rate=15.0)
        assert result == 0.0

    def test_cache_read_multiplier(self):
        # cache_read billed at 0.1x input rate
        c = _counts(input_tokens=0, output_tokens=0, cache_read_tokens=1_000_000, cache_write_tokens=0, web_searches=0)
        # cost = (0 + 1M * 1.0 * 0.1 + 0 + 0) / 1M = 0.1
        result = self._tier_cost(c, in_rate=1.0, out_rate=5.0)
        assert abs(result - 0.1) < 1e-9

    def test_cache_write_multiplier(self):
        # cache_write billed at 1.25x input rate
        c = _counts(input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=1_000_000, web_searches=0)
        # cost = (0 + 0 + 1M * 1.0 * 1.25 + 0) / 1M = 1.25
        result = self._tier_cost(c, in_rate=1.0, out_rate=5.0)
        assert abs(result - 1.25) < 1e-9

    def test_web_search_cost(self):
        # $0.01 per web search
        c = _zero_counts()
        c["web_searches"] = 100
        result = self._tier_cost(c, in_rate=1.0, out_rate=5.0)
        assert abs(result - 1.0) < 1e-9

    def test_combined_all_components(self):
        from app.management.enrichment_spend import _CACHE_READ_MULT, _CACHE_WRITE_MULT, _WEB_SEARCH_USD

        c = _counts(
            input_tokens=500_000,
            output_tokens=200_000,
            cache_read_tokens=100_000,
            cache_write_tokens=50_000,
            web_searches=5,
        )
        in_rate, out_rate = 3.0, 15.0
        expected = (
            500_000 * in_rate
            + 100_000 * in_rate * _CACHE_READ_MULT
            + 50_000 * in_rate * _CACHE_WRITE_MULT
            + 200_000 * out_rate
        ) / 1_000_000 + 5 * _WEB_SEARCH_USD
        result = self._tier_cost(c, in_rate=in_rate, out_rate=out_rate)
        assert abs(result - expected) < 1e-9


# ---------------------------------------------------------------------------
# collect
# ---------------------------------------------------------------------------


class TestCollect:
    def test_single_date_single_tier_accumulation(self):
        call_map = {}
        for m in ("calls", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "web_searches"):
            call_map[f"claude_usage:enrichment:fast:{m}:2026-06-18"] = 42 if m == "calls" else 1000

        def fake_get_count(key):
            return call_map.get(key, 0)

        with patch("app.management.enrichment_spend.intel_cache") as mock_cache:
            mock_cache.get_count.side_effect = fake_get_count
            from app.management.enrichment_spend import collect

            result = collect("enrichment", ["2026-06-18"])

        assert result["fast"]["calls"] == 42
        assert result["fast"]["input_tokens"] == 1000

    def test_multiple_dates_summed(self):
        def fake_get_count(key):
            if "calls" in key:
                return 10
            return 0

        with patch("app.management.enrichment_spend.intel_cache") as mock_cache:
            mock_cache.get_count.side_effect = fake_get_count
            from app.management.enrichment_spend import collect

            result = collect("enrichment", ["2026-06-18", "2026-06-17", "2026-06-16"])

        # 3 dates × 10 calls each = 30 for each tier
        assert result["fast"]["calls"] == 30
        assert result["smart"]["calls"] == 30
        assert result["opus"]["calls"] == 30

    def test_all_zeros_when_no_data(self):
        with patch("app.management.enrichment_spend.intel_cache") as mock_cache:
            mock_cache.get_count.return_value = 0
            from app.management.enrichment_spend import collect

            result = collect("enrichment", ["2026-06-18"])

        for tier in ("fast", "smart", "opus"):
            assert all(v == 0 for v in result[tier].values())

    def test_custom_bucket_used_in_key(self):
        seen_keys = []

        def fake_get_count(key):
            seen_keys.append(key)
            return 0

        with patch("app.management.enrichment_spend.intel_cache") as mock_cache:
            mock_cache.get_count.side_effect = fake_get_count
            from app.management.enrichment_spend import collect

            collect("my_bucket", ["2026-06-18"])

        assert any("my_bucket" in k for k in seen_keys)


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------


class TestRender:
    def setup_method(self):
        # Force reimport to avoid cached state from mocks above
        import importlib

        import app.management.enrichment_spend as mod

        importlib.reload(mod)
        self._render = mod.render

    def test_zero_calls_shows_idle_message(self):
        by_tier = {t: _zero_counts() for t in ("fast", "smart", "opus")}
        output = self._render("enrichment", ["2026-06-18"], by_tier)

        assert "no metered enrichment calls" in output.lower() or "idle" in output.lower()

    def test_single_tier_with_calls(self):
        by_tier = {
            "fast": _counts(calls=5, input_tokens=1_000_000, output_tokens=500_000),
            "smart": _zero_counts(),
            "opus": _zero_counts(),
        }
        output = self._render("enrichment", ["2026-06-18"], by_tier)

        assert "fast" in output
        assert "calls=5" in output
        assert "TOTAL" in output

    def test_multiple_tiers_with_calls(self):
        by_tier = {
            "fast": _counts(calls=10, input_tokens=1_000_000, output_tokens=200_000),
            "smart": _counts(calls=5, input_tokens=2_000_000, output_tokens=500_000),
            "opus": _zero_counts(),
        }
        output = self._render("enrichment", ["2026-06-18"], by_tier)

        assert "fast" in output
        assert "smart" in output
        assert "TOTAL" in output

    def test_single_date_span_label(self):
        by_tier = {t: _zero_counts() for t in ("fast", "smart", "opus")}
        output = self._render("enrichment", ["2026-06-18"], by_tier)
        assert "2026-06-18" in output

    def test_multi_date_span_label(self):
        by_tier = {t: _zero_counts() for t in ("fast", "smart", "opus")}
        dates = ["2026-06-18", "2026-06-17", "2026-06-16"]
        output = self._render("enrichment", dates, by_tier)
        # Multi-day range shows count
        assert "3d" in output

    def test_bucket_name_in_header(self):
        by_tier = {t: _zero_counts() for t in ("fast", "smart", "opus")}
        output = self._render("my_bucket", ["2026-06-18"], by_tier)
        assert "my_bucket" in output

    def test_per_call_cost_and_monthly_projection(self):
        by_tier = {
            "fast": _counts(calls=100, input_tokens=10_000_000, output_tokens=5_000_000),
            "smart": _zero_counts(),
            "opus": _zero_counts(),
        }
        output = self._render("enrichment", ["2026-06-18"], by_tier)
        # Should contain per-call and /day and /mo
        assert "/call" in output
        assert "/day" in output
        assert "/mo" in output


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


class TestMain:
    def test_main_today_default(self):
        with patch("app.management.enrichment_spend.intel_cache") as mock_cache:
            mock_cache.get_count.return_value = 0
            with patch("builtins.print") as mock_print:
                with patch("sys.argv", ["enrichment_spend"]):
                    import importlib

                    from app.management import enrichment_spend

                    importlib.reload(enrichment_spend)
                    enrichment_spend.main()

        mock_print.assert_called_once()
        output = mock_print.call_args[0][0]
        assert "enrichment" in output

    def test_main_explicit_date(self):
        with patch("app.management.enrichment_spend.intel_cache") as mock_cache:
            mock_cache.get_count.return_value = 5
            with patch("builtins.print") as mock_print:
                with patch("sys.argv", ["enrichment_spend", "--date", "2026-06-01"]):
                    import importlib

                    import app.management.enrichment_spend as mod

                    importlib.reload(mod)
                    mod.main()

        output = mock_print.call_args[0][0]
        assert "2026-06-01" in output

    def test_main_days_flag(self):
        with patch("app.management.enrichment_spend.intel_cache") as mock_cache:
            mock_cache.get_count.return_value = 0
            with patch("builtins.print") as mock_print:
                with patch("sys.argv", ["enrichment_spend", "--days", "7"]):
                    import importlib

                    import app.management.enrichment_spend as mod

                    importlib.reload(mod)
                    mod.main()

        output = mock_print.call_args[0][0]
        assert "7d" in output

    def test_main_custom_bucket(self):
        with patch("app.management.enrichment_spend.intel_cache") as mock_cache:
            mock_cache.get_count.return_value = 0
            with patch("builtins.print") as mock_print:
                with patch("sys.argv", ["enrichment_spend", "--bucket", "web"]):
                    import importlib

                    import app.management.enrichment_spend as mod

                    importlib.reload(mod)
                    mod.main()

        output = mock_print.call_args[0][0]
        assert "web" in output
