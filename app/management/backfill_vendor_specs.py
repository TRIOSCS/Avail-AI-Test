"""Paced vendor-API parametric-spec backfill — populate materials filter facets for
high-demand cards by pulling distributor (Mouser) data through the desc grammar.

Usage: python -m app.management.backfill_vendor_specs --apply --limit N --daily-cap 800

Selects non-deleted, non-internal, UNCATEGORIZED material cards (v1 needs-enrichment
predicate: ``category IS NULL``), ordered demand-first (``sourced_qty_90d DESC NULLS
LAST, id``), limited to ``--limit``. For each card within the per-day Mouser call cap it
runs ``connector.search(display_mpn)`` and, on a hit, enriches via
``vendor_spec_enrich.enrich_card_from_mouser`` (category + spec facets through the F1
ladder at connector_desc/tier 84). A date-keyed request counter
``vendor_api:mouser:calls:{date}`` is billed via the atomic ``intel_cache.incr_count``
BEFORE each call (cap checked first; stop when reached) so the worker and this backfill
never lose each other's updates. Commits per chunk so progress survives interruption.
``--apply`` is OFF by default — the dry run counts would-enrich cards, searches nothing,
writes nothing, bills nothing, and prints the ordered report.

Mirrors app/management/backfill_oem_crosswalk.py (argparse / demand-ordered DB select /
date-keyed cap counters / chunked commits / dry-run report). Loguru everywhere except
the final CLI report (print, like backfill_oem_crosswalk's pattern).

Called by: an operator (manually). Depends on: app.database.SessionLocal,
      search_service._build_connectors, vendor_spec_enrich.enrich_card_from_mouser,
      models.MaterialCard, cache.intel_cache.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

from loguru import logger

from app.cache import intel_cache
from app.models import MaterialCard
from app.search_service import _build_connectors
from app.services.vendor_spec_enrich import enrich_card_from_mouser

_DEFAULT_DAILY_CAP = 800  # safe Mouser free-tier budget (1000 calls/day) with headroom
_COMMIT_EVERY = 25
_DRY_RUN_LOG_LIMIT = 50  # cap per-candidate dry-run logging (the backlog is ~737k rows)


def select_candidates(db, limit: int | None) -> list[MaterialCard]:
    """Return uncategorized cards demand-first (``sourced_qty_90d DESC NULLS LAST,
    id``).

    v1 needs-enrichment predicate is ``category IS NULL`` (keep it simple — a categorized
    card with missing facets is a later iteration). Filters soft-deleted + internal
    parts. NULLS-LAST is emulated portably (SQLite has no NULLS LAST) by ordering on
    ``sourced_qty_90d IS NULL`` first, so NULL demand sorts after every real value.
    """
    query = (
        db.query(MaterialCard)
        .filter(
            MaterialCard.deleted_at.is_(None),
            MaterialCard.is_internal_part.is_(False),
            MaterialCard.category.is_(None),
        )
        .order_by(
            MaterialCard.sourced_qty_90d.is_(None),  # False (real value) sorts before True (NULL)
            MaterialCard.sourced_qty_90d.desc(),
            MaterialCard.id,
        )
    )
    if limit is not None:
        query = query.limit(limit)
    return query.all()


def _mouser_connector(db):
    """The live Mouser connector (source_name == "mouser"), or None if not
    configured."""
    connectors, _stats, _disabled = _build_connectors(db)
    for connector in connectors:
        if getattr(connector, "source_name", None) == "mouser":
            return connector
    return None


async def run(
    *, source: str = "mouser", limit: int | None = None, daily_cap: int = _DEFAULT_DAILY_CAP, apply: bool = False
) -> dict:
    """Backfill parametric facets for up to *limit* uncategorized cards via *source*.

    Returns a summary dict (seen / would_enrich / enriched / categorized / specs_written /
    calls / failed). Stops when the per-day call counter reaches *daily_cap* (checked
    BEFORE each call). When *limit* is None the candidate selection is bounded to
    *daily_cap* (at most that many cards can be processed per run) so the whole
    uncategorized backlog is never materialized. A per-card enrichment that raises is
    isolated (its writes rolled back), tallied in ``failed`` and skipped — the run
    continues and prior committed chunks survive. Dry-run (``apply=False``) searches
    nothing and writes nothing, and caps per-candidate logging at ``_DRY_RUN_LOG_LIMIT``.
    """
    from app.database import SessionLocal

    if source != "mouser":
        raise ValueError(f"unsupported source {source!r} — only 'mouser' is wired in v1")

    summary = {
        "seen": 0,
        "would_enrich": 0,
        "enriched": 0,
        "categorized": 0,
        "specs_written": 0,
        "calls": 0,
        "failed": 0,
    }
    db = SessionLocal()
    try:
        # At most daily_cap cards can be processed per run, so a no-limit run is bounded to
        # daily_cap rather than materializing the entire uncategorized backlog (~737k rows)
        # into memory. An explicit --limit always wins (the operator's tighter cap).
        effective_limit = limit if limit is not None else daily_cap
        candidates = select_candidates(db, effective_limit)
        summary["seen"] = len(candidates)
        logger.info(
            "backfill-vendor-specs: {} uncategorized candidate(s){}",
            len(candidates),
            ", DRY-RUN" if not apply else "",
        )
        if not apply:
            summary["would_enrich"] = len(candidates)
            for card in candidates[:_DRY_RUN_LOG_LIMIT]:
                logger.info(
                    "backfill-vendor-specs: would enrich {} (sourced_qty_90d={})",
                    card.display_mpn,
                    card.sourced_qty_90d,
                )
            if len(candidates) > _DRY_RUN_LOG_LIMIT:
                logger.info(
                    "backfill-vendor-specs: … and {} more (per-candidate log capped at {})",
                    len(candidates) - _DRY_RUN_LOG_LIMIT,
                    _DRY_RUN_LOG_LIMIT,
                )
            return summary

        connector = _mouser_connector(db)
        if connector is None:
            logger.error(
                "backfill-vendor-specs: no '{}' connector configured (missing API key) — nothing to do", source
            )
            return summary

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        calls_key = f"vendor_api:{source}:calls:{today_str}"
        calls = intel_cache.get_count(calls_key)
        since_commit = 0

        for card in candidates:
            # Re-read the shared counter each iteration and stop at the cap — the cap is
            # checked BEFORE the call so the day's budget is never exceeded.
            calls = max(intel_cache.get_count(calls_key), calls)
            if calls >= daily_cap:
                logger.info("backfill-vendor-specs: daily cap reached ({}) — stopping", daily_cap)
                break

            # Bill BEFORE the await via the atomic incr (no lost updates against the
            # worker's biller) — a call that bills then raises is already counted.
            calls = max(intel_cache.incr_count(calls_key, ttl_days=1.0), calls + 1)
            summary["calls"] += 1
            try:
                results = await connector.search(card.display_mpn)
            except Exception as exc:
                logger.warning(
                    "backfill-vendor-specs: {} search failed: {} — skipping", card.display_mpn, type(exc).__name__
                )
                continue

            if not results:
                logger.debug("backfill-vendor-specs: {} no {} hit", card.display_mpn, source)
                continue

            # Per-card isolation (mirrors writer.extract_and_record_specs): the whole
            # enrichment runs in ONE begin_nested SAVEPOINT so a re-raise rolls back ALL of
            # this card's writes — including set_category's card.category mutation, which
            # the writer performs OUTSIDE its own inner savepoint — without poisoning the
            # caller's transaction or discarding already-committed chunks. A failure is
            # tallied and skipped; the run continues.
            try:
                with db.begin_nested():
                    card_summary = enrich_card_from_mouser(db, card, results)
            except Exception:
                summary["failed"] += 1
                logger.warning(
                    "backfill-vendor-specs: card id={} ({}) enrichment failed — skipping", card.id, card.display_mpn
                )
                continue

            if card_summary["categorized"] or card_summary["specs_written"]:
                summary["enriched"] += 1
                summary["categorized"] += card_summary["categorized"]
                summary["specs_written"] += card_summary["specs_written"]
                logger.info(
                    "backfill-vendor-specs: {} -> {} (categorized={}, specs={})",
                    card.display_mpn,
                    card.category,
                    card_summary["categorized"],
                    card_summary["specs_written"],
                )

            since_commit += 1
            if since_commit >= _COMMIT_EVERY:
                db.commit()  # chunked commit: progress survives interruption
                since_commit = 0

        db.commit()  # flush the final partial chunk
        logger.info(
            "backfill-vendor-specs: done — {} enriched of {} seen ({} calls)",
            summary["enriched"],
            summary["seen"],
            summary["calls"],
        )
        return summary
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Paced vendor-API parametric-spec backfill (category + facets via the F1 ladder)"
    )
    parser.add_argument("--apply", action="store_true", help="write enrichments (default: dry-run, no writes/calls)")
    parser.add_argument("--limit", type=int, default=None, help="max cards to consider this run")
    parser.add_argument("--daily-cap", type=int, default=_DEFAULT_DAILY_CAP, help="max vendor-API calls per day")
    parser.add_argument("--source", choices=["mouser"], default="mouser", help="distributor source")
    args = parser.parse_args()
    summary = asyncio.run(run(source=args.source, limit=args.limit, daily_cap=args.daily_cap, apply=args.apply))
    print(
        f"backfill-vendor-specs[{args.source}]: seen={summary['seen']} "
        f"{'would_enrich' if not args.apply else 'enriched'}="
        f"{summary['would_enrich'] if not args.apply else summary['enriched']} "
        f"categorized={summary['categorized']} specs_written={summary['specs_written']} "
        f"failed={summary['failed']} calls={summary['calls']}"
    )


if __name__ == "__main__":
    main()
