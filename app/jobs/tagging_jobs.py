"""Material tagging background jobs — connector enrichment, boost, prefix, sighting, Nexar.

Called by: app/jobs/__init__.py via register_tagging_jobs()
Depends on: app.database, app.models, app.services.enrichment, app.services.tagging_backfill
"""

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from ..scheduler import _traced_job


def register_tagging_jobs(scheduler, settings):
    """Register tagging jobs with the scheduler."""
    scheduler.add_job(
        _job_connector_enrichment,
        IntervalTrigger(hours=2),
        id="connector_enrichment",
        name="Enrich low-confidence material cards via connectors",
    )
    scheduler.add_job(
        _job_internal_boost,
        CronTrigger(hour=2, minute=0),
        id="internal_confidence_boost",
        name="Cross-check and boost tag confidence",
    )
    scheduler.add_job(
        _job_prefix_backfill,
        CronTrigger(hour=3, minute=0),
        id="prefix_backfill",
        name="Run prefix lookup on untagged cards",
    )
    scheduler.add_job(
        _job_sighting_mining,
        CronTrigger(hour=4, minute=0),
        id="sighting_mining",
        name="Mine sighting manufacturer data for untagged cards",
    )
    scheduler.add_job(
        _job_nexar_validate,
        IntervalTrigger(hours=6),
        id="nexar_validate",
        name="Validate AI tags via Nexar",
    )

    if settings.material_enrichment_enabled:
        scheduler.add_job(
            _job_material_enrichment,
            IntervalTrigger(hours=6),
            id="material_enrichment",
            name="Material card AI enrichment",
        )


@_traced_job
async def _job_connector_enrichment():  # pragma: no cover
    """Enrich low-confidence material cards via API connectors. Runs every 4h.

    Two phases:
    1. Enrich untagged/low-conf cards with no manufacturer (new data)
    2. Cross-validate existing AI tags against connectors (upgrade confidence)
    """
    from ..database import SessionLocal
    from ..models.intelligence import MaterialCard
    from ..models.tags import MaterialTag, Tag
    from ..services.enrichment import cross_validate_batch, enrich_batch

    db = SessionLocal()
    try:
        # Phase 1: Enrich cards with low-confidence tags
        low_conf_cards = (
            db.query(MaterialCard.normalized_mpn)
            .join(MaterialTag, MaterialCard.id == MaterialTag.material_card_id)
            .join(Tag, MaterialTag.tag_id == Tag.id)
            .filter(Tag.tag_type == "brand", MaterialTag.confidence < 0.9)
            .order_by(MaterialTag.confidence.asc())
            .limit(2000)
            .all()
        )
        mpns = [row.normalized_mpn for row in low_conf_cards]
        if mpns:
            logger.info(f"Connector enrichment: processing {len(mpns)} low-confidence cards")
            result = await enrich_batch(mpns, db, concurrency=3)
            logger.info(f"Connector enrichment done: {result}")

        # Phase 2: Cross-validate AI tags against connectors
        cv_result = await cross_validate_batch(db, limit=500, concurrency=3)
        if cv_result["total"] > 0:
            logger.info(f"Cross-validation done: {cv_result}")

        # Phase 3: Run boost cascade to propagate new manufacturer data
        from ..services.enrichment import boost_confidence_internal
        from ..services.tagging_backfill import backfill_manufacturer_from_sightings

        boost_result = boost_confidence_internal(db)
        if boost_result.get("total_boosted", 0) > 0:
            logger.info(f"Post-enrichment boost: {boost_result}")

        sighting_result = backfill_manufacturer_from_sightings(db)
        if sighting_result.get("total_tagged", 0) > 0:
            logger.info(f"Post-enrichment sighting mining: {sighting_result}")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@_traced_job
async def _job_internal_boost():
    """Cross-check and boost tag confidence using internal data (no API calls). Daily at 2 AM."""
    from ..database import SessionLocal
    from ..services.enrichment import boost_confidence_internal

    db = SessionLocal()
    try:
        result = boost_confidence_internal(db)
        logger.info(f"Internal confidence boost: {result}")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@_traced_job
async def _job_prefix_backfill():
    """Run prefix lookup on untagged cards. Daily at 3 AM."""
    from ..database import SessionLocal
    from ..services.tagging_backfill import run_prefix_backfill

    db = SessionLocal()
    try:
        result = run_prefix_backfill(db)
        logger.info(f"Prefix backfill: {result}")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@_traced_job
async def _job_sighting_mining():
    """Mine sighting manufacturer data for untagged cards. Daily at 4 AM."""
    from ..database import SessionLocal
    from ..services.tagging_backfill import backfill_manufacturer_from_sightings

    db = SessionLocal()
    try:
        result = backfill_manufacturer_from_sightings(db)
        logger.info(f"Sighting mining: {result}")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@_traced_job
async def _job_nexar_validate():
    """Validate AI-classified tags via Nexar. Every 6 hours."""
    from ..database import SessionLocal
    from ..services.enrichment import nexar_bulk_validate

    db = SessionLocal()
    try:
        result = await nexar_bulk_validate(db, limit=2000)
        logger.info(f"Nexar validate job: {result}")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def _job_material_enrichment():
    """Every 6h — AI-enrich material cards missing descriptions/categories."""
    from ..config import settings
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        from ..services.material_enrichment_service import enrich_pending_cards

        result = await enrich_pending_cards(db, limit=settings.material_enrichment_batch_size)
        logger.info(
            "Material enrichment: enriched=%d errors=%d pending=%d",
            result["enriched"],
            result["errors"],
            result.get("pending", 0),
        )
    except Exception:
        logger.exception("Material enrichment job failed")
    finally:
        db.close()


@_traced_job
async def _job_tagging_backfill():  # pragma: no cover
    """One-shot: classify untagged material cards via prefix lookup."""
    from ..database import SessionLocal
    from ..services.tagging_backfill import run_prefix_backfill, seed_from_existing_manufacturers

    db = SessionLocal()
    try:
        seed_from_existing_manufacturers(db)
        run_prefix_backfill(db)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
