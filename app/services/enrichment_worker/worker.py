"""Enrichment worker — paced background loop.

Selects small batches of unenriched/retryable parts, runs each through
``enrich_card`` (verified → web_sourced → ai_inferred → not_found),
paces via a daily web-call budget + per-source cooldowns, and heartbeats to
``enrichment_worker_status``.

Run: python -m app.services.enrichment_worker

Called by: enrichment-worker Docker container
Depends on: database, Redis (optional, falls back to Postgres cache)
"""

from __future__ import annotations

import asyncio
import signal
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.cache import intel_cache
from app.constants import MaterialEnrichmentStatus
from app.services.authoritative_enrichment_service import (
    _connectors_in_order,
    enrich_card,
)
from app.services.enrichment_types import WebMeter
from app.utils.claude_errors import ClaudeError

if TYPE_CHECKING:
    from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig

_shutdown_requested = False


def _handle_shutdown(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
    logger.info(
        "Enrichment worker: shutdown signal received (signal {}) — finishing current batch then stopping",
        signum,
    )


signal.signal(signal.SIGTERM, _handle_shutdown)
signal.signal(signal.SIGINT, _handle_shutdown)


# ---------------------------------------------------------------------------
# Pure, testable helpers
# ---------------------------------------------------------------------------


def select_batch(db: Session, config: "EnrichmentWorkerConfig") -> list:
    """Return the next batch of cards eligible for enrichment.

    Anti-spin query — prevents re-hammering recently-failed parts:
    - ``unenriched``: always eligible.
    - ``not_found``: eligible only when ``enriched_at IS NULL`` OR older than
      ``not_found_retry_hours`` (self-heal as quotas reset daily).
    - ``is_internal_part``, ``deleted_at`` are excluded.
    - Ordered by ``search_count DESC, created_at DESC``: demand still wins
      (high-demand parts first); among equal demand — the common case where
      newly-added parts have ``search_count=0`` — the most-recently-added part
      is enriched first, so a just-added part heads the next batch (fast lane).
    """
    from app.models import MaterialCard

    now = datetime.now(timezone.utc)
    retry_cutoff = now - timedelta(hours=config.not_found_retry_hours)

    not_found_eligible = and_(
        MaterialCard.enrichment_status == MaterialEnrichmentStatus.NOT_FOUND,
        or_(
            MaterialCard.enriched_at.is_(None),
            MaterialCard.enriched_at < retry_cutoff,
        ),
    )

    not_catalogued_cutoff = now - timedelta(days=config.not_catalogued_retry_days)
    not_catalogued_eligible = and_(
        MaterialCard.enrichment_status == MaterialEnrichmentStatus.NOT_CATALOGUED,
        or_(
            MaterialCard.enriched_at.is_(None),
            MaterialCard.enriched_at < not_catalogued_cutoff,
        ),
    )

    return (
        db.query(MaterialCard)
        .filter(
            MaterialCard.deleted_at.is_(None),
            MaterialCard.is_internal_part.is_(False),
            or_(
                MaterialCard.enrichment_status == MaterialEnrichmentStatus.UNENRICHED,
                not_found_eligible,
                not_catalogued_eligible,
            ),
        )
        .order_by(
            MaterialCard.search_count.desc(),
            MaterialCard.created_at.desc(),
        )
        .limit(config.batch_size)
        .all()
    )


async def run_one_batch(
    db: Session,
    config: "EnrichmentWorkerConfig",
    cooldown: dict[str, float],
    breaker: "EnrichmentCircuitBreaker",
    disabled: set[str] | None = None,
    web_state: dict[str, int] | None = None,
) -> dict[str, int]:
    """Enrich one batch of cards and return per-tier counts.

    Returns an empty dict if the batch is empty (caller should idle-sleep).

    ``disabled`` accumulates sources that hit a quota/auth wall (connectors) and the
    ``"web_search"`` flag once the web budget is spent. It is owned by ``main()`` so the
    state PERSISTS across batches (a quota'd connector or an exhausted web budget stays
    disabled instead of being re-tried every 30s) and is cleared at the daily reset. When
    omitted (tests) a fresh per-call set is used.

    Web daily-budget gate: the per-day call count is read from
    ``enrichment_worker:web_calls:{date}`` in the intel_cache, but defended in depth by an
    in-process tally in ``web_state`` — the cache silently no-ops if Redis AND Postgres are
    both down, which would otherwise let WEB_DAILY_CAP be bypassed entirely. The gate is
    re-checked BEFORE each card (not once per batch). A single card may fire up to 3 billable
    web calls, so the per-card gate can overshoot the cap by at most 2; the meter is flushed
    in a finally so every dispatched call is billed even when a later tier raises. Once the
    cap is hit, ``"web_search"`` is added to ``disabled`` so ``enrich_card`` skips the web
    tier and falls through to Opus ai_inferred.

    After each card the dispatched web-call count is flushed into the cache counter and the
    ``web_state`` tally (TTL = 1 day).
    """
    batch = select_batch(db, config)
    if not batch:
        return {}

    if disabled is None:
        disabled = set()
    if web_state is None:
        web_state = {"web_calls": 0}

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    web_cache_key = f"enrichment_worker:web_calls:{today_str}"
    cached_web = intel_cache.get_cached(web_cache_key)
    cache_count = int(cached_web.get("count", 0)) if isinstance(cached_web, dict) else 0
    # Defense in depth: take the max of the (cross-restart) cache value and the in-process
    # tally so the cap holds even when the cache is unavailable and silently no-ops.
    web_calls_today = max(cache_count, web_state.get("web_calls", 0))

    conns = _connectors_in_order(db)
    counts: dict[str, int] = {}
    now = datetime.now(timezone.utc)

    for card in batch:
        # Per-card budget gate (exact — no batch_size overshoot). Once tripped, "web_search"
        # stays in the persistent disabled set until the daily reset re-enables it.
        if web_calls_today >= config.web_daily_cap and "web_search" not in disabled:
            logger.info(
                "ENRICH_WORKER: web daily cap reached ({}/{}) — disabling web tier",
                web_calls_today,
                config.web_daily_cap,
            )
            disabled.add("web_search")
        card_meter = WebMeter()
        try:
            status = await enrich_card(
                card,
                db,
                connectors=conns,
                disabled=disabled,
                cooldown=cooldown,
                web_meter=card_meter,
            )
            card.enriched_at = now
            counts[status] = counts.get(status, 0) + 1

            # A Claude call (web/cross-ref/OEM/infer) returned without raising → backend healthy.
            if card_meter.claude_ok:
                breaker.record_claude_success()

        except ClaudeError as e:
            # Claude backend is failing — feed the circuit breaker so a sustained outage
            # trips it (sleep 1h) instead of silently marking the whole queue not_found and
            # burning API spend. The card is left unenriched and retried next batch.
            logger.warning(
                "ENRICH_WORKER: Claude error for {} ({}): {}",
                card.display_mpn,
                card.normalized_mpn,
                type(e).__name__,
            )
            breaker.record_claude_error()
        except Exception as e:
            # Non-Claude failure (a bug, a DB hiccup). Log loudly, but do NOT trip the
            # Claude-specific breaker. Quarantine the card as not_found + stamp enriched_at
            # so the not_found retry backoff applies — otherwise a poison-pill card (one
            # whose data deterministically triggers the failure) would be re-selected at
            # the front of EVERY batch (fast-lane) and spin forever. It self-heals at the
            # next retry window if the underlying bug is fixed.
            logger.error(
                "ENRICH_WORKER: enrich_card failed for {} ({}): {} — quarantining as not_found",
                card.display_mpn,
                card.normalized_mpn,
                e,
            )
            card.enrichment_status = MaterialEnrichmentStatus.NOT_FOUND
            card.enriched_at = now
            counts[MaterialEnrichmentStatus.NOT_FOUND] = counts.get(MaterialEnrichmentStatus.NOT_FOUND, 0) + 1
        finally:
            # Bill every web call that was DISPATCHED, even if a later tier raised — the
            # meter reserves before each await, so calls that fired then failed are still
            # counted (otherwise WEB_DAILY_CAP drifts over on a ClaudeError mid-card).
            if card_meter.web_calls > 0:
                web_calls_today += card_meter.web_calls
                intel_cache.set_cached(web_cache_key, {"count": web_calls_today}, ttl_days=1.0)

    web_state["web_calls"] = web_calls_today

    try:
        db.commit()
    except Exception as e:
        logger.error("ENRICH_WORKER: commit failed for batch: {}", e)
        db.rollback()

    return counts


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def main() -> None:
    """Enrichment worker main loop.

    Mirrors ``app/services/ics_worker/worker.py`` structure:
    - SIGTERM/SIGINT → graceful shutdown after current batch
    - Startup heartbeat (is_running=True)
    - Per-iteration SessionLocal (fresh session each loop)
    - DAILY_CAP / circuit breaker → long sleep (1h)
    - Empty batch → idle sleep (IDLE_SLEEP_SECONDS)
    - Non-empty batch → loop sleep (LOOP_SLEEP_SECONDS)
    - Daily reset at UTC midnight archives yesterday's stats
    """
    from app.database import SessionLocal
    from app.models.enrichment_worker_status import update_enrichment_worker_status
    from app.services.enrichment_worker.circuit_breaker import EnrichmentCircuitBreaker
    from app.services.enrichment_worker.config import EnrichmentWorkerConfig

    config = EnrichmentWorkerConfig.from_env()
    breaker = EnrichmentCircuitBreaker(config)
    cooldown: dict[str, float] = {}
    # Persist across batches (cleared at the daily reset): connectors that hit a
    # quota/auth wall and the "web_search" flag once the daily web budget is spent —
    # so they stay disabled instead of being re-tried (and re-logged) every loop.
    disabled: set[str] = set()
    # In-process web-call tally — the durable backstop for WEB_DAILY_CAP if the cache
    # is unavailable. Reset at the daily reset alongside the cache's date-keyed counter.
    web_state: dict[str, int] = {"web_calls": 0}

    # Running totals for today
    enriched_today = 0
    web_sourced_today = 0
    ai_inferred_today = 0
    not_found_today = 0
    last_stats_date = None

    logger.info(
        "ENRICH_WORKER: starting up (batch_size={}, daily_cap={}, web_daily_cap={})",
        config.batch_size,
        config.daily_cap,
        config.web_daily_cap,
    )

    # Startup heartbeat
    db = SessionLocal()
    try:
        update_enrichment_worker_status(
            db,
            is_running=True,
            last_heartbeat=datetime.now(timezone.utc),
        )
    finally:
        db.close()

    try:
        while True:
            if _shutdown_requested:
                logger.info("ENRICH_WORKER: graceful shutdown — exiting main loop")
                break

            try:
                now_utc = datetime.now(timezone.utc)
                today_date = now_utc.date()

                # Daily reset at UTC midnight
                if last_stats_date != today_date:
                    if last_stats_date is not None:
                        logger.info(
                            "ENRICH_WORKER daily summary: enriched={}, web={}, ai={}, not_found={}",
                            enriched_today,
                            web_sourced_today,
                            ai_inferred_today,
                            not_found_today,
                        )
                        db = SessionLocal()
                        try:
                            update_enrichment_worker_status(
                                db,
                                daily_stats_json={
                                    "date": str(last_stats_date),
                                    "enriched": enriched_today,
                                    "web_sourced": web_sourced_today,
                                    "ai_inferred": ai_inferred_today,
                                    "not_found": not_found_today,
                                },
                                enriched_today=0,
                                web_sourced_today=0,
                                ai_inferred_today=0,
                                not_found_today=0,
                            )
                        finally:
                            db.close()
                    enriched_today = 0
                    web_sourced_today = 0
                    ai_inferred_today = 0
                    not_found_today = 0
                    last_stats_date = today_date
                    # New day: quotas/budgets reset, so re-enable disabled sources and the
                    # web tier, and zero the in-process web tally (the cache counter is
                    # date-keyed and resets on its own).
                    disabled.clear()
                    web_state["web_calls"] = 0

                # Daily cap check — long sleep when exhausted
                if enriched_today >= config.daily_cap:
                    logger.info(
                        "ENRICH_WORKER: daily cap reached ({}/{}), sleeping 1h",
                        enriched_today,
                        config.daily_cap,
                    )
                    await asyncio.sleep(3600)
                    continue

                # Circuit breaker check — long sleep when open
                if breaker.should_stop():
                    info = breaker.get_trip_info()
                    logger.error(
                        "ENRICH_WORKER: circuit breaker open ({}), sleeping 1h",
                        info["trip_reason"],
                    )
                    db = SessionLocal()
                    try:
                        update_enrichment_worker_status(
                            db,
                            circuit_breaker_open=True,
                            circuit_breaker_reason=info["trip_reason"],
                        )
                    finally:
                        db.close()
                    await asyncio.sleep(3600)
                    continue

                # Run one batch. The session is closed exactly once (in finally) so the
                # connection is released before the idle/loop sleep below.
                db = SessionLocal()
                batch_counts: dict[str, int] = {}
                try:
                    batch_counts = await run_one_batch(db, config, cooldown, breaker, disabled, web_state)

                    if batch_counts:
                        # Accumulate daily totals
                        total_this_batch = sum(batch_counts.values())
                        enriched_today += total_this_batch
                        web_sourced_today += batch_counts.get(MaterialEnrichmentStatus.WEB_SOURCED, 0)
                        ai_inferred_today += batch_counts.get(MaterialEnrichmentStatus.AI_INFERRED, 0)
                        not_found_today += batch_counts.get(MaterialEnrichmentStatus.NOT_FOUND, 0)

                        # Heartbeat + counters
                        update_enrichment_worker_status(
                            db,
                            last_heartbeat=datetime.now(timezone.utc),
                            last_enriched_at=datetime.now(timezone.utc),
                            enriched_today=enriched_today,
                            web_sourced_today=web_sourced_today,
                            ai_inferred_today=ai_inferred_today,
                            not_found_today=not_found_today,
                            circuit_breaker_open=False,
                            circuit_breaker_reason=None,
                        )

                        logger.info(
                            "ENRICH_WORKER: batch done {} (today: {}/{})",
                            batch_counts,
                            enriched_today,
                            config.daily_cap,
                        )

                except Exception as e:
                    logger.error("ENRICH_WORKER: batch error: {}", e)
                finally:
                    db.close()

                if not batch_counts:
                    # Queue empty — idle sleep (longer; connection already released).
                    logger.debug("ENRICH_WORKER: queue empty, sleeping {}s", config.idle_sleep_seconds)
                    await asyncio.sleep(config.idle_sleep_seconds)
                else:
                    await asyncio.sleep(config.loop_sleep_seconds)

            except Exception as e:
                logger.error("ENRICH_WORKER: unexpected error in main loop: {}", e)
                await asyncio.sleep(300)

    finally:
        logger.info(
            "ENRICH_WORKER shutting down: enriched_today={}, web={}, ai={}, not_found={}",
            enriched_today,
            web_sourced_today,
            ai_inferred_today,
            not_found_today,
        )
        db = SessionLocal()
        try:
            update_enrichment_worker_status(db, is_running=False)
        finally:
            db.close()
