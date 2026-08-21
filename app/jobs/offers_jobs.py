"""Offers background jobs — proactive matching, offer expiry, stale flagging, scoring.

Called by: app/jobs/__init__.py via register_offers_jobs()
Depends on: app.database, app.models, app.services.proactive_matching, app.services.avail_score_service
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast

import sqlalchemy.exc
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from ..constants import ActivityType, OfferStatus, ProactiveMatchStatus, ProactiveOfferStatus
from ..scheduler import _traced_job
from ..utils.timezones import as_utc


def register_offers_jobs(scheduler, settings, db=None):
    """Register offer-related jobs with the scheduler.

    *db* (when provided) lets proactive_matching_enabled resolve from the system_config
    DB row (admin toggle) instead of only the env default.
    """
    from ..services.admin_service import get_effective_flag

    if get_effective_flag(db, "proactive_matching_enabled", settings.proactive_matching_enabled):
        interval_h = max(1, settings.proactive_scan_interval_hours)
        scheduler.add_job(
            _job_proactive_matching,
            IntervalTrigger(hours=interval_h),
            id="proactive_matching",
            name="Proactive matching",
        )

    # `is True` (not truthiness): register only on an explicit True so a MagicMock
    # settings passed by unrelated scheduler tests resolves to off, not a spurious job.
    if get_effective_flag(db, "proactive_teams_push_enabled", settings.proactive_teams_push_enabled) is True:
        push_interval_h = max(1, settings.proactive_scan_interval_hours)
        scheduler.add_job(
            _job_proactive_teams_push,
            IntervalTrigger(hours=push_interval_h),
            id="proactive_teams_push",
            name="Push new proactive matches to Teams",
        )

    if get_effective_flag(db, "proactive_matching_enabled", settings.proactive_matching_enabled):
        # Weekly digest DRAFTS only — a human reviews and sends from the Digests
        # tab; this job never emails anyone.
        scheduler.add_job(
            _job_proactive_digest_drafts,
            CronTrigger(day_of_week="mon", hour=7, minute=0),
            id="proactive_digest_drafts",
            name="Generate proactive digest drafts",
        )

    scheduler.add_job(
        _job_performance_tracking, IntervalTrigger(hours=12), id="performance_tracking", name="Scoring and leaderboards"
    )

    scheduler.add_job(
        _job_proactive_offer_expiry,
        CronTrigger(hour=4, minute=30),
        id="proactive_offer_expiry",
        name="Expire stale proactive offers",
    )

    scheduler.add_job(
        _job_flag_stale_offers, CronTrigger(hour=5, minute=0), id="flag_stale_offers", name="Flag stale offers (14d+)"
    )

    scheduler.add_job(
        _job_expire_strategic_vendors,
        CronTrigger(hour=6, minute=0),
        id="expire_strategic_vendors",
        name="Expire strategic vendors (39d TTL)",
    )

    scheduler.add_job(
        _job_warn_strategic_expiring,
        CronTrigger(hour=8, minute=0),
        id="warn_strategic_expiring",
        name="Warn strategic vendors expiring soon",
    )


@_traced_job
async def _job_proactive_digest_drafts():
    """Weekly: build/replace DRAFT digests per salesperson. Never sends anything."""
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        from ..services.proactive_digest import generate_digests

        loop = asyncio.get_running_loop()
        stats = await asyncio.wait_for(loop.run_in_executor(None, generate_digests, db), timeout=300)
        db.commit()
        logger.info(
            "Proactive digest drafts: {} generated from {} matches",
            stats.get("digests_generated", 0),
            stats.get("matches_considered", 0),
        )
    except Exception as e:
        logger.exception(f"Proactive digest draft generation error: {e}")
        db.rollback()
        raise
    finally:
        db.close()


@_traced_job
async def _job_proactive_matching():
    """Scan new offers for proactive matching (requirement history + hotlists)."""
    from ..database import SessionLocal

    def _run_all_matching() -> tuple[dict, int, int]:
        """All DB work in ONE worker thread with ONE Session it fully owns (QC
        2026-08-14).

        Previously the job created the Session and handed it to run_in_executor, then
        rolled back / closed it in the timeout & except branches — while the executor
        thread was STILL mid-query on it (a ``wait_for`` timeout cancels the await but
        cannot stop the running thread). Owning the Session inside the thread removes
        that cross-thread corruption; the outer timeout simply abandons the future and
        the thread cleans up its own Session in ``finally``.
        """
        from ..models import ProactiveMatch
        from ..services.part_equivalence import classify_new_pairs, windowed_spellings_by_key
        from ..services.proactive_matching import expire_old_matches, run_proactive_scan

        db = SessionLocal()
        try:
            # Classify new part-equivalence candidate pairs FIRST so the scan pools
            # freshly-judged variants (verdicts cached forever; empty pass = no-op).
            try:
                spellings = windowed_spellings_by_key(db)
                asyncio.run(asyncio.wait_for(classify_new_pairs(db, spellings), timeout=120))
            except Exception as eq_exc:  # classifier down ≠ matching broken (same thread → safe rollback)
                logger.warning("Part-equivalence classification pass failed: {}", eq_exc)
                db.rollback()

            scan_result = run_proactive_scan(db)
            expired = expire_old_matches(db)
            total_pending = db.query(ProactiveMatch).filter(ProactiveMatch.status == ProactiveMatchStatus.NEW).count()
            return scan_result, expired, total_pending
        finally:
            db.close()

    loop = asyncio.get_running_loop()
    try:
        scan_result, expired, total_pending = await asyncio.wait_for(
            loop.run_in_executor(None, _run_all_matching), timeout=300
        )
    except TimeoutError:
        # The worker thread owns + closes its own Session — nothing to roll back/close here.
        logger.error("Proactive matching timed out after 300s")
        raise

    if scan_result.get("matches_created"):
        logger.info(
            f"Proactive matching: {scan_result['matches_created']} new matches "
            f"from {scan_result['scanned_offers']} offers"
        )
    if expired:
        logger.info(f"Proactive matching: expired {expired} old matches")
    logger.info(
        f"Proactive scan complete: {scan_result.get('matches_created', 0)} new matches, {total_pending} pending"
    )


@_traced_job
async def _job_proactive_teams_push():
    """Push not-yet-pushed NEW proactive matches to Teams as an Adaptive Card digest.

    Flag-gated (proactive_teams_push_enabled, default off). Idempotent via a
    SystemConfig watermark, so it is safe to run every scan cycle.
    """
    from ..database import SessionLocal
    from ..services.proactive_teams_push import push_new_matches_to_teams

    db = SessionLocal()
    try:
        result = await push_new_matches_to_teams(db)
        if result["pushed"]:
            logger.info(f"Proactive Teams push: delivered {result['pushed']} new match(es)")
    except Exception as e:
        logger.exception(f"Proactive Teams push error: {e}")
        db.rollback()
    finally:
        db.close()


@_traced_job
async def _job_performance_tracking():
    """Compute vendor scorecards, buyer leaderboard, Avail Scores, and other scoring
    metrics."""
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        now = datetime.now(UTC)
        from ..services.avail_score_service import compute_all_avail_scores
        from ..services.buyer_leaderboard import compute_buyer_leaderboard
        from ..services.vendor_scorecard import compute_all_vendor_scorecards

        loop = asyncio.get_running_loop()

        async def _run(fn, *args, timeout):
            return await asyncio.wait_for(loop.run_in_executor(None, fn, db, *args), timeout=timeout)

        # Vendor scorecards
        vs_result = await _run(compute_all_vendor_scorecards, timeout=600)
        logger.info(f"Vendor scorecards: {vs_result['updated']} updated, {vs_result['skipped_cold_start']} cold-start")
        current_month = now.date().replace(day=1)
        # Buyer leaderboard
        bl_result = await _run(compute_buyer_leaderboard, current_month, timeout=300)
        logger.info(f"Buyer leaderboard: {bl_result['entries']} entries for {current_month}")
        # Avail Scores
        as_result = await _run(compute_all_avail_scores, current_month, timeout=300)
        logger.info(
            f"Avail Scores: {as_result['buyers']} buyers, "
            f"{as_result['sales']} sales, {as_result['saved']} saved for {current_month}"
        )
        # Multiplier Scores
        from ..services.multiplier_score_service import compute_all_multiplier_scores

        ms_result = await _run(compute_all_multiplier_scores, current_month, timeout=300)
        logger.info(
            f"Multiplier Scores: {ms_result['buyers']} buyers, "
            f"{ms_result['sales']} sales, {ms_result['saved']} saved for {current_month}"
        )
        # Unified Scores (cross-role leaderboard)
        from ..services.unified_score_service import compute_all_unified_scores

        us_result = await _run(compute_all_unified_scores, current_month, timeout=300)
        logger.info(f"Unified Scores: {us_result['computed']} computed, {us_result['saved']} saved for {current_month}")
        # Recompute previous month during grace period (first 7 days)
        if now.day <= 7:
            prev_month = (current_month - timedelta(days=1)).replace(day=1)
            await _run(compute_buyer_leaderboard, prev_month, timeout=300)
            await _run(compute_all_avail_scores, prev_month, timeout=300)
            await _run(compute_all_multiplier_scores, prev_month, timeout=300)
            await _run(compute_all_unified_scores, prev_month, timeout=300)
    except TimeoutError:
        logger.error("Performance tracking timed out")
        db.rollback()
    except Exception as e:
        logger.exception(f"Performance tracking error: {e}")
        db.rollback()
    finally:
        db.close()


@_traced_job
async def _job_proactive_offer_expiry():
    """Daily — expire proactive offers with status='sent' that are older than 14 days.

    Proactive offers that never got a customer response should not linger indefinitely.
    After 14 days, mark them as 'expired' so they don't clutter the active pipeline.
    """
    from ..database import SessionLocal
    from ..models.intelligence import ProactiveOffer

    db = SessionLocal()
    try:
        cutoff = datetime.now(UTC) - timedelta(days=14)
        expired_count = (
            db.query(ProactiveOffer)
            .filter(
                ProactiveOffer.status == ProactiveOfferStatus.SENT,
                ProactiveOffer.sent_at < cutoff,
            )
            .update({"status": ProactiveOfferStatus.EXPIRED}, synchronize_session="fetch")
        )
        if expired_count:
            db.commit()
            logger.info(f"Expired {expired_count} stale proactive offer(s)")
    except sqlalchemy.exc.SQLAlchemyError as e:
        logger.error(f"Proactive offer expiry DB error: {e}")
        db.rollback()
    except Exception as e:
        logger.exception(f"Proactive offer expiry error: {e}")
        db.rollback()
    finally:
        db.close()


@_traced_job
async def _job_flag_stale_offers():
    """Daily — flag active offers older than 14 days as is_stale.

    Display-only metadata. Stale offers remain fully visible everywhere. "Leave no stone
    unturned" — we never hide or filter by is_stale.
    """
    from ..database import SessionLocal
    from ..models.offers import Offer

    db = SessionLocal()
    try:
        cutoff = datetime.now(UTC) - timedelta(days=14)
        flagged = (
            db.query(Offer)
            .filter(
                Offer.status == OfferStatus.ACTIVE,
                Offer.is_stale.is_(False),
                Offer.created_at < cutoff,
            )
            .update({"is_stale": True}, synchronize_session="fetch")
        )
        if flagged:
            db.commit()
            logger.info(f"Flagged {flagged} offer(s) as stale (14d+)")
    except sqlalchemy.exc.SQLAlchemyError as e:
        logger.error(f"Offer stale flagging DB error: {e}")
        db.rollback()
    except Exception as e:
        logger.exception(f"Offer stale flagging error: {e}")
        db.rollback()
    finally:
        db.close()


@_traced_job
async def _job_expire_strategic_vendors():
    """Daily 6 AM — expire strategic vendors past their 39-day TTL."""
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        from ..services.strategic_vendor_service import expire_stale

        count = expire_stale(db)
        if count:
            logger.info(f"Expired {count} strategic vendor assignment(s)")
    except sqlalchemy.exc.SQLAlchemyError as e:
        logger.error(f"Strategic vendor expiry DB error: {e}")
        db.rollback()
    except Exception as e:
        logger.exception(f"Strategic vendor expiry error: {e}")
        db.rollback()
    finally:
        db.close()


@_traced_job
async def _job_warn_strategic_expiring():
    """Daily 8 AM — notify buyers whose strategic vendors expire within 7 days."""
    from ..database import SessionLocal
    from ..models.intelligence import ActivityLog

    db = SessionLocal()
    try:
        from ..services.strategic_vendor_service import get_expiring_soon

        expiring = get_expiring_soon(db, days=7)
        for sv in expiring:
            now = datetime.now(UTC)
            expires = as_utc(cast("datetime", sv.expires_at))
            days_left = max(0, (expires - now).days)
            vendor_name = sv.vendor_card.display_name if sv.vendor_card else "Unknown"

            # Dedup: only one warning per strategic vendor assignment
            existing = (
                db.query(ActivityLog.id)
                .filter(
                    ActivityLog.user_id == sv.user_id,
                    ActivityLog.activity_type == ActivityType.STRATEGIC_VENDOR_EXPIRING,
                    ActivityLog.external_id == str(sv.id),
                    ActivityLog.dismissed_at.is_(None),
                )
                .first()
            )
            if existing:
                continue

            db.add(
                ActivityLog(
                    user_id=sv.user_id,
                    activity_type=ActivityType.STRATEGIC_VENDOR_EXPIRING,
                    channel="system",
                    contact_name=vendor_name,
                    subject=f"Strategic vendor {vendor_name} expires in {days_left} days — get an offer to keep them",
                    external_id=str(sv.id),
                )
            )
        db.commit()
        if expiring:
            logger.info(f"Warned about {len(expiring)} strategic vendor(s) expiring soon")
    except sqlalchemy.exc.SQLAlchemyError as e:
        logger.error(f"Strategic vendor warning DB error: {e}")
        db.rollback()
    except Exception as e:
        logger.exception(f"Strategic vendor warning error: {e}")
        db.rollback()
    finally:
        db.close()
