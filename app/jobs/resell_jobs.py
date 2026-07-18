"""Resell background jobs — auto-expire stale excess-list postings + sweep stale sends.

Two nightly backstops for the Resell lifecycle:
  - M5 list-expiry: an ``open``/``collecting`` ExcessList whose ``close_at`` deadline has
    passed without being awarded or closed is flipped to ``expired`` and its Sighting
    live-mirror retired, so a lapsed posting stops advertising supply and drops out of the
    offerable ("Open to Me") lens.
  - Outreach send-durability sweep: an ExcessOutreach row stuck in ``sending`` past the
    staleness threshold (its background send job died mid-flight) is flipped to
    ``interrupted`` so it stops polling and becomes retryable — never resent here (the
    manual retry path does the Sent-folder lookup before any resend).

Called by: app/jobs/__init__.py via register_resell_jobs()
Depends on: app.database (SessionLocal), app.services.excess_service (expire_overdue_lists),
    app.services.resell_outreach_service (sweep_stale_sending_outreach), app.scheduler
    (_traced_job)
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
    scheduler.add_job(
        _job_sweep_stale_sending_outreach,
        CronTrigger(hour=2, minute=25),
        id="sweep_stale_sending_outreach",
        name="Sweep stale 'sending' resell outreach",
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


@_traced_job
async def _job_sweep_stale_sending_outreach():
    """Daily — flip outreach rows stuck in ``sending`` past the threshold to
    ``interrupted``.

    Delegates to ``resell_outreach_service.sweep_stale_sending_outreach``. Idempotent — a
    row already settled is skipped; nothing is resent (the row becomes retryable).
    """
    from ..database import SessionLocal
    from ..services.resell_outreach_service import sweep_stale_sending_outreach

    db = SessionLocal()
    try:
        swept = sweep_stale_sending_outreach(db)
        if swept:
            logger.info(f"Swept {swept} stale 'sending' outreach row(s) to 'interrupted'")
    except sqlalchemy.exc.SQLAlchemyError as e:
        logger.error(f"Resell stale-sending sweep DB error: {e}")
        db.rollback()
    except Exception as e:
        logger.exception(f"Resell stale-sending sweep error: {e}")
        db.rollback()
    finally:
        db.close()
