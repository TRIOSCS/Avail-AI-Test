"""AI quality scoring background job.

Scores unscored non-email ActivityLog entries every 15 minutes
using Claude Haiku via the activity_quality_service.

Called by: app/jobs/__init__.py (registered with APScheduler)
Depends on: app/services/activity_quality_service.py
"""

from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from ..scheduler import _traced_job


def register_quality_jobs(scheduler, settings):
    """Register AI quality scoring jobs."""
    scheduler.add_job(
        _job_score_activities,
        IntervalTrigger(minutes=15),
        id="quality_score_activities",
        name="AI quality score unscored activities",
    )


@_traced_job
async def _job_score_activities():
    """Batch score unscored ActivityLog entries."""
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
