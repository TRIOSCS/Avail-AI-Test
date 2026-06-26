"""requisition_state.py — Requisition status state machine.

Enforces valid status transitions and logs changes to ActivityLog.
Replaces raw `req.status = "..."` assignments scattered across routers.

Called by: routers (requisitions, offers, quotes, buy_plans)
Depends on: enums.py, models (ActivityLog)
"""

from loguru import logger
from sqlalchemy.orm import Session

from ..constants import ActivityType, Channel, RequisitionStatus
from ..models import ActivityLog

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"open", "hotlist"},
    "open": {"rfqs_sent", "offers", "quoted", "won", "lost", "hotlist"},
    "rfqs_sent": {"open", "offers", "quoted", "won", "lost", "hotlist"},
    "offers": {"open", "quoted", "won", "lost", "hotlist"},
    "quoted": {"open", "offers", "won", "lost", "hotlist"},
    "won": {"open"},
    "lost": {"open", "hotlist"},
    "hotlist": {"open", "rfqs_sent", "offers", "quoted", "won", "lost"},
    "cancelled": {"open"},
    # Legacy origins (pre-157 rows / in-flight sessions) — always allow normalising to open.
    "active": {"open", "rfqs_sent", "offers", "quoted", "won", "lost", "hotlist"},
    "sourcing": {"open", "rfqs_sent", "offers", "quoted", "won", "lost", "hotlist"},
    "quoting": {"open", "quoted", "won", "lost", "hotlist"},
    "reopened": {"open", "rfqs_sent", "offers", "quoted", "won", "lost", "hotlist"},
    "archived": {"open"},
}


def transition(req, new_status: str | RequisitionStatus, actor, db: Session) -> None:
    """Validate and apply a requisition status transition.

    Raises ValueError for illegal transitions. Logs to ActivityLog.
    """
    old_status = req.status or "open"
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
            activity_type=ActivityType.STATUS_CHANGED,
            channel=Channel.SYSTEM,
            requisition_id=req.id,
            subject=f"Status: {old_status} → {new_val}",
        )
        db.add(log_entry)
    except Exception as e:
        logger.error("Failed to log status transition: {}", e, exc_info=True)


def set_hotlist(req, actor, db: Session) -> None:
    """Put a requisition on the Hotlist monitor (Proactive surfaces matches)."""
    transition(req, RequisitionStatus.HOTLIST, actor, db)


def set_archived(req, archived: bool, actor, db: Session) -> None:
    """Archive/unarchive a requisition (hidden-but-retrievable; orthogonal to
    status)."""
    from ..constants import ActivityType

    if req.is_archived == archived:
        return
    req.is_archived = archived
    try:
        actor_id = actor.id if actor else None
        db.add(
            ActivityLog(
                user_id=actor_id,
                activity_type=ActivityType.REQ_ARCHIVED if archived else ActivityType.REQ_UNARCHIVED,
                channel=Channel.SYSTEM,
                requisition_id=req.id,
                subject="Archived" if archived else "Unarchived",
            )
        )
    except Exception as e:  # pragma: no cover - logging best-effort
        logger.error("Failed to log archive change: {}", e, exc_info=True)
