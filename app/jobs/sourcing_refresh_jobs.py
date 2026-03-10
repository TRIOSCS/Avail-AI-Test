"""Sourcing refresh jobs — auto-refresh stale active requisitions.

Called by: app/jobs/__init__.py via register_sourcing_refresh_jobs()
Depends on: app.database, app.models, app.search_service
"""

import asyncio
from datetime import datetime, timedelta, timezone

from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from ..scheduler import _traced_job


def register_sourcing_refresh_jobs(scheduler, settings):
    """Register sourcing auto-refresh jobs."""
    scheduler.add_job(
        _job_refresh_stale_requisitions,
        CronTrigger(hour=3, minute=0),
        id="refresh_stale_requisitions",
        name="Auto-refresh stale active requisitions",
    )


@_traced_job
async def _job_refresh_stale_requisitions():
    """Daily 3 AM — re-search requirements on active requisitions with stale sightings.

    A requirement is "stale" if its newest sighting is older than 24 hours.
    Only processes active/sourcing/offers requisitions, max 20 per run to
    avoid API rate limit issues.
    """
    from ..database import SessionLocal
    from ..models import Requirement, Requisition, Sighting

    db = SessionLocal()
    try:
        from sqlalchemy import func

        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

        # Find active requisitions with stale requirements
        stale_reqs = (
            db.query(Requirement)
            .join(Requisition, Requirement.requisition_id == Requisition.id)
            .outerjoin(Sighting, Sighting.requirement_id == Requirement.id)
            .filter(
                Requisition.status.in_(["active", "sourcing", "offers"]),
                Requirement.primary_mpn.isnot(None),
            )
            .group_by(Requirement.id)
            .having(
                (func.max(Sighting.created_at) < cutoff) | (func.max(Sighting.created_at).is_(None))
            )
            .limit(20)
            .all()
        )

        if not stale_reqs:
            return

        logger.info(f"Auto-refresh: found {len(stale_reqs)} stale requirement(s)")

        from ..search_service import search_requirement

        refreshed = 0
        for req in stale_reqs:
            try:
                result = await search_requirement(req, db)
                sighting_count = len(result.get("sightings", []))
                if sighting_count > 0:
                    refreshed += 1
                    logger.debug(
                        f"Auto-refresh: req {req.id} ({req.primary_mpn}) → {sighting_count} sightings"
                    )
            except Exception as e:
                logger.warning(f"Auto-refresh failed for req {req.id}: {e}")
                continue

        logger.info(f"Auto-refresh complete: {refreshed}/{len(stale_reqs)} requirements refreshed")
    except Exception as e:
        logger.error(f"Auto-refresh stale requisitions error: {e}")
        db.rollback()
    finally:
        db.close()
