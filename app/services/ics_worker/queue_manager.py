"""ICsource search queue manager.

Handles enqueuing parts for ICS search, deduplication, polling the
next item to search, and updating queue item status.

Called by: requisition triggers, ai_gate, worker loop, admin endpoints
Depends on: ics_search_queue model, sightings model, mpn_normalizer
"""

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import IcsSearchQueue, Requirement, Sighting
from app.models.sourcing import Requisition

from .config import IcsConfig
from .mpn_normalizer import normalize_mpn

# Requisition statuses that indicate active sourcing work
_ACTIVE_STATUSES = {"active", "sourcing", "offers", "quoting", "reopened"}

_config = IcsConfig()


def enqueue_for_ics_search(requirement_id: int, db: Session) -> IcsSearchQueue | None:
    """Queue a requirement for ICsource search.

    Checks for dedup (same normalized MPN searched within dedup window). If deduped,
    links existing ICS sightings to this requirement's material card. Returns the queue
    item or None if deduped/skipped.
    """
    req = db.get(Requirement, requirement_id)
    if not req or not req.primary_mpn:
        logger.debug("ICS enqueue skip: requirement {} has no MPN", requirement_id)
        return None

    norm_mpn = normalize_mpn(req.primary_mpn)
    if not norm_mpn:
        return None

    # Check if already queued
    existing = db.query(IcsSearchQueue).filter_by(requirement_id=requirement_id).first()
    if existing:
        logger.debug("ICS enqueue skip: requirement {} already queued (id={})", requirement_id, existing.id)
        return existing

    # Dedup: look for completed searches of same normalized MPN within window
    cutoff = datetime.now(timezone.utc) - timedelta(days=_config.ICS_DEDUP_WINDOW_DAYS)
    recent = (
        db.query(IcsSearchQueue)
        .filter(
            IcsSearchQueue.normalized_mpn == norm_mpn,
            IcsSearchQueue.status == "completed",
            IcsSearchQueue.last_searched_at >= cutoff,
        )
        .first()
    )

    if recent:
        # Link existing ICS sightings from the previous search to this requirement's material card
        if req.material_card_id and recent.requirement_id:
            ics_sightings = (
                db.query(Sighting)
                .filter(
                    Sighting.requirement_id == recent.requirement_id,
                    Sighting.source_type == "icsource",
                )
                .all()
            )
            for s in ics_sightings:
                new_s = Sighting(
                    requirement_id=requirement_id,
                    material_card_id=req.material_card_id,
                    vendor_name=s.vendor_name,
                    vendor_name_normalized=s.vendor_name_normalized,
                    vendor_email=s.vendor_email,
                    vendor_phone=s.vendor_phone,
                    mpn_matched=s.mpn_matched,
                    normalized_mpn=s.normalized_mpn,
                    manufacturer=s.manufacturer,
                    qty_available=s.qty_available,
                    unit_price=s.unit_price,
                    currency=s.currency,
                    source_type="icsource",
                    source_searched_at=s.source_searched_at,
                    is_authorized=s.is_authorized,
                    confidence=s.confidence,
                    date_code=s.date_code,
                    raw_data=s.raw_data,
                    created_at=datetime.now(timezone.utc),
                )
                db.add(new_s)
            db.commit()
            logger.info(
                "ICS dedup: requirement {} linked {} sightings from previous search (queue {})",
                requirement_id,
                len(ics_sightings),
                recent.id,
            )
        else:
            logger.info("ICS dedup: requirement {} matched recent search but no material card to link", requirement_id)
        return None

    # Priority: 1 for actively-sourced requisitions, 3 for everything else
    reqn = db.get(Requisition, req.requisition_id) if req.requisition_id else None
    priority = 1 if reqn and reqn.status in _ACTIVE_STATUSES else 3

    # Create new queue entry
    item = IcsSearchQueue(
        requirement_id=requirement_id,
        requisition_id=req.requisition_id,
        mpn=req.primary_mpn,
        normalized_mpn=norm_mpn,
        manufacturer=req.brand,
        status="pending",
        priority=priority,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    logger.info("ICS enqueue: requirement {} queued as item {} (mpn={})", requirement_id, item.id, norm_mpn)
    return item


def recover_stale_searches(db: Session) -> int:
    """Reset any items stuck in 'searching' status (from a previous crash).

    Called once on worker startup.
    """
    stale = db.query(IcsSearchQueue).filter(IcsSearchQueue.status == "searching").all()
    count = 0
    for item in stale:
        item.status = "queued"
        item.error_message = "Reset from stale 'searching' status on worker restart"
        item.updated_at = datetime.now(timezone.utc)
        count += 1
    if count:
        db.commit()
        logger.info("Recovered {} stale queue items from 'searching' → 'queued'", count)
    return count


def get_next_queued_item(db: Session) -> IcsSearchQueue | None:
    """Get the next queued item — active-sourcing first (priority 1), newest first."""
    return (
        db.query(IcsSearchQueue)
        .filter(IcsSearchQueue.status == "queued")
        .order_by(IcsSearchQueue.priority.asc(), IcsSearchQueue.created_at.desc())
        .first()
    )


def mark_status(db: Session, queue_item: IcsSearchQueue, new_status: str, error: str | None = None):
    """Update a queue item's status."""
    queue_item.status = new_status
    queue_item.updated_at = datetime.now(timezone.utc)
    if error:
        queue_item.error_message = error
    db.commit()
    logger.debug("ICS queue {} status -> {}", queue_item.id, new_status)


def mark_completed(db: Session, queue_item: IcsSearchQueue, results_found: int, sightings_created: int):
    """Mark a queue item as completed with result counts."""
    queue_item.status = "completed"
    queue_item.last_searched_at = datetime.now(timezone.utc)
    queue_item.results_count = results_found
    queue_item.search_count = (queue_item.search_count or 0) + 1
    queue_item.updated_at = datetime.now(timezone.utc)
    db.commit()
    logger.info(
        "ICS queue {} completed: {} results, {} sightings",
        queue_item.id,
        results_found,
        sightings_created,
    )


def get_queue_stats(db: Session) -> dict:
    """Return queue statistics by status plus daily totals."""
    rows = db.query(IcsSearchQueue.status, func.count()).group_by(IcsSearchQueue.status).all()
    counts = {status: count for status, count in rows}

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    total_today = (
        db.query(func.count())
        .filter(
            IcsSearchQueue.status == "completed",
            IcsSearchQueue.last_searched_at >= today_start,
        )
        .scalar()
    ) or 0

    return {
        "pending": counts.get("pending", 0),
        "queued": counts.get("queued", 0),
        "searching": counts.get("searching", 0),
        "completed": counts.get("completed", 0),
        "failed": counts.get("failed", 0),
        "gated_out": counts.get("gated_out", 0),
        "total_today": total_today,
        "remaining": counts.get("pending", 0) + counts.get("queued", 0),
    }
