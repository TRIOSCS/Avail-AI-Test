"""Self-heal pipeline background jobs — weekly report, ticket consolidation, retry stuck tickets.

Called by: app/jobs/__init__.py via register_selfheal_jobs()
Depends on: app.database, app.services.pattern_tracker,
            app.services.ticket_consolidation, app.services.execution_service,
            app.services.diagnosis_service
"""

from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from ..config import settings
from ..scheduler import _traced_job

MAX_RETRY_BATCH = 10


def register_selfheal_jobs(scheduler, settings):
    """Register self-heal pipeline jobs with the scheduler."""
    scheduler.add_job(
        _job_self_heal_weekly_report,
        CronTrigger(day_of_week="mon", hour=8),
        id="self_heal_weekly_report",
        name="Self-heal weekly report",
    )
    scheduler.add_job(
        _job_consolidate_tickets,
        CronTrigger(hour=5),
        id="consolidate_tickets",
        name="Daily ticket consolidation sweep",
    )
    scheduler.add_job(
        _job_retry_stuck_diagnosed,
        CronTrigger(hour="*/4"),
        id="retry_stuck_diagnosed",
        name="Retry stuck diagnosed tickets",
    )


@_traced_job
async def _job_self_heal_weekly_report():  # pragma: no cover
    """Generate weekly self-heal pipeline report and log stats."""
    from ..database import SessionLocal
    from ..services.pattern_tracker import detect_recurring_patterns, get_weekly_stats

    db = SessionLocal()
    try:
        stats = get_weekly_stats(db, weeks_back=1)
        patterns = detect_recurring_patterns(db, min_occurrences=3)
        logger.info(
            "Self-heal weekly report: {} created, {} resolved, {:.0f}% success, ${:.2f} cost",
            stats["tickets_created"],
            stats["tickets_resolved"],
            stats["success_rate"],
            stats["total_cost"],
        )
        if patterns:
            for p in patterns:
                logger.warning(
                    "Recurring pattern: {} on {} ({} occurrences)",
                    p["category"],
                    p["page"],
                    p["count"],
                )
    except Exception:
        logger.exception("Self-heal weekly report failed")
    finally:
        db.close()


@_traced_job
async def _job_consolidate_tickets():  # pragma: no cover
    """Daily sweep: find and link duplicate open tickets."""
    from ..database import SessionLocal
    from ..services.ticket_consolidation import batch_consolidate

    db = SessionLocal()
    try:
        linked = await batch_consolidate(db)
        if linked:
            logger.info("Daily consolidation: linked {} duplicate tickets", linked)
    except Exception:
        logger.exception("Daily ticket consolidation failed")
    finally:
        db.close()
