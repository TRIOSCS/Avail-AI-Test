"""Search queue manager — shared logic for enqueue, dedup, status updates.

Parameterized by queue model class, source_type string, config prefix,
and an optional sighting linking callback. Handles enqueuing parts for
search, deduplication, polling the next item, and updating queue status.

Called by: requisition triggers, ai_gate, worker loop, admin endpoints
Depends on: queue model, sightings model, mpn_normalizer,
            vendor_unavailability (apply_to_fresh_sightings on dedup-cloned rows)
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.constants import RequisitionStatus, SearchQueueStatus
from app.models import Requirement, Sighting
from app.models.base import Base
from app.models.sourcing import Requisition
from app.services.vendor_unavailability import apply_to_fresh_sightings
from app.utils.normalization import normalize_mpn_key

from .mpn_normalizer import strip_packaging_suffixes

# Requisition statuses that indicate active sourcing work (Sales Hub open pipeline:
# open/rfqs_sent/offers/quoted). "open" automatically means sourcing.
_ACTIVE_STATUSES = set(RequisitionStatus.OPEN_PIPELINE)


# QM is the queue model this manager instance operates on (IcsSearchQueue,
# NcSearchQueue, TbfSearchQueue) — makes enqueue/get/claim return the concrete row
# type. Bound to the declarative Base so mypy accepts the **kwargs constructor.
class QueueManager[QM: Base]:
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
        queue_model: type[QM],
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
            created_at=datetime.now(UTC),
        )

    def enqueue_search(
        self,
        requirement_id: int,
        db: Session,
        override_mpn: str | None = None,
        resolved_via_spec_code: str | None = None,
    ) -> QM | None:
        """Queue a requirement for browser-driven search.

        When ``override_mpn`` is None (default), the worker reads
        ``req.primary_mpn``. When ``override_mpn`` is provided (a resolved
        AVL MPN from the spec-code resolver), the worker searches that MPN
        instead; ``resolved_via_spec_code`` is recorded on the queue row.
        (The worker's sighting writers do not yet copy that lineage tag onto
        the sightings they create — only the synchronous fanout in
        search_service.py tags sightings today.)

        Dedup short-circuit keys on ``(requirement_id, normalized_mpn)`` so
        one requirement can have multiple queue rows (primary + resolved
        AVL MPNs).

        Checks for cross-requirement dedup (same normalized MPN searched
        within dedup window). If deduped, links existing sightings to this
        requirement's material card. Returns the queue item or None if
        deduped/skipped.
        """
        req = db.get(Requirement, requirement_id)
        if not req:
            logger.debug("{} enqueue skip: requirement {} not found", self.log_prefix, requirement_id)
            return None

        mpn_to_search = override_mpn or req.primary_mpn
        if not mpn_to_search:
            logger.debug("{} enqueue skip: requirement {} has no MPN", self.log_prefix, requirement_id)
            return None

        norm_mpn = strip_packaging_suffixes(mpn_to_search)
        if not norm_mpn:
            return None

        model = self.queue_model

        # Already queued for THIS (requirement, mpn) pair? Re-keyed from the
        # legacy per-requirement check so resolver-driven AVL enqueues can
        # coexist with the primary MPN row for the same requirement.
        existing = db.query(model).filter_by(requirement_id=requirement_id, normalized_mpn=norm_mpn).first()
        if existing:
            logger.debug(
                "{} enqueue skip: requirement {} mpn {} already queued (id={})",
                self.log_prefix,
                requirement_id,
                norm_mpn,
                existing.id,
            )
            return existing

        # Dedup: look for completed searches of same normalized MPN within window
        cutoff = datetime.now(UTC) - timedelta(days=self.dedup_window_days)
        recent = (
            db.query(model)
            .filter(
                model.normalized_mpn == norm_mpn,
                model.status == SearchQueueStatus.COMPLETED,
                model.last_searched_at >= cutoff,
            )
            .first()
        )

        if recent:
            # Link existing sightings from the previous search to this requirement's material card
            if req.material_card_id and recent.requirement_id:
                # Scope to the deduped MPN only. A requirement can have multiple
                # queue rows (primary + resolved-AVL MPNs), so its sightings span
                # several normalized MPNs; without scoping we'd clone the source
                # requirement's OTHER MPNs' sightings onto THIS requirement.
                # Compare on the canonical MPN KEY (normalize_mpn_key), NOT the raw
                # column: sightings store strip_packaging_suffixes(the vendor's TYPED
                # part number), which preserves internal dashes/dots, so a vendor who
                # listed "ABC-123" for a search of "ABC123" would be dropped by raw
                # equality. The key form collapses both to "abc123" — cloning
                # punctuation variants of the SAME part while still excluding the
                # requirement's other MPNs (whose keys differ). Filtered in Python
                # because the stored column is the packaging-suffix form, not the key.
                target_key = normalize_mpn_key(norm_mpn)
                existing_sightings = [
                    s
                    for s in db.query(Sighting)
                    .filter(
                        Sighting.requirement_id == recent.requirement_id,
                        Sighting.source_type == self.source_type,
                    )
                    .all()
                    if normalize_mpn_key(s.normalized_mpn) == target_key
                ]
                cloned_rows = []
                for s in existing_sightings:
                    new_s = self._link_sighting(s, requirement_id, req.material_card_id, self.source_type)
                    db.add(new_s)
                    cloned_rows.append(new_s)
                # The cross-requirement dedup clones prior rows onto THIS
                # requirement — re-apply durable vendor+part unavailability
                # knowledge before the commit so the clones never resurrect a
                # dead vendor (never-gated rows would render a false
                # "Possible restock" chip).
                apply_to_fresh_sightings(db, req, cloned_rows)
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
            mpn=mpn_to_search,
            normalized_mpn=norm_mpn,
            manufacturer=req.brand,
            status=SearchQueueStatus.PENDING,
            priority=priority,
            resolved_via_spec_code=resolved_via_spec_code,
        )
        db.add(item)
        try:
            db.commit()
        except IntegrityError:
            # Lost the insert race against a concurrent enqueue for the same
            # (requirement_id, normalized_mpn): the uq_*_queue_requirement_mpn
            # constraint rejected our row. Roll back and return the row the
            # winner committed — identical to the in-Python ``existing`` path.
            db.rollback()
            winner = db.query(model).filter_by(requirement_id=requirement_id, normalized_mpn=norm_mpn).first()
            logger.debug(
                "{} enqueue race: requirement {} mpn {} already inserted concurrently (id={})",
                self.log_prefix,
                requirement_id,
                norm_mpn,
                getattr(winner, "id", None),
            )
            return winner
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
        stale = db.query(model).filter(model.status == SearchQueueStatus.SEARCHING).all()
        count = 0
        for item in stale:
            item.status = SearchQueueStatus.QUEUED
            item.error_message = "Reset from stale 'searching' status on worker restart"
            item.updated_at = datetime.now(UTC)
            count += 1
        if count:
            db.commit()
            logger.info("Recovered {} stale queue items from 'searching' -> 'queued'", count)
        return count

    # Items stuck in "searching" longer than this are presumed abandoned by a
    # crashed/killed worker and are reclaimed back to "queued".
    STUCK_SEARCH_TIMEOUT_MINUTES = 30

    def get_next_queued_item(self, db: Session) -> QM | None:
        """Get the next queued item — priority ASC (lowest first), then newest first.

        NOTE: read-only (does not claim). Prefer ``claim_next_queued_item`` in the
        worker loop so concurrent workers don't double-pick the same row.
        """
        model = self.queue_model
        return (
            db.query(model)
            .filter(model.status == SearchQueueStatus.QUEUED)
            .order_by(model.priority.asc(), model.created_at.desc())
            .first()
        )

    def reclaim_stuck_searches(self, db: Session, max_age_minutes: int | None = None) -> int:
        """Reset items stuck in 'searching' past the timeout back to 'queued'.

        Unlike ``recover_stale_searches`` (startup-only), this is safe to call on a
        cadence: a worker that crashed mid-search has its in-flight item picked up
        by another worker without waiting for a restart.
        """
        model = self.queue_model
        timeout = max_age_minutes or self.STUCK_SEARCH_TIMEOUT_MINUTES
        cutoff = datetime.now(UTC) - timedelta(minutes=timeout)
        stuck = db.query(model).filter(model.status == SearchQueueStatus.SEARCHING, model.updated_at < cutoff).all()
        for item in stuck:
            item.status = SearchQueueStatus.QUEUED
            item.error_message = f"Reclaimed from stale 'searching' after {timeout}m"
            item.updated_at = datetime.now(UTC)
        if stuck:
            db.commit()
            logger.warning("{} reclaimed {} stuck 'searching' item(s) -> 'queued'", self.log_prefix, len(stuck))
        return len(stuck)

    def claim_next_queued_item(self, db: Session) -> QM | None:
        """Atomically claim the next queued item.

        Selects the next 'queued' row and marks it 'searching' in one short
        transaction. On PostgreSQL it uses ``FOR UPDATE SKIP LOCKED`` so multiple
        concurrent workers never grab the same row; SQLite (tests) has no such
        support and is single-threaded, so it falls back to a plain read.

        Also reclaims stale 'searching' items first, so a crashed worker's
        in-flight work is recovered automatically. Returns the claimed item or None.
        """
        self.reclaim_stuck_searches(db)
        model = self.queue_model
        q = (
            db.query(model)
            .filter(model.status == SearchQueueStatus.QUEUED)
            .order_by(model.priority.asc(), model.created_at.desc())
        )
        dialect = ""
        try:
            dialect = db.get_bind().dialect.name
        except Exception:  # pragma: no cover - defensive
            pass
        if dialect == "postgresql":
            q = q.with_for_update(skip_locked=True)

        item = q.first()
        if item is None:
            return None
        item.status = SearchQueueStatus.SEARCHING
        item.updated_at = datetime.now(UTC)
        db.commit()
        db.refresh(item)
        logger.debug("{} claimed queue item {} (mpn={})", self.log_prefix, item.id, item.normalized_mpn)
        return item

    def mark_status(self, db: Session, queue_item, new_status: str | SearchQueueStatus, error: str | None = None):
        """Update a queue item's status."""
        queue_item.status = new_status
        queue_item.updated_at = datetime.now(UTC)
        if error:
            queue_item.error_message = error
        db.commit()
        logger.debug("{} queue {} status -> {}", self.log_prefix, queue_item.id, new_status)

    def mark_completed(self, db: Session, queue_item, results_found: int, sightings_created: int):
        """Mark a queue item as completed with result counts."""
        queue_item.status = SearchQueueStatus.COMPLETED
        queue_item.last_searched_at = datetime.now(UTC)
        queue_item.results_count = results_found
        queue_item.search_count = (queue_item.search_count or 0) + 1
        queue_item.updated_at = datetime.now(UTC)
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

        today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        total_today = (
            db.query(func.count())
            .filter(
                model.status == SearchQueueStatus.COMPLETED,
                model.last_searched_at >= today_start,
            )
            .scalar()
        ) or 0

        return {
            "pending": counts.get(SearchQueueStatus.PENDING, 0),
            "queued": counts.get(SearchQueueStatus.QUEUED, 0),
            "searching": counts.get(SearchQueueStatus.SEARCHING, 0),
            "completed": counts.get(SearchQueueStatus.COMPLETED, 0),
            "failed": counts.get(SearchQueueStatus.FAILED, 0),
            "gated_out": counts.get(SearchQueueStatus.GATED_OUT, 0),
            "total_today": total_today,
            "remaining": counts.get(SearchQueueStatus.PENDING, 0) + counts.get(SearchQueueStatus.QUEUED, 0),
        }
