"""Material tagging background jobs — Gradient AI (free), prefix, sighting, boost.

Called by: app/jobs/__init__.py via register_tagging_jobs()
Depends on: app.database, app.models, app.services.enrichment, app.services.tagging_backfill,
            app.services.tagging_ai, app.services.gradient_service
"""

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from ..scheduler import _traced_job


def register_tagging_jobs(scheduler, settings):
    """Register tagging jobs with the scheduler.

    NOTE (2026-03-05): Paid API jobs DISABLED (Nexar/DigiKey/Mouser/Element14/OEMSecrets).
    Gradient AI is FREE — runs every 30min to classify untagged cards aggressively.
    Prefix/sighting/boost run frequently to maximize non-API coverage.
    """
    # DISABLED — consumes DigiKey/Mouser/Element14/OEMSecrets/Nexar API quota
    # scheduler.add_job(
    #     _job_connector_enrichment,
    #     IntervalTrigger(hours=2),
    #     id="connector_enrichment",
    #     name="Enrich low-confidence material cards via connectors",
    # )

    # Non-API jobs — run more frequently to maximize free coverage
    scheduler.add_job(
        _job_internal_boost,
        IntervalTrigger(hours=4),
        id="internal_confidence_boost",
        name="Cross-check and boost tag confidence",
    )
    scheduler.add_job(
        _job_prefix_backfill,
        IntervalTrigger(hours=2),
        id="prefix_backfill",
        name="Run prefix lookup on untagged cards",
    )
    scheduler.add_job(
        _job_sighting_mining,
        IntervalTrigger(hours=2),
        id="sighting_mining",
        name="Mine sighting manufacturer data for untagged cards",
    )

    # DISABLED — Nexar API quota exhausted (2000 part limit hit)
    # scheduler.add_job(
    #     _job_nexar_backfill,
    #     IntervalTrigger(hours=2),
    #     id="nexar_backfill",
    #     name="Backfill untagged cards via Nexar (primary high-confidence source)",
    # )

    # Gradient AI classification — FREE, runs every 30 min, 500 cards per batch
    scheduler.add_job(
        _job_gradient_ai_tagging,
        IntervalTrigger(minutes=30),
        id="gradient_ai_tagging",
        name="Classify untagged cards via Gradient AI (free)",
    )

    if settings.material_enrichment_enabled:
        scheduler.add_job(
            _job_material_enrichment,
            IntervalTrigger(hours=2),
            id="material_enrichment",
            name="Material card AI enrichment (Gradient)",
        )


@_traced_job
async def _job_connector_enrichment():  # pragma: no cover
    """Enrich untagged material cards via API connectors (0.95 confidence). DISABLED."""
    from ..database import SessionLocal
    from ..models.intelligence import MaterialCard
    from ..models.tags import MaterialTag, Tag
    from ..services.enrichment import enrich_batch

    db = SessionLocal()
    try:
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
    """Cross-check and boost tag confidence using internal data (no API calls). Every 4h."""
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
    """Run prefix lookup on untagged cards. Every 2h."""
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
    """Mine sighting manufacturer data for untagged cards. Every 2h."""
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
    """Backfill untagged cards via Nexar — DISABLED (quota exhausted)."""
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


@_traced_job
async def _job_gradient_ai_tagging():
    """Classify untagged material cards via Gradient AI (FREE). Every 30 min, 500 cards/batch.

    Waterfall: prefix_backfill + sighting_mining first (instant), then Gradient AI for remainder.
    Uses Sonnet via Gradient for fast, accurate classification at zero cost.
    """
    import asyncio

    from ..database import SessionLocal
    from ..models.intelligence import MaterialCard
    from ..models.tags import MaterialTag, Tag
    from ..services.tagging_ai import classify_parts_with_ai, _apply_ai_results

    db = SessionLocal()
    try:
        # Find cards with NO brand tag, excluding internal parts
        tagged_brand_ids = (
            db.query(MaterialTag.material_card_id)
            .join(Tag, MaterialTag.tag_id == Tag.id)
            .filter(Tag.tag_type == "brand")
            .distinct()
            .subquery()
        )
        untagged = (
            db.query(MaterialCard.id, MaterialCard.normalized_mpn)
            .filter(
                ~MaterialCard.id.in_(db.query(tagged_brand_ids.c.material_card_id)),
                MaterialCard.is_internal_part.is_(False),
            )
            .order_by(MaterialCard.id)
            .limit(500)
            .all()
        )

        if not untagged:
            logger.info("Gradient AI tagging: no untagged cards remaining")
            return

        logger.info(f"Gradient AI tagging: classifying {len(untagged)} cards")

        total_matched = 0
        total_unknown = 0

        # Process in batches of 50 MPNs, 5 concurrent Gradient calls
        batch_size = 50
        concurrency = 5
        sem = asyncio.Semaphore(concurrency)

        async def _classify_batch(batch):
            mpns = [row.normalized_mpn for row in batch]
            async with sem:
                return await classify_parts_with_ai(mpns)

        all_batches = [untagged[i:i + batch_size] for i in range(0, len(untagged), batch_size)]

        for round_start in range(0, len(all_batches), concurrency):
            round_batches = all_batches[round_start:round_start + concurrency]
            results = await asyncio.gather(
                *[_classify_batch(b) for b in round_batches],
                return_exceptions=True,
            )

            for batch, classified in zip(round_batches, results):
                if isinstance(classified, Exception):
                    logger.warning(f"Gradient batch failed: {classified}")
                    continue
                batch_tuples = [(row.id, row.normalized_mpn) for row in batch]
                matched, unknown = _apply_ai_results(classified, batch_tuples, db)
                total_matched += matched
                total_unknown += unknown

            db.commit()

        logger.info(
            f"Gradient AI tagging done: {len(untagged)} processed, "
            f"{total_matched} matched, {total_unknown} unknown"
        )
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def _job_material_enrichment():
    """Every 2h — AI-enrich material cards missing descriptions/categories via Gradient."""
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
