"""ICsource search queue manager — thin wrapper around search_worker_base.

Delegates to the shared QueueManager class with ICS-specific parameters
(model=IcsSearchQueue, source_type="icsource"). Exposes the same function
signatures as the original for full backward compatibility.

Called by: requisition triggers, ai_gate, worker loop, admin endpoints, scripts
Depends on: search_worker_base.queue_manager, IcsSearchQueue model, config
"""

from sqlalchemy.orm import Session

from app.models import IcsSearchQueue

from ..search_worker_base.queue_manager import QueueManager
from .config import IcsConfig

_config = IcsConfig()

_qm = QueueManager(
    queue_model=IcsSearchQueue,
    source_type="icsource",
    dedup_window_days=_config.ICS_DEDUP_WINDOW_DAYS,
    log_prefix="ICS",
)


def enqueue_for_ics_search(requirement_id: int, db: Session) -> IcsSearchQueue | None:
    """Queue a requirement for ICsource search."""
    return _qm.enqueue_search(requirement_id, db)


def recover_stale_searches(db: Session) -> int:
    """Reset any items stuck in 'searching' status (from a previous crash)."""
    return _qm.recover_stale_searches(db)


def get_next_queued_item(db: Session) -> IcsSearchQueue | None:
    """Get the next queued item — active-sourcing first, newest first."""
    return _qm.get_next_queued_item(db)


def mark_status(db: Session, queue_item: IcsSearchQueue, new_status: str, error: str | None = None):
    """Update a queue item's status."""
    _qm.mark_status(db, queue_item, new_status, error)


def mark_completed(db: Session, queue_item: IcsSearchQueue, results_found: int, sightings_created: int):
    """Mark a queue item as completed with result counts."""
    _qm.mark_completed(db, queue_item, results_found, sightings_created)


def get_queue_stats(db: Session) -> dict:
    """Return queue statistics by status plus daily totals."""
    return _qm.get_queue_stats(db)
