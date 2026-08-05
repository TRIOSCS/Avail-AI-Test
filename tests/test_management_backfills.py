"""tests/test_management_backfills.py — Coverage for the enrichment_spend CLI.

Targets:
- app/management/enrichment_spend.py

(The backfill_quote_source / backfill_buyplan_cph one-shots were verified complete
and deleted in W2.9 — their tests went with them.)

Called by: pytest
Depends on: tests/conftest.py
"""

import os
import sys
from unittest.mock import patch

import pytest

os.environ["TESTING"] = "1"


class TestEnrichmentSpendTierCost:
    def test_all_zero_counters_returns_zero(self):
        from app.management.enrichment_spend import _tier_cost

        c = {m: 0 for m in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "web_searches")}
        assert _tier_cost(c, 3.0, 15.0) == 0.0

    def test_input_tokens_billed_at_input_rate(self):
        from app.management.enrichment_spend import _tier_cost

        c = {
            "input_tokens": 1_000_000,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "web_searches": 0,
        }
        assert _tier_cost(c, 3.0, 15.0) == pytest.approx(3.0)

    def test_output_tokens_billed_at_output_rate(self):
        from app.management.enrichment_spend import _tier_cost

        c = {
            "input_tokens": 0,
            "output_tokens": 1_000_000,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "web_searches": 0,
        }
        assert _tier_cost(c, 3.0, 15.0) == pytest.approx(15.0)

    def test_web_searches_billed_at_fixed_rate(self):
        from app.management.enrichment_spend import _tier_cost

        c = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "web_searches": 100,
        }
        assert _tier_cost(c, 3.0, 15.0) == pytest.approx(1.0)

    def test_cache_read_billed_at_tenth_input_rate(self):
        from app.management.enrichment_spend import _tier_cost

        c = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 1_000_000,
            "cache_write_tokens": 0,
            "web_searches": 0,
        }
        assert _tier_cost(c, 3.0, 15.0) == pytest.approx(0.3)

    def test_cache_write_billed_at_1_25x_input_rate(self):
        from app.management.enrichment_spend import _tier_cost

        c = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 1_000_000,
            "web_searches": 0,
        }
        assert _tier_cost(c, 3.0, 15.0) == pytest.approx(3.75)


class TestEnrichmentSpendCollect:
    def test_collect_sums_across_dates(self):
        from app.management.enrichment_spend import collect

        call_log = {}

        def fake_get_count(key):
            call_log[key] = call_log.get(key, 0) + 1
            if "fast:calls" in key:
                return 5
            return 0

        with patch("app.management.enrichment_spend.intel_cache") as mock_cache:
            mock_cache.get_count.side_effect = fake_get_count
            result = collect("enrichment", ["2026-06-17", "2026-06-18"])

        assert result["fast"]["calls"] == 10  # 5 per date × 2 dates
        assert result["smart"]["calls"] == 0

    def test_collect_returns_all_tiers(self):
        from app.management.enrichment_spend import collect

        with patch("app.management.enrichment_spend.intel_cache") as mock_cache:
            mock_cache.get_count.return_value = 0
            result = collect("enrichment", ["2026-06-17"])

        assert set(result.keys()) == {"fast", "smart", "opus"}


class TestEnrichmentSpendRender:
    def test_render_no_calls_shows_idle_message(self):
        from app.management.enrichment_spend import render

        by_tier = {
            t: {
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "web_searches": 0,
            }
            for t in ("fast", "smart", "opus")
        }
        output = render("enrichment", ["2026-06-17"], by_tier)
        assert "no metered enrichment calls" in output

    def test_render_with_calls_shows_cost_breakdown(self):
        from app.management.enrichment_spend import render

        by_tier = {
            "fast": {
                "calls": 10,
                "input_tokens": 1_000_000,
                "output_tokens": 100_000,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "web_searches": 0,
            },
            "smart": {
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "web_searches": 0,
            },
            "opus": {
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "web_searches": 0,
            },
        }
        output = render("enrichment", ["2026-06-17"], by_tier)
        assert "fast" in output
        assert "TOTAL" in output
        assert "$" in output

    def test_render_multi_date_span_label(self):
        from app.management.enrichment_spend import render

        by_tier = {
            t: {
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "web_searches": 0,
            }
            for t in ("fast", "smart", "opus")
        }
        output = render("enrichment", ["2026-06-18", "2026-06-17"], by_tier)
        assert "2d" in output


class TestEnrichmentSpendMain:
    def test_main_default_args_prints_today(self, capsys):
        from app.management.enrichment_spend import main

        with patch("app.management.enrichment_spend.intel_cache") as mock_cache:
            mock_cache.get_count.return_value = 0
            with patch.object(sys, "argv", ["enrichment_spend"]):
                main()

        captured = capsys.readouterr()
        assert "Measured Claude spend" in captured.out

    def test_main_with_date_arg(self, capsys):
        from app.management.enrichment_spend import main

        with patch("app.management.enrichment_spend.intel_cache") as mock_cache:
            mock_cache.get_count.return_value = 0
            with patch.object(sys, "argv", ["enrichment_spend", "--date", "2026-06-17"]):
                main()

        captured = capsys.readouterr()
        assert "2026-06-17" in captured.out

    def test_main_with_days_arg(self, capsys):
        from app.management.enrichment_spend import main

        with patch("app.management.enrichment_spend.intel_cache") as mock_cache:
            mock_cache.get_count.return_value = 0
            with patch.object(sys, "argv", ["enrichment_spend", "--days", "7"]):
                main()

        captured = capsys.readouterr()
        assert "7d" in captured.out

    def test_main_block_via_runpy(self, capsys):
        import runpy

        with patch("app.management.enrichment_spend.intel_cache") as mock_cache:
            mock_cache.get_count.return_value = 0
            with patch.object(sys, "argv", ["enrichment_spend"]):
                sys.modules.pop("app.management.enrichment_spend", None)
                runpy.run_module("app.management.enrichment_spend", run_name="__main__")

        captured = capsys.readouterr()
        assert "Measured Claude spend" in captured.out
