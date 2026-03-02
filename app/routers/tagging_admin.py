"""Tagging admin endpoints — status dashboard, backfill trigger, enrichment.

Called by: app.main (router registration)
Depends on: app.models.tags, app.services.tagging_backfill, app.services.enrichment, app.scheduler
"""

import asyncio
import time

from fastapi import APIRouter, Depends
from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_user
from app.models.intelligence import MaterialCard
from app.models.tags import EntityTag, MaterialTag, Tag

# Track enrichment progress (simple in-memory state)
_enrichment_status: dict = {"running": False, "started_at": None, "result": None}

router = APIRouter(prefix="/api/admin/tagging", tags=["admin"])


@router.get("/status")
async def tagging_status(db: Session = Depends(get_db), _user=Depends(require_user)):
    """Return tagging coverage statistics."""
    total_cards = db.query(func.count(MaterialCard.id)).scalar() or 0
    tagged_count = db.query(func.count(func.distinct(MaterialTag.material_card_id))).scalar() or 0
    untagged_count = total_cards - tagged_count
    brand_count = db.query(func.count(Tag.id)).filter(Tag.tag_type == "brand").scalar() or 0
    commodity_count = db.query(func.count(Tag.id)).filter(Tag.tag_type == "commodity").scalar() or 0
    coverage = round((tagged_count / total_cards * 100), 1) if total_cards > 0 else 0.0

    # Internal part triage
    internal_count = (
        db.query(func.count(MaterialCard.id))
        .filter(MaterialCard.is_internal_part.is_(True))
        .scalar()
        or 0
    )
    effective_total = total_cards - internal_count
    effective_coverage = round((tagged_count / effective_total * 100), 1) if effective_total > 0 else 0.0

    # Top brands
    top_brands = (
        db.query(Tag.name, func.count(MaterialTag.id).label("cnt"))
        .join(MaterialTag, MaterialTag.tag_id == Tag.id)
        .filter(Tag.tag_type == "brand")
        .group_by(Tag.name)
        .order_by(func.count(MaterialTag.id).desc())
        .limit(10)
        .all()
    )

    # Top commodities
    top_commodities = (
        db.query(Tag.name, func.count(MaterialTag.id).label("cnt"))
        .join(MaterialTag, MaterialTag.tag_id == Tag.id)
        .filter(Tag.tag_type == "commodity")
        .group_by(Tag.name)
        .order_by(func.count(MaterialTag.id).desc())
        .limit(10)
        .all()
    )

    # Source distribution
    source_dist = (
        db.query(MaterialTag.source, func.count(MaterialTag.id), func.avg(MaterialTag.confidence))
        .group_by(MaterialTag.source)
        .all()
    )

    # Entity coverage
    entity_coverage = (
        db.query(EntityTag.entity_type, func.count(func.distinct(EntityTag.entity_id)))
        .filter(EntityTag.is_visible.is_(True))
        .group_by(EntityTag.entity_type)
        .all()
    )

    # Confidence distribution
    confidence_dist = (
        db.query(
            func.count(MaterialTag.id).label("cnt"),
        )
        .filter(MaterialTag.confidence >= 0.90)
        .scalar()
        or 0
    )
    visible_tags = (
        db.query(func.count(MaterialTag.id))
        .filter(MaterialTag.confidence >= 0.70)
        .scalar()
        or 0
    )
    total_tags = db.query(func.count(MaterialTag.id)).scalar() or 0

    return {
        "total_material_cards": total_cards,
        "tagged_count": tagged_count,
        "untagged_count": untagged_count,
        "brand_tag_count": brand_count,
        "commodity_tag_count": commodity_count,
        "coverage_percentage": coverage,
        "internal_part_count": internal_count,
        "effective_total": effective_total,
        "effective_coverage": effective_coverage,
        "high_confidence_tags": confidence_dist,
        "visible_tags": visible_tags,
        "total_material_tags": total_tags,
        "top_brands": [{"name": n, "count": c} for n, c in top_brands],
        "top_commodities": [{"name": n, "count": c} for n, c in top_commodities],
        "source_distribution": [
            {"source": s, "count": c, "avg_confidence": round(float(a), 2) if a else 0}
            for s, c, a in source_dist
        ],
        "entity_coverage": [{"entity_type": t, "entities_with_visible_tags": c} for t, c in entity_coverage],
    }


@router.post("/backfill")
async def trigger_backfill(db: Session = Depends(get_db), _user=Depends(require_user)):
    """Trigger tagging backfill in a background thread."""

    def _run_backfill():
        from app.database import SessionLocal
        from app.services.tagging_backfill import run_prefix_backfill, seed_from_existing_manufacturers

        session = SessionLocal()
        try:
            seed_from_existing_manufacturers(session)
            run_prefix_backfill(session)  # pragma: no cover
        except Exception:
            logger.exception("Backfill failed")
            session.rollback()
        finally:
            session.close()

    asyncio.get_event_loop().run_in_executor(None, _run_backfill)
    return {"ok": True, "message": "Backfill triggered in background"}


@router.post("/enrich")
async def trigger_enrichment(db: Session = Depends(get_db), _user=Depends(require_user)):
    """Trigger connector enrichment for low-confidence and untagged cards."""
    global _enrichment_status

    if _enrichment_status["running"]:
        return {"ok": False, "message": "Enrichment already running", "started_at": _enrichment_status["started_at"]}

    # Find cards with low-confidence AI tags (< 0.9)
    low_conf_mpns = (
        db.query(MaterialCard.normalized_mpn)
        .join(MaterialTag, MaterialCard.id == MaterialTag.material_card_id)
        .join(Tag, MaterialTag.tag_id == Tag.id)
        .filter(Tag.tag_type == "brand", MaterialTag.confidence < 0.9)
        .order_by(MaterialTag.confidence.asc())
        .limit(500)
        .all()
    )

    # Plus untagged cards
    tagged_ids = (
        db.query(MaterialTag.material_card_id)
        .join(Tag, MaterialTag.tag_id == Tag.id)
        .filter(Tag.tag_type == "brand")
        .distinct()
        .subquery()
    )
    untagged_mpns = (
        db.query(MaterialCard.normalized_mpn)
        .filter(~MaterialCard.id.in_(db.query(tagged_ids.c.material_card_id)))
        .limit(500)
        .all()
    )

    mpns = list({row.normalized_mpn for row in low_conf_mpns} | {row.normalized_mpn for row in untagged_mpns})

    if not mpns:
        return {"ok": True, "message": "No cards need enrichment", "count": 0}

    _enrichment_status = {"running": True, "started_at": time.time(), "result": None}

    async def _run():
        from app.database import SessionLocal
        from app.services.enrichment import enrich_batch

        session = SessionLocal()
        try:
            result = await enrich_batch(mpns, session, concurrency=3)
            _enrichment_status["result"] = result
        except Exception:
            logger.exception("Connector enrichment failed")
            _enrichment_status["result"] = {"error": "enrichment failed"}
            session.rollback()
        finally:
            session.close()
            _enrichment_status["running"] = False

    asyncio.create_task(_run())
    return {"ok": True, "message": f"Enrichment started for {len(mpns)} cards", "count": len(mpns)}


@router.get("/enrich/status")
async def enrichment_status(_user=Depends(require_user)):
    """Check enrichment progress."""
    return {
        "running": _enrichment_status["running"],
        "started_at": _enrichment_status["started_at"],
        "result": _enrichment_status["result"],
    }


@router.post("/apply-batch")
async def apply_batch_results(batch_id: str = "msgbatch_01M2nTyzQ141rLBb6SJte9fi", _user=Depends(require_user)):
    """Apply pending Batch API results (chunked, memory-safe)."""

    async def _run():
        from app.services.tagging_ai import apply_batch_results_chunked

        return await apply_batch_results_chunked(batch_id)

    asyncio.create_task(_run())
    return {"ok": True, "message": f"Applying batch {batch_id} results in background"}


@router.post("/ai-backfill")
async def trigger_ai_backfill(limit: int = 50000, db: Session = Depends(get_db), _user=Depends(require_user)):
    """Submit untagged cards to Anthropic Batch API for AI classification."""

    async def _run():
        from app.database import SessionLocal
        from app.services.tagging_ai import submit_targeted_backfill

        session = SessionLocal()
        try:
            result = await submit_targeted_backfill(session, limit=limit)
            logger.info(f"AI backfill result: {result}")
        except Exception:
            logger.exception("AI backfill failed")
            session.rollback()
        finally:
            session.close()

    asyncio.create_task(_run())
    return {"ok": True, "message": f"AI backfill submitted for up to {limit} cards"}


@router.post("/cross-validate")
async def trigger_cross_validation(
    limit: int = 500,
    db: Session = Depends(get_db),
    _user=Depends(require_user),
):
    """Cross-check low-confidence AI tags against connectors to upgrade confidence."""

    async def _run():
        from app.database import SessionLocal
        from app.services.enrichment import cross_validate_batch

        session = SessionLocal()
        try:
            result = await cross_validate_batch(session, limit=limit, concurrency=3)
            logger.info(f"Cross-validation result: {result}")
        except Exception:
            logger.exception("Cross-validation failed")
            session.rollback()
        finally:
            session.close()

    asyncio.create_task(_run())
    return {"ok": True, "message": f"Cross-validation started (limit={limit})"}


@router.post("/repair-visibility")
async def repair_visibility(_user=Depends(require_user)):
    """Recalculate is_visible for all entity tags using corrected thresholds."""

    def _run():
        from app.database import SessionLocal
        from app.services.tagging_backfill import repair_entity_tag_visibility

        session = SessionLocal()
        try:
            result = repair_entity_tag_visibility(session)
            logger.info(f"Visibility repair result: {result}")
        except Exception:
            logger.exception("Visibility repair failed")
            session.rollback()
        finally:
            session.close()

    asyncio.get_event_loop().run_in_executor(None, _run)
    return {"ok": True, "message": "Entity tag visibility repair started in background"}


@router.post("/backfill-sightings")
async def trigger_sighting_backfill(_user=Depends(require_user)):
    """Mine sighting manufacturer data for untagged material cards (no API calls)."""

    def _run():
        from app.database import SessionLocal
        from app.services.tagging_backfill import backfill_manufacturer_from_sightings

        session = SessionLocal()
        try:
            result = backfill_manufacturer_from_sightings(session)
            logger.info(f"Sighting backfill result: {result}")
        except Exception:
            logger.exception("Sighting backfill failed")
            session.rollback()
        finally:
            session.close()

    asyncio.get_event_loop().run_in_executor(None, _run)
    return {"ok": True, "message": "Sighting manufacturer mining started in background"}


@router.post("/boost-confidence")
async def boost_confidence(db: Session = Depends(get_db), _user=Depends(require_user)):
    """Boost confidence for AI tags confirmed by internal data (instant, no API calls).

    Upgrades tags where MaterialCard.manufacturer matches the AI-classified brand.
    """

    def _run():
        from app.database import SessionLocal
        from app.services.enrichment import boost_confidence_internal

        session = SessionLocal()
        try:
            result = boost_confidence_internal(session)
            logger.info(f"Confidence boost result: {result}")
        except Exception:
            logger.exception("Confidence boost failed")
            session.rollback()
        finally:
            session.close()

    asyncio.get_event_loop().run_in_executor(None, _run)
    return {"ok": True, "message": "Internal confidence boost started (no API calls)"}


@router.post("/nexar-validate")
async def trigger_nexar_validate(limit: int = 5000, _user=Depends(require_user)):
    """Bulk validate AI tags via Nexar GraphQL (fast batch queries)."""

    async def _run():
        from app.database import SessionLocal
        from app.services.enrichment import nexar_bulk_validate

        session = SessionLocal()
        try:
            result = await nexar_bulk_validate(session, limit=limit)
            logger.info(f"Nexar validate result: {result}")
        except Exception:
            logger.exception("Nexar validate failed")
            session.rollback()
        finally:
            session.close()

    asyncio.create_task(_run())
    return {"ok": True, "message": f"Nexar bulk validation started (limit={limit})"}


@router.post("/nexar-validate-all")
async def trigger_nexar_validate_all(batch_limit: int = 5000, _user=Depends(require_user)):
    """Loop Nexar bulk validation until all AI-classified tags are checked.

    Runs iteratively with batch_limit per iteration, sleeping between rounds.
    """

    async def _run():
        from app.database import SessionLocal
        from app.services.enrichment import nexar_bulk_validate

        total_confirmed = 0
        total_changed = 0
        total_checked = 0
        iteration = 0

        while True:
            iteration += 1
            session = SessionLocal()
            try:
                result = await nexar_bulk_validate(session, limit=batch_limit)
                checked = result.get("total_checked", 0)
                total_checked += checked
                total_confirmed += result.get("confirmed", 0)
                total_changed += result.get("changed", 0)

                logger.info(
                    f"Nexar validate-all iteration {iteration}: "
                    f"checked={checked}, confirmed={total_confirmed}, changed={total_changed}"
                )

                if checked == 0:
                    break  # No more cards to validate
            except Exception:
                logger.exception(f"Nexar validate-all iteration {iteration} failed")
                session.rollback()
                break
            finally:
                session.close()

            await asyncio.sleep(5)  # Pause between iterations

        logger.info(
            f"Nexar validate-all complete: {total_checked} total checked, "
            f"{total_confirmed} confirmed, {total_changed} changed"
        )

    asyncio.create_task(_run())
    return {"ok": True, "message": f"Nexar validate-all started (batch_limit={batch_limit})"}


@router.post("/nexar-backfill-untagged")
async def trigger_nexar_backfill_untagged(limit: int = 5000, _user=Depends(require_user)):
    """Backfill untagged cards via Nexar for cards that survived prefix/sighting passes."""

    async def _run():
        from app.database import SessionLocal
        from app.services.enrichment import nexar_backfill_untagged

        session = SessionLocal()
        try:
            result = await nexar_backfill_untagged(session, limit=limit)
            logger.info(f"Nexar backfill-untagged result: {result}")
        except Exception:
            logger.exception("Nexar backfill-untagged failed")
            session.rollback()
        finally:
            session.close()

    asyncio.create_task(_run())
    return {"ok": True, "message": f"Nexar backfill for untagged cards started (limit={limit})"}


@router.post("/cross-validate-all")
async def trigger_cross_validate_all(
    batch_limit: int = 500,
    _user=Depends(require_user),
):
    """Loop cross-validation until all low-confidence AI tags are checked.

    Checks connector health first, then iterates with batch_limit per round.
    """

    async def _run():
        from app.database import SessionLocal
        from app.services.enrichment import cross_validate_batch

        total_confirmed = 0
        total_changed = 0
        total_checked = 0
        iteration = 0

        while True:
            iteration += 1
            session = SessionLocal()
            try:
                result = await cross_validate_batch(session, limit=batch_limit, concurrency=3)
                checked = result.get("total", 0)
                total_checked += checked
                total_confirmed += result.get("confirmed", 0)
                total_changed += result.get("changed_manufacturer", 0)

                logger.info(
                    f"Cross-validate-all iteration {iteration}: "
                    f"checked={checked}, confirmed={total_confirmed}, changed={total_changed}"
                )

                if checked == 0:
                    break
            except Exception:
                logger.exception(f"Cross-validate-all iteration {iteration} failed")
                session.rollback()
                break
            finally:
                session.close()

            await asyncio.sleep(10)  # Longer pause — connector rate limits

        logger.info(
            f"Cross-validate-all complete: {total_checked} total checked, "
            f"{total_confirmed} confirmed, {total_changed} changed"
        )

    asyncio.create_task(_run())
    return {"ok": True, "message": f"Cross-validate-all started (batch_limit={batch_limit})"}


@router.post("/purge-unknown")
async def purge_unknown_tags(_user=Depends(require_user)):
    """Purge 'Unknown' brand junk tags that block reprocessing (instant, no API calls)."""

    def _run():
        from app.database import SessionLocal
        from app.services.tagging_backfill import purge_unknown_tags

        session = SessionLocal()
        try:
            result = purge_unknown_tags(session)
            logger.info(f"Purge unknown result: {result}")
        except Exception:
            logger.exception("Purge unknown failed")
            session.rollback()
        finally:
            session.close()

    asyncio.get_event_loop().run_in_executor(None, _run)
    return {"ok": True, "message": "Purging 'Unknown' junk tags in background"}


@router.get("/analyze-prefixes")
async def analyze_prefixes(db: Session = Depends(get_db), _user=Depends(require_user)):
    """Analyze untagged MPNs to discover missing prefix patterns."""
    from app.services.tagging_backfill import analyze_untagged_prefixes

    results = analyze_untagged_prefixes(db)
    return {"ok": True, "candidates": results, "count": len(results)}


@router.post("/boost-cascade")
async def trigger_boost_cascade(_user=Depends(require_user)):
    """Run full internal boost cascade: confidence boost + sighting mining + prefix backfill.

    Call this after applying AI batch results or connector enrichment to propagate
    all newly discovered manufacturer data.
    """

    def _run():
        from app.database import SessionLocal
        from app.services.enrichment import boost_confidence_internal
        from app.services.tagging_backfill import (
            backfill_manufacturer_from_sightings,
            run_prefix_backfill,
        )

        session = SessionLocal()
        try:
            r1 = boost_confidence_internal(session)
            logger.info(f"Boost cascade — confidence boost: {r1}")

            r2 = backfill_manufacturer_from_sightings(session)
            logger.info(f"Boost cascade — sighting mining: {r2}")

            r3 = run_prefix_backfill(session)
            logger.info(f"Boost cascade — prefix backfill: {r3}")

            logger.info("Boost cascade complete")
        except Exception:
            logger.exception("Boost cascade failed")
            session.rollback()
        finally:
            session.close()

    asyncio.get_event_loop().run_in_executor(None, _run)
    return {"ok": True, "message": "Boost cascade started (confidence boost → sighting mining → prefix backfill)"}


@router.post("/triage-internal")
async def trigger_triage(limit: int = 50000, db: Session = Depends(get_db), _user=Depends(require_user)):
    """Triage untagged cards as real MPNs vs internal part numbers."""

    async def _run():
        from app.database import SessionLocal
        from app.services.tagging_ai import submit_triage_batch

        session = SessionLocal()
        try:
            result = await submit_triage_batch(session, limit=limit)
            logger.info(f"Triage result: {result}")
        except Exception:
            logger.exception("Triage failed")
            session.rollback()
        finally:
            session.close()

    asyncio.create_task(_run())
    return {"ok": True, "message": f"Internal part triage started (limit={limit})"}


@router.post("/apply-triage")
async def apply_triage(batch_id: str, _user=Depends(require_user)):
    """Apply triage batch results to flag internal parts."""

    async def _run():
        from app.services.tagging_ai import apply_triage_results

        result = await apply_triage_results(batch_id)
        logger.info(f"Triage apply result: {result}")

    asyncio.create_task(_run())
    return {"ok": True, "message": f"Applying triage results for batch {batch_id}"}
