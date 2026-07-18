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


def notes_thread(
    db: Session,
    *,
    buy_plan_id: int | None = None,
    buy_plan_line_id: int | None = None,
    prepayment_id: int | None = None,
) -> list[ActivityLog]:
    """The subject's notes thread, oldest first — exactly ONE subject id must be given.

    Scoping is by the NARROWEST key: a line thread is that line's rows; a prepayment
    thread that prepayment's rows; a PLAN thread only the plan-level rows (line- and
    prepayment-tagged notes belong to their own panes, not the SO pane).
    """
    given = [v for v in (buy_plan_id, buy_plan_line_id, prepayment_id) if v is not None]
    if len(given) != 1:
        raise ValueError("notes_thread needs exactly one of buy_plan_id / buy_plan_line_id / prepayment_id")

    query = db.query(ActivityLog).filter(ActivityLog.activity_type == ActivityType.NOTE.value)
    if buy_plan_line_id is not None:
        query = query.filter(ActivityLog.buy_plan_line_id == buy_plan_line_id)
    elif prepayment_id is not None:
        query = query.filter(ActivityLog.prepayment_id == prepayment_id)
    else:
        query = query.filter(
            ActivityLog.buy_plan_id == buy_plan_id,
            ActivityLog.buy_plan_line_id.is_(None),
            ActivityLog.prepayment_id.is_(None),
        )
    return query.order_by(ActivityLog.created_at.asc(), ActivityLog.id.asc()).all()


def note_counts(
    db: Session,
    *,
    buy_plan_ids: list[int] | None = None,
    buy_plan_line_ids: list[int] | None = None,
    prepayment_ids: list[int] | None = None,
) -> dict[int, int]:
    """Batched note counts for cards/rows, keyed by subject id — pass exactly one id
    list. Plan counts use the same plan-level scoping as :func:`notes_thread`."""
    from sqlalchemy import func

    given = [v for v in (buy_plan_ids, buy_plan_line_ids, prepayment_ids) if v is not None]
    if len(given) != 1:
        raise ValueError("note_counts needs exactly one of buy_plan_ids / buy_plan_line_ids / prepayment_ids")

    base = db.query(ActivityLog).filter(ActivityLog.activity_type == ActivityType.NOTE.value)
    if buy_plan_line_ids is not None:
        if not buy_plan_line_ids:
            return {}
        rows = (
            base.filter(ActivityLog.buy_plan_line_id.in_(buy_plan_line_ids))
            .with_entities(ActivityLog.buy_plan_line_id, func.count(ActivityLog.id))
            .group_by(ActivityLog.buy_plan_line_id)
            .all()
        )
    elif prepayment_ids is not None:
        if not prepayment_ids:
            return {}
        rows = (
            base.filter(ActivityLog.prepayment_id.in_(prepayment_ids))
            .with_entities(ActivityLog.prepayment_id, func.count(ActivityLog.id))
            .group_by(ActivityLog.prepayment_id)
            .all()
        )
    else:
        if not buy_plan_ids:
            return {}
        rows = (
            base.filter(
                ActivityLog.buy_plan_id.in_(buy_plan_ids),
                ActivityLog.buy_plan_line_id.is_(None),
                ActivityLog.prepayment_id.is_(None),
            )
            .with_entities(ActivityLog.buy_plan_id, func.count(ActivityLog.id))
            .group_by(ActivityLog.buy_plan_id)
            .all()
        )
    return {int(subject_id): int(count) for subject_id, count in rows}
