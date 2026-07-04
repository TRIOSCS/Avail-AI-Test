"""Resell background jobs — auto-expire stale excess-list postings.

The nightly backstop for the Resell list lifecycle (M5): an ``open``/``collecting``
ExcessList whose ``close_at`` deadline has passed without being awarded or closed is
flipped to ``expired`` and its Sighting live-mirror retired, so a lapsed posting stops
advertising supply and drops out of the offerable ("Open to Me") lens.

Called by: app/jobs/__init__.py via register_resell_jobs()
Depends on: app.database (SessionLocal), app.services.excess_service (expire_overdue_lists),
    app.scheduler (_traced_job)
"""

import sqlalchemy.exc
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from ..scheduler import _traced_job


def register_resell_jobs(scheduler, settings):
    """Register the Resell lifecycle jobs with the scheduler."""
    scheduler.add_job(
        _job_expire_resell_lists,
        CronTrigger(hour=2, minute=15),
        id="expire_resell_lists",
        name="Expire past-close resell lists",
    )


@_traced_job
async def _job_expire_resell_lists():
    """Daily — flip unresolved excess lists past ``close_at`` to ``expired``.

    Delegates to ``excess_service.expire_overdue_lists`` (which also retires each expired
    list's Sighting mirror). Idempotent — already-resolved lists are skipped.
    """
    from ..database import SessionLocal
    from ..services.excess_service import expire_overdue_lists

    db = SessionLocal()
    try:
        expired = expire_overdue_lists(db)
        if expired:
            logger.info(f"Expired {expired} overdue excess list(s)")
    except sqlalchemy.exc.SQLAlchemyError as e:
        logger.error(f"Resell list expiry DB error: {e}")
        db.rollback()
    except Exception as e:
        logger.exception(f"Resell list expiry error: {e}")
        db.rollback()
    finally:
        db.close()
