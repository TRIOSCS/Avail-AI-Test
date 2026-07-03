"""queue.py — read-side view-model helpers for a single approval gate's queue.

Purpose: ``pending_rows_for_gate`` / ``resolved_rows_for_gate`` build the rows one
         gate-type tab renders, with a constant number of queries (no N+1 regardless of
         row count):
           - a Pending section (REQUESTED, org-wide) + a Recently-resolved section
             (terminal statuses, capped, coalesce-ordered so cancelled rows with a NULL
             resolved_at still sort sanely);
           - per-row can_act (True only when the user is an eligible PENDING recipient —
             mirrors the engine's decide() gate), plus the routed-to approver names so an
             org-wide viewer sees who owns a request they cannot action.
         ``pending_count_for_gate`` is the org-wide REQUESTED total that drives a tab pill.

         These per-gate helpers are the leaner successor to the retired three-way
         ``build_queue_view``: the new Approvals hub (routers/htmx/approvals_hub.py) owns
         one tab per SURVIVING engine gate (Buy Plan = BUY_PLAN, Prepayment = PREPAYMENT)
         and calls these directly. The PO Approval tab is NOT engine-backed — it reads
         PENDING_VERIFY buy-plan lines via services/approvals/po_queue.py — so there is no
         longer a single "three-tab view" object here.

Called by: routers/htmx/approvals_hub.py (Buy Plan + Prepayment tabs).
Depends on: models.approvals (ApprovalRequest/Step/StepRecipient), models.auth (User),
            models.buy_plan (BuyPlan, BuyPlanLine), models.quality_plan (QualityPlan,
            Prepayment), models.sourcing (Requisition, via BuyPlan), app.constants enums,
            services.buyplan_workflow._line_amount (lazy — the PO line-total delta). Reuses
            the exact awaiting-me join from services/alerts/sources/approvals.py.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from ...constants import (
    ApprovalGateType,
    ApprovalRecipientStatus,
    ApprovalRequestStatus,
    ApprovalSubjectType,
)
from ...models.approvals import ApprovalRequest, ApprovalStep, ApprovalStepRecipient
from ...models.auth import User
from ...models.buy_plan import BuyPlan, BuyPlanLine
from ...models.quality_plan import Prepayment, QualityPlan

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
    subject_type: str | None
    subject_id: int | None
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
    # ── Prepayment-only decision context (None for other gate types). The Prepayment tab is
    # the surface where cash is authorised, so a row carries the full payee / PO / amount
    # picture the approver needs. ``amount``/``currency`` above are overridden to the
    # Prepayment's own ``total_incl_fees``/``currency`` (the authoritative authorised figure).
    beneficiary: str | None = None
    test_report_sent: bool | None = None
    po_number: str | None = None
    so_number: str | None = None
    plan_id: int | None = None
    buyer_remarks: str | None = None
    po_line_total: float | None = None
    decided_by: str | None = None


def _mine_clause(user: User):
    """Ownership filter for the SEE-MINE scope: requests the user raised or owns."""
    return or_(ApprovalRequest.requested_by_id == user.id, ApprovalRequest.owner_id == user.id)


def pending_rows_for_gate(db: Session, user: User, gate_type, *, scope: str = "all") -> list[RowVM]:
    """Pending (REQUESTED, org-wide) rows for one engine gate, fully resolved for Jinja.

    ``can_act`` is True only where *user* is an eligible PENDING recipient (mirrors the
    engine's ``decide()`` gate); every row also carries the routed-to approver names so an
    org-wide viewer sees who owns a request they cannot action. Constant query count.

    ``scope="mine"`` narrows to requests *user* raised or owns (SEE-MINE toggle); ``"all"``
    (default) is the unchanged org-wide queue an approver lands on.
    """
    pending = _pending_rows(db, gate_type, mine=(scope == "mine"), user=user)
    if not pending:
        return []
    actionable = _actionable_request_ids(db, user)
    approver_names = _approver_names_by_request(db, [ar.id for ar in pending])
    requester_names = _requester_names(db, pending)
    subjects = _load_subjects(db, pending)
    return [
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


def resolved_rows_for_gate(db: Session, gate_type, *, scope: str = "all", user: User | None = None) -> list[RowVM]:
    """Recently-resolved (terminal, capped, coalesce-ordered) rows for one engine gate.

    ``scope="mine"`` (with *user*) narrows to requests the user raised or owns.
    """
    resolved = _resolved_rows(db, gate_type, mine=(scope == "mine" and user is not None), user=user)
    if not resolved:
        return []
    requester_names = _requester_names(db, resolved)
    subjects = _load_subjects(db, resolved)
    decider_names = _decider_names(db, [ar.id for ar in resolved])
    return [
        _row_vm(
            ar,
            pending=False,
            actionable=set(),
            approver_names={},
            requester_names=requester_names,
            subjects=subjects,
            decider_names=decider_names,
        )
        for ar in resolved
    ]


def pending_count_for_gate(db: Session, gate_type) -> int:
    """Org-wide REQUESTED count for one gate_type (drives a tab pill).

    Always org-wide, independent of the SEE-MINE tab toggle — the pill is an at-a-glance
    "how many are open" cue, not the scoped list.
    """
    return int(
        db.execute(
            select(func.count(ApprovalRequest.id)).where(
                ApprovalRequest.status == ApprovalRequestStatus.REQUESTED,
                ApprovalRequest.gate_type == gate_type,
            )
        ).scalar_one()
    )


# Cap on the Buy Plan tracking list's recent-plans window (pending-approval plans are
# ALWAYS surfaced regardless of this cap — see buy_plan_tracking_rows).
PLAN_TRACKING_LIMIT = 50


@dataclass
class PlanTrackingRow:
    """One buy plan for the Buy Plan tab's approvals+tracking list (no ORM in Jinja).

    ``can_decide`` marks a plan the viewer may approve inline (an open BUY_PLAN request on
    which they hold a PENDING recipient slot); every other plan renders as a status-only
    tracking row.
    """

    plan_id: int
    status: str
    customer_name: str | None
    so_number: str | None
    amount: Decimal | None
    can_decide: bool


def buy_plan_tracking_rows(db: Session, user: User, *, scope: str = "all") -> list[PlanTrackingRow]:
    """Buy plans for the Buy Plan tab — approvals AND tracking (lifecycle status), one
    row per plan.

    The Buy Plan tab is a status board for the deals themselves, not just their open
    approval-gate rows: it lists buy plans with their lifecycle status (draft / pending /
    active / completed / …) so a viewer can TRACK a plan here, with the Approve action
    inline where a plan is pending the viewer's decision.

    ``scope="mine"`` narrows to plans the viewer owns (``submitted_by_id``). Plans the
    viewer can decide are ALWAYS included (they need action) even if older than the recent
    window; the rest are the most-recent ``PLAN_TRACKING_LIMIT`` plans. Rows sort
    decidable-first, then newest.
    """
    mine = scope == "mine"

    # Which plans can this viewer decide right now (open BUY_PLAN request + PENDING slot)?
    actionable = _actionable_request_ids(db, user)
    decidable_plan_ids: set[int] = set()
    if actionable:
        rows = db.execute(
            select(ApprovalRequest.subject_id).where(
                ApprovalRequest.gate_type == ApprovalGateType.BUY_PLAN,
                ApprovalRequest.subject_type == ApprovalSubjectType.BUY_PLAN,
                ApprovalRequest.status == ApprovalRequestStatus.REQUESTED,
                ApprovalRequest.id.in_(actionable),
            )
        ).all()
        decidable_plan_ids = {sid for (sid,) in rows if sid}

    def _scoped(sel):
        return sel.where(BuyPlan.submitted_by_id == user.id) if mine else sel

    plans: dict[int, BuyPlan] = {}
    # Decidable plans first — always shown, never dropped by the recent-window cap.
    if decidable_plan_ids:
        sel = _scoped(
            select(BuyPlan).options(joinedload(BuyPlan.requisition)).where(BuyPlan.id.in_(decidable_plan_ids))
        )
        for p in db.execute(sel).unique().scalars():
            plans[p.id] = p
    # Recent plans for tracking (capped, newest-first).
    recent = _scoped(
        select(BuyPlan)
        .options(joinedload(BuyPlan.requisition))
        .order_by(BuyPlan.created_at.desc(), BuyPlan.id.desc())
        .limit(PLAN_TRACKING_LIMIT)
    )
    for p in db.execute(recent).unique().scalars():
        plans.setdefault(p.id, p)

    out = [
        PlanTrackingRow(
            plan_id=p.id,
            status=p.status,
            # requisition_id is NOT NULL, so requisition.customer_name is the light,
            # N+1-free customer label (deeper quote→site→company chain is not worth the joins
            # for a tracking list).
            customer_name=(p.requisition.customer_name if p.requisition else None),
            so_number=p.sales_order_number,
            amount=p.total_cost,
            can_decide=p.id in decidable_plan_ids,
        )
        for p in plans.values()
    ]
    out.sort(key=lambda r: (not r.can_decide, -r.plan_id))
    return out


# ── Queries ──────────────────────────────────────────────────────────────


def _pending_rows(db: Session, gate, *, mine: bool = False, user: User | None = None) -> list[ApprovalRequest]:
    q = (
        select(ApprovalRequest)
        .where(ApprovalRequest.status == ApprovalRequestStatus.REQUESTED, ApprovalRequest.gate_type == gate)
        .order_by(ApprovalRequest.created_at.asc(), ApprovalRequest.id.asc())
        .limit(PENDING_CAP)
    )
    if mine and user is not None:
        q = q.where(_mine_clause(user))
    return list(db.execute(q).scalars())


def _resolved_rows(db: Session, gate, *, mine: bool = False, user: User | None = None) -> list[ApprovalRequest]:
    # coalesce(resolved_at, updated_at, created_at): cancelled rows never set resolved_at,
    # so a bare resolved_at order would scatter them — portable across PG and SQLite.
    order_key = func.coalesce(ApprovalRequest.resolved_at, ApprovalRequest.updated_at, ApprovalRequest.created_at)
    q = (
        select(ApprovalRequest)
        .where(ApprovalRequest.status.in_(RESOLVED_STATUSES), ApprovalRequest.gate_type == gate)
        .order_by(order_key.desc(), ApprovalRequest.id.desc())
        .limit(RESOLVED_LIMIT)
    )
    if mine and user is not None:
        q = q.where(_mine_clause(user))
    return list(db.execute(q).scalars())


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
        # Eager-load every relationship the Prepayment RowVM reads (beneficiary needs the
        # vendor_card; the SO#/customer needs buy_plan→requisition; the PO#/line-total delta
        # needs buy_plan_line→offer) so the enriched fields add NO per-row query — the
        # module's constant-query / no-N+1 contract holds regardless of row count (finding #8).
        pps = (
            db.execute(
                select(Prepayment)
                .options(
                    joinedload(Prepayment.vendor_card),
                    joinedload(Prepayment.buy_plan).joinedload(BuyPlan.requisition),
                    joinedload(Prepayment.buy_plan_line).joinedload(BuyPlanLine.offer),
                )
                .where(Prepayment.id.in_(pp_ids))
            )
            .unique()
            .scalars()
        )
        for pp in pps:
            subjects[(ApprovalSubjectType.PREPAYMENT, pp.id)] = pp

    return subjects


def _decider_names(db: Session, request_ids: list[int]) -> dict[int, str]:
    """For each resolved request id, the name of the recipient who decided it (approved
    / rejected) — the "approved-by" a self-documenting resolved row shows (finding #7).

    One query for the whole page (keyed on a non-null ``decided_at``), most-recent decision
    wins — no per-row lookup.
    """
    if not request_ids:
        return {}
    rows = db.execute(
        select(ApprovalStep.request_id, User.name, ApprovalStepRecipient.decided_at)
        .join(ApprovalStepRecipient, ApprovalStepRecipient.step_id == ApprovalStep.id)
        .join(User, User.id == ApprovalStepRecipient.user_id)
        .where(
            ApprovalStep.request_id.in_(request_ids),
            ApprovalStepRecipient.decided_at.isnot(None),
        )
        .order_by(ApprovalStepRecipient.decided_at.desc())
    ).all()
    out: dict[int, str] = {}
    for req_id, name, _decided_at in rows:
        out.setdefault(req_id, name or "—")  # first row per request = most-recent decider
    return out


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


def _beneficiary(pp: Prepayment) -> str:
    """Who is actually being paid — the legal payee, most-authoritative first.

    Chain (finding #3): vendor_card.legal_name → the request-time vendor_name snapshot →
    vendor_card.display_name → "—". The approver/AP must never see a blank payee.
    """
    vc = pp.vendor_card
    if vc is not None and vc.legal_name:
        return vc.legal_name
    if pp.vendor_name:
        return pp.vendor_name
    if vc is not None and vc.display_name:
        return vc.display_name
    return "—"


def _row_vm(
    ar: ApprovalRequest,
    *,
    pending: bool,
    actionable: set[int],
    approver_names: dict[int, list[str]],
    requester_names: dict[int, str],
    subjects: dict,
    decider_names: dict[int, str] | None = None,
) -> RowVM:
    subject_label, subject_href, parent_label, payment_method = _resolve_subject(ar, subjects)

    amount = ar.amount
    currency = ar.currency or "USD"
    beneficiary = po_number = so_number = buyer_remarks = None
    plan_id = po_line_total = test_report_sent = None

    if ar.subject_type == ApprovalSubjectType.PREPAYMENT:
        pp = subjects.get((ar.subject_type, ar.subject_id)) if ar.subject_id else None
        if pp is not None:
            # Authoritative authorised figure comes from the Prepayment itself, not the
            # (possibly-drifted) request amount — this is the surface where cash is signed off.
            from ..buyplan_workflow import _line_amount  # lazy: avoid an import cycle

            beneficiary = _beneficiary(pp)
            test_report_sent = pp.test_report_sent
            buyer_remarks = pp.buyer_remarks
            plan_id = pp.buy_plan_id
            if pp.total_incl_fees is not None:
                amount = pp.total_incl_fees
            currency = pp.currency or currency
            line = pp.buy_plan_line
            if line is not None:
                po_number = line.po_number
                po_line_total = _line_amount(line)
            bp = pp.buy_plan
            if bp is not None:
                so_number = bp.sales_order_number

    return RowVM(
        id=ar.id,
        gate_type=ar.gate_type,
        status=ar.status,
        subject_type=ar.subject_type,
        subject_id=ar.subject_id,
        amount=amount,
        currency=currency,
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
        beneficiary=beneficiary,
        test_report_sent=test_report_sent,
        po_number=po_number,
        so_number=so_number,
        plan_id=plan_id,
        buyer_remarks=buyer_remarks,
        po_line_total=po_line_total,
        decided_by=(decider_names.get(ar.id) if decider_names else None),
    )
