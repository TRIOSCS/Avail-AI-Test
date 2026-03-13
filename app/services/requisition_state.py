"""requisition_state.py — Requisition status state machine.

Enforces valid status transitions and logs changes to ActivityLog.
Replaces raw `req.status = "..."` assignments scattered across routers.

Called by: routers (requisitions, offers, quotes, buy_plans)
Depends on: enums.py, models (ActivityLog)
"""

from loguru import logger
from sqlalchemy.orm import Session

from ..enums import RequisitionStatus
from ..models import ActivityLog

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"active", "archived"},
    "open": {"active", "sourcing", "offers", "quoting", "won", "archived"},  # legacy alias for active
    "active": {"sourcing", "offers", "quoting", "won", "archived"},
    "sourcing": {"active", "offers", "archived"},
    "offers": {"quoting", "won", "archived"},
    "quoting": {"quoted", "reopened", "won", "archived"},
    "quoted": {"won", "lost", "reopened", "archived"},
    "reopened": {"quoting", "won", "archived"},
    "won": {"active", "archived"},  # Can re-open to active if deal is re-worked
    "lost": {"active", "archived", "reopened"},
    "archived": {"active"},
}


def transition(req, new_status: str | RequisitionStatus, actor, db: Session) -> None:
    """Validate and apply a requisition status transition.

    Raises ValueError for illegal transitions. Logs to ActivityLog.
    """
    old_status = req.status or "active"
    new_val = new_status.value if isinstance(new_status, RequisitionStatus) else new_status

    if old_status == new_val:
        return  # no-op

    allowed = ALLOWED_TRANSITIONS.get(old_status, set())
    if new_val not in allowed:
        raise ValueError(f"Invalid transition: {old_status} → {new_val} (allowed: {sorted(allowed)})")

    req.status = new_val

    try:
        actor_id = actor.id if actor else None
        log_entry = ActivityLog(
            user_id=actor_id,
            activity_type="status_change",
            channel="system",
            requisition_id=req.id,
            subject=f"Status: {old_status} → {new_val}",
        )
        db.add(log_entry)
    except Exception as e:
        logger.debug("Failed to log status transition: %s", e)
