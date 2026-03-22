"""Core background jobs — archive, token refresh, inbox scan, batch results, webhooks.

Called by: app/jobs/__init__.py via register_core_jobs()
Depends on: app.database, app.models, app.email_service, app.services.webhook_service
"""

import asyncio
from datetime import datetime, timedelta, timezone

from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from ..scheduler import _traced_job
from ..utils.token_manager import _utc


def register_core_jobs(scheduler, settings):
    """Register core jobs with the scheduler."""
    scheduler.add_job(
        _job_auto_archive, IntervalTrigger(minutes=5), id="auto_archive", name="Auto-archive stale requisitions"
    )
    scheduler.add_job(_job_token_refresh, IntervalTrigger(minutes=5), id="token_refresh", name="Token refresh")
    scheduler.add_job(
        _job_inbox_scan, IntervalTrigger(minutes=settings.inbox_scan_interval_min), id="inbox_scan", name="Inbox scan"
    )
    scheduler.add_job(_job_batch_results, IntervalTrigger(minutes=5), id="batch_results", name="Process batch results")
    # Material enrichment jobs disabled — AI-only enrichment produces hallucinated
    # data. Rebuild with real connector data before re-enabling.
    # scheduler.add_job(
    #     _job_batch_enrich_materials,
    #     IntervalTrigger(minutes=30),
    #     id="batch_enrich_materials",
    #     name="Batch enrich materials",
    # )
    # scheduler.add_job(
    #     _job_poll_material_batch,
    #     IntervalTrigger(minutes=5),
    #     id="poll_material_batch",
    #     name="Poll material batch results",
    # )
    scheduler.add_job(
        _job_batch_parse_signatures,
        IntervalTrigger(minutes=10),
        id="batch_parse_signatures",
        name="Batch parse signatures",
    )
    scheduler.add_job(
        _job_poll_signature_batch,
        IntervalTrigger(minutes=5),
        id="poll_signature_batch",
        name="Poll signature batch results",
    )
    if settings.activity_tracking_enabled:
        scheduler.add_job(
            _job_webhook_subscriptions, IntervalTrigger(minutes=5), id="webhook_subs", name="Webhook subscriptions"
        )


@_traced_job
async def _job_auto_archive():
    """Auto-archive stale requisitions (no activity for 30 days)."""
    from ..database import SessionLocal
    from ..models import Requisition

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=30)
        archived_count = (
            db.query(Requisition)
            .filter(
                Requisition.status == "active",
                Requisition.last_searched_at.isnot(None),
                Requisition.last_searched_at < cutoff,
            )
            .update({"status": "archived"}, synchronize_session="fetch")
        )
        if archived_count:
            db.commit()
            logger.info(f"Auto-archived {archived_count} stale requisition(s)")
    except Exception as e:
        logger.error(f"Auto-archive error: {e}")
        db.rollback()
    finally:
        db.close()


@_traced_job
async def _job_token_refresh():
    """Refresh tokens for all users with refresh tokens."""
    from ..database import SessionLocal
    from ..models import User
    from ..utils.token_manager import refresh_user_token

    selector_db = SessionLocal()
    users_to_refresh: list[int] = []
    try:
        now = datetime.now(timezone.utc)
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
    except Exception as e:
        logger.error(f"Token refresh job error: {e}")
        return
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
            try:
                user = task_db.get(User, user_id)
                if not user:
                    return
                if r:
                    acquired = r.set(lock_key, "1", nx=True, ex=60)
                    if not acquired:
                        logger.debug("Token refresh skipped for %s — lock held", user.email)
                        return
                await refresh_user_token(user, task_db)
            except Exception as e:
                logger.error(f"Token refresh error for user {user_id}: {e}")
            finally:
                if r:
                    try:
                        r.delete(lock_key)
                    except Exception:
                        pass
                task_db.close()

    if users_to_refresh:
        await asyncio.gather(*[_safe_refresh(uid) for uid in users_to_refresh])


@_traced_job
async def _job_inbox_scan():
    """Scan inboxes for all connected users."""
    from ..config import settings
    from ..database import SessionLocal
    from ..models import User
    from .email_jobs import _scan_user_inbox

    # Use a short-lived session just to identify users that need scanning
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        users = db.query(User).filter(User.refresh_token.isnot(None)).all()
        scan_interval = timedelta(minutes=settings.inbox_scan_interval_min)

        users_to_scan = []
        for user in users:
            if not user.access_token or not user.m365_connected:
                continue
            should_scan = False
            if not user.last_inbox_scan:
                should_scan = True
            elif now - _utc(user.last_inbox_scan) > scan_interval:
                should_scan = True
            if should_scan:
                # Detach user data we need so we can close this session
                users_to_scan.append(user.id)
    except Exception as e:
        logger.error(f"Inbox scan job error: {e}")
        return
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
            except asyncio.TimeoutError:
                logger.error(f"Inbox scan TIMEOUT for user {user_id} (90s) — skipping")
                scan_db.rollback()
                try:
                    user = scan_db.get(User, user_id)
                    if user:
                        user.m365_error_reason = "Inbox scan timed out"
                        scan_db.commit()
                except Exception:
                    scan_db.rollback()
            except Exception as e:
                logger.error(f"Inbox scan error for user {user_id}: {e}")
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
    except asyncio.TimeoutError:
        logger.error("Batch results processing timed out (120s)")
        db.rollback()
    except Exception as e:
        logger.error(f"Batch results processing error: {e}")
        db.rollback()
    finally:
        db.close()


@_traced_job
async def _job_batch_enrich_materials():
    """Submit unenriched material cards to Claude Batch API (runs every 30 min)."""
    from ..database import SessionLocal
    from ..services.material_enrichment_service import batch_enrich_materials

    db = SessionLocal()
    try:
        batch_id = await asyncio.wait_for(batch_enrich_materials(db), timeout=120)
        if batch_id:
            logger.info(f"Material batch enrich submitted: {batch_id}")
    except asyncio.TimeoutError:
        logger.error("Material batch enrich timed out (120s)")
        db.rollback()
    except Exception as e:
        logger.error(f"Material batch enrich error: {e}")
        db.rollback()
    finally:
        db.close()


@_traced_job
async def _job_poll_material_batch():
    """Poll and apply material batch enrichment results (runs every 5 min)."""
    from ..database import SessionLocal
    from ..services.material_enrichment_service import process_material_batch_results

    db = SessionLocal()
    try:
        result = await asyncio.wait_for(process_material_batch_results(db), timeout=120)
        if result is not None:
            logger.info(f"Material batch results: {result['applied']} applied, {result['errors']} errors")
    except asyncio.TimeoutError:
        logger.error("Material batch poll timed out (120s)")
        db.rollback()
    except Exception as e:
        logger.error(f"Material batch poll error: {e}")
        db.rollback()
    finally:
        db.close()


@_traced_job
async def _job_batch_parse_signatures():
    """Submit low-confidence regex-parsed signatures to Claude Batch API (runs every 10
    min)."""
    from ..database import SessionLocal
    from ..services.signature_parser import batch_parse_signatures

    db = SessionLocal()
    try:
        batch_id = await asyncio.wait_for(batch_parse_signatures(db), timeout=120)
        if batch_id:
            logger.info(f"Signature batch parse submitted: {batch_id}")
    except asyncio.TimeoutError:
        logger.error("Signature batch parse timed out (120s)")
        db.rollback()
    except Exception as e:
        logger.error(f"Signature batch parse error: {e}")
        db.rollback()
    finally:
        db.close()


@_traced_job
async def _job_poll_signature_batch():
    """Poll and apply signature batch parsing results (runs every 5 min)."""
    from ..database import SessionLocal
    from ..services.signature_parser import process_signature_batch_results

    db = SessionLocal()
    try:
        result = await asyncio.wait_for(process_signature_batch_results(db), timeout=120)
        if result is not None:
            logger.info(f"Signature batch results: {result['applied']} applied, {result['errors']} errors")
    except asyncio.TimeoutError:
        logger.error("Signature batch poll timed out (120s)")
        db.rollback()
    except Exception as e:
        logger.error(f"Signature batch poll error: {e}")
        db.rollback()
    finally:
        db.close()


@_traced_job
async def _job_webhook_subscriptions():
    """Manage Graph webhook subscriptions."""
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        from ..services.webhook_service import (
            ensure_all_users_subscribed,
            renew_expiring_subscriptions,
        )

        await renew_expiring_subscriptions(db)
        await ensure_all_users_subscribed(db)
    except Exception as e:
        logger.error(f"Webhook subscription error: {e}")
        db.rollback()
    finally:
        db.close()
