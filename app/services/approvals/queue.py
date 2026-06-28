"""queue.py — read-side view-model builder for the three-tab approvals queue.

Purpose: build_queue_view assembles everything the approvals lens body renders, with a
         constant number of queries (no N+1 regardless of row count):
           - three tabs segmented by ApprovalRequest.gate_type
             (sales_orders / purchase_orders / prepayments);
           - a Pending section (REQUESTED, org-wide) + a Recently-resolved section
             (terminal statuses, capped, coalesce-ordered so cancelled rows with a NULL
             resolved_at still sort sanely);
           - per-tab pill counts that are ORG-WIDE pending totals;
           - a smart-default tab (the gate with the most items awaiting THIS user; tie or
             zero → Sales Orders) used only when no explicit tab is requested;
           - per-row can_act (True only when the user is an eligible PENDING recipient —
             mirrors the engine's decide() gate), plus the routed-to approver names so an
             org-wide viewer sees who owns a request they cannot action.

Called by: routers/approvals.py (get_queue) and the Buy-Plans hub "approvals" lens body
           (routers/htmx_views.py).
Depends on: models.approvals (ApprovalRequest/Step/StepRecipient), models.auth (User),
            models.buy_plan (BuyPlan), models.quality_plan (QualityPlan, Prepayment),
            models.sourcing (Requisition, via BuyPlan), app.constants enums. Reuses the
            exact awaiting-me join from services/alerts/sources/approvals.py.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from ...constants import (
    ApprovalGateType,
    ApprovalRecipientStatus,
    ApprovalRequestStatus,
    ApprovalSubjectType,
)
from ...models.approvals import ApprovalRequest, ApprovalStep, ApprovalStepRecipient
from ...models.auth import User
from ...models.buy_plan import BuyPlan
from ...models.quality_plan import Prepayment, QualityPlan

# tab key → gate_type. Order is the on-screen left-to-right order and the smart-default
# tie-break order (leftmost wins).
TAB_ORDER = ["sales_orders", "purchase_orders", "prepayments"]
TAB_GATE = {
    "sales_orders": ApprovalGateType.BUY_PLAN,
    "purchase_orders": ApprovalGateType.PURCHASE_ORDER,
    "prepayments": ApprovalGateType.PREPAYMENT,
}
TAB_LABEL = {
    "sales_orders": "Sales Orders",
    "purchase_orders": "Purchase Orders",
    "prepayments": "Vendor Prepayments",
}
DEFAULT_TAB = "sales_orders"

RESOLVED_STATUSES = (
    ApprovalRequestStatus.APPROVED,
    ApprovalRequestStatus.REJECTED,
    ApprovalRequestStatus.CANCELLED,
    ApprovalRequestStatus.EXPIRED,
)
RESOLVED_LIMIT = 10
# Defensive ceiling on the org-wide pending list (it is naturally self-clearing, but
# never let a runaway backlog render an unbounded table). Oldest-first, so the work most
# in need of a decision is never the part that gets hidden.
PENDING_CAP = 200


@dataclass
class RowVM:
    """One approval row, fully resolved for the template (no ORM access in Jinja)."""

    id: int
    gate_type: str
    status: str
    amount: Decimal | None
    currency: str
    created_at: datetime | None
    resolved_at: datetime | None
    resolution_note: str | None
    subject_label: str
    subject_href: str | None
    requester_name: str
    approver_names: str
    parent_label: str | None
    payment_method: str | None
    can_act: bool


@dataclass
class QueueView:
    """Everything the approvals lens body needs."""

    active_tab: str
    active_label: str = ""
    tabs: list[dict] = field(default_factory=list)
    pending_rows: list[RowVM] = field(default_factory=list)
    resolved_rows: list[RowVM] = field(default_factory=list)


def build_queue_view(db: Session, user: User, tab: str | None) -> QueueView:
    """Assemble the QueueView for *user* on *tab* (smart-default when tab is absent)."""
    active_tab = tab if tab in TAB_GATE else _smart_default_tab(db, user)
    gate = TAB_GATE[active_tab]

    pill_by_gate = _pending_counts_by_gate(db)
    pending = _pending_rows(db, gate)
    resolved = _resolved_rows(db, gate)

    actionable = _actionable_request_ids(db, user)
    pending_ids = [ar.id for ar in pending]
    approver_names = _approver_names_by_request(db, pending_ids)
    requester_names = _requester_names(db, pending + resolved)
    subjects = _load_subjects(db, pending + resolved)

    pending_vms = [
        _row_vm(
            ar,
            pending=True,
            actionable=actionable,
            approver_names=approver_names,
            requester_names=requester_names,
            subjects=subjects,
        )
        for ar in pending
    ]
    resolved_vms = [
        _row_vm(
            ar,
            pending=False,
            actionable=actionable,
            approver_names=approver_names,
            requester_names=requester_names,
            subjects=subjects,
        )
        for ar in resolved
    ]

    tabs = [{"key": k, "label": TAB_LABEL[k], "count": pill_by_gate.get(TAB_GATE[k], 0)} for k in TAB_ORDER]
    return QueueView(
        active_tab=active_tab,
        active_label=TAB_LABEL[active_tab],
        tabs=tabs,
        pending_rows=pending_vms,
        resolved_rows=resolved_vms,
    )


# ── Queries ──────────────────────────────────────────────────────────────


def _pending_counts_by_gate(db: Session) -> dict[str, int]:
    """Org-wide REQUESTED count per gate_type (drives the tab pills)."""
    rows = db.execute(
        select(ApprovalRequest.gate_type, func.count(ApprovalRequest.id))
        .where(ApprovalRequest.status == ApprovalRequestStatus.REQUESTED)
        .group_by(ApprovalRequest.gate_type)
    ).all()
    return {gate: count for gate, count in rows}


def _awaiting_me_counts(db: Session, user: User) -> dict[str, int]:
    """REQUESTED requests where *user* holds a PENDING recipient row, grouped by
    gate."""
    rows = db.execute(
        select(ApprovalRequest.gate_type, func.count(func.distinct(ApprovalRequest.id)))
        .join(ApprovalStep, ApprovalStep.request_id == ApprovalRequest.id)
        .join(ApprovalStepRecipient, ApprovalStepRecipient.step_id == ApprovalStep.id)
        .where(
            ApprovalRequest.status == ApprovalRequestStatus.REQUESTED,
            ApprovalStepRecipient.user_id == user.id,
            ApprovalStepRecipient.status == ApprovalRecipientStatus.PENDING,
        )
        .group_by(ApprovalRequest.gate_type)
    ).all()
    return {gate: count for gate, count in rows}


def _smart_default_tab(db: Session, user: User) -> str:
    """Tab with the most items awaiting *user*; tie or zero → leftmost (Sales
    Orders)."""
    counts = _awaiting_me_counts(db, user)
    best_tab, best_count = DEFAULT_TAB, -1
    for tab_key in TAB_ORDER:
        c = counts.get(TAB_GATE[tab_key], 0)
        if c > best_count:  # strict → leftmost wins on ties
            best_tab, best_count = tab_key, c
    return best_tab


def _pending_rows(db: Session, gate) -> list[ApprovalRequest]:
    return list(
        db.execute(
            select(ApprovalRequest)
            .where(ApprovalRequest.status == ApprovalRequestStatus.REQUESTED, ApprovalRequest.gate_type == gate)
            .order_by(ApprovalRequest.created_at.asc(), ApprovalRequest.id.asc())
            .limit(PENDING_CAP)
        ).scalars()
    )


def _resolved_rows(db: Session, gate) -> list[ApprovalRequest]:
    # coalesce(resolved_at, updated_at, created_at): cancelled rows never set resolved_at,
    # so a bare resolved_at order would scatter them — portable across PG and SQLite.
    order_key = func.coalesce(ApprovalRequest.resolved_at, ApprovalRequest.updated_at, ApprovalRequest.created_at)
    return list(
        db.execute(
            select(ApprovalRequest)
            .where(ApprovalRequest.status.in_(RESOLVED_STATUSES), ApprovalRequest.gate_type == gate)
            .order_by(order_key.desc(), ApprovalRequest.id.desc())
            .limit(RESOLVED_LIMIT)
        ).scalars()
    )


def _actionable_request_ids(db: Session, user: User) -> set[int]:
    """Request ids *user* may decide (REQUESTED + a PENDING recipient row) — mirrors
    decide()."""
    return set(
        db.execute(
            select(ApprovalRequest.id)
            .join(ApprovalStep, ApprovalStep.request_id == ApprovalRequest.id)
            .join(ApprovalStepRecipient, ApprovalStepRecipient.step_id == ApprovalStep.id)
            .where(
                ApprovalRequest.status == ApprovalRequestStatus.REQUESTED,
                ApprovalStepRecipient.user_id == user.id,
                ApprovalStepRecipient.status == ApprovalRecipientStatus.PENDING,
            )
            .distinct()
        ).scalars()
    )


def _approver_names_by_request(db: Session, request_ids: list[int]) -> dict[int, list[str]]:
    """For each request id, the names of its PENDING recipients (the routed-to
    approvers)."""
    if not request_ids:
        return {}
    rows = db.execute(
        select(ApprovalStep.request_id, User.name)
        .join(ApprovalStepRecipient, ApprovalStepRecipient.step_id == ApprovalStep.id)
        .join(User, User.id == ApprovalStepRecipient.user_id)
        .where(
            ApprovalStep.request_id.in_(request_ids),
            ApprovalStepRecipient.status == ApprovalRecipientStatus.PENDING,
        )
    ).all()
    out: dict[int, list[str]] = defaultdict(list)
    for req_id, name in rows:
        out[req_id].append(name or "—")
    return out


def _requester_names(db: Session, rows: list[ApprovalRequest]) -> dict[int, str]:
    ids = {ar.requested_by_id for ar in rows if ar.requested_by_id}
    if not ids:
        return {}
    return {uid: (name or "—") for uid, name in db.execute(select(User.id, User.name).where(User.id.in_(ids))).all()}


def _load_subjects(db: Session, rows: list[ApprovalRequest]) -> dict[tuple[str, int], object]:
    """Batch-load subject objects keyed by (subject_type, subject_id).

    One query per type.
    """
    by_type: dict[str, set[int]] = defaultdict(set)
    for ar in rows:
        if ar.subject_type and ar.subject_id:
            by_type[ar.subject_type].add(ar.subject_id)

    subjects: dict[tuple[str, int], object] = {}

    bp_ids = by_type.get(ApprovalSubjectType.BUY_PLAN)
    if bp_ids:
        for bp in db.execute(select(BuyPlan).where(BuyPlan.id.in_(bp_ids))).scalars():
            subjects[(ApprovalSubjectType.BUY_PLAN, bp.id)] = bp

    qp_ids = by_type.get(ApprovalSubjectType.QUALITY_PLAN)
    if qp_ids:
        qps = (
            db.execute(
                select(QualityPlan)
                .options(joinedload(QualityPlan.buy_plan).joinedload(BuyPlan.requisition))
                .where(QualityPlan.id.in_(qp_ids))
            )
            .unique()
            .scalars()
        )
        for qp in qps:
            subjects[(ApprovalSubjectType.QUALITY_PLAN, qp.id)] = qp

    pp_ids = by_type.get(ApprovalSubjectType.PREPAYMENT)
    if pp_ids:
        pps = (
            db.execute(select(Prepayment).options(joinedload(Prepayment.vendor_card)).where(Prepayment.id.in_(pp_ids)))
            .unique()
            .scalars()
        )
        for pp in pps:
            subjects[(ApprovalSubjectType.PREPAYMENT, pp.id)] = pp

    return subjects


# ── Assembly ─────────────────────────────────────────────────────────────


def _resolve_subject(ar: ApprovalRequest, subjects: dict) -> tuple[str, str | None, str | None, str | None]:
    """Return (subject_label, subject_href, parent_label, payment_method) for a row."""
    obj = subjects.get((ar.subject_type, ar.subject_id)) if ar.subject_id else None

    if obj is not None and ar.subject_type == ApprovalSubjectType.BUY_PLAN:
        return f"Plan #{ar.subject_id}", f"/v2/partials/buy-plans/{ar.subject_id}", None, None

    if obj is not None and ar.subject_type == ApprovalSubjectType.QUALITY_PLAN:
        parent = None
        bp = getattr(obj, "buy_plan", None)
        req = getattr(bp, "requisition", None) if bp is not None else None
        if req is not None:
            parent = req.customer_name or req.name
        return f"QP #{ar.subject_id}", f"/v2/qp/{ar.subject_id}", parent, None

    if obj is not None and ar.subject_type == ApprovalSubjectType.PREPAYMENT:
        vendor = obj.vendor_card.display_name if obj.vendor_card else f"Prepayment #{ar.subject_id}"
        href = f"/v2/partials/buy-plans/{obj.buy_plan_id}" if obj.buy_plan_id else None
        return vendor, href, None, obj.payment_method

    # Deleted/missing subject → audit-safe fallback, no link.
    return f"Request #{ar.id}", None, None, None


def _row_vm(
    ar: ApprovalRequest,
    *,
    pending: bool,
    actionable: set[int],
    approver_names: dict[int, list[str]],
    requester_names: dict[int, str],
    subjects: dict,
) -> RowVM:
    subject_label, subject_href, parent_label, payment_method = _resolve_subject(ar, subjects)
    return RowVM(
        id=ar.id,
        gate_type=ar.gate_type,
        status=ar.status,
        amount=ar.amount,
        currency=ar.currency or "USD",
        created_at=ar.created_at,
        resolved_at=ar.resolved_at,
        resolution_note=ar.resolution_note,
        subject_label=subject_label,
        subject_href=subject_href,
        requester_name=requester_names.get(ar.requested_by_id, "—") if ar.requested_by_id else "—",
        approver_names=(", ".join(approver_names.get(ar.id, [])) or "—") if pending else "—",
        parent_label=parent_label,
        payment_method=payment_method,
        can_act=pending and ar.id in actionable,
    )
