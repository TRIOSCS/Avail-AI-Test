"""Paced OEM crosswalk backfill — drain the spare-PN backlog faster than organic
batches.

Usage: python -m app.management.backfill_oem_crosswalk --vendor hpe --limit N [--dry-run]

Selects DISTINCT uncached-or-stale spare norms from non-deleted material cards whose
display_mpn classifies as *vendor*, ordered demand-first:
  (1) CPU-commodity cards with search_count > 0,
  (2) remaining CPU-commodity cards,
  (3) other commodities,
newest spare numbers first within each bucket (see select_candidates),
then resolves each via the Claude-grounded resolver and UPSERTS oem_crosswalk rows only
(resolved or 90-day-negative no_match) — NO spec writes: the enrichment worker's Pass B
back-fills cards deterministically as batches cycle. Designed to run ALONGSIDE the live
worker: each item re-checks pending_resolution immediately before resolving (the
startup snapshot goes stale while the loop paces — a norm the worker cached mid-run is
skipped, not re-billed), a unique-key IntegrityError at commit is tolerated (rollback +
continue — the concurrent writer's row is the desired end state), and the SAME two
date-keyed counters as the worker (enrichment_worker:web_calls:{date} +
enrichment_worker:oem_resolves:{date}) are billed via the atomic intel_cache.incr_count
BEFORE each await, so neither biller can lose the other's updates. Stops at EITHER
daily cap (web_daily_cap / oem_resolve_daily_cap from EnrichmentWorkerConfig.from_env()
— raise ENRICHMENT_WEB_DAILY_CAP / ENRICHMENT_OEM_RESOLVE_DAILY_CAP for a drain
window), and sleeps >= 2s between calls. Commits after every upsert so progress
survives interruption. Stops after 5 consecutive ClaudeErrors (a backend outage must
not burn the day's budget on failures). --dry-run prints the ordered candidate list and
exits without any web call.

Called by: an operator (manually, post-deploy of migration 101 — NOT at startup).
Depends on: app.database.SessionLocal, enrichment_worker.config.EnrichmentWorkerConfig,
      enrichment_worker.oem_classifier.classify_oem_vendor,
      enrichment_worker.oem_crosswalk_resolver.resolve_oem_spare,
      oem_crosswalk_enrich.pending_resolution/apply_resolution,
      models.MaterialCard, cache.intel_cache, utils.normalization.normalize_mpn_key.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy.exc import IntegrityError

from app.cache import intel_cache
from app.models import MaterialCard
from app.services.enrichment_worker.config import EnrichmentWorkerConfig
from app.services.enrichment_worker.oem_classifier import classify_oem_vendor
from app.services.enrichment_worker.oem_crosswalk_resolver import resolve_oem_spare
from app.services.oem_crosswalk_enrich import apply_resolution, pending_resolution
from app.utils.claude_errors import ClaudeError
from app.utils.normalization import normalize_mpn_key

_SLEEP_SECONDS = 2.0
_MAX_CONSECUTIVE_CLAUDE_ERRORS = 5


def select_candidates(db, vendor: str) -> list[tuple[str, str]]:
    """Return ordered ``(spare_norm, display_mpn)`` candidates for *vendor*.

    Demand-first ordering per spec §5: (1) CPU-commodity cards with search_count > 0,
    (2) remaining CPU-commodity cards, (3) other commodities; search_count DESC then
    norm DESCENDING within each bucket. Descending because OEM spare numbering grows
    monotonically — L/P-series and high six-digit spares (modern, PartSurfer-covered,
    tradeable) sort lexicographically AFTER 1990s Compaq-era numbers, which resolve to
    near-universal no_match; ascending order front-loads the daily resolve budget with
    dead stock for weeks. Distinct by norm (a norm keeps its best bucket across the
    cards sharing it). Does NOT filter by cache freshness — the caller intersects with
    ``pending_resolution``.
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
    # Newest spares first within each bucket: norm DESC, then a stable re-sort on
    # (bucket, -search_count) so bucket priority still dominates.
    ordered = sorted(best.values(), key=lambda k: k[2], reverse=True)
    ordered.sort(key=lambda k: (k[0], k[1]))
    return [(norm, display) for _b, _s, norm, display in ordered]


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

        today_str = datetime.now(UTC).strftime("%Y-%m-%d")
        web_key = f"enrichment_worker:web_calls:{today_str}"
        oem_key = f"enrichment_worker:oem_resolves:{today_str}"

        web_calls = intel_cache.get_count(web_key)
        oem_resolves = intel_cache.get_count(oem_key)
        attempted = 0
        consecutive_errors = 0
        for norm, display in queue:
            # Re-read both counters each iteration (the worker shares them) and stop
            # at EITHER cap — the sub-cap is counted INSIDE the web cap. The local is
            # the in-process floor when the cache no-ops.
            web_calls = max(intel_cache.get_count(web_key), web_calls)
            oem_resolves = max(intel_cache.get_count(oem_key), oem_resolves)
            if web_calls >= config.web_daily_cap:
                logger.info("backfill-oem-crosswalk: web daily cap reached ({}) — stopping", config.web_daily_cap)
                break
            if oem_resolves >= config.oem_resolve_daily_cap:
                logger.info(
                    "backfill-oem-crosswalk: oem-resolve daily cap reached ({}) — stopping",
                    config.oem_resolve_daily_cap,
                )
                break

            # Per-item freshness re-check: the startup `pending` snapshot goes stale
            # while this loop paces — the live worker may have cached this norm
            # minutes ago. Skipping costs one SELECT; not skipping costs a billed
            # resolve plus a unique-key collision at commit.
            item_pending = pending_resolution(db, [norm], vendor)
            if norm not in item_pending:
                logger.info("backfill-oem-crosswalk: {} already cached by a concurrent writer — skipping", display)
                continue
            stale_row = item_pending[norm]

            # Bill BEFORE the await via the atomic incr (no lost updates against the
            # worker's biller) — a call that bills then raises is already counted.
            web_calls = max(intel_cache.incr_count(web_key, ttl_days=1.0), web_calls + 1)
            oem_resolves = max(intel_cache.incr_count(oem_key, ttl_days=1.0), oem_resolves + 1)
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
            consecutive_errors = 0

            row = apply_resolution(stale_row, result, display_mpn=display, spare_norm=norm, vendor=vendor)
            if stale_row is None:
                db.add(row)
            try:
                db.commit()  # per-row commit: progress survives interruption
            except IntegrityError:
                # The worker committed the same edge during our await — rollback and
                # move on; the existing row IS the desired end state (the cache won).
                db.rollback()
                logger.info("backfill-oem-crosswalk: {} upserted concurrently — keeping the existing row", display)
                await asyncio.sleep(_SLEEP_SECONDS)
                continue
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
