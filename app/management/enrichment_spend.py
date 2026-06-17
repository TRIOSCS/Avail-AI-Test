"""CLI: report MEASURED Claude spend for the enrichment cost bucket.

Reads the per-(tier, metric) date-counters that ``claude_client._meter_usage`` writes
whenever an enrichment Claude call passes ``cost_bucket="enrichment"`` (OEM resolve,
per-card web tier, spec extraction, Opus inference), and prices them with current
Anthropic rates to print real $/call and $/day — turning the previously-estimated
enrichment spend into a measured number.

Run:
    python -m app.management.enrichment_spend                # today (UTC)
    python -m app.management.enrichment_spend --date 2026-06-18
    python -m app.management.enrichment_spend --days 7       # sum of the last 7 days

Called by: humans (ops readout). Depends on: app.cache.intel_cache (the same Redis/PG
date-counters as the worker's web_calls/oem_resolves), app.utils.claude_client.MODELS.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from app.cache import intel_cache

# Per-1M-token (input, output) USD rates, verified from the live Anthropic pricing page
# 2026-06-17. cache_read bills at 0.1x input, cache_write at 1.25x input (5-min TTL).
_TIER_RATES: dict[str, tuple[float, float]] = {
    "fast": (1.0, 5.0),  # claude-haiku-4-5
    "smart": (3.0, 15.0),  # claude-sonnet-4-6
    "opus": (5.0, 25.0),  # claude-opus-4-8
}
_WEB_SEARCH_USD = 0.01  # $10 per 1,000 searches
_CACHE_READ_MULT = 0.1
_CACHE_WRITE_MULT = 1.25
_METRICS = ("calls", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "web_searches")


def _tier_cost(c: dict[str, int], in_rate: float, out_rate: float) -> float:
    """USD for one tier's day-counters.

    ``input_tokens`` is the uncached remainder;
    cached reads/writes are billed separately off the input rate.
    """
    return (
        c["input_tokens"] * in_rate
        + c["cache_read_tokens"] * in_rate * _CACHE_READ_MULT
        + c["cache_write_tokens"] * in_rate * _CACHE_WRITE_MULT
        + c["output_tokens"] * out_rate
    ) / 1_000_000 + c["web_searches"] * _WEB_SEARCH_USD


def collect(bucket: str, dates: list[str]) -> dict[str, dict[str, int]]:
    """Sum each tier's counters across the given UTC date strings."""
    out: dict[str, dict[str, int]] = {}
    for tier in _TIER_RATES:
        totals = {m: 0 for m in _METRICS}
        for date in dates:
            for m in _METRICS:
                totals[m] += intel_cache.get_count(f"claude_usage:{bucket}:{tier}:{m}:{date}")
        out[tier] = totals
    return out


def render(bucket: str, dates: list[str], by_tier: dict[str, dict[str, int]]) -> str:
    span = dates[0] if len(dates) == 1 else f"{dates[-1]}..{dates[0]} ({len(dates)}d)"
    lines = [f"Measured Claude spend — bucket={bucket!r}, {span}", ""]
    grand_cost = 0.0
    grand_calls = 0
    for tier, c in by_tier.items():
        if not c["calls"]:
            continue
        in_rate, out_rate = _TIER_RATES[tier]
        cost = _tier_cost(c, in_rate, out_rate)
        grand_cost += cost
        grand_calls += c["calls"]
        per_call = cost / c["calls"] if c["calls"] else 0.0
        lines.append(
            f"  {tier:<5} calls={c['calls']:<6} in={c['input_tokens']:<10} out={c['output_tokens']:<8} "
            f"cache_r={c['cache_read_tokens']:<8} web={c['web_searches']:<5} "
            f"-> ${cost:.2f}  (${per_call:.4f}/call)"
        )
    lines.append("")
    overall = grand_cost / grand_calls if grand_calls else 0.0
    daily = grand_cost / len(dates)
    lines.append(
        f"  TOTAL  calls={grand_calls}  ${grand_cost:.2f}  (${overall:.4f}/call, ${daily:.2f}/day, ~${daily * 30.4:.0f}/mo)"
    )
    if not grand_calls:
        lines.append(
            "  (no metered enrichment calls in this window — paid tiers idle, which is the expected near-term state)"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Report measured Claude spend for the enrichment cost bucket.")
    parser.add_argument("--bucket", default="enrichment", help="cost bucket name (default: enrichment)")
    parser.add_argument("--date", default=None, help="UTC date YYYY-MM-DD (default: today)")
    parser.add_argument("--days", type=int, default=1, help="sum over the last N days ending at --date (default: 1)")
    args = parser.parse_args()

    end = (
        datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if args.date
        else datetime.now(timezone.utc)
    )
    dates = [(end - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(max(1, args.days))]
    print(render(args.bucket, dates, collect(args.bucket, dates)))


if __name__ == "__main__":
    main()
