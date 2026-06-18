"""Nightly CRM cadence clock recompute — self-healing backstop for the real-time bump
(bump_clocks_from_activity)."""

from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from ..scheduler import _traced_job


def register_cadence_jobs(scheduler, settings):
    scheduler.add_job(
        _job_materialize_cadence,
        CronTrigger(hour=4, minute=0),
        id="cadence_materialize",
        name="Nightly CRM cadence clock recompute",
    )


@_traced_job
async def _job_materialize_cadence():
    from ..database import SessionLocal
    from ..services.cadence_service import materialize_all_clocks

    db = SessionLocal()
    try:
        n = materialize_all_clocks(db)
        db.commit()
        logger.info(f"Cadence job: recomputed clocks for {n} companies")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
