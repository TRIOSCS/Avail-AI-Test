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


async def retry_stuck_diagnosed(db) -> dict:
    """Re-process tickets stuck in 'diagnosed' status.

    - Tickets with detailed=None get full re-diagnosis first.
    - Tickets with good diagnosis get execute_fix retried.
    - Only low/medium risk tickets are retried.
    - Skips tickets where iterations_used >= max_iterations.
    - Processes at most MAX_RETRY_BATCH tickets per run.

    Returns: {retried, rediagnosed, succeeded, failed}
    """
    from ..models.trouble_ticket import TroubleTicket
    from ..services.diagnosis_service import diagnose_full
    from ..services.execution_service import execute_fix

    if not settings.self_heal_enabled:
        logger.debug("Self-heal disabled, skipping stuck ticket retry")
        return {"retried": 0, "rediagnosed": 0, "succeeded": 0, "failed": 0}

    candidates = (
        db.query(TroubleTicket)
        .filter(
            TroubleTicket.status == "diagnosed",
            TroubleTicket.risk_tier.in_(["low", "medium"]),
        )
        .order_by(TroubleTicket.created_at.asc())
        .limit(MAX_RETRY_BATCH)
        .all()
    )

    retried = 0
    rediagnosed = 0
    succeeded = 0
    failed = 0

    for ticket in candidates:
        iterations = ticket.iterations_used or 0
        max_iter = (
            settings.self_heal_max_iterations_low
            if ticket.risk_tier == "low"
            else settings.self_heal_max_iterations_medium
        )

        if iterations >= max_iter:
            logger.debug(
                "Ticket {} skipped: iterations {}/{} exhausted",
                ticket.id, iterations, max_iter,
            )
            continue

        diagnosis = ticket.diagnosis or {}
        detailed = diagnosis.get("detailed") if isinstance(diagnosis, dict) else None

        if not detailed:
            logger.info("Ticket {} missing detailed diagnosis, re-diagnosing", ticket.id)
            diag_result = await diagnose_full(ticket.id, db)
            if "error" in diag_result:
                logger.warning("Re-diagnosis failed for ticket {}: {}", ticket.id, diag_result["error"])
                failed += 1
                continue
            rediagnosed += 1

        logger.info("Retrying execute_fix for ticket {}", ticket.id)
        exec_result = await execute_fix(ticket.id, db)
        retried += 1

        if exec_result.get("ok"):
            succeeded += 1
            logger.info("Ticket {} retry succeeded", ticket.id)
        else:
            failed += 1
            logger.warning("Ticket {} retry failed: {}", ticket.id, exec_result.get("error", "unknown"))

    logger.info(
        "Stuck ticket retry complete: {} retried, {} rediagnosed, {} succeeded, {} failed",
        retried, rediagnosed, succeeded, failed,
    )
    return {"retried": retried, "rediagnosed": rediagnosed, "succeeded": succeeded, "failed": failed}


@_traced_job
async def _job_retry_stuck_diagnosed():  # pragma: no cover
    """Scheduled wrapper: retry stuck diagnosed tickets."""
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        await retry_stuck_diagnosed(db)
    except Exception:
        logger.exception("Stuck ticket retry job failed")
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
