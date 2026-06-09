"""Enrichment worker — paced background loop.

Selects small batches of unenriched/retryable parts, runs each through
``enrich_card`` (verified → web_sourced → oem_sourced → ai_inferred →
not_found/not_catalogued), paces via a daily web-call budget + per-source
cooldowns, and heartbeats to ``enrichment_worker_status``.

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


def _record_heartbeat(db: Session, breaker: "EnrichmentCircuitBreaker") -> bool:
    """Refresh the liveness heartbeat on the status singleton — EVERY loop tick.

    Called at the top of the main loop (before any branch returns/sleeps) so
    ``last_heartbeat`` advances on idle, daily-cap, breaker-open and business-hours
    ticks too — not only after a non-empty batch. Without this, a perfectly alive
    worker can go ~1h between writes and a liveness monitor false-alarms "DOWN".

    Also keeps the persisted breaker flag honest: ``breaker.should_stop()`` is
    idempotent (auto-resets after cooldown), so writing it every tick clears a stale
    ``circuit_breaker_open=True`` even when the queue is empty (the non-empty-batch
    write that previously reset it never runs in that case).

    Returns the current ``breaker_open`` state so the caller can reuse it without a
    second ``should_stop()`` call.
    """
    from app.models.enrichment_worker_status import update_enrichment_worker_status

    breaker_open = breaker.should_stop()
    update_enrichment_worker_status(
        db,
        is_running=True,
        last_heartbeat=datetime.now(timezone.utc),
        circuit_breaker_open=breaker_open,
        circuit_breaker_reason=(breaker.get_trip_info()["trip_reason"] if breaker_open else None),
    )
    return breaker_open


def select_batch(db: Session, config: "EnrichmentWorkerConfig") -> list:
    """Return the next batch of cards eligible for enrichment.

    Anti-spin query — prevents re-hammering recently-failed parts:
    - ``unenriched``: always eligible.
    - ``not_found``: eligible only when ``enriched_at IS NULL`` OR older than
      ``not_found_retry_hours`` (self-heal as quotas reset daily).
    - ``not_catalogued``: eligible when ``enriched_at IS NULL`` OR older than
      ``not_catalogued_retry_days`` (long backoff — uncatalogued OEM service parts
      rarely become catalogued, so re-check infrequently).
    - ``is_internal_part``, ``deleted_at`` are excluded.
    - Ordered by ``status=unenriched DESC, search_count DESC, created_at DESC``:
      never-resolved parts drain before re-checks of already-terminal cards (so old,
      low-demand ``unenriched`` parts aren't starved by the daily ``not_found``
      re-check churn); then demand wins (high-demand first); then, among equal
      demand, the most-recently-added part heads the next batch (fast lane).
    """
    from app.models import MaterialCard

    # NOTE: eligibility is read from material_cards.enrichment_status — NOT the (unused)
    # enrichment_queue table. material_cards is the single source of enrichment state.
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
            # Never-resolved parts drain before re-checks of already-terminal ones.
            # Without this, old low-demand `unenriched` cards are starved: they share
            # search_count=0 with the daily `not_found` re-check churn, and created_at
            # DESC then favours the newer not_found cards until the daily cap is spent.
            (MaterialCard.enrichment_status == MaterialEnrichmentStatus.UNENRICHED).desc(),
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

    After the per-card core-enrichment loop completes, this also triggers a paced,
    second-pass PARAMETRIC SPEC extraction (``enrich_card_specs``) for the cards that
    landed a real category this batch (verified / web_sourced / ai_inferred) — so the
    materials filter tree gets MaterialSpecFacet + specs_structured data. It runs ONCE
    per batch (the extractor groups by category internally) and shares the same session
    and final commit, so core attrs and specs persist together. Bounded by the worker's
    daily_cap (≤ daily_cap cards/day receive a spec pass).

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
    # Cards that landed a real category this batch (verified / web_sourced / ai_inferred) —
    # the ones eligible for a second-pass parametric spec extraction. not_found and
    # exception (poison-pill) cards are deliberately excluded.
    enriched_ids: list[int] = []
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

            if status not in (MaterialEnrichmentStatus.NOT_FOUND, MaterialEnrichmentStatus.NOT_CATALOGUED):
                # Landed a real category (verified / web_sourced / oem_sourced / ai_inferred)
                # — queue it for the second-pass parametric spec extraction below. not_found
                # and not_catalogued (terminal misses) are excluded, as are poison-pill cards.
                enriched_ids.append(int(card.id))

            # A Claude call (web/cross-ref/OEM/infer) returned without raising → backend
            # healthy. (A pure-connector VERIFIED hit makes no Claude call, so card_meter
            # .claude_ok stays False and must not reset the breaker.)
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

    # Deterministic MPN→spec decode (storage/DRAM): zero-LLM, regex-gated, enum-validated by
    # record_spec. Runs BEFORE the AI spec pass so its 0.95 values are the baseline the 0.85
    # description-mined pass cannot overwrite. Same session, committed together below.
    if enriched_ids:
        from app.config import settings

        if settings.mpn_decode_enabled:
            from app.services.mpn_decoder.writer import decode_and_record_specs

            try:
                logger.info("ENRICH_WORKER: mpn-decode {}", decode_and_record_specs(db, enriched_ids))
            except Exception:
                logger.exception("ENRICH_WORKER: mpn-decode failed over {} cards", len(enriched_ids))

    # Deterministic description→spec extraction (storage/DRAM token grammar): zero-LLM,
    # enum-validated by record_spec. Runs AFTER mpn-decode (its 0.95 values outrank this
    # pass's 0.90 — the writer skips higher-confidence keys) and BEFORE the AI spec pass
    # (0.85), on the same shared post-await session. Committed together below.
    if enriched_ids:
        from app.config import settings

        if settings.desc_parse_enabled:
            from app.services.desc_extractor.writer import extract_and_record_specs

            try:
                logger.info("ENRICH_WORKER: desc-parse {}", extract_and_record_specs(db, enriched_ids))
            except Exception:
                logger.exception("ENRICH_WORKER: desc-parse failed over {} cards", len(enriched_ids))

    # Second pass: parametric spec extraction for cards that landed a real category this
    # batch. Runs ONCE per batch (the extractor groups by category internally) on the same
    # session, so specs persist together with core attrs at the commit below.
    if enriched_ids:
        from app.services.spec_enrichment_service import enrich_card_specs

        try:
            spec_stats = await enrich_card_specs(enriched_ids, db)
            logger.info("ENRICH_WORKER: specs {}", spec_stats)
        except ClaudeError as e:
            logger.warning("ENRICH_WORKER: spec extraction Claude error: {}", type(e).__name__)
            breaker.record_claude_error()
        except Exception as e:
            logger.error("ENRICH_WORKER: spec extraction failed: {}", e)

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
    oem_sourced_today = 0
    ai_inferred_today = 0
    not_found_today = 0
    not_catalogued_today = 0
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
                # Liveness heartbeat — EVERY tick, before any branch sleeps/continues, so
                # last_heartbeat never goes stale on idle/cap/breaker/business-hours paths.
                # Also reused below as the breaker-open gate (no second should_stop() call).
                db = SessionLocal()
                try:
                    breaker_open = _record_heartbeat(db, breaker)
                finally:
                    db.close()

                now_utc = datetime.now(timezone.utc)
                today_date = now_utc.date()

                # Daily reset at UTC midnight
                if last_stats_date != today_date:
                    if last_stats_date is not None:
                        logger.info(
                            "ENRICH_WORKER daily summary: enriched={}, web={}, oem={}, ai={}, "
                            "not_found={}, not_catalogued={}",
                            enriched_today,
                            web_sourced_today,
                            oem_sourced_today,
                            ai_inferred_today,
                            not_found_today,
                            not_catalogued_today,
                        )
                        db = SessionLocal()
                        try:
                            update_enrichment_worker_status(
                                db,
                                daily_stats_json={
                                    "date": str(last_stats_date),
                                    "enriched": enriched_today,
                                    "web_sourced": web_sourced_today,
                                    "oem_sourced": oem_sourced_today,
                                    "ai_inferred": ai_inferred_today,
                                    "not_found": not_found_today,
                                    "not_catalogued": not_catalogued_today,
                                },
                                enriched_today=0,
                                web_sourced_today=0,
                                oem_sourced_today=0,
                                ai_inferred_today=0,
                                not_found_today=0,
                                not_catalogued_today=0,
                            )
                        finally:
                            db.close()
                    enriched_today = 0
                    web_sourced_today = 0
                    oem_sourced_today = 0
                    ai_inferred_today = 0
                    not_found_today = 0
                    not_catalogued_today = 0
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

                # Circuit breaker check — long sleep when open. Reuses the breaker_open
                # value already persisted by _record_heartbeat at the top of this tick, so
                # the status write is not duplicated here.
                if breaker_open:
                    logger.error(
                        "ENRICH_WORKER: circuit breaker open ({}), sleeping 1h",
                        breaker.get_trip_info()["trip_reason"],
                    )
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
                        oem_sourced_today += batch_counts.get(MaterialEnrichmentStatus.OEM_SOURCED, 0)
                        ai_inferred_today += batch_counts.get(MaterialEnrichmentStatus.AI_INFERRED, 0)
                        not_found_today += batch_counts.get(MaterialEnrichmentStatus.NOT_FOUND, 0)
                        not_catalogued_today += batch_counts.get(MaterialEnrichmentStatus.NOT_CATALOGUED, 0)

                        # Heartbeat + counters
                        update_enrichment_worker_status(
                            db,
                            last_heartbeat=datetime.now(timezone.utc),
                            last_enriched_at=datetime.now(timezone.utc),
                            enriched_today=enriched_today,
                            web_sourced_today=web_sourced_today,
                            oem_sourced_today=oem_sourced_today,
                            ai_inferred_today=ai_inferred_today,
                            not_found_today=not_found_today,
                            not_catalogued_today=not_catalogued_today,
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
            "ENRICH_WORKER shutting down: enriched_today={}, web={}, oem={}, ai={}, not_found={}, not_catalogued={}",
            enriched_today,
            web_sourced_today,
            oem_sourced_today,
            ai_inferred_today,
            not_found_today,
            not_catalogued_today,
        )
        db = SessionLocal()
        try:
            update_enrichment_worker_status(db, is_running=False)
        finally:
            db.close()
