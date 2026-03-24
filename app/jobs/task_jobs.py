"""Selective auto-task scheduler — bid due alerts only.

Called by: app/jobs/__init__.py via register_task_jobs()
Depends on: app.models (Requisition), app.services.task_service

The other useful task triggers (new requirements, buy plan assignments,
email-parsed offers, new offers) fire inline from their respective
service/router hooks — not from the scheduler.  The scheduler only
handles the time-based check: requisitions approaching their deadline.

Design: Highly selective to avoid noise.
  - Bid due alerts: only for active requisitions with a parseable
    deadline within 2 days, capped at 20 tasks per run.
"""

from datetime import datetime, timedelta, timezone

from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from ..constants import RequisitionStatus
from ..scheduler import _traced_job

# Maximum tasks created per run to prevent noise
_BID_DUE_CAP = 20

# Active requisition statuses worth alerting on
_ACTIVE_REQ_STATUSES = {
    RequisitionStatus.ACTIVE,
    RequisitionStatus.SOURCING,
    RequisitionStatus.QUOTED,
    RequisitionStatus.OFFERS,
}


def register_task_jobs(scheduler, settings):
    """Register task auto-generation jobs with the scheduler."""
    scheduler.add_job(
        _job_bid_due_alerts,
        CronTrigger(hour=8, minute=0),  # Once daily at 8 AM
        id="bid_due_alerts",
        name="Create bid-due alert tasks for approaching deadlines",
    )


@_traced_job
async def _job_bid_due_alerts():
    """Create 'Bid due' tasks for requisitions with deadlines within 2 days.

    Selective filters:
    - Requisition has a parseable deadline (ISO date, not 'ASAP')
    - Deadline is within the next 2 days
    - Requisition is active (not archived/won/lost/completed)
    - No existing open bid-due task for this requisition
    """
    from ..database import SessionLocal
    from ..models.sourcing import Requisition
    from ..services.task_service import on_bid_due_soon

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(days=2)

        reqs = (
            db.query(Requisition)
            .filter(
                Requisition.status.in_(_ACTIVE_REQ_STATUSES),
                Requisition.deadline.isnot(None),
                Requisition.deadline != "",
                Requisition.deadline != "ASAP",
            )
            .limit(_BID_DUE_CAP * 3)  # Fetch extra; many may not parse or be in range
            .all()
        )

        created = 0
        for req in reqs:
            if created >= _BID_DUE_CAP:
                break
            try:
                deadline_dt = datetime.fromisoformat(req.deadline)
                if not deadline_dt.tzinfo:
                    deadline_dt = deadline_dt.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue  # Skip non-ISO deadlines

            if deadline_dt > horizon or deadline_dt < now - timedelta(days=1):
                continue  # Not within 2 days or already past by > 1 day

            task = on_bid_due_soon(db, req.id, req.deadline, req.name or "")
            if task:
                created += 1

        if created:
            logger.info("Bid-due alert tasks created: {}", created)
        else:
            logger.debug("Bid-due alerts job: no new tasks needed")
    except Exception as e:
        logger.error("Bid-due alert task job failed: {}", str(e))
        db.rollback()
        raise
    finally:
        db.close()
