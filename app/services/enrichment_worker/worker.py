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
    - Ordered by ``search_count DESC, created_at ASC`` (high-demand parts first,
      then oldest un-enriched first for fairness).
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

    return (
        db.query(MaterialCard)
        .filter(
            MaterialCard.deleted_at.is_(None),
            MaterialCard.is_internal_part.is_(False),
            or_(
                MaterialCard.enrichment_status == MaterialEnrichmentStatus.UNENRICHED,
                not_found_eligible,
            ),
        )
        .order_by(
            MaterialCard.search_count.desc(),
            MaterialCard.created_at.asc(),
        )
        .limit(config.batch_size)
        .all()
    )


async def run_one_batch(
    db: Session,
    config: "EnrichmentWorkerConfig",
    cooldown: dict[str, float],
    breaker: "EnrichmentCircuitBreaker",
) -> dict[str, int]:
    """Enrich one batch of cards and return per-tier counts.

    Returns an empty dict if the batch is empty (caller should idle-sleep).

    Web daily-budget gate: reads ``enrichment_worker:web_calls:{date}`` from the
    intel_cache. If the counter is already at or above ``WEB_DAILY_CAP``, adds
    ``"web_search"`` to the disabled set so ``enrich_card`` skips the web tier
    and falls through to Opus ai_inferred instead.

    After each card that successfully used the web tier the Redis/cache counter
    is incremented (TTL = 1 day).
    """
    batch = select_batch(db, config)
    if not batch:
        return {}

    # Check web daily budget
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    web_cache_key = f"enrichment_worker:web_calls:{today_str}"
    cached_web = intel_cache.get_cached(web_cache_key)
    web_calls_today = int(cached_web.get("count", 0)) if isinstance(cached_web, dict) else 0

    disabled: set[str] = set()
    if web_calls_today >= config.web_daily_cap:
        logger.info(
            "ENRICH_WORKER: web daily cap reached ({}/{}) — skipping web tier this batch",
            web_calls_today,
            config.web_daily_cap,
        )
        disabled.add("web_search")

    conns = _connectors_in_order(db)
    counts: dict[str, int] = {}
    now = datetime.now(timezone.utc)

    for card in batch:
        try:
            status = await enrich_card(
                card,
                db,
                connectors=conns,
                disabled=disabled,
                cooldown=cooldown,
            )
            card.enriched_at = now
            counts[status] = counts.get(status, 0) + 1

            if status == MaterialEnrichmentStatus.WEB_SOURCED:
                web_calls_today += 1
                intel_cache.set_cached(
                    web_cache_key,
                    {"count": web_calls_today},
                    ttl_days=1.0,
                )
                breaker.record_claude_success()
            elif status == MaterialEnrichmentStatus.AI_INFERRED:
                breaker.record_claude_success()
            # verified and not_found are connector results — no Claude call to record

        except Exception as e:
            logger.error(
                "ENRICH_WORKER: enrich_card failed for {} ({}): {}",
                card.display_mpn,
                card.normalized_mpn,
                e,
            )
            breaker.record_claude_error()

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

                # Run one batch
                db = SessionLocal()
                try:
                    batch_counts = await run_one_batch(db, config, cooldown, breaker)

                    if not batch_counts:
                        # Queue empty — idle sleep
                        logger.debug("ENRICH_WORKER: queue empty, sleeping {}s", config.idle_sleep_seconds)
                        db.close()
                        await asyncio.sleep(config.idle_sleep_seconds)
                        continue

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
