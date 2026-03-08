"""Offers background jobs — proactive matching, offer expiry, stale flagging, performance.

Called by: app/jobs/__init__.py via register_offers_jobs()
Depends on: app.database, app.models, app.services.proactive_matching, app.services.performance_service
"""

import asyncio
from datetime import datetime, timedelta, timezone

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from ..scheduler import _traced_job


def register_offers_jobs(scheduler, settings):
    """Register offer-related jobs with the scheduler."""
    if settings.proactive_matching_enabled:
        interval_h = max(1, settings.proactive_scan_interval_hours)
        scheduler.add_job(
            _job_proactive_matching,
            IntervalTrigger(hours=interval_h),
            id="proactive_matching",
            name="Proactive matching",
        )

    scheduler.add_job(
        _job_performance_tracking, IntervalTrigger(hours=12), id="performance_tracking", name="Performance tracking"
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
async def _job_proactive_matching():
    """Scan new offers/sightings for proactive matching via CPH + archived reqs."""
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        from ..models import ProactiveMatch
        from ..services.proactive_matching import expire_old_matches, run_proactive_scan
        from ..services.proactive_service import scan_new_offers_for_matches

        loop = asyncio.get_running_loop()

        # Legacy scan (archived requisitions)
        result = await asyncio.wait_for(
            loop.run_in_executor(None, scan_new_offers_for_matches, db),
            timeout=300,
        )
        if result.get("matches_created"):
            logger.info(
                f"Proactive matching (legacy): {result['matches_created']} new matches from {result['scanned']} offers"
            )

        # CPH-based scan (purchase history)
        cph_result = await asyncio.wait_for(
            loop.run_in_executor(None, run_proactive_scan, db),
            timeout=300,
        )
        if cph_result.get("matches_created"):
            logger.info(
                f"Proactive matching (CPH): {cph_result['matches_created']} new matches "
                f"from {cph_result['scanned_offers']} offers + {cph_result['scanned_sightings']} sightings"
            )

        # Expire stale matches
        expired = await loop.run_in_executor(None, expire_old_matches, db)
        if expired:
            logger.info(f"Proactive matching: expired {expired} old matches")

        # Summary log with total pending
        new_matches = result.get("matches_created", 0) + cph_result.get("matches_created", 0)
        total_pending = db.query(ProactiveMatch).filter(ProactiveMatch.status == "new").count()
        logger.info(f"Proactive scan complete: {new_matches} new matches, {total_pending} pending")
    except asyncio.TimeoutError:
        logger.error("Proactive matching timed out after 300s")
        db.rollback()
    except Exception as e:
        logger.error(f"Proactive matching error: {e}")
        db.rollback()
    finally:
        db.close()


@_traced_job
async def _job_performance_tracking():
    """Compute vendor scorecards, buyer leaderboard, and Avail Scores."""
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        from ..services.avail_score_service import compute_all_avail_scores
        from ..services.performance_service import (
            compute_all_vendor_scorecards,
            compute_buyer_leaderboard,
        )

        loop = asyncio.get_running_loop()
        vs_result = await asyncio.wait_for(
            loop.run_in_executor(None, compute_all_vendor_scorecards, db),
            timeout=600,
        )
        logger.info(f"Vendor scorecards: {vs_result['updated']} updated, {vs_result['skipped_cold_start']} cold-start")
        current_month = now.date().replace(day=1)
        bl_result = await asyncio.wait_for(
            loop.run_in_executor(None, compute_buyer_leaderboard, db, current_month),
            timeout=300,
        )
        logger.info(f"Buyer leaderboard: {bl_result['entries']} entries for {current_month}")
        # Avail Scores
        as_result = await asyncio.wait_for(
            loop.run_in_executor(None, compute_all_avail_scores, db, current_month),
            timeout=300,
        )
        logger.info(
            f"Avail Scores: {as_result['buyers']} buyers, "
            f"{as_result['sales']} sales, {as_result['saved']} saved for {current_month}"
        )
        # Multiplier Scores
        from ..services.multiplier_score_service import compute_all_multiplier_scores

        ms_result = await asyncio.wait_for(
            loop.run_in_executor(None, compute_all_multiplier_scores, db, current_month),
            timeout=300,
        )
        logger.info(
            f"Multiplier Scores: {ms_result['buyers']} buyers, "
            f"{ms_result['sales']} sales, {ms_result['saved']} saved for {current_month}"
        )
        # Unified Scores (cross-role leaderboard)
        from ..services.unified_score_service import compute_all_unified_scores

        us_result = await asyncio.wait_for(
            loop.run_in_executor(None, compute_all_unified_scores, db, current_month),
            timeout=300,
        )
        logger.info(f"Unified Scores: {us_result['computed']} computed, {us_result['saved']} saved for {current_month}")
        # Recompute previous month during grace period (first 7 days)
        if now.day <= 7:
            prev_month = (current_month - timedelta(days=1)).replace(day=1)
            await asyncio.wait_for(
                loop.run_in_executor(None, compute_buyer_leaderboard, db, prev_month),
                timeout=300,
            )
            await asyncio.wait_for(
                loop.run_in_executor(None, compute_all_avail_scores, db, prev_month),
                timeout=300,
            )
            await asyncio.wait_for(
                loop.run_in_executor(None, compute_all_multiplier_scores, db, prev_month),
                timeout=300,
            )
            await asyncio.wait_for(
                loop.run_in_executor(None, compute_all_unified_scores, db, prev_month),
                timeout=300,
            )
    except asyncio.TimeoutError:
        logger.error("Performance tracking timed out")
        db.rollback()
    except Exception as e:
        logger.error(f"Performance tracking error: {e}")
        db.rollback()
    finally:
        db.close()


@_traced_job
async def _job_proactive_offer_expiry():
    """Daily — expire proactive offers with status='sent' that are older than 14 days.

    Proactive offers that never got a customer response should not linger
    indefinitely. After 14 days, mark them as 'expired' so they don't
    clutter the active pipeline.
    """
    from ..database import SessionLocal
    from ..models.intelligence import ProactiveOffer

    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=14)
        expired_count = (
            db.query(ProactiveOffer)
            .filter(
                ProactiveOffer.status == "sent",
                ProactiveOffer.sent_at < cutoff,
            )
            .update({"status": "expired"}, synchronize_session="fetch")
        )
        if expired_count:
            db.commit()
            logger.info(f"Expired {expired_count} stale proactive offer(s)")
    except Exception as e:
        logger.error(f"Proactive offer expiry error: {e}")
        db.rollback()
    finally:
        db.close()


@_traced_job
async def _job_flag_stale_offers():
    """Daily — flag active offers older than 14 days as is_stale.

    Display-only metadata. Stale offers remain fully visible everywhere.
    "Leave no stone unturned" — we never hide or filter by is_stale.
    """
    from ..database import SessionLocal
    from ..models.offers import Offer

    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=14)
        flagged = (
            db.query(Offer)
            .filter(
                Offer.status == "active",
                Offer.is_stale.is_(False),
                Offer.created_at < cutoff,
            )
            .update({"is_stale": True}, synchronize_session="fetch")
        )
        if flagged:
            db.commit()
            logger.info(f"Flagged {flagged} offer(s) as stale (14d+)")
    except Exception as e:
        logger.error(f"Offer stale flagging error: {e}")
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
    except Exception as e:
        logger.error(f"Strategic vendor expiry error: {e}")
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
            now = datetime.now(timezone.utc)
            expires = sv.expires_at
            if expires and expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            days_left = max(0, (expires - now).days)
            vendor_name = sv.vendor_card.display_name if sv.vendor_card else "Unknown"

            # Dedup: only one warning per strategic vendor assignment
            existing = (
                db.query(ActivityLog.id)
                .filter(
                    ActivityLog.user_id == sv.user_id,
                    ActivityLog.activity_type == "strategic_vendor_expiring",
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
                    activity_type="strategic_vendor_expiring",
                    channel="system",
                    contact_name=vendor_name,
                    subject=f"Strategic vendor {vendor_name} expires in {days_left} days — get an offer to keep them",
                    external_id=str(sv.id),
                )
            )
        db.commit()
        if expiring:
            logger.info(f"Warned about {len(expiring)} strategic vendor(s) expiring soon")
    except Exception as e:
        logger.error(f"Strategic vendor warning error: {e}")
        db.rollback()
    finally:
        db.close()
