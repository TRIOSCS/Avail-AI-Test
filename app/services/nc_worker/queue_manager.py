"""NetComponents search queue manager — thin wrapper around search_worker_base.

Delegates to the shared QueueManager class with NC-specific parameters
(model=NcSearchQueue, source_type="netcomponents"). Exposes the same function
signatures as the original for full backward compatibility.

Called by: requisition triggers, ai_gate, worker loop, admin endpoints
Depends on: search_worker_base.queue_manager, NcSearchQueue model, config
"""

from sqlalchemy.orm import Session

from app.models import NcSearchQueue

from ..search_worker_base.queue_manager import QueueManager
from .config import NcConfig

_config = NcConfig()

_qm = QueueManager(
    queue_model=NcSearchQueue,
    source_type="netcomponents",
    dedup_window_days=_config.NC_DEDUP_WINDOW_DAYS,
    log_prefix="NC",
)


def enqueue_for_nc_search(
    requirement_id: int,
    db: Session,
    override_mpn: str | None = None,
    resolved_via_spec_code: str | None = None,
) -> NcSearchQueue | None:
    """Queue a requirement for NetComponents search.

    Optional ``override_mpn`` enables enqueueing a resolved-AVL MPN distinct
    from ``req.primary_mpn`` (spec §6.4). ``resolved_via_spec_code`` is
    recorded on the queue row for lineage tracking.
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


def get_next_queued_item(db: Session) -> NcSearchQueue | None:
    """Get the next queued item — priority ASC (lowest first), then newest first."""
    return _qm.get_next_queued_item(db)


def claim_next_queued_item(db: Session) -> NcSearchQueue | None:
    """Atomically claim the next queued item (mark 'searching'; skip-locked on PG)."""
    return _qm.claim_next_queued_item(db)


def reclaim_stuck_searches(db: Session, max_age_minutes: int | None = None) -> int:
    """Reclaim items stuck in 'searching' past the timeout (crashed worker)."""
    return _qm.reclaim_stuck_searches(db, max_age_minutes)


def mark_status(db: Session, queue_item: NcSearchQueue, new_status: str, error: str | None = None):
    """Update a queue item's status."""
    _qm.mark_status(db, queue_item, new_status, error)


def mark_completed(db: Session, queue_item: NcSearchQueue, results_found: int, sightings_created: int):
    """Mark a queue item as completed with result counts."""
    _qm.mark_completed(db, queue_item, results_found, sightings_created)


def get_queue_stats(db: Session) -> dict:
    """Return queue statistics by status plus daily totals."""
    return _qm.get_queue_stats(db)
