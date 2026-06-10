"""Paced OEM crosswalk backfill — drain the spare-PN backlog faster than organic
batches.

Usage: python -m app.management.backfill_oem_crosswalk --vendor hpe --limit N [--dry-run]

Selects DISTINCT uncached-or-stale spare norms from non-deleted material cards whose
display_mpn classifies as *vendor*, ordered demand-first:
  (1) CPU-commodity cards with search_count > 0,
  (2) remaining CPU-commodity cards,
  (3) other commodities,
then resolves each via the Claude-grounded resolver and UPSERTS oem_crosswalk rows only
(resolved or 90-day-negative no_match) — NO spec writes: the enrichment worker's Pass B
back-fills cards deterministically as batches cycle. Bills the SAME two date-keyed
counters as the worker (enrichment_worker:web_calls:{date} +
enrichment_worker:oem_resolves:{date}), stops at EITHER daily cap (web_daily_cap /
oem_resolve_daily_cap from EnrichmentWorkerConfig.from_env() — raise
ENRICHMENT_WEB_DAILY_CAP / ENRICHMENT_OEM_RESOLVE_DAILY_CAP for a drain window), and
sleeps >= 2s between calls. Commits after every upsert so progress survives
interruption. Stops after 5 consecutive ClaudeErrors (a backend outage must not burn
the day's budget on failures). --dry-run prints the ordered candidate list and exits
without any web call.

Called by: an operator (manually, post-deploy of migration 100 — NOT at startup).
Depends on: app.database.SessionLocal, enrichment_worker.config.EnrichmentWorkerConfig,
      enrichment_worker.oem_classifier.classify_oem_vendor,
      enrichment_worker.oem_crosswalk_resolver.resolve_oem_spare,
      oem_crosswalk_enrich.pending_resolution, models.MaterialCard/OemCrosswalk,
      cache.intel_cache, utils.normalization.normalize_mpn_key.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

from loguru import logger

from app.cache import intel_cache
from app.models import MaterialCard, OemCrosswalk
from app.services.enrichment_worker.config import EnrichmentWorkerConfig
from app.services.enrichment_worker.oem_classifier import classify_oem_vendor
from app.services.enrichment_worker.oem_crosswalk_resolver import resolve_oem_spare
from app.services.oem_crosswalk_enrich import pending_resolution
from app.utils.claude_errors import ClaudeError
from app.utils.normalization import normalize_mpn_key

_SLEEP_SECONDS = 2.0
_MAX_CONSECUTIVE_CLAUDE_ERRORS = 5


def select_candidates(db, vendor: str) -> list[tuple[str, str]]:
    """Return ordered ``(spare_norm, display_mpn)`` candidates for *vendor*.

    Demand-first ordering per spec §5: (1) CPU-commodity cards with search_count > 0,
    (2) remaining CPU-commodity cards, (3) other commodities; search_count DESC then
    norm within each bucket for determinism. Distinct by norm (a norm keeps its best
    bucket across the cards sharing it). Does NOT filter by cache freshness — the
    caller intersects with ``pending_resolution``.
    """
    rows = (
        db.query(MaterialCard.display_mpn, MaterialCard.category, MaterialCard.search_count)
        .filter(MaterialCard.deleted_at.is_(None), MaterialCard.is_internal_part.is_(False))
        .all()
    )
    best: dict[str, tuple[int, int, str, str]] = {}  # norm -> (bucket, -search_count, norm, display)
    for display_mpn, category, search_count in rows:
        if classify_oem_vendor(display_mpn) != vendor:
            continue
        norm = normalize_mpn_key(display_mpn)
        if not norm:
            continue
        is_cpu = (category or "").lower().strip() == "cpu"
        searches = int(search_count or 0)
        bucket = 0 if (is_cpu and searches > 0) else (1 if is_cpu else 2)
        key = (bucket, -searches, norm, str(display_mpn))
        if norm not in best or key < best[norm]:
            best[norm] = key
    return [(norm, display) for _b, _s, norm, display in sorted(best.values())]


async def run(vendor: str, limit: int | None, dry_run: bool) -> int:
    """Resolve up to *limit* pending spares for *vendor*.

    Returns resolves attempted.
    """
    from app.database import SessionLocal

    config = EnrichmentWorkerConfig.from_env()
    db = SessionLocal()
    try:
        candidates = select_candidates(db, vendor)
        pending = pending_resolution(db, [norm for norm, _ in candidates], vendor)
        queue = [(norm, display) for norm, display in candidates if norm in pending]
        if limit is not None:
            queue = queue[:limit]
        logger.info(
            "backfill-oem-crosswalk: {} {} candidates ({} uncached-or-stale queued{})",
            len(candidates),
            vendor,
            len(queue),
            ", DRY-RUN" if dry_run else "",
        )
        if dry_run:
            for norm, display in queue:
                logger.info("backfill-oem-crosswalk: would resolve {} ({})", display, norm)
            return 0

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        web_key = f"enrichment_worker:web_calls:{today_str}"
        oem_key = f"enrichment_worker:oem_resolves:{today_str}"

        def _count(key: str, floor: int) -> int:
            cached = intel_cache.get_cached(key)
            return max(int(cached.get("count", 0)) if isinstance(cached, dict) else 0, floor)

        web_calls = _count(web_key, 0)
        oem_resolves = _count(oem_key, 0)
        attempted = 0
        consecutive_errors = 0
        for norm, display in queue:
            # Re-read both counters each iteration (the worker shares them) and stop
            # at EITHER cap — the sub-cap is counted INSIDE the web cap.
            web_calls = _count(web_key, web_calls)
            oem_resolves = _count(oem_key, oem_resolves)
            if web_calls >= config.web_daily_cap:
                logger.info("backfill-oem-crosswalk: web daily cap reached ({}) — stopping", config.web_daily_cap)
                break
            if oem_resolves >= config.oem_resolve_daily_cap:
                logger.info(
                    "backfill-oem-crosswalk: oem-resolve daily cap reached ({}) — stopping",
                    config.oem_resolve_daily_cap,
                )
                break
            # Reserve BEFORE the await; flush in finally (the WebMeter discipline).
            web_calls += 1
            oem_resolves += 1
            attempted += 1
            try:
                result = await resolve_oem_spare(display, norm, vendor)
            except ClaudeError as e:
                consecutive_errors += 1
                logger.warning("backfill-oem-crosswalk: Claude error for {}: {} — no row", display, type(e).__name__)
                if consecutive_errors >= _MAX_CONSECUTIVE_CLAUDE_ERRORS:
                    logger.error(
                        "backfill-oem-crosswalk: {} consecutive Claude errors — aborting run", consecutive_errors
                    )
                    break
                await asyncio.sleep(_SLEEP_SECONDS)
                continue
            finally:
                intel_cache.set_cached(web_key, {"count": web_calls}, ttl_days=1.0)
                intel_cache.set_cached(oem_key, {"count": oem_resolves}, ttl_days=1.0)
            consecutive_errors = 0

            stale_row = pending.get(norm)
            row = (
                stale_row if stale_row is not None else OemCrosswalk(spare_raw=display, spare_norm=norm, vendor=vendor)
            )
            resolved = result.status == "resolved"
            row.status = result.status
            row.canonical_mpn_raw = result.canonical_mpn if resolved else None
            row.canonical_mpn_norm = normalize_mpn_key(result.canonical_mpn) if resolved else None
            row.canonical_manufacturer = result.manufacturer if resolved else None
            row.title = result.title if resolved else None
            row.confidence = result.confidence if resolved else None
            row.source_url = result.source_url if resolved else None
            row.source_domain = result.source_domain if resolved else None
            row.payload = result.payload
            row.looked_up_at = datetime.now(timezone.utc)
            if stale_row is None:
                db.add(row)
            db.commit()  # per-row commit: progress survives interruption
            logger.info("backfill-oem-crosswalk: {} -> {} ({})", display, result.status, result.canonical_mpn)
            await asyncio.sleep(_SLEEP_SECONDS)
        logger.info("backfill-oem-crosswalk: done — {} resolves attempted", attempted)
        return attempted
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Paced OEM crosswalk backfill (resolve + upsert rows only)")
    parser.add_argument("--vendor", choices=["hpe", "lenovo"], default="hpe")
    parser.add_argument("--limit", type=int, default=None, help="max resolves this run")
    parser.add_argument("--dry-run", action="store_true", help="print the queue, no web calls")
    args = parser.parse_args()
    asyncio.run(run(args.vendor, args.limit, args.dry_run))


if __name__ == "__main__":
    main()
