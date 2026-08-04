"""Core background jobs — token refresh, inbox scan, batch results.

Called by: app/jobs/__init__.py via register_core_jobs()
Depends on: app.database, app.models, app.email_service
"""

import asyncio
from datetime import UTC, datetime, timedelta

import sqlalchemy.exc
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from ..scheduler import _traced_job
from ..services.m365_status import REASON_TRANSIENT, reason_for
from ..utils.token_manager import _utc, m365_configured


def register_core_jobs(scheduler, settings, db=None):
    """Register core jobs with the scheduler.

    *db* (when provided) lets inbox_scan_interval_min resolve from the system_config DB
    row (admin toggle) instead of only the env default.

    Keys-off honesty (spec §7): without the Azure app credentials every token-refresh
    attempt 404s against login.microsoftonline.com — twice per 5-minute run, which alone
    fails the 48h zero-recurring-warnings gate (§11). Gate the registration itself (the
    register_email_jobs/register_offers_jobs idiom) with one notice.
    """
    from ..services.admin_service import get_effective_int

    scan_interval_min = get_effective_int(db, "inbox_scan_interval_min", settings.inbox_scan_interval_min)
    if m365_configured():
        scheduler.add_job(_job_token_refresh, IntervalTrigger(minutes=5), id="token_refresh", name="Token refresh")
    else:
        logger.info("Token refresh not registered — Azure credentials not configured (M365 is off)")
    scheduler.add_job(_job_inbox_scan, IntervalTrigger(minutes=scan_interval_min), id="inbox_scan", name="Inbox scan")
    scheduler.add_job(_job_batch_results, IntervalTrigger(minutes=5), id="batch_results", name="Process batch results")


@_traced_job
async def _job_token_refresh():
    """Refresh tokens for all users with refresh tokens.

    Registered only when M365 is configured (see register_core_jobs), so the job body
    carries no keys-off guard.
    """
    from ..database import SessionLocal
    from ..models import User
    from ..utils.token_manager import refresh_user_token

    selector_db = SessionLocal()
    users_to_refresh: list[int] = []
    try:
        now = datetime.now(UTC)
        users = selector_db.query(User).filter(User.refresh_token.isnot(None)).all()
        for user in users:
            needs_refresh = False
            if user.token_expires_at:
                exp = _utc(user.token_expires_at)
                needs_refresh = now > exp - timedelta(minutes=15)
            elif not user.access_token:
                needs_refresh = True

            if needs_refresh:
                users_to_refresh.append(user.id)
    except sqlalchemy.exc.OperationalError as e:
        logger.error(f"Token refresh job DB error: {e}")
        raise
    except Exception as e:
        logger.exception(f"Token refresh job error: {e}")
        raise  # Re-raise so _traced_job / Sentry can capture
    finally:
        selector_db.close()

    # Refresh users in parallel, but each task gets its own DB session.
    sem = asyncio.Semaphore(5)

    async def _safe_refresh(user_id: int):
        async with sem:
            task_db = SessionLocal()
            from ..cache.intel_cache import _get_redis

            r = _get_redis()
            lock_key = f"lock:token_refresh:{user_id}"
            acquired = False
            try:
                user = task_db.get(User, user_id)
                if not user:
                    return
                if r:
                    acquired = r.set(lock_key, "1", nx=True, ex=60)
                    if not acquired:
                        logger.debug("Token refresh skipped for {} — lock held", user.email)
                        return
                await refresh_user_token(user, task_db)
            except Exception as e:
                logger.exception(f"Token refresh error for user {user_id}: {e}")
                task_db.rollback()
                try:
                    user = task_db.get(User, user_id)
                    if user:
                        user.m365_error_reason = reason_for(e)
                        task_db.commit()
                except Exception:
                    task_db.rollback()
            finally:
                if r and acquired:
                    try:
                        r.delete(lock_key)
                    except Exception as e:
                        logger.warning("Failed to release token refresh lock: {}", e)
                task_db.close()

    if users_to_refresh:
        await asyncio.gather(*[_safe_refresh(uid) for uid in users_to_refresh])


@_traced_job
async def _job_inbox_scan():
    """Scan inboxes for all connected users."""
    from ..config import settings
    from ..database import SessionLocal
    from ..models import User
    from ..services.admin_service import get_effective_int
    from .email_jobs import _scan_user_inbox

    # Use a short-lived session just to identify users that need scanning
    db = SessionLocal()
    try:
        now = datetime.now(UTC)
        users = db.query(User).filter(User.refresh_token.isnot(None)).all()
        scan_interval = timedelta(
            minutes=get_effective_int(db, "inbox_scan_interval_min", settings.inbox_scan_interval_min)
        )

        users_to_scan = []
        for user in users:
            if not user.access_token or not user.m365_connected:
                continue
            should_scan = not user.last_inbox_scan or now - _utc(user.last_inbox_scan) > scan_interval
            if should_scan:
                # Detach user data we need so we can close this session
                users_to_scan.append(user.id)
    except sqlalchemy.exc.OperationalError as e:
        logger.error(f"Inbox scan DB error: {e}")
        raise
    except Exception as e:
        logger.exception(f"Inbox scan job error: {e}")
        raise  # Re-raise so _traced_job / Sentry can capture
    finally:
        db.close()

    # Scan each user with its own session (returned to pool after each scan)
    sem = asyncio.Semaphore(3)

    async def _safe_scan(user_id):
        async with sem:
            scan_db = SessionLocal()
            try:
                user = scan_db.get(User, user_id)
                if not user:
                    return
                await asyncio.wait_for(_scan_user_inbox(user, scan_db), timeout=90)
            except TimeoutError:
                logger.error(f"Inbox scan TIMEOUT for user {user_id} (90s) — skipping")
                scan_db.rollback()
                try:
                    user = scan_db.get(User, user_id)
                    if user:
                        # A timeout is transient — it self-heals on the next scan.
                        user.m365_error_reason = REASON_TRANSIENT
                        scan_db.commit()
                except sqlalchemy.exc.SQLAlchemyError:
                    scan_db.rollback()
                    logger.warning("Inbox scan timeout commit failed", exc_info=True)
            except Exception as e:
                logger.exception(f"Inbox scan error for user {user_id}: {e}")
                scan_db.rollback()
                try:
                    user = scan_db.get(User, user_id)
                    if user:
                        user.m365_error_reason = reason_for(e)
                        scan_db.commit()
                except sqlalchemy.exc.SQLAlchemyError:
                    scan_db.rollback()
            finally:
                scan_db.close()

    if users_to_scan:
        await asyncio.gather(*[_safe_scan(uid) for uid in users_to_scan])


@_traced_job
async def _job_batch_results():
    """Process pending AI batch results."""
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        from ..email_service import process_batch_results

        batch_applied = await asyncio.wait_for(process_batch_results(db), timeout=120)
        if batch_applied:
            logger.info(f"Batch processing: {batch_applied} results applied")
    except TimeoutError:
        logger.error("Batch results processing timed out (120s)")
        raise  # Re-raise so _traced_job / Sentry can capture
    except Exception as e:
        logger.exception(f"Batch results processing error: {e}")
        raise  # Re-raise so _traced_job / Sentry can capture
    finally:
        # process_batch_results handles its own commit/rollback per batch;
        # rollback here only cleans up any uncommitted leftovers safely
        try:
            db.rollback()
        except sqlalchemy.exc.SQLAlchemyError:
            logger.debug("Batch results cleanup rollback", exc_info=True)
        db.close()
