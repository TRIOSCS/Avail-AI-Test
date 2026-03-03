"""Maintenance background jobs — cache cleanup, dedup, connector reset, attribution, integrity.

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


@_traced_job
async def _job_cache_cleanup():
    """Clean up expired cache entries."""
    try:
        from ..cache.intel_cache import cleanup_expired

        cleanup_expired()
    except Exception as e:
        logger.error(f"Cache cleanup error: {e}")


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
    finally:
        db.close()


@_traced_job
async def _job_reset_connector_errors():
    """Reset 24h error counters on all API sources."""
    from ..database import SessionLocal
    from ..models import ApiSource

    db = SessionLocal()
    try:
        db.query(ApiSource).filter(ApiSource.error_count_24h > 0).update(
            {"error_count_24h": 0}, synchronize_session="fetch"
        )
        db.commit()
    except Exception:
        logger.exception("Reset connector error counts failed")
        db.rollback()
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
    finally:
        db.close()
