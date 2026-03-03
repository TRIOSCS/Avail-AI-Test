"""API health monitoring background jobs.

Called by: app/jobs/__init__.py via register_health_jobs()
Depends on: app.database, app.models.config, app.services.health_monitor
"""

from datetime import datetime, timedelta, timezone

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from ..scheduler import _traced_job


def register_health_jobs(scheduler, settings):
    """Register API health monitoring jobs with the scheduler."""
    scheduler.add_job(_job_health_ping, IntervalTrigger(minutes=15), id="health_ping", name="API health ping")
    scheduler.add_job(_job_health_deep, IntervalTrigger(hours=2), id="health_deep", name="API deep health test")
    scheduler.add_job(
        _job_cleanup_usage_log, CronTrigger(day=1, hour=1), id="cleanup_usage_log", name="Cleanup old usage logs"
    )
    scheduler.add_job(
        _job_reset_monthly_usage,
        CronTrigger(day=1, hour=0, minute=5),
        id="reset_monthly_usage",
        name="Reset monthly API usage",
    )


@_traced_job
async def _job_health_ping():
    """Lightweight health check on all active API sources."""
    from ..services.health_monitor import run_health_checks

    await run_health_checks("ping")


@_traced_job
async def _job_health_deep():
    """Full functional test on all active API sources."""
    from ..services.health_monitor import run_health_checks

    await run_health_checks("deep")


@_traced_job
async def _job_cleanup_usage_log():
    """Delete usage log entries older than 90 days."""
    from ..database import SessionLocal
    from ..models.config import ApiUsageLog

    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=90)
        deleted = db.query(ApiUsageLog).filter(ApiUsageLog.timestamp < cutoff).delete()
        db.commit()
        if deleted:
            logger.info("Cleaned up {} old usage log entries", deleted)
    except Exception:
        logger.exception("Usage log cleanup failed")
        db.rollback()
    finally:
        db.close()


@_traced_job
async def _job_reset_monthly_usage():
    """Reset calls_this_month on the 1st of each month."""
    from ..database import SessionLocal
    from ..models.config import ApiSource

    db = SessionLocal()
    try:
        db.query(ApiSource).update({ApiSource.calls_this_month: 0})
        db.commit()
        logger.info("Reset monthly API usage counters")
    except Exception:
        logger.exception("Monthly usage reset failed")
        db.rollback()
    finally:
        db.close()
