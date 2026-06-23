"""The Broker Forum (TBF) search queue manager — thin wrapper around search_worker_base.

Delegates to the shared QueueManager class with TBF-specific parameters
(model=TbfSearchQueue, source_type="thebrokersite"). Exposes the same function
signatures as the ICS/NC wrappers for full consistency.

Called by: requisition triggers, ai_gate, worker loop, admin endpoints
Depends on: search_worker_base.queue_manager, TbfSearchQueue model, config
"""

from sqlalchemy.orm import Session

from app.models import TbfSearchQueue

from ..search_worker_base.queue_manager import QueueManager
from .config import TbfConfig

_config = TbfConfig()

_qm = QueueManager(
    queue_model=TbfSearchQueue,
    source_type="thebrokersite",
    dedup_window_days=_config.TBF_DEDUP_WINDOW_DAYS,
    log_prefix="TBF",
)


def enqueue_for_tbf_search(
    requirement_id: int,
    db: Session,
    override_mpn: str | None = None,
    resolved_via_spec_code: str | None = None,
) -> TbfSearchQueue | None:
    """Queue a requirement for The Broker Forum search.

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


def get_next_queued_item(db: Session) -> TbfSearchQueue | None:
    """Get the next queued item — priority ASC (lowest first), then newest first."""
    return _qm.get_next_queued_item(db)


def claim_next_queued_item(db: Session) -> TbfSearchQueue | None:
    """Atomically claim the next queued item (mark 'searching'; skip-locked on PG)."""
    return _qm.claim_next_queued_item(db)


def reclaim_stuck_searches(db: Session, max_age_minutes: int | None = None) -> int:
    """Reclaim items stuck in 'searching' past the timeout (crashed worker)."""
    return _qm.reclaim_stuck_searches(db, max_age_minutes)


def mark_status(db: Session, queue_item: TbfSearchQueue, new_status: str, error: str | None = None):
    """Update a queue item's status."""
    _qm.mark_status(db, queue_item, new_status, error)


def mark_completed(db: Session, queue_item: TbfSearchQueue, results_found: int, sightings_created: int):
    """Mark a queue item as completed with result counts."""
    _qm.mark_completed(db, queue_item, results_found, sightings_created)


def get_queue_stats(db: Session) -> dict:
    """Return queue statistics by status plus daily totals."""
    return _qm.get_queue_stats(db)
