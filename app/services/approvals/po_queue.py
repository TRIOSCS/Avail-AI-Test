"""po_queue.py — read-side view-model for the Approvals hub's PO Approval tab.

The PO Approval tab is the ONE Approvals-hub tab that is NOT engine-backed. A "PO" here is
exactly one ``BuyPlanLine`` in PENDING_VERIFY (Phase 3 retired the redundant deal-level
PURCHASE_ORDER ``ApprovalRequest`` gate — the per-line verify/reject/cancel trio IS the PO
sign-off). So this builds:
  - ``pending``: every org-wide PENDING_VERIFY line (oldest first), eager-loaded so the tab
    can render the same verify / send-back / cancel(→re-source) trio the plan detail shows.
    Per-row verify/reject visibility is gated in the template by ``can_verify_po_line`` (the
    dollar-limit-aware Jinja global); the row itself is always shown for org-wide awareness.
  - ``history``: a resolved feed unifying the durable per-line PO decisions —
    ``PO_LINE_VERIFIED`` / ``PO_LINE_REJECTED`` ActivityLog rows (written by
    ``buyplan_workflow.verify_po``) plus ``POCancellation`` rows (written at re-source time)
    — so this tab has a real recently-resolved section to match the other two gate tabs
    without needing an ApprovalRequest.

Called by: routers/htmx/approvals_hub.py (PO Approval tab).
Depends on: services/buyplan_hub (line query + display helpers), buyplan_workflow._line_amount,
            models.buy_plan (BuyPlanLine), models.po_cancellation (POCancellation),
            models.intelligence (ActivityLog), models.auth (User), app.constants enums.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...constants import ActivityType
from ...models.auth import User
from ...models.buy_plan import BuyPlanLine
from ...models.intelligence import ActivityLog
from ...models.po_cancellation import POCancellation

HISTORY_LIMIT = 15


@dataclass
class POPendingRow:
    """One PENDING_VERIFY line, resolved for the PO Approval tab (keeps the ORM line +
    plan so the shared verify / re-source forms and the ``can_verify_po_line`` gate can
    read them, exactly as the plan-detail line table does)."""

    line: BuyPlanLine
    plan: object
    customer_name: str | None
    primary_mpn: str | None
    vendor_name: str | None
    amount: float
    po_number: str | None
    age_hours: float


@dataclass
class POHistoryRow:
    """One resolved per-line PO event (verified / sent-back / cancelled) for the history
    feed.

    Plain fields only — no ORM access in Jinja.
    """

    kind: str  # "verified" | "rejected" | "cancelled"
    when: datetime | None
    actor_name: str
    label: str
    plan_id: int | None
    note: str | None


@dataclass
class POQueueView:
    """Everything the PO Approval tab body renders."""

    pending: list[POPendingRow] = field(default_factory=list)
    history: list[POHistoryRow] = field(default_factory=list)
    pending_count: int = 0


def build_po_queue_view(db: Session, user: User, *, scope: str = "all") -> POQueueView:
    """Assemble the PO Approval tab view for *user* (pending PENDING_VERIFY lines + a
    unified verified/rejected/cancelled history feed).

    ``scope="mine"`` narrows the pending lines to those on a plan the viewer owns
    (``buy_plan.submitted_by_id``) — the owner-based definition, for consistency with the
    Buy Plan tab's SEE-MINE. (Alternative considered: lines the viewer is eligible to
    verify via can_verify_po_line; owner-based was chosen so the three tabs scope
    identically.) The history feed stays org-wide.
    """
    from ..buyplan_hub import _age_hours, _customer_name, _line_mpn, _query_po_pending_verify
    from ..buyplan_workflow import _line_amount

    lines = _query_po_pending_verify(db)
    if scope == "mine":
        lines = [ln for ln in lines if ln.buy_plan and ln.buy_plan.submitted_by_id == user.id]
    pending = [
        POPendingRow(
            line=line,
            plan=line.buy_plan,
            customer_name=_customer_name(line.buy_plan) if line.buy_plan else None,
            primary_mpn=_line_mpn(line),
            vendor_name=line.offer.vendor_name if line.offer else None,
            amount=_line_amount(line),
            po_number=line.po_number,
            age_hours=_age_hours(line.po_confirmed_at or line.created_at),
        )
        for line in lines
    ]

    return POQueueView(pending=pending, history=_history_rows(db), pending_count=len(pending))


def _history_rows(db: Session) -> list[POHistoryRow]:
    """Most-recent per-line PO decisions: verified/rejected (ActivityLog) + cancelled
    (POCancellation), interleaved newest-first and capped."""
    rows: list[POHistoryRow] = []

    logs = list(
        db.execute(
            select(ActivityLog)
            .where(ActivityLog.activity_type.in_([ActivityType.PO_LINE_VERIFIED, ActivityType.PO_LINE_REJECTED]))
            .order_by(ActivityLog.occurred_at.desc(), ActivityLog.id.desc())
            .limit(HISTORY_LIMIT)
        ).scalars()
    )
    cancels = list(
        db.execute(
            select(POCancellation)
            .order_by(POCancellation.cancelled_at.desc(), POCancellation.id.desc())
            .limit(HISTORY_LIMIT)
        ).scalars()
    )

    actor_ids = {log.user_id for log in logs if log.user_id} | {c.cancelled_by_id for c in cancels if c.cancelled_by_id}
    names = _actor_names(db, actor_ids)

    for log in logs:
        rows.append(
            POHistoryRow(
                kind="verified" if log.activity_type == ActivityType.PO_LINE_VERIFIED else "rejected",
                when=log.occurred_at,
                actor_name=names.get(log.user_id, "—"),
                label=log.notes or log.subject or f"PO decision (plan #{log.buy_plan_id})",
                plan_id=log.buy_plan_id,
                note=None,
            )
        )
    for c in cancels:
        reason = (c.reason_text or c.reason_code or "").strip() or None
        rows.append(
            POHistoryRow(
                kind="cancelled",
                when=c.cancelled_at,
                actor_name=names.get(c.cancelled_by_id, "—"),
                label=f"PO {c.po_number} cancelled — {c.reason_code}",
                plan_id=c.buy_plan_id,
                note=reason,
            )
        )

    # Interleave newest-first; None timestamps sort last.
    rows.sort(key=lambda r: (r.when is not None, r.when or datetime.min), reverse=True)
    return rows[:HISTORY_LIMIT]


def _actor_names(db: Session, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    return {uid: (name or "—") for uid, name in db.execute(select(User.id, User.name).where(User.id.in_(ids))).all()}
