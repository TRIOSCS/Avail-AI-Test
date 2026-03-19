"""Part discovery scheduler jobs — grow the material card library continuously.

Registers periodic jobs with APScheduler to discover new parts via
cross-reference expansion, family/series expansion, and commodity gap fill.

Called by: app.scheduler (job registration)
Depends on: app.services.part_discovery_service
"""

from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from app.database import SessionLocal
from app.scheduler import _traced_job


def register_discovery_jobs(scheduler):
    """Register part discovery jobs with the scheduler."""
    from app.config import settings

    if not getattr(settings, "part_discovery_enabled", True):
        logger.info("Part discovery disabled — skipping job registration")
        return

    scheduler.add_job(
        _job_cross_ref_expansion,
        IntervalTrigger(hours=6),
        id="part_discovery_crossref",
        name="Cross-reference expansion (discover substitute parts)",
        replace_existing=True,
    )

    scheduler.add_job(
        _job_family_expansion,
        IntervalTrigger(days=3),
        id="part_discovery_family",
        name="Cross/substitute discovery (find alternative parts for existing MPNs)",
        replace_existing=True,
    )

    scheduler.add_job(
        _job_commodity_gap_fill,
        IntervalTrigger(days=30),
        id="part_discovery_commodity",
        name="Commodity gap fill (discover common parts in small categories)",
        replace_existing=True,
    )

    logger.info("Part discovery jobs registered: crossref (6h), cross/sub (3d), gap fill (30d)")


@_traced_job
async def _job_cross_ref_expansion():
    """Expand cross-references into new MaterialCards."""
    from app.services.part_discovery_service import expand_cross_references

    db = SessionLocal()
    try:
        result = await expand_cross_references(db, limit=500)
        logger.info(f"Cross-ref expansion complete: {result}")
    finally:
        db.close()


@_traced_job
async def _job_family_expansion():
    """Discover family/series members for popular MPNs."""
    from app.services.part_discovery_service import expand_families

    db = SessionLocal()
    try:
        result = await expand_families(db, batch_size=100)
        logger.info(f"Family expansion complete: {result}")
    finally:
        db.close()


@_traced_job
async def _job_commodity_gap_fill():
    """Fill commodity gaps with common MPNs."""
    from app.services.part_discovery_service import fill_commodity_gaps

    db = SessionLocal()
    try:
        result = await fill_commodity_gaps(db)
        logger.info(f"Commodity gap fill complete: {result}")
    finally:
        db.close()
