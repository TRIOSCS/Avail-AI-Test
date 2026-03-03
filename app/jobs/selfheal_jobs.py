"""Self-heal pipeline background jobs — weekly report, auto-close stale tickets.

Called by: app/jobs/__init__.py via register_selfheal_jobs()
Depends on: app.database, app.models.trouble_ticket, app.services.pattern_tracker,
            app.services.trouble_ticket_service, app.services.notification_service
"""

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from ..scheduler import _traced_job


def register_selfheal_jobs(scheduler, settings):
    """Register self-heal pipeline jobs with the scheduler."""
    scheduler.add_job(
        _job_self_heal_weekly_report,
        CronTrigger(day_of_week="mon", hour=8),
        id="self_heal_weekly_report",
        name="Self-heal weekly report",
    )
    scheduler.add_job(
        _job_self_heal_auto_close,
        IntervalTrigger(hours=6),
        id="self_heal_auto_close",
        name="Auto-close stale tickets",
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
            stats["tickets_created"], stats["tickets_resolved"],
            stats["success_rate"], stats["total_cost"],
        )
        if patterns:
            for p in patterns:
                logger.warning(
                    "Recurring pattern: {} on {} ({} occurrences)",
                    p["category"], p["page"], p["count"],
                )
    except Exception:
        logger.exception("Self-heal weekly report failed")
    finally:
        db.close()


@_traced_job
async def _job_self_heal_auto_close():  # pragma: no cover
    """Auto-close stale tickets: 7d open with no progress → resolved."""
    from datetime import datetime, timedelta, timezone

    from ..database import SessionLocal
    from ..models.trouble_ticket import TroubleTicket
    from ..services.notification_service import create_notification
    from ..services.trouble_ticket_service import update_ticket

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)

        # 7d open with no progress → auto-resolved
        stale = (
            db.query(TroubleTicket)
            .filter(
                TroubleTicket.status == "open",
                TroubleTicket.created_at < now - timedelta(days=7),
            )
            .all()
        )
        for ticket in stale:
            update_ticket(db, ticket.id, status="resolved",
                          resolution_notes="Auto-resolved: no activity after 7 days")
            if ticket.submitted_by:
                create_notification(
                    db, user_id=ticket.submitted_by, event_type="fixed",
                    title=f"Ticket #{ticket.id} auto-resolved",
                    body="No activity after 7 days — ticket closed.",
                    ticket_id=ticket.id,
                )

        if stale:
            logger.info("Auto-closed {} stale tickets", len(stale))
    except Exception:
        logger.exception("Self-heal auto-close failed")
    finally:
        db.close()
