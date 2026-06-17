"""Tests for opt-in Claude usage metering (cost_bucket) and the spend readout CLI.

Covers:
- ``_meter_usage`` writes the right per-(bucket, tier, metric, date) counters, skips
  zero-amount metrics, and never raises when the counter backend is down.
- ``claude_text`` meters only when ``cost_bucket`` is set (default None = no metering,
  so app/search/RFQ/email traffic is unaffected).
- ``app.management.enrichment_spend`` prices counters with the verified Anthropic rates.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.management import enrichment_spend
from app.utils import claude_client

SAMPLE_USAGE = {
    "input_tokens": 12000,
    "output_tokens": 600,
    "cache_read_input_tokens": 0,
    "cache_creation_input_tokens": 0,
    "server_tool_use": {"web_search_requests": 4},
}


def _by_metric(seen: dict[str, int]) -> dict[str, int]:
    """Map metered key -> amount keyed by the metric segment
    (claude_usage:bucket:tier:METRIC:date)."""
    out: dict[str, int] = {}
    for key, amt in seen.items():
        out[key.split(":")[3]] = amt
    return out


def test_meter_usage_writes_expected_counters(monkeypatch):
    seen: dict[str, int] = {}

    def fake_incr(key, amount=1, ttl_days=1.0):
        seen[key] = seen.get(key, 0) + amount
        return seen[key]

    monkeypatch.setattr("app.cache.intel_cache.incr_count", fake_incr)

    claude_client._meter_usage("enrichment", "smart", SAMPLE_USAGE)

    # All keys carry the bucket + tier namespace.
    assert all(k.startswith("claude_usage:enrichment:smart:") for k in seen)
    by_metric = _by_metric(seen)
    assert by_metric["calls"] == 1
    assert by_metric["input_tokens"] == 12000
    assert by_metric["output_tokens"] == 600
    assert by_metric["web_searches"] == 4
    # Zero-amount metrics are skipped (no empty keys created).
    assert "cache_read_tokens" not in by_metric
    assert "cache_write_tokens" not in by_metric


def test_meter_usage_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("redis + pg both down")

    monkeypatch.setattr("app.cache.intel_cache.incr_count", boom)
    # Must swallow the error — metering can never break a real Claude call.
    claude_client._meter_usage("enrichment", "smart", SAMPLE_USAGE)


def test_meter_usage_handles_missing_server_tool(monkeypatch):
    seen: dict[str, int] = {}
    monkeypatch.setattr(
        "app.cache.intel_cache.incr_count",
        lambda key, amount=1, ttl_days=1.0: seen.update({key: amount}) or amount,
    )
    claude_client._meter_usage("enrichment", "opus", {"input_tokens": 300, "output_tokens": 100})
    by_metric = _by_metric(seen)
    assert by_metric["input_tokens"] == 300
    assert "web_searches" not in by_metric  # no server_tool_use block -> 0 -> skipped


def _patch_http(monkeypatch, usage=SAMPLE_USAGE):
    monkeypatch.setattr(claude_client, "get_credential_cached", lambda *a, **k: "key")
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"content": [{"type": "text", "text": "hi"}], "usage": usage}
    monkeypatch.setattr(claude_client.http, "post", AsyncMock(return_value=resp))


async def test_cost_bucket_none_skips_metering(monkeypatch):
    _patch_http(monkeypatch)
    meter = MagicMock()
    monkeypatch.setattr(claude_client, "_meter_usage", meter)
    out = await claude_client.claude_text("prompt", model_tier="smart", cost_bucket=None)
    assert out == "hi"
    meter.assert_not_called()


async def test_cost_bucket_set_meters(monkeypatch):
    _patch_http(monkeypatch)
    meter = MagicMock()
    monkeypatch.setattr(claude_client, "_meter_usage", meter)
    out = await claude_client.claude_text("prompt", model_tier="smart", cost_bucket="enrichment")
    assert out == "hi"
    meter.assert_called_once_with("enrichment", "smart", SAMPLE_USAGE)


async def test_claude_json_threads_cost_bucket(monkeypatch):
    _patch_http(monkeypatch, usage=SAMPLE_USAGE)
    # claude_json delegates to claude_text; the bucket must flow through.
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"content": [{"type": "text", "text": '{"ok": true}'}], "usage": SAMPLE_USAGE}
    monkeypatch.setattr(claude_client.http, "post", AsyncMock(return_value=resp))
    meter = MagicMock()
    monkeypatch.setattr(claude_client, "_meter_usage", meter)
    out = await claude_client.claude_json("prompt", model_tier="smart", cost_bucket="enrichment")
    assert out == {"ok": True}
    meter.assert_called_once_with("enrichment", "smart", SAMPLE_USAGE)


def test_tier_cost_pricing():
    # 1M Sonnet input tokens = $3.00 exactly.
    c = {
        "input_tokens": 1_000_000,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "web_searches": 0,
    }
    assert enrichment_spend._tier_cost(c, 3.0, 15.0) == pytest.approx(3.0)
    # 100 web searches = $1.00 ($10/1000).
    c2 = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "web_searches": 100}
    assert enrichment_spend._tier_cost(c2, 3.0, 15.0) == pytest.approx(1.0)
    # cache_read bills at 0.1x input.
    c3 = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 1_000_000,
        "cache_write_tokens": 0,
        "web_searches": 0,
    }
    assert enrichment_spend._tier_cost(c3, 3.0, 15.0) == pytest.approx(0.3)


def test_collect_and_render(monkeypatch):
    store = {
        "claude_usage:enrichment:smart:calls:2026-06-17": 50,
        "claude_usage:enrichment:smart:input_tokens:2026-06-17": 600_000,
        "claude_usage:enrichment:smart:output_tokens:2026-06-17": 25_000,
        "claude_usage:enrichment:smart:web_searches:2026-06-17": 200,
    }
    monkeypatch.setattr("app.cache.intel_cache.get_count", lambda k: store.get(k, 0))
    by_tier = enrichment_spend.collect("enrichment", ["2026-06-17"])
    assert by_tier["smart"]["calls"] == 50
    assert by_tier["fast"]["calls"] == 0  # untouched tiers read as zero
    out = enrichment_spend.render("enrichment", ["2026-06-17"], by_tier)
    # smart cost = 0.6M*$3 + 0.025M*$15 + 200*$0.01 = 1.8 + 0.375 + 2.0 = $4.175
    assert "TOTAL" in out
    assert "calls=50" in out
    assert "$4.17" in out or "$4.18" in out


def test_render_empty_window(monkeypatch):
    monkeypatch.setattr("app.cache.intel_cache.get_count", lambda k: 0)
    by_tier = enrichment_spend.collect("enrichment", ["2026-06-17"])
    out = enrichment_spend.render("enrichment", ["2026-06-17"], by_tier)
    assert "no metered enrichment calls" in out
