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


def enqueue_for_nc_search(requirement_id: int, db: Session) -> NcSearchQueue | None:
    """Queue a requirement for NetComponents search."""
    return _qm.enqueue_search(requirement_id, db)


def recover_stale_searches(db: Session) -> int:
    """Reset any items stuck in 'searching' status (from a previous crash)."""
    return _qm.recover_stale_searches(db)


def get_next_queued_item(db: Session) -> NcSearchQueue | None:
    """Get the next queued item — priority ASC (lowest first), then newest first."""
    return _qm.get_next_queued_item(db)


def mark_status(db: Session, queue_item: NcSearchQueue, new_status: str, error: str | None = None):
    """Update a queue item's status."""
    _qm.mark_status(db, queue_item, new_status, error)


def mark_completed(db: Session, queue_item: NcSearchQueue, results_found: int, sightings_created: int):
    """Mark a queue item as completed with result counts."""
    _qm.mark_completed(db, queue_item, results_found, sightings_created)


def get_queue_stats(db: Session) -> dict:
    """Return queue statistics by status plus daily totals."""
    return _qm.get_queue_stats(db)
