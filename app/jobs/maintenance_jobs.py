"""Maintenance background jobs — cache cleanup, dedup, connector reset, attribution,
integrity.

Called by: app/jobs/__init__.py via register_maintenance_jobs()
Depends on: app.database, app.models, app.services.*
"""

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from ..scheduler import _traced_job


def register_maintenance_jobs(scheduler, settings):
    """Register maintenance jobs with the scheduler."""
    scheduler.add_job(_job_cache_cleanup, IntervalTrigger(hours=24), id="cache_cleanup", name="Cache cleanup")

    scheduler.add_job(
        _job_auto_attribute_activities,
        IntervalTrigger(hours=2),
        id="auto_attribute_activities",
        name="Auto-attribute unmatched activities",
    )

    scheduler.add_job(
        _job_auto_dedup, IntervalTrigger(hours=24), id="auto_dedup", name="Auto-dedup companies and vendors"
    )

    scheduler.add_job(
        _job_reset_connector_errors,
        CronTrigger(hour=0, minute=0),
        id="reset_connector_errors",
        name="Reset connector 24h error counts",
    )

    scheduler.add_job(
        _job_integrity_check, IntervalTrigger(hours=6), id="integrity_check", name="Material card integrity check"
    )

    scheduler.add_job(
        _job_contact_dedup,
        CronTrigger(hour=3, minute=30),
        id="contact_dedup",
        name="Deduplicate site contacts",
    )


@_traced_job
async def _job_cache_cleanup():
    """Clean up expired cache entries."""
    try:
        from ..cache.intel_cache import cleanup_expired

        cleanup_expired()
    except Exception as e:
        logger.error(f"Cache cleanup error: {e}")
        raise


@_traced_job
async def _job_auto_attribute_activities():
    """Auto-attribute unmatched activities using rule-based + AI matching."""
    from ..database import SessionLocal
    from ..services.auto_attribution_service import run_auto_attribution

    db = SessionLocal()
    try:
        stats = run_auto_attribution(db)
        total = stats["rule_matched"] + stats["ai_matched"]
        if total:
            logger.info(
                "Auto-attribution: %d rule-matched, %d AI-matched, %d dismissed",
                stats["rule_matched"],
                stats["ai_matched"],
                stats["auto_dismissed"],
            )
    except Exception:
        logger.exception("Auto-attribution job failed")
        db.rollback()
        raise
    finally:
        db.close()


@_traced_job
async def _job_auto_dedup():
    """Auto-deduplicate companies and vendors using fuzzy matching + AI confirmation."""
    from ..database import SessionLocal
    from ..services.auto_dedup_service import run_auto_dedup

    db = SessionLocal()
    try:
        stats = run_auto_dedup(db)
        total = stats["vendors_merged"] + stats["companies_merged"]
        if total:
            logger.info(
                "Auto-dedup: %d vendors merged, %d companies merged",
                stats["vendors_merged"],
                stats["companies_merged"],
            )
    except Exception:
        logger.exception("Auto-dedup job failed")
        db.rollback()
        raise
    finally:
        db.close()


@_traced_job
async def _job_reset_connector_errors():
    """Reset 24h error counters on all API sources (batched to avoid lock timeout)."""
    from ..database import SessionLocal
    from ..models import ApiSource

    db = SessionLocal()
    try:
        # Fetch IDs first, then update in small batches to avoid lock contention
        source_ids = [s.id for s in db.query(ApiSource.id).filter(ApiSource.error_count_24h > 0).all()]
        batch_size = 50
        for i in range(0, len(source_ids), batch_size):
            batch = source_ids[i : i + batch_size]
            db.query(ApiSource).filter(ApiSource.id.in_(batch)).update(
                {"error_count_24h": 0}, synchronize_session="fetch"
            )
            db.commit()
    except Exception:
        logger.exception("Reset connector error counts failed")
        db.rollback()
        raise
    finally:
        db.close()


@_traced_job
async def _job_contact_dedup():
    """Daily dedup of site contacts sharing (customer_site_id, lower(email))."""
    from ..database import SessionLocal
    from ..models import SiteContact

    db = SessionLocal()
    try:
        from sqlalchemy import func

        dupes = (
            db.query(
                SiteContact.customer_site_id,
                func.lower(SiteContact.email).label("em"),
                func.count().label("cnt"),
            )
            .filter(SiteContact.email.isnot(None))
            .group_by(SiteContact.customer_site_id, func.lower(SiteContact.email))
            .having(func.count() > 1)
            .all()
        )
        merged = 0
        for dupe in dupes:
            contacts = (
                db.query(SiteContact)
                .filter(
                    SiteContact.customer_site_id == dupe.customer_site_id,
                    func.lower(SiteContact.email) == dupe.em,
                )
                .order_by(SiteContact.id)
                .all()
            )
            best = max(
                contacts,
                key=lambda c: sum(
                    1 for col in ["full_name", "title", "phone", "notes", "linkedin_url"] if getattr(c, col, None)
                ),
            )
            for other in contacts:
                if other.id == best.id:
                    continue
                for col in ["full_name", "title", "phone", "notes", "linkedin_url"]:
                    if getattr(best, col, None) is None and getattr(other, col, None) is not None:
                        setattr(best, col, getattr(other, col))
                db.delete(other)
                merged += 1
        db.commit()
        if merged:
            logger.info(f"Contact dedup: merged {merged} duplicate contacts")
    except Exception:
        logger.exception("Contact dedup job failed")
        db.rollback()
        raise
    finally:
        db.close()


@_traced_job
async def _job_integrity_check():
    """Every 6h — check material card linkage integrity and self-heal."""
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        from ..services.integrity_service import run_integrity_check

        report = run_integrity_check(db)
        logger.info(
            "Integrity check complete: status=%s cards=%d healed=(%d/%d/%d)",
            report["status"],
            report["material_cards_total"],
            report["healed"]["requirements"],
            report["healed"]["sightings"],
            report["healed"]["offers"],
        )
    except Exception:
        logger.exception("Integrity check failed")
        db.rollback()
        raise
    finally:
        db.close()
