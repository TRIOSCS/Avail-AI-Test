"""Offers background jobs — proactive matching pair (parked) + performance tracking
(parked).

Wave-1 simplification (docs/W1_JOB_DISPOSITION.md):
- proactive_matching: PARKED — registration removed (its flag defaulted ON);
  implementation kept. Comeback: Proactive workspace revival / Deals-merge
  badge wiring (spec §4, Wave 4).
- proactive_teams_push: PARKED — registration stays but only fires on an
  explicit proactive_teams_push_enabled=True (defaults False). Same comeback.
- performance_tracking: PARKED — registration removed; implementation kept.
  Comeback: team exists (spec §5.4).
- proactive_offer_expiry, flag_stale_offers, expire_strategic_vendors,
  warn_strategic_expiring: DELETED (git restores).

Called by: app/jobs/__init__.py via register_offers_jobs()
Depends on: app.database, app.models, app.services.proactive_matching,
app.services.proactive_teams_push, app.services.avail_score_service
"""

import asyncio
from datetime import UTC, datetime, timedelta

from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from ..constants import ProactiveMatchStatus
from ..scheduler import _traced_job


def register_offers_jobs(scheduler, settings, db=None):
    """Register offer-related jobs with the scheduler.

    Only the proactive Teams push remains registrable, behind its existing flag
    (defaults off). *db* (when provided) lets the flag resolve from the system_config DB
    row (admin toggle) instead of only the env default.
    """
    from ..services.admin_service import get_effective_flag

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


@_traced_job
async def _job_proactive_matching():
    """Scan new offers/sightings for proactive matching via CPH + archived reqs.

    PARKED (not registered) — kept for the Proactive workspace comeback.
    """
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        from ..models import ProactiveMatch
        from ..services.proactive_matching import expire_old_matches, run_proactive_scan

        loop = asyncio.get_running_loop()

        # CPH-based scan (purchase history)
        scan_result = await asyncio.wait_for(
            loop.run_in_executor(None, run_proactive_scan, db),
            timeout=300,
        )
        if scan_result.get("matches_created"):
            logger.info(
                f"Proactive matching: {scan_result['matches_created']} new matches "
                f"from {scan_result['scanned_offers']} offers"
            )

        # Expire stale matches
        expired = await loop.run_in_executor(None, expire_old_matches, db)
        if expired:
            logger.info(f"Proactive matching: expired {expired} old matches")

        # Summary log with total pending
        new_matches = scan_result.get("matches_created", 0)
        total_pending = db.query(ProactiveMatch).filter(ProactiveMatch.status == ProactiveMatchStatus.NEW).count()
        logger.info(f"Proactive scan complete: {new_matches} new matches, {total_pending} pending")
    except TimeoutError:
        logger.error("Proactive matching timed out after 300s")
        db.rollback()
        raise
    except Exception as e:
        logger.exception(f"Proactive matching error: {e}")
        db.rollback()
        raise
    finally:
        db.close()


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
    metrics.

    PARKED (not registered) — kept for the team-exists comeback (spec §5.4).
    """
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
