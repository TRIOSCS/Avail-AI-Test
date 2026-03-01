"""ICsource search worker — main entry point.

Runs as a long-lived background process that:
1. Classifies pending parts via AI gate
2. Searches queued parts on ICsource via browser automation
3. Parses results and writes sightings to AVAIL database
4. Respects rate limits, business hours, and circuit breaker

Run: python -m app.services.ics_worker.worker

Called by: systemd service (avail-ics-worker.service)
Depends on: all ics_worker modules, database
"""

import asyncio
import hashlib
import signal
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # pragma: no cover

EASTERN = ZoneInfo("America/New_York")

_shutdown_requested = False


def _handle_shutdown(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
    logger.info("Shutdown signal received (signal {}) — finishing current search then stopping", signum)


signal.signal(signal.SIGTERM, _handle_shutdown)
signal.signal(signal.SIGINT, _handle_shutdown)


def update_worker_status(db: Session, **kwargs):
    """Update the ics_worker_status singleton row.

    Pass any column as a kwarg: is_running=True, searches_today=5, etc.
    """
    from app.models import IcsWorkerStatus

    status = db.query(IcsWorkerStatus).filter(IcsWorkerStatus.id == 1).first()
    if not status:
        return
    for key, value in kwargs.items():
        if hasattr(status, key):
            setattr(status, key, value)
    status.updated_at = datetime.now(timezone.utc)
    db.commit()


async def main():
    """Main worker loop."""
    from app.database import SessionLocal
    from app.models import IcsSearchLog

    from .ai_gate import process_ai_gate
    from .circuit_breaker import CircuitBreaker
    from .config import IcsConfig
    from .queue_manager import (
        get_next_queued_item,
        mark_completed,
        mark_status,
        recover_stale_searches,
    )
    from .result_parser import parse_results_html
    from .scheduler import SearchScheduler
    from .search_engine import search_part
    from .session_manager import IcsSessionManager
    from .sighting_writer import save_ics_sightings

    config = IcsConfig()
    scheduler = SearchScheduler(config)
    breaker = CircuitBreaker()
    searches_today = 0
    sightings_today = 0
    last_stats_date = None

    logger.info("ICS worker starting...")

    # Recover stale items from previous crash
    db = SessionLocal()
    try:
        recover_stale_searches(db)
        update_worker_status(db, is_running=True, last_heartbeat=datetime.now(timezone.utc))
    finally:
        db.close()

    # Start browser session
    session = IcsSessionManager(config)
    try:
        await session.start()
    except Exception as e:
        logger.error("ICS worker: failed to start browser session: {}", e)
        db = SessionLocal()
        try:
            update_worker_status(db, is_running=False)
        finally:
            db.close()
        return

    if not session.is_logged_in:
        if not await session.login():
            logger.error("ICS worker: initial login failed, exiting")
            await session.stop()
            db = SessionLocal()
            try:
                update_worker_status(db, is_running=False)
            finally:
                db.close()
            return

    logger.info("ICS worker: browser session ready")

    try:
        while True:
            if _shutdown_requested:
                logger.info("Graceful shutdown requested — exiting main loop")
                break

            try:
                now_eastern = datetime.now(EASTERN)

                # Reset daily stats at midnight
                today_date = now_eastern.date()
                if last_stats_date != today_date:
                    if last_stats_date is not None:
                        logger.info(
                            "ICS daily summary: {} searches, {} sightings",
                            searches_today,
                            sightings_today,
                        )
                        # Update daily stats in worker status
                        db = SessionLocal()
                        try:
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
                        finally:
                            db.close()
                    searches_today = 0
                    sightings_today = 0
                    last_stats_date = today_date

                # Check business hours
                if not scheduler.is_business_hours():
                    logger.debug("ICS worker: outside business hours, sleeping 30 min")
                    await asyncio.sleep(30 * 60)
                    continue

                # Check daily limit
                if searches_today >= config.ICS_MAX_DAILY_SEARCHES:
                    logger.info("ICS worker: daily limit reached ({}), sleeping until tomorrow", searches_today)
                    await asyncio.sleep(60 * 60)
                    continue

                # Check circuit breaker
                if breaker.should_stop():
                    info = breaker.get_trip_info()
                    logger.error("ICS worker: circuit breaker open ({}), sleeping 1hr", info["trip_reason"])
                    db = SessionLocal()
                    try:
                        update_worker_status(
                            db,
                            circuit_breaker_open=True,
                            circuit_breaker_reason=info["trip_reason"],
                        )
                    finally:
                        db.close()
                    await asyncio.sleep(60 * 60)
                    continue

                # Check if time for a break
                if scheduler.time_for_break():
                    duration = scheduler.get_break_duration()
                    logger.info("ICS worker: taking a break ({:.0f} min)", duration / 60)
                    scheduler.reset_break_counter()
                    await asyncio.sleep(duration)
                    continue

                # Run AI gate for any pending items
                db = SessionLocal()
                try:
                    await process_ai_gate(db)
                except Exception as e:
                    logger.error("ICS worker: AI gate error: {}", e)
                finally:
                    db.close()

                # Get next queued item
                db = SessionLocal()
                try:
                    item = get_next_queued_item(db)
                    if not item:
                        logger.debug("ICS worker: queue empty, sleeping 60s")
                        db.close()
                        await asyncio.sleep(60)
                        continue

                    # Ensure session is valid
                    if not await session.ensure_session():
                        logger.error("ICS worker: session re-auth failed, sleeping 5 min")
                        mark_status(db, item, "failed", error="Session authentication failed")
                        db.close()
                        await asyncio.sleep(5 * 60)
                        continue

                    # Execute search
                    mark_status(db, item, "searching")
                    logger.info("ICS worker: searching '{}' (queue id={})", item.mpn, item.id)

                    search_result = await search_part(session.page, item.mpn)

                    # Check page health
                    health = await breaker.check_page_health(session.page)
                    if health == "SESSION_EXPIRED":
                        mark_status(db, item, "queued")  # re-queue for next attempt
                        db.close()
                        continue
                    if breaker.should_stop():
                        mark_status(db, item, "failed", error=f"Circuit breaker: {breaker.trip_reason}")
                        db.close()
                        continue

                    # Parse results
                    html = search_result["html"]
                    ics_sightings = parse_results_html(html)

                    if not ics_sightings:
                        breaker.record_empty_results()
                    else:
                        breaker.record_results()

                    # Write sightings
                    created_count = save_ics_sightings(db, item, ics_sightings)

                    # Log search
                    html_hash = hashlib.sha256(html.encode()).hexdigest() if html else None
                    log_entry = IcsSearchLog(
                        queue_id=item.id,
                        duration_ms=search_result["duration_ms"],
                        results_found=len(ics_sightings),
                        sightings_created=created_count,
                        page_html_hash=html_hash,
                    )
                    db.add(log_entry)

                    # Mark completed
                    mark_completed(db, item, results_found=len(ics_sightings), sightings_created=created_count)

                    searches_today += 1
                    sightings_today += created_count

                    # Update worker status after each search
                    update_worker_status(
                        db,
                        last_search_at=datetime.now(timezone.utc),
                        searches_today=searches_today,
                        sightings_today=sightings_today,
                        last_heartbeat=datetime.now(timezone.utc),
                    )

                    logger.info(
                        "ICS worker: '{}' done — {} results, {} sightings (today: {}/{})",
                        item.mpn,
                        len(ics_sightings),
                        created_count,
                        searches_today,
                        config.ICS_MAX_DAILY_SEARCHES,
                    )

                except Exception as e:
                    logger.error("ICS worker: search iteration error: {}", e)
                    try:
                        if item:
                            mark_status(db, item, "failed", error=str(e)[:500])
                    except Exception as mark_err:
                        logger.debug("ICS worker: failed to mark item as failed: %s", mark_err)
                finally:
                    db.close()

                # Delay before next search
                delay = scheduler.next_delay()
                logger.debug("ICS worker: sleeping {:.0f}s before next search", delay)
                await asyncio.sleep(delay)

            except Exception as e:
                logger.error("ICS worker: unexpected error in main loop: {}", e)
                await asyncio.sleep(5 * 60)

    finally:
        # Shutdown cleanup
        logger.info(
            "ICS worker shutting down: {} searches, {} sightings today",
            searches_today,
            sightings_today,
        )
        await session.stop()
        db = SessionLocal()
        try:
            update_worker_status(db, is_running=False)
        finally:
            db.close()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
