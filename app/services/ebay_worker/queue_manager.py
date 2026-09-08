"""EBay search queue manager — thin wrapper around search_worker_base.

Delegates to the shared QueueManager class with eBay-specific parameters
(model=EbaySearchQueue, source_type="ebay"). Exposes the same function
signatures as the ICS/NC/TBF wrappers for full consistency.

Called by: requisition triggers (search_service._worker_enqueues), worker
           loop, admin endpoints
Depends on: search_worker_base.queue_manager, EbaySearchQueue model, config
"""

from sqlalchemy.orm import Session

from app.constants import SearchQueueStatus
from app.models import EbaySearchQueue

from ..search_worker_base.queue_manager import QueueManager
from .config import EbayConfig

_config = EbayConfig()

_qm = QueueManager(
    queue_model=EbaySearchQueue,
    source_type="ebay",
    dedup_window_days=_config.EBAY_DEDUP_WINDOW_DAYS,
    log_prefix="EBAY",
    # Rows are enqueued QUEUED, not PENDING. PENDING exists for the browser
    # workers' AI commodity gate, which promotes PENDING -> QUEUED; the eBay
    # worker deliberately has no gate (every queued MPN is searched, spend is
    # bounded by the daily call budget), so a PENDING row here would never be
    # claimable and the queue would grow forever.
    initial_status=SearchQueueStatus.QUEUED,
)


def enqueue_for_ebay_search(
    requirement_id: int,
    db: Session,
    override_mpn: str | None = None,
    resolved_via_spec_code: str | None = None,
) -> EbaySearchQueue | None:
    """Queue a requirement for an eBay Browse API search.

    The row is created directly in ``queued`` (no AI gate stands between
    enqueue and the worker's claim — see the QueueManager construction above).

    Optional ``override_mpn`` enables enqueueing a resolved-AVL MPN distinct
    from ``req.primary_mpn``. ``resolved_via_spec_code`` is recorded on the
    queue row for lineage tracking.
    """
    return _qm.enqueue_search(
        requirement_id,
        db,
        override_mpn=override_mpn,
        resolved_via_spec_code=resolved_via_spec_code,
    )


def recover_stale_searches(db: Session) -> int:
    """Reset any items stuck in 'searching' status (from a previous crash)."""
    return _qm.recover_stale_searches(db)


def get_next_queued_item(db: Session) -> EbaySearchQueue | None:
    """Get the next queued item — priority ASC (lowest first), then newest first."""
    return _qm.get_next_queued_item(db)


def claim_next_queued_item(db: Session) -> EbaySearchQueue | None:
    """Atomically claim the next queued item (mark 'searching'; skip-locked on PG)."""
    return _qm.claim_next_queued_item(db)


def reclaim_stuck_searches(db: Session, max_age_minutes: int | None = None) -> int:
    """Reclaim items stuck in 'searching' past the timeout (crashed worker)."""
    return _qm.reclaim_stuck_searches(db, max_age_minutes)


def mark_status(db: Session, queue_item: EbaySearchQueue, new_status: str, error: str | None = None):
    """Update a queue item's status."""
    _qm.mark_status(db, queue_item, new_status, error)


def mark_completed(db: Session, queue_item: EbaySearchQueue, results_found: int, sightings_created: int):
    """Mark a queue item as completed with result counts."""
    _qm.mark_completed(db, queue_item, results_found, sightings_created)


def get_queue_stats(db: Session) -> dict:
    """Return queue statistics by status plus daily totals."""
    return _qm.get_queue_stats(db)
