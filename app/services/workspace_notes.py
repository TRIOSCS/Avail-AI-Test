"""workspace_notes.py — notes threads for the Approvals Workspace (design D2).

Purpose: Per-item notes on the sales order (buy plan), each PO line, and each
         prepayment — ActivityLog NOTE rows keyed by the migration-192 subject FKs
         (buy_plan_id / buy_plan_line_id / prepayment_id). A decision note (the
         note-to-the-fixer on reject / send-back) is the SAME row tagged via
         details={"decision": "rejected"|"sent_back"} so the thread shows the full
         submit → rejected → fixed → resubmitted history inline. Notes are NEVER
         status-locked — field locks lock fields, not conversation (spec §7).

Called by: routers/htmx/approvals_hub.py (notes/attachments routes + pane renders),
           routers/htmx/buy_plans.py (decision note-to-fixer writes),
           the prepay decide handler (decision-tagged reject notes).
Depends on: app.services.activity_service.log_activity, app.models.intelligence
            .ActivityLog, app.constants (ActivityType, Channel).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from ..constants import ActivityType, Channel
from ..models.auth import User
from ..models.intelligence import ActivityLog

# The decision tags a note-to-the-fixer may carry (spec §7: the note posts into the
# item's thread tagged with the decision).
DECISION_TAGS = ("rejected", "sent_back")


def add_note(
    db: Session,
    *,
    user: User,
    body: str,
    buy_plan_id: int,
    buy_plan_line_id: int | None = None,
    prepayment_id: int | None = None,
    decision: str | None = None,
) -> ActivityLog:
    """Write one NOTE row on the narrowest subject (plan / line / prepayment).

    ``decision`` (one of :data:`DECISION_TAGS`, or None for a plain note) tags the row
    as a reject / send-back note-to-the-fixer. Blank bodies raise ValueError — a note
    is text or it is nothing. The caller owns the commit. Never status-gated.
    """
    text = (body or "").strip()
    if not text:
        raise ValueError("A note needs some text.")
    if decision is not None and decision not in DECISION_TAGS:
        raise ValueError(f"Unknown decision tag: {decision!r}")

    from .activity_service import log_activity

    return log_activity(
        db,
        activity_type=ActivityType.NOTE,
        channel=Channel.MANUAL,
        user_id=user.id,
        buy_plan_id=buy_plan_id,
        buy_plan_line_id=buy_plan_line_id,
        prepayment_id=prepayment_id,
        description=text,
        details={"decision": decision} if decision else None,
    )
