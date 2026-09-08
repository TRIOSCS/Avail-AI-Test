"""EBay search worker — main entry point.

Runs as a long-lived background process that:
1. Claims queued parts from ebay_search_queue
2. Searches each MPN on eBay's Browse API (no browser, no Xvfb)
3. Applies the strict part-number filter and writes sightings to AVAIL
4. Paces on a flat minimum delay and a daily Browse API call budget

This is the fourth search worker and the first that is an API poller rather
than a browser automation. It therefore has no session manager, no search
engine, no human-behavior module and no AI commodity gate: every queued MPN is
searched, and spend is bounded by calls, not by classification.

Run: python -m app.services.ebay_worker.worker

Called by: systemd service (avail-ebay-worker.service)
Depends on: all ebay_worker modules, credential_service, database
"""

import asyncio
import hashlib
import json
import signal
from contextlib import contextmanager
from datetime import UTC, datetime

import httpx
from loguru import logger
from sqlalchemy.orm import Session

from app.connectors.errors import ConnectorRateLimitError
from app.constants import SearchQueueStatus

# Private module-level alias for the pacing primitive. The main loop awaits
# ``_async_sleep`` (never ``asyncio.sleep`` directly) so tests patch
# ``worker._async_sleep`` in isolation — patching the shared ``asyncio.sleep``
# would intercept sleeps from every other coroutine in the process. Production
# behavior is identical: ``_async_sleep is asyncio.sleep``.
_async_sleep = asyncio.sleep

_shutdown_requested = False

# Written to ebay_worker_status.circuit_breaker_reason while the worker has no
# credentials, so the Connectors card shows the real problem instead of a green pill.
CREDENTIALS_MISSING_REASON = "EBAY_CLIENT_ID/EBAY_CLIENT_SECRET not configured (Settings -> Connectors -> eBay)"


def _handle_shutdown(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
    logger.info("Shutdown signal received (signal {}) — finishing current search then stopping", signum)


signal.signal(signal.SIGTERM, _handle_shutdown)
signal.signal(signal.SIGINT, _handle_shutdown)


@contextmanager
def _db_session():
    """Open a short-lived DB session and guarantee it is closed."""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def update_worker_status(db: Session, **kwargs):
    """Update the ebay_worker_status singleton row.

    Pass any column as a kwarg: is_running=True, calls_today=12, etc.
    """
    from app.models import EbayWorkerStatus

    status = db.get(EbayWorkerStatus, 1)
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
    path (idle, budget-exhausted, breaker-open), not just after a search.
    """
    update_worker_status(db, is_running=True, last_heartbeat=datetime.now(UTC))


def load_credentials(db: Session) -> tuple[str | None, str | None]:
    """Read the eBay app credentials DB-first with an env fallback.

    Unlike the browser workers (host-only .env logins), eBay reuses the EBAY_CLIENT_ID /
    EBAY_CLIENT_SECRET pair Settings -> Connectors already stores encrypted, so rotating
    the key in the UI reaches the worker without editing a file on the host.
    """
    from app.services.credential_service import get_credential

    return (
        get_credential(db, "ebay", "EBAY_CLIENT_ID"),
        get_credential(db, "ebay", "EBAY_CLIENT_SECRET"),
    )


def ensure_status_row(db: Session):
    """Return the status singleton, seeding it when absent.

    The whole daily call budget lives on this row: with no row, ``read_budget``
    would report 0 spent on every tick and the worker would poll eBay all day with
    no cap. Seeding (the same idempotent helper app startup uses) is the safe
    repair — refusing to search would silently stop the worker instead.
    """
    from app.models import EbayWorkerStatus
    from app.startup import seed_ebay_worker_status_singleton

    row = db.get(EbayWorkerStatus, 1)
    if row is None:
        logger.warning("eBay worker: ebay_worker_status singleton missing — reseeding it (budget bookkeeping)")
        seed_ebay_worker_status_singleton(db)
        db.commit()
        row = db.get(EbayWorkerStatus, 1)
    return row


def read_budget(db: Session, today) -> int:
    """Calls already spent today (UTC), applying the lazy midnight rollover.

    Reads calls_today/budget_day off the status singleton and writes the reset back when
    the stored day is stale, so the counter survives a restart but never carries
    yesterday's spend into today.
    """
    from .scheduler import rollover_calls

    row = ensure_status_row(db)
    if row is None:
        return 0
    spent = rollover_calls(row.calls_today, row.budget_day, today)
    if row.budget_day != today:
        row.calls_today = 0
        row.budget_day = today
        row.updated_at = datetime.now(UTC)
        db.commit()
    return spent


def record_calls(db: Session, today, calls: int) -> int:
    """Add ``calls`` to today's spend on the singleton; return the new total."""
    from .scheduler import rollover_calls

    row = ensure_status_row(db)
    if row is None:
        return 0
    spent = rollover_calls(row.calls_today, row.budget_day, today) + calls
    row.calls_today = spent
    row.budget_day = today
    row.updated_at = datetime.now(UTC)
    db.commit()
    return spent


async def main():
    """Main worker loop."""
    from app.database import SessionLocal
    from app.models import EbaySearchLog

    from .circuit_breaker import CircuitBreaker
    from .config import EbayConfig
    from .queue_manager import (
        claim_next_queued_item,
        mark_completed,
        mark_status,
        recover_stale_searches,
    )
    from .result_parser import parse_item_summaries
    from .scheduler import EbayScheduler, utc_today
    from .search_client import CallCounter, search_mpn
    from .sighting_writer import save_ebay_sightings

    config = EbayConfig()
    scheduler = EbayScheduler(config)
    breaker = CircuitBreaker(cooldown_seconds=config.EBAY_BREAKER_COOLDOWN_MINUTES * 60)
    searches_today = 0
    sightings_today = 0
    last_stats_date = None
    breaker_was_open = False
    creds_missing_flagged = False

    def _log_search(db, item, started, *, results_found: int, sightings_created: int, payload=None, error=None):
        """Append one ebay_search_log row — success AND failure alike.

        Failures used to write no row at all, which left the table's ``error``
        column dead and made "why did eBay stop producing sightings" answerable
        only from journald.
        """
        payload_hash = (
            hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
            if payload is not None
            else None
        )
        db.add(
            EbaySearchLog(
                queue_id=item.id,
                duration_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
                results_found=results_found,
                sightings_created=sightings_created,
                page_html_hash=payload_hash,
                error=error,
            )
        )

    logger.info(
        "eBay worker starting (marketplace={}, budget={} calls/day, {} page(s) x {})",
        config.EBAY_MARKETPLACE_ID,
        config.EBAY_DAILY_CALL_BUDGET,
        config.EBAY_MAX_PAGES,
        config.EBAY_PAGE_LIMIT,
    )

    # Recover stale items from a previous crash, and make sure the status
    # singleton exists — every budget write is a no-op without it.
    with _db_session() as db:
        recover_stale_searches(db)
        ensure_status_row(db)
        update_worker_status(db, is_running=True, last_heartbeat=datetime.now(UTC))

    try:
        while True:
            if _shutdown_requested:
                logger.info("Graceful shutdown requested — exiting main loop")
                break

            try:
                today = utc_today()

                # Refresh liveness heartbeat every tick — runs on ALL paths.
                with _db_session() as db:
                    _record_heartbeat(db)
                    calls_today = read_budget(db, today)
                    client_id, client_secret = load_credentials(db)

                # Reset daily stats at midnight UTC
                if last_stats_date != today:
                    if last_stats_date is not None:
                        logger.info(
                            "eBay daily summary: {} searches, {} sightings",
                            searches_today,
                            sightings_today,
                        )
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
                    last_stats_date = today

                if not client_id or not client_secret:
                    logger.warning(
                        "eBay worker: EBAY_CLIENT_ID/EBAY_CLIENT_SECRET not configured "
                        "(Settings -> Connectors -> eBay) — idling"
                    )
                    # Say so on the status singleton, not just in the log. The
                    # Connectors card reads worker health for worker-backed sources;
                    # without this an unconfigured worker heartbeats happily and the
                    # card stays green while nothing is ever searched.
                    if not creds_missing_flagged:
                        with _db_session() as db:
                            update_worker_status(
                                db,
                                circuit_breaker_open=True,
                                circuit_breaker_reason=CREDENTIALS_MISSING_REASON,
                            )
                        creds_missing_flagged = True
                    await _async_sleep(15 * 60)
                    continue

                if creds_missing_flagged:
                    logger.info("eBay worker: credentials configured — resuming searches")
                    with _db_session() as db:
                        update_worker_status(db, circuit_breaker_open=False, circuit_breaker_reason=None)
                    creds_missing_flagged = False

                # Daily call budget spent — sleep to midnight UTC, do not call.
                # Gated on the WHOLE next search (up to EBAY_MAX_PAGES calls), so the
                # last search of the day cannot overshoot the budget.
                if not scheduler.can_afford_search(calls_today):
                    sleep_s = scheduler.sleep_until_budget_resets()
                    logger.info(
                        "eBay worker: daily call budget spent ({}/{}) — sleeping {:.0f} min to midnight UTC",
                        calls_today,
                        config.EBAY_DAILY_CALL_BUDGET,
                        sleep_s / 60,
                    )
                    await _async_sleep(sleep_s)
                    continue

                # Circuit breaker
                if breaker.should_stop():
                    info = breaker.get_trip_info()
                    logger.error("eBay worker: circuit breaker open ({}), sleeping 1hr", info["trip_reason"])
                    with _db_session() as db:
                        update_worker_status(
                            db,
                            circuit_breaker_open=True,
                            circuit_breaker_reason=info["trip_reason"],
                        )
                    breaker_was_open = True
                    await _async_sleep(60 * 60)
                    continue

                # Breaker healthy: clear a previously-open flag on the
                # open->healthy transition (it auto-resets after cooldown).
                if breaker_was_open:
                    logger.info("eBay worker: circuit breaker self-healed, resuming searches")
                    with _db_session() as db:
                        update_worker_status(db, circuit_breaker_open=False, circuit_breaker_reason=None)
                    breaker_was_open = False

                db = SessionLocal()
                item = None  # pre-init so the except path's `if item` guard is safe
                try:
                    item = claim_next_queued_item(db)
                    if not item:
                        logger.debug("eBay worker: queue empty, sleeping {}s", config.EBAY_POLL_IDLE_SECONDS)
                        db.close()
                        await _async_sleep(scheduler.idle_delay())
                        continue

                    logger.info("eBay worker: searching '{}' (queue id={})", item.mpn, item.id)
                    started = datetime.now(UTC)
                    # The counter is read on EVERY exit path: a search that raises or
                    # times out has still spent the calls it already made, and booking
                    # a hardcoded 1 (or nothing) let real spend drift past the budget.
                    counter = CallCounter()
                    try:
                        payload, calls_made = await asyncio.wait_for(
                            search_mpn(item.mpn, config, client_id, client_secret, counter),
                            # The whole search, not one page: config.EBAY_SEARCH_TIMEOUT_SECONDS
                            # is httpx's PER-REQUEST timeout (see scheduler.search_deadline).
                            timeout=scheduler.search_deadline(),
                        )
                    except TimeoutError:
                        logger.error(
                            "eBay worker: search timed out after {:.0f}s (queue id={}) — failing item",
                            scheduler.search_deadline(),
                            item.id,
                        )
                        breaker.record_api_failure(TimeoutError("Browse API search timeout"))
                        record_calls(db, today, counter.calls)
                        _log_search(db, item, started, results_found=0, sightings_created=0, error="Search timeout")
                        mark_status(db, item, SearchQueueStatus.FAILED, error="Search timeout")
                        db.close()
                        await _async_sleep(scheduler.next_delay())
                        continue
                    except ConnectorRateLimitError as e:
                        # eBay throttled us even after the client honored Retry-After.
                        # Re-QUEUE (never FAILED — failed is terminal for this
                        # requirement+MPN pair) and stand down for a while instead of
                        # coming back in 3 seconds.
                        breaker.record_api_failure(e, 429)
                        logger.warning(
                            "eBay worker: rate limited (queue id={}) — re-queued, backing off {:.0f}s",
                            item.id,
                            scheduler.rate_limit_backoff(),
                        )
                        record_calls(db, today, counter.calls)
                        _log_search(db, item, started, results_found=0, sightings_created=0, error=str(e)[:500])
                        mark_status(db, item, SearchQueueStatus.QUEUED, error=str(e)[:500])
                        db.close()
                        await _async_sleep(scheduler.rate_limit_backoff())
                        continue
                    except (httpx.HTTPError, ValueError) as e:
                        # Every failure the Browse API itself can produce: an HTTP
                        # status (raise_for_status), a transport error, or a body that
                        # is not JSON. Anything else is a bug, not an upstream
                        # problem, and falls through to the iteration handler below.
                        status_code = getattr(getattr(e, "response", None), "status_code", None)
                        verdict = breaker.record_api_failure(e, status_code)
                        logger.error("eBay worker: Browse API error ({}): {}", verdict, e)
                        record_calls(db, today, counter.calls)
                        _log_search(db, item, started, results_found=0, sightings_created=0, error=str(e)[:500])
                        mark_status(db, item, SearchQueueStatus.FAILED, error=str(e)[:500])
                        db.close()
                        await _async_sleep(scheduler.next_delay())
                        continue

                    breaker.record_api_success()
                    calls_today = record_calls(db, today, calls_made)

                    ebay_sightings = parse_item_summaries(
                        payload, item.mpn, include_auctions=config.EBAY_INCLUDE_AUCTIONS
                    )
                    # The empty-result streak is a SHADOW-BLOCK detector, so it must
                    # count what eBay returned, not what survived the strict MPN
                    # filter: dropping every hit is routine for a part eBay does not
                    # stock, and counting those would trip the breaker during healthy
                    # operation.
                    if payload.get("itemSummaries"):
                        breaker.record_results()
                    else:
                        breaker.record_empty_results()

                    created_count = save_ebay_sightings(db, item, ebay_sightings)

                    _log_search(
                        db,
                        item,
                        started,
                        results_found=len(ebay_sightings),
                        sightings_created=created_count,
                        payload=payload,
                    )

                    mark_completed(db, item, results_found=len(ebay_sightings), sightings_created=created_count)

                    searches_today += 1
                    sightings_today += created_count

                    update_worker_status(
                        db,
                        last_search_at=datetime.now(UTC),
                        searches_today=searches_today,
                        sightings_today=sightings_today,
                        last_heartbeat=datetime.now(UTC),
                    )

                    logger.info(
                        "eBay worker: '{}' done — {} kept, {} sightings (today: {} searches, {}/{} calls)",
                        item.mpn,
                        len(ebay_sightings),
                        created_count,
                        searches_today,
                        calls_today,
                        config.EBAY_DAILY_CALL_BUDGET,
                    )

                except Exception as e:  # noqa: BLE001 — one bad queue item must fail alone, never kill the poller
                    # Feed the breaker here too. An unexpected failure that repeats
                    # (a token body without access_token, a parser AttributeError, a
                    # save IntegrityError) would otherwise mark one item FAILED every
                    # EBAY_MIN_DELAY_SECONDS forever, burning the queue with nothing
                    # ever tripping.
                    breaker.record_api_failure(e)
                    logger.error("eBay worker: search iteration error: {}", e)
                    try:
                        if item:
                            mark_status(db, item, SearchQueueStatus.FAILED, error=str(e)[:500])
                    except Exception as mark_err:  # noqa: BLE001 — the failure is already logged; a dead DB session must not mask it
                        logger.debug("eBay worker: failed to mark item as failed: {}", mark_err)
                finally:
                    db.close()

                await _async_sleep(scheduler.next_delay())

            except Exception as e:  # noqa: BLE001 — last-resort loop guard: back off and retry rather than exit and lose the heartbeat
                logger.error("eBay worker: unexpected error in main loop: {}", e)
                await _async_sleep(5 * 60)

    finally:
        logger.info(
            "eBay worker shutting down: {} searches, {} sightings today",
            searches_today,
            sightings_today,
        )
        with _db_session() as db:
            update_worker_status(db, is_running=False)


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
