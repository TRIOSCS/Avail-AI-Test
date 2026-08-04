"""AI quality scoring background job — PARKED since W1 simplification (2026-08-04).

The quality_score_activities registration (every 15 min, Claude Haiku scoring of
unscored non-email ActivityLog entries) was removed per docs/W1_JOB_DISPOSITION.md
(spec §5.4 park list — the Activity Scorecard is its only consumer). The job
implementation below stays. Comeback trigger: team exists (§5.4) — re-add the
``scheduler.add_job`` call then.

Called by: app/jobs/__init__.py (register_quality_jobs, now a no-op)
Depends on: app/services/activity_quality_service.py
"""

from loguru import logger

from ..scheduler import _traced_job


def register_quality_jobs(scheduler, settings):
    """Register AI quality scoring jobs — none since W1 (parked, no-op)."""


@_traced_job
async def _job_score_activities():
    """PARKED (not registered) — batch score unscored ActivityLog entries."""
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        from ..services.activity_quality_service import score_unscored_activities

        scored = await score_unscored_activities(db, batch_size=50)
        if scored:
            logger.info(f"Quality job: scored {scored} activities")
    except Exception as e:
        logger.exception(f"Quality scoring job failed: {e}")
        db.rollback()
        raise
    finally:
        db.close()
