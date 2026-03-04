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
        _job_nexar_backfill,
        IntervalTrigger(hours=2),
        id="nexar_backfill",
        name="Backfill untagged cards via Nexar (primary high-confidence source)",
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
    """Enrich untagged material cards via API connectors (0.95 confidence). Runs every 2h.

    Phase 1: Enrich untagged cards via DigiKey/Mouser/Element14/OEMSecrets/Nexar
    Phase 2: Run sighting mining + internal boost cascade
    """
    from ..database import SessionLocal
    from ..models.intelligence import MaterialCard
    from ..models.tags import MaterialTag, Tag
    from ..services.enrichment import enrich_batch

    db = SessionLocal()
    try:
        # Phase 1: Enrich cards with NO brand tag via connectors
        tagged_brand_ids = (
            db.query(MaterialTag.material_card_id)
            .join(Tag, MaterialTag.tag_id == Tag.id)
            .filter(Tag.tag_type == "brand")
            .distinct()
            .subquery()
        )
        untagged_cards = (
            db.query(MaterialCard.normalized_mpn)
            .filter(~MaterialCard.id.in_(db.query(tagged_brand_ids.c.material_card_id)))
            .order_by(MaterialCard.id)
            .limit(2000)
            .all()
        )
        mpns = [row.normalized_mpn for row in untagged_cards]
        if mpns:
            logger.info(f"Connector enrichment: processing {len(mpns)} untagged cards")
            result = await enrich_batch(mpns, db, concurrency=3)
            logger.info(f"Connector enrichment done: {result}")

        # Phase 2: Run boost cascade + sighting mining
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
async def _job_nexar_backfill():
    """Backfill untagged cards via Nexar — primary high-confidence source. Every 2 hours."""
    from ..database import SessionLocal
    from ..services.enrichment import nexar_backfill_untagged

    db = SessionLocal()
    try:
        result = await nexar_backfill_untagged(db, limit=5000)
        logger.info(f"Nexar backfill job: {result}")
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
