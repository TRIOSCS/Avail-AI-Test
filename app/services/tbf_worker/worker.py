"""The Broker Forum (TBF) search worker — main entry point.

Runs as a long-lived background process that:
1. Classifies pending parts via AI gate
2. Searches queued parts on thebrokersite.com via browser automation
3. Parses results and writes sightings to AVAIL database
4. Respects rate limits, business hours, and circuit breaker

Run: python -m app.services.tbf_worker.worker

Called by: systemd service (avail-tbf-worker.service)
Depends on: all tbf_worker modules, database
"""

import asyncio
import hashlib
import signal
from contextlib import contextmanager
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy.orm import Session

from app.constants import SearchQueueStatus

EASTERN = ZoneInfo("America/New_York")

# Private module-level alias for the pacing primitive. The main loop awaits
# ``_async_sleep`` (never ``asyncio.sleep`` directly) so tests patch
# ``worker._async_sleep`` in isolation. Patching the shared ``asyncio.sleep`` would
# intercept sleeps from every other coroutine in the process, skewing circuit-breaker
# call-count assertions and causing xdist-order flakes. Production behavior is
# identical: ``_async_sleep is asyncio.sleep``.
_async_sleep = asyncio.sleep

_shutdown_requested = False


def _handle_shutdown(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
    logger.info("Shutdown signal received (signal {}) — finishing current search then stopping", signum)


signal.signal(signal.SIGTERM, _handle_shutdown)
signal.signal(signal.SIGINT, _handle_shutdown)


@contextmanager
def _db_session():
    """Open a short-lived DB session and guarantee it is closed.

    Wraps the ``db = SessionLocal(); try: ... finally: db.close()`` boilerplate the
    main loop repeats on every status/heartbeat write.
    """
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def update_worker_status(db: Session, **kwargs):
    """Update the tbf_worker_status singleton row.

    Pass any column as a kwarg: is_running=True, searches_today=5, etc.
    """
    from app.models import TbfWorkerStatus

    status = db.query(TbfWorkerStatus).filter(TbfWorkerStatus.id == 1).first()
    if not status:
        return
    for key, value in kwargs.items():
        if hasattr(status, key):
            setattr(status, key, value)
    status.updated_at = datetime.now(UTC)
    db.commit()


def _record_heartbeat(db: Session):
    """Refresh the worker liveness heartbeat to now.

    Called at the top of every main-loop tick so last_heartbeat stays fresh on EVERY
    path (idle, cap-sleep, breaker-open, off-hours) — not just after a completed search.
    Keeps liveness monitors from false-alarming "worker DOWN".
    """
    update_worker_status(db, is_running=True, last_heartbeat=datetime.now(UTC))


async def main():
    """Main worker loop."""
    from app.database import SessionLocal
    from app.models import TbfSearchLog

    from .ai_gate import process_ai_gate
    from .circuit_breaker import CircuitBreaker
    from .config import TbfConfig
    from .queue_manager import (
        claim_next_queued_item,
        mark_completed,
        mark_status,
        recover_stale_searches,
    )
    from .result_parser import parse_results_html
    from .scheduler import SearchScheduler
    from .search_engine import search_part
    from .session_manager import TbfSessionManager
    from .sighting_writer import save_tbf_sightings

    config = TbfConfig()
    scheduler = SearchScheduler(config)
    breaker = CircuitBreaker(cooldown_seconds=config.TBF_BREAKER_COOLDOWN_MINUTES * 60)
    searches_today = 0
    sightings_today = 0
    last_stats_date = None
    breaker_was_open = False

    logger.info("TBF worker starting...")

    # Recover stale items from previous crash
    with _db_session() as db:
        recover_stale_searches(db)
        update_worker_status(db, is_running=True, last_heartbeat=datetime.now(UTC))

    # Start browser session
    session = TbfSessionManager(config)
    try:
        await session.start()
    except Exception as e:
        logger.error("TBF worker: failed to start browser session: {}", e)
        # start() may have launched Playwright/Chromium before failing (e.g. a nav or
        # health-check raised), so tear it down or the browser subprocess leaks — matches
        # the login-failure path below. stop() is None-safe on partial state.
        await session.stop()
        with _db_session() as db:
            update_worker_status(db, is_running=False)
        return

    if not session.is_logged_in:
        if not await session.login():
            logger.error("TBF worker: initial login failed, exiting")
            await session.stop()
            with _db_session() as db:
                update_worker_status(db, is_running=False)
            return

    logger.info("TBF worker: browser session ready")

    try:
        while True:
            if _shutdown_requested:
                logger.info("Graceful shutdown requested — exiting main loop")
                break

            try:
                # Refresh liveness heartbeat every tick — runs on ALL paths
                # (idle, cap-sleep, breaker-open, off-hours), not just searches.
                with _db_session() as db:
                    _record_heartbeat(db)

                now_eastern = datetime.now(EASTERN)

                # Reset daily stats at midnight
                today_date = now_eastern.date()
                if last_stats_date != today_date:
                    if last_stats_date is not None:
                        logger.info(
                            "TBF daily summary: {} searches, {} sightings",
                            searches_today,
                            sightings_today,
                        )
                        # Update daily stats in worker status
                        with _db_session() as db:
                            update_worker_status(
                                db,
                                daily_stats_json={
                                    "date": str(last_stats_date),
                                    "searches": searches_today,
                                    "sightings": sightings_today,
                                },
                                searches_today=0,
                                sightings_today=0,
                            )
                    searches_today = 0
                    sightings_today = 0
                    last_stats_date = today_date

                # Check business hours
                if not scheduler.is_business_hours():
                    logger.debug("TBF worker: outside business hours, sleeping 30 min")
                    await _async_sleep(30 * 60)
                    continue

                # Check daily limit
                if searches_today >= config.TBF_MAX_DAILY_SEARCHES:
                    logger.info("TBF worker: daily limit reached ({}), sleeping until tomorrow", searches_today)
                    await _async_sleep(60 * 60)
                    continue

                # Check circuit breaker
                if breaker.should_stop():
                    info = breaker.get_trip_info()
                    logger.error("TBF worker: circuit breaker open ({}), sleeping 1hr", info["trip_reason"])
                    with _db_session() as db:
                        update_worker_status(
                            db,
                            circuit_breaker_open=True,
                            circuit_breaker_reason=info["trip_reason"],
                        )
                    breaker_was_open = True
                    await _async_sleep(60 * 60)
                    continue

                # Breaker healthy: clear a previously-open flag on the open->healthy
                # transition (the breaker auto-resets after cooldown, so without this
                # the status row would show circuit_breaker_open=True forever).
                if breaker_was_open:
                    logger.info("TBF worker: circuit breaker self-healed, resuming searches")
                    with _db_session() as db:
                        update_worker_status(
                            db,
                            circuit_breaker_open=False,
                            circuit_breaker_reason=None,
                        )
                    breaker_was_open = False

                # Check if time for a break
                if scheduler.time_for_break():
                    duration = scheduler.get_break_duration()
                    logger.info("TBF worker: taking a break ({:.0f} min)", duration / 60)
                    scheduler.reset_break_counter()
                    await _async_sleep(duration)
                    continue

                # Run AI gate for any pending items
                with _db_session() as db:
                    try:
                        await process_ai_gate(db)
                    except Exception as e:
                        logger.error("TBF worker: AI gate error: {}", e)

                # Get next queued item
                db = SessionLocal()
                item = None  # pre-init so the except path's `if item` guard is safe
                try:
                    # Atomically claim (marks 'searching'; skip-locked on PG) and
                    # auto-reclaim any items a crashed worker left mid-search.
                    item = claim_next_queued_item(db)
                    if not item:
                        logger.debug("TBF worker: queue empty, sleeping 60s")
                        db.close()
                        await _async_sleep(60)
                        continue

                    # Ensure session is valid
                    if not await session.ensure_session():
                        logger.error("TBF worker: session re-auth failed, sleeping 5 min")
                        mark_status(db, item, SearchQueueStatus.FAILED, error="Session authentication failed")
                        db.close()
                        await _async_sleep(5 * 60)
                        continue

                    # Execute search (already marked 'searching' by the claim).
                    # Hard timeout so a wedged page can't stall the loop/heartbeat forever.
                    logger.info("TBF worker: searching '{}' (queue id={})", item.mpn, item.id)

                    try:
                        search_result = await asyncio.wait_for(
                            search_part(session.page, item.mpn),
                            timeout=config.TBF_SEARCH_TIMEOUT_SECONDS,
                        )
                    except TimeoutError:
                        logger.error(
                            "TBF worker: search timed out after {}s (queue id={}) — failing item",
                            config.TBF_SEARCH_TIMEOUT_SECONDS,
                            item.id,
                        )
                        mark_status(db, item, SearchQueueStatus.FAILED, error="Search timeout")
                        db.close()
                        continue

                    # Check page health
                    health = await breaker.check_page_health(session.page)
                    if health == "SESSION_EXPIRED":
                        mark_status(db, item, SearchQueueStatus.QUEUED)  # re-queue for next attempt
                        db.close()
                        continue
                    if breaker.should_stop():
                        mark_status(db, item, SearchQueueStatus.FAILED, error=f"Circuit breaker: {breaker.trip_reason}")
                        db.close()
                        continue

                    # Parse results
                    html = search_result["html"]
                    tbf_sightings = parse_results_html(html)

                    if not tbf_sightings:
                        breaker.record_empty_results()
                    else:
                        breaker.record_results()

                    # Write sightings
                    created_count = save_tbf_sightings(db, item, tbf_sightings)

                    # Log search
                    html_hash = hashlib.sha256(html.encode()).hexdigest() if html else None
                    log_entry = TbfSearchLog(
                        queue_id=item.id,
                        duration_ms=search_result["duration_ms"],
                        results_found=len(tbf_sightings),
                        sightings_created=created_count,
                        page_html_hash=html_hash,
                    )
                    db.add(log_entry)

                    # Mark completed
                    mark_completed(db, item, results_found=len(tbf_sightings), sightings_created=created_count)

                    searches_today += 1
                    sightings_today += created_count

                    # Update worker status after each search
                    update_worker_status(
                        db,
                        last_search_at=datetime.now(UTC),
                        searches_today=searches_today,
                        sightings_today=sightings_today,
                        last_heartbeat=datetime.now(UTC),
                    )

                    logger.info(
                        "TBF worker: '{}' done — {} results, {} sightings (today: {}/{})",
                        item.mpn,
                        len(tbf_sightings),
                        created_count,
                        searches_today,
                        config.TBF_MAX_DAILY_SEARCHES,
                    )

                except Exception as e:
                    logger.error("TBF worker: search iteration error: {}", e)
                    try:
                        if item:
                            mark_status(db, item, SearchQueueStatus.FAILED, error=str(e)[:500])
                    except Exception as mark_err:
                        logger.debug("TBF worker: failed to mark item as failed: {}", mark_err)
                finally:
                    db.close()

                # Delay before next search
                delay = scheduler.next_delay()
                logger.debug("TBF worker: sleeping {:.0f}s before next search", delay)
                await _async_sleep(delay)

            except Exception as e:
                logger.error("TBF worker: unexpected error in main loop: {}", e)
                await _async_sleep(5 * 60)

    finally:
        # Shutdown cleanup
        logger.info(
            "TBF worker shutting down: {} searches, {} sightings today",
            searches_today,
            sightings_today,
        )
        await session.stop()
        with _db_session() as db:
            update_worker_status(db, is_running=False)


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
