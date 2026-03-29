"""Material tagging background jobs — Claude Haiku AI, prefix, sighting, boost.

Called by: app/jobs/__init__.py via register_tagging_jobs()
Depends on: app.database, app.models, app.services.enrichment, app.services.tagging_backfill,
            app.services.tagging_ai, app.utils.claude_client
"""

from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from ..scheduler import _traced_job


def register_tagging_jobs(scheduler, settings):
    """Register tagging jobs with the scheduler.

    NOTE (2026-03-20): Gradient removed, all AI via Claude Haiku. Paid connector jobs
    DISABLED (Nexar/DigiKey/Mouser/Element14/OEMSecrets). Prefix/sighting/boost run
    frequently to maximize non-API coverage.
    """
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

    # Claude Haiku AI classification — reduced from 30min to 4h (2026-03-26)
    scheduler.add_job(
        _job_ai_tagging,
        IntervalTrigger(hours=4),
        id="ai_tagging",
        name="Classify untagged cards via Claude Haiku",
    )

    if settings.material_enrichment_enabled:
        scheduler.add_job(
            _job_material_enrichment,
            IntervalTrigger(hours=2),
            id="material_enrichment",
            name="Material card AI enrichment (Claude Haiku)",
        )


@_traced_job
async def _job_internal_boost():
    """Cross-check and boost tag confidence using internal data (no API calls).

    Every 4h.
    """
    import asyncio

    from ..database import SessionLocal
    from ..services.enrichment import boost_confidence_internal

    db = SessionLocal()
    try:
        result = await asyncio.to_thread(boost_confidence_internal, db)
        logger.info(f"Internal confidence boost: {result}")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@_traced_job
async def _job_prefix_backfill():
    """Run prefix lookup on untagged cards.

    Every 2h.
    """
    import asyncio

    from ..database import SessionLocal
    from ..services.tagging_backfill import run_prefix_backfill

    db = SessionLocal()
    try:
        result = await asyncio.to_thread(run_prefix_backfill, db)
        logger.info(f"Prefix backfill: {result}")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@_traced_job
async def _job_sighting_mining():
    """Mine sighting manufacturer data for untagged cards.

    Every 2h.
    """
    import asyncio

    from ..database import SessionLocal
    from ..services.tagging_backfill import backfill_manufacturer_from_sightings

    db = SessionLocal()
    try:
        result = await asyncio.to_thread(backfill_manufacturer_from_sightings, db)
        logger.info(f"Sighting mining: {result}")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@_traced_job
async def _job_ai_tagging():
    """Classify untagged material cards via Claude Haiku. Every 30 min, 500 cards/batch.

    Waterfall: prefix_backfill + sighting_mining first (instant), then Claude Haiku for remainder.
    """
    import asyncio

    from ..database import SessionLocal
    from ..models.intelligence import MaterialCard
    from ..models.tags import MaterialTag, Tag
    from ..services.tagging_ai import _apply_ai_results, classify_parts_with_ai

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
            logger.info("AI tagging: no untagged cards remaining")
            return

        logger.info(f"AI tagging: classifying {len(untagged)} cards")

        total_matched = 0
        total_unknown = 0

        # Process in batches of 50 MPNs, 5 concurrent Claude calls
        batch_size = 50
        concurrency = 5
        sem = asyncio.Semaphore(concurrency)

        async def _classify_batch(batch):
            mpns = [row.normalized_mpn for row in batch]
            async with sem:
                return await classify_parts_with_ai(mpns)

        all_batches = [untagged[i : i + batch_size] for i in range(0, len(untagged), batch_size)]

        for round_start in range(0, len(all_batches), concurrency):
            round_batches = all_batches[round_start : round_start + concurrency]
            results = await asyncio.gather(
                *[_classify_batch(b) for b in round_batches],
                return_exceptions=True,
            )

            for batch, classified in zip(round_batches, results):
                if isinstance(classified, Exception):
                    logger.warning(f"AI batch failed: {classified}")
                    continue
                batch_tuples = [(row.id, row.normalized_mpn) for row in batch]
                matched, unknown = _apply_ai_results(classified, batch_tuples, db)
                total_matched += matched
                total_unknown += unknown

            db.commit()

        logger.info(f"AI tagging done: {len(untagged)} processed, {total_matched} matched, {total_unknown} unknown")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@_traced_job
async def _job_material_enrichment():
    """Every 2h — AI-enrich material cards missing descriptions/categories via Claude
    Haiku."""
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
        db.rollback()
        raise
    finally:
        db.close()
