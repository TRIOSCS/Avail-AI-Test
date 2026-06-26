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

# Closing a requisition requires recording why. Single source of truth for the
# states that demand a non-empty outcome_reason.
TERMINAL_REASON_REQUIRED: frozenset[str] = frozenset({RequisitionStatus.WON, RequisitionStatus.LOST})


class OutcomeReasonRequired(ValueError):
    """Raised when a transition to WON/LOST is attempted without a reason.

    Routers translate this into a 400 with the user-facing message; it is a distinct
    subclass of ValueError so callers can tell it apart from an illegal-transition
    ValueError when they need to (status code differs).
    """

    MESSAGE = "A reason is required to mark a requisition Won or Lost"

    def __init__(self, message: str = MESSAGE) -> None:
        super().__init__(message)


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


def transition(
    req,
    new_status: str | RequisitionStatus,
    actor,
    db: Session,
    *,
    reason: str | None = None,
) -> None:
    """Validate and apply a requisition status transition.

    Closing to WON or LOST requires a non-empty ``reason`` (persisted on
    ``req.outcome_reason``); a blank reason raises ``OutcomeReasonRequired``.
    Non-terminal transitions clear any stale outcome_reason. Raises ValueError
    for illegal transitions. Logs to ActivityLog.
    """
    old_status = req.status or "open"
    new_val = new_status.value if isinstance(new_status, RequisitionStatus) else new_status

    if old_status == new_val:
        return  # no-op

    allowed = ALLOWED_TRANSITIONS.get(old_status, set())
    if new_val not in allowed:
        raise ValueError(f"Invalid transition: {old_status} → {new_val} (allowed: {sorted(allowed)})")

    if new_val in TERMINAL_REASON_REQUIRED:
        clean_reason = (reason or "").strip()
        if not clean_reason:
            raise OutcomeReasonRequired()
        req.outcome_reason = clean_reason
    else:
        # Reopening / moving off a terminal state drops the stale close reason.
        req.outcome_reason = None

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
