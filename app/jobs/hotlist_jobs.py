"""jobs/hotlist_jobs.py — weekly automated re-search of hotlist demand (FLAG-OFF).

Hotlist demand (part-level `Requirement.sourcing_status == 'hotlist'`, migration
210, plus deal-level `Requisition.status == 'hotlist'`) is a standing "watch
this part for the customer" request. The passive catalog builds from manual
searches, vendor stock-list emails, logged offers, and the excess mirror; this
job adds ACTIVE weekly re-searching through the ICS/NC/TBF browser connectors
so the catalog keeps growing even when nobody touches the part.

Ships behind `hotlist_research_enabled` (config default False; admin override
via system_config) by explicit owner decision (2026-08-14): the job exists so
enabling it later is a one-flag flip, but it spends real connector quota so it
stays OFF until the owner says otherwise. Load is bounded three ways: one run a
week (Sun 02:00), `hotlist_research_max_parts` distinct MPNs per run (oldest
`last_searched_at` first, never-searched first of all), and the workers' 7-day
cross-requirement dedup (a recently searched MPN clones sightings instead of
re-searching).

Called by: app/jobs/__init__.py register_all_jobs (flag-gated registration).
Depends on: app.models (Requirement, Requisition), ics/nc/tbf queue managers.
"""

from apscheduler.triggers.cron import CronTrigger
from loguru import logger
from sqlalchemy import or_

from ..constants import RequisitionStatus, SourcingStatus
from ..scheduler import _traced_job


@_traced_job
async def _job_hotlist_research():
    """Enqueue the stalest hotlist MPNs (one requirement each) for connector search."""
    from ..config import settings
    from ..database import SessionLocal
    from ..models import Requirement, Requisition
    from ..services.ics_worker.queue_manager import enqueue_for_ics_search
    from ..services.nc_worker.queue_manager import enqueue_for_nc_search
    from ..services.tbf_worker.queue_manager import enqueue_for_tbf_search

    db = SessionLocal()
    try:
        rows = (
            db.query(Requirement)
            .join(Requisition, Requirement.requisition_id == Requisition.id)
            .filter(
                Requisition.is_scratch.is_(False),
                Requirement.normalized_mpn.isnot(None),
                or_(
                    Requirement.sourcing_status == SourcingStatus.HOTLIST,
                    Requisition.status == RequisitionStatus.HOTLIST,
                ),
            )
            .order_by(Requirement.last_searched_at.asc().nullsfirst())
            .all()
        )
        cap = max(1, settings.hotlist_research_max_parts)
        seen_keys: set[str] = set()
        enqueued = 0
        for req_item in rows:
            if len(seen_keys) >= cap:
                break
            if req_item.normalized_mpn in seen_keys:
                continue
            seen_keys.add(req_item.normalized_mpn)
            for enqueue in (enqueue_for_ics_search, enqueue_for_nc_search, enqueue_for_tbf_search):
                try:
                    enqueue(req_item.id, db)
                    enqueued += 1
                except Exception as exc:
                    logger.warning("Hotlist research enqueue failed (req {}): {}", req_item.id, exc)
        db.commit()
        logger.info(
            "Hotlist research: {} distinct MPNs → {} queue entries (cap {})",
            len(seen_keys),
            enqueued,
            cap,
        )
    except Exception as exc:
        logger.error("Hotlist research job failed: {}", exc)
        db.rollback()
    finally:
        db.close()


def register_hotlist_jobs(scheduler, settings, db=None):
    """Register the weekly hotlist re-search job (only when the flag is ON).

    *db* (when provided) lets hotlist_research_enabled resolve from the system_config DB
    row (admin toggle) instead of only the env default. `is True` (not truthiness) so a
    MagicMock settings in unrelated scheduler tests resolves to off, not a spurious job.
    """
    from ..services.admin_service import get_effective_flag

    if get_effective_flag(db, "hotlist_research_enabled", settings.hotlist_research_enabled) is True:
        scheduler.add_job(
            _job_hotlist_research,
            CronTrigger(day_of_week="sun", hour=2, minute=0),
            id="hotlist_research",
            name="Hotlist weekly re-search",
        )
        logger.info("Hotlist weekly re-search job registered (flag ON)")
