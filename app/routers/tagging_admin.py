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

    return {
        "total_material_cards": total_cards,
        "tagged_count": tagged_count,
        "untagged_count": untagged_count,
        "brand_tag_count": brand_count,
        "commodity_tag_count": commodity_count,
        "coverage_percentage": coverage,
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
