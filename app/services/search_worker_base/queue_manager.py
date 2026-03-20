"""Search queue manager — shared logic for enqueue, dedup, status updates.

Parameterized by queue model class, source_type string, config prefix,
and an optional sighting linking callback. Handles enqueuing parts for
search, deduplication, polling the next item, and updating queue status.

Called by: requisition triggers, ai_gate, worker loop, admin endpoints
Depends on: queue model, sightings model, mpn_normalizer
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Requirement, Sighting
from app.models.sourcing import Requisition

from .mpn_normalizer import strip_packaging_suffixes

# Requisition statuses that indicate active sourcing work
_ACTIVE_STATUSES = {"active", "sourcing", "offers", "quoting", "reopened"}


class QueueManager:
    """Parameterized queue manager for any search worker.

    Args:
        queue_model: The SQLAlchemy model class for the queue table
            (e.g. IcsSearchQueue, NcSearchQueue).
        source_type: The source_type string for sightings
            (e.g. "icsource", "netcomponents").
        dedup_window_days: Number of days for dedup window.
        log_prefix: Short prefix for log messages (e.g. "ICS", "NC").
        link_sighting_fn: Optional callback to create a linked sighting.
            Signature: (original_sighting, requirement_id, material_card_id) -> Sighting.
            If None, a default linker is used that copies all common fields.
    """

    def __init__(
        self,
        queue_model: type,
        source_type: str,
        dedup_window_days: int = 7,
        log_prefix: str = "WORKER",
        link_sighting_fn: Callable[..., Any] | None = None,
    ):
        self.queue_model = queue_model
        self.source_type = source_type
        self.dedup_window_days = dedup_window_days
        self.log_prefix = log_prefix
        self._link_sighting = link_sighting_fn or self._default_link_sighting

    @staticmethod
    def _default_link_sighting(s: Sighting, requirement_id: int, material_card_id: int, source_type: str) -> Sighting:
        """Default sighting linker — copies all common fields."""
        return Sighting(
            requirement_id=requirement_id,
            material_card_id=material_card_id,
            vendor_name=s.vendor_name,
            vendor_name_normalized=s.vendor_name_normalized,
            vendor_email=getattr(s, "vendor_email", None),
            vendor_phone=getattr(s, "vendor_phone", None),
            mpn_matched=s.mpn_matched,
            normalized_mpn=s.normalized_mpn,
            manufacturer=s.manufacturer,
            qty_available=s.qty_available,
            unit_price=s.unit_price,
            currency=s.currency,
            source_type=source_type,
            source_searched_at=s.source_searched_at,
            is_authorized=s.is_authorized,
            confidence=s.confidence,
            date_code=s.date_code,
            raw_data=s.raw_data,
            created_at=datetime.now(timezone.utc),
        )

    def enqueue_search(self, requirement_id: int, db: Session):
        """Queue a requirement for search.

        Checks for dedup (same normalized MPN searched within dedup window). If deduped,
        links existing sightings to this requirement's material card. Returns the queue
        item or None if deduped/skipped.
        """
        req = db.get(Requirement, requirement_id)
        if not req or not req.primary_mpn:
            logger.debug("{} enqueue skip: requirement {} has no MPN", self.log_prefix, requirement_id)
            return None

        norm_mpn = strip_packaging_suffixes(req.primary_mpn)
        if not norm_mpn:
            return None

        model = self.queue_model

        # Check if already queued
        existing = db.query(model).filter_by(requirement_id=requirement_id).first()
        if existing:
            logger.debug(
                "{} enqueue skip: requirement {} already queued (id={})", self.log_prefix, requirement_id, existing.id
            )
            return existing

        # Dedup: look for completed searches of same normalized MPN within window
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.dedup_window_days)
        recent = (
            db.query(model)
            .filter(
                model.normalized_mpn == norm_mpn,
                model.status == "completed",
                model.last_searched_at >= cutoff,
            )
            .first()
        )

        if recent:
            # Link existing sightings from the previous search to this requirement's material card
            if req.material_card_id and recent.requirement_id:
                existing_sightings = (
                    db.query(Sighting)
                    .filter(
                        Sighting.requirement_id == recent.requirement_id,
                        Sighting.source_type == self.source_type,
                    )
                    .all()
                )
                for s in existing_sightings:
                    new_s = self._link_sighting(s, requirement_id, req.material_card_id, self.source_type)
                    db.add(new_s)
                db.commit()
                logger.info(
                    "{} dedup: requirement {} linked {} sightings from previous search (queue {})",
                    self.log_prefix,
                    requirement_id,
                    len(existing_sightings),
                    recent.id,
                )
            else:
                logger.info(
                    "{} dedup: requirement {} matched recent search but no material card to link",
                    self.log_prefix,
                    requirement_id,
                )
            return None

        # Priority: 1 for actively-sourced requisitions, 3 for everything else
        reqn = db.get(Requisition, req.requisition_id) if req.requisition_id else None
        priority = 1 if reqn and reqn.status in _ACTIVE_STATUSES else 3

        # Create new queue entry
        item = model(
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
        logger.info(
            "{} enqueue: requirement {} queued as item {} (mpn={})", self.log_prefix, requirement_id, item.id, norm_mpn
        )
        return item

    def recover_stale_searches(self, db: Session) -> int:
        """Reset any items stuck in 'searching' status (from a previous crash).

        Called once on worker startup.
        """
        model = self.queue_model
        stale = db.query(model).filter(model.status == "searching").all()
        count = 0
        for item in stale:
            item.status = "queued"
            item.error_message = "Reset from stale 'searching' status on worker restart"
            item.updated_at = datetime.now(timezone.utc)
            count += 1
        if count:
            db.commit()
            logger.info("Recovered {} stale queue items from 'searching' -> 'queued'", count)
        return count

    def get_next_queued_item(self, db: Session):
        """Get the next queued item — priority ASC (lowest first), then newest first."""
        model = self.queue_model
        return (
            db.query(model)
            .filter(model.status == "queued")
            .order_by(model.priority.asc(), model.created_at.desc())
            .first()
        )

    def mark_status(self, db: Session, queue_item, new_status: str, error: str | None = None):
        """Update a queue item's status."""
        queue_item.status = new_status
        queue_item.updated_at = datetime.now(timezone.utc)
        if error:
            queue_item.error_message = error
        db.commit()
        logger.debug("{} queue {} status -> {}", self.log_prefix, queue_item.id, new_status)

    def mark_completed(self, db: Session, queue_item, results_found: int, sightings_created: int):
        """Mark a queue item as completed with result counts."""
        queue_item.status = "completed"
        queue_item.last_searched_at = datetime.now(timezone.utc)
        queue_item.results_count = results_found
        queue_item.search_count = (queue_item.search_count or 0) + 1
        queue_item.updated_at = datetime.now(timezone.utc)
        db.commit()
        logger.info(
            "{} queue {} completed: {} results, {} sightings",
            self.log_prefix,
            queue_item.id,
            results_found,
            sightings_created,
        )

    def get_queue_stats(self, db: Session) -> dict:
        """Return queue statistics by status plus daily totals."""
        model = self.queue_model
        rows = db.query(model.status, func.count()).group_by(model.status).all()
        counts = {status: count for status, count in rows}

        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        total_today = (
            db.query(func.count())
            .filter(
                model.status == "completed",
                model.last_searched_at >= today_start,
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
