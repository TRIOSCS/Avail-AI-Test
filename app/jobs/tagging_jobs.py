"""Material tagging background jobs — Claude Haiku AI, prefix, sighting, boost, spec
sweep.

Called by: app/jobs/__init__.py via register_tagging_jobs()
Depends on: app.database, app.models, app.services.enrichment, app.services.tagging_backfill,
            app.services.tagging_ai, app.services.spec_enrichment_service, app.utils.claude_client
"""

import asyncio

from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from ..scheduler import _traced_job


async def _run_threaded_db_job(label, fn):
    """Run a synchronous ``fn(db)`` off the event loop with a fresh session.

    Shared body of the prefix/sighting/boost jobs: open a session, call ``fn`` via
    ``asyncio.to_thread``, log the result, and roll back + re-raise on failure.
    """
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        result = await asyncio.to_thread(fn, db)
        logger.info(f"{label}: {result}")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


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

    # Spec extraction backlog sweep. SP1 (2026-06-09): the automated Haiku card-enrichment
    # path was removed — this job ONLY runs the status-gated structured-spec second pass over
    # cards already enriched by the authoritative ladder (verified/web_sourced/oem_sourced).
    scheduler.add_job(
        _job_spec_enrichment,
        IntervalTrigger(hours=2),
        id="spec_enrichment",
        name="Material card spec extraction (backlog sweep)",
    )


@_traced_job
async def _job_internal_boost():
    """Cross-check and boost tag confidence using internal data (no API calls).

    Every 4h.
    """
    from ..services.enrichment import boost_confidence_internal

    await _run_threaded_db_job("Internal confidence boost", boost_confidence_internal)


@_traced_job
async def _job_prefix_backfill():
    """Run prefix lookup on untagged cards.

    Every 2h.
    """
    from ..services.tagging_backfill import run_prefix_backfill

    await _run_threaded_db_job("Prefix backfill", run_prefix_backfill)


@_traced_job
async def _job_sighting_mining():
    """Mine sighting manufacturer data for untagged cards.

    Every 2h.
    """
    from ..services.tagging_backfill import backfill_manufacturer_from_sightings

    await _run_threaded_db_job("Sighting mining", backfill_manufacturer_from_sightings)


@_traced_job
async def _job_ai_tagging():
    """Classify untagged material cards via Claude Haiku. Every 30 min, 500 cards/batch.

    Waterfall: prefix_backfill + sighting_mining first (instant), then Claude Haiku for remainder.
    """
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
async def _job_spec_enrichment():
    """Every 2h — structured-spec second pass over the spec backlog.

    SP1 (2026-06-09): the automated Claude Haiku card-enrichment path was removed. This job
    runs ONLY ``enrich_pending_specs`` (no card-level enrichment), which is status-gated so it
    seeds facets exclusively from cards with a trustworthy/source-attributed status.
    """
    from ..database import SessionLocal
    from ..services.spec_enrichment_service import enrich_pending_specs

    db = SessionLocal()
    try:
        spec_stats = await enrich_pending_specs(db)
        logger.info(
            "Material spec enrichment: cards={} specs={} errors={} skipped_no_schema={}",
            spec_stats["cards_processed"],
            spec_stats["specs_written"],
            spec_stats["errors"],
            spec_stats["skipped_no_schema"],
        )
    except Exception:
        logger.exception("Material spec enrichment job failed")
        db.rollback()
        raise
    finally:
        db.close()
