"""Approvals Workspace left work list — the WorkspaceRow view-model, the per-tab list
route, and the Deals / Purchase Orders / Prepayments row builders.

W4.8 split of app/routers/htmx/approvals_hub.py — pure structural move: URLs and
behavior unchanged; routes attach to the shared router imported from .common.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from ....constants import (
    ApprovalGateType,
    BuyPlanLineStatus,
    BuyPlanStatus,
)
from ....database import get_db
from ....dependencies import can_verify_po_line, require_user
from ....models import BuyPlan, BuyPlanLine, User
from ....services.approvals.po_queue import build_po_queue_view
from ....services.approvals.queue import (
    buy_plan_tracking_rows,
    pending_rows_for_gate,
    resolved_rows_for_gate,
)
from ....template_env import template_response
from .._shared import _base_ctx
from .common import ORDER_TYPE_LABELS, PO_DECISION_LABELS, _resolve_tab, router

# Plan lifecycle statuses shown in the default (live) list vs behind the Closed filter.
# INBOUND was retired in W3 (migration 208; no row can carry it since 176).
_LIVE_PLAN_STATUSES = (
    BuyPlanStatus.DRAFT.value,
    BuyPlanStatus.PENDING.value,
    BuyPlanStatus.ACTIVE.value,
    BuyPlanStatus.HALTED.value,
)
_CLOSED_PLAN_STATUSES = (BuyPlanStatus.COMPLETED.value, BuyPlanStatus.CANCELLED.value)

_LIVE_LINE_STATUSES = (
    BuyPlanLineStatus.AWAITING_PO.value,
    BuyPlanLineStatus.PENDING_VERIFY.value,
    BuyPlanLineStatus.RESOURCING.value,
    BuyPlanLineStatus.ISSUE.value,
)
_CLOSED_LINE_STATUSES = (BuyPlanLineStatus.VERIFIED.value, BuyPlanLineStatus.CANCELLED.value)


# ── Row view-model ──────────────────────────────────────────────────────


@dataclass
class WorkspaceRow:
    """One left-list row, fully resolved for the template (no ORM in Jinja)."""

    key: str  # unique per row across the tab, e.g. "plan-7" / "line-12" / "prepay-3"
    pane_url: str
    title: str
    subtitle: str
    status: str
    status_label: str
    needs_approval: bool
    amount: float | None = None
    age_at: datetime | None = None
    copy_number: str | None = None  # SO#/PO# rendered as a copy chip on the row
    order_type: str | None = None
    closed: bool = False
    # 2.5: the plan is silently stalled — no configured approver can decide it
    # (plan_needs_approver_reason). Rendered as an amber warning on Deals rows.
    stalled: bool = False


def _matches(q: str, *fields: str | None) -> bool:
    """Case-insensitive substring match of *q* against any of *fields*."""
    if not q:
        return True
    needle = q.strip().lower()
    return any(needle in (f or "").lower() for f in fields)


# ── The left work list ──────────────────────────────────────────────────


def _selected_plan_row(db: Session, user: User, rows: list[WorkspaceRow], select: int) -> WorkspaceRow | None:
    """Resolve a ``?select=<plan id>`` deep link to the row whose pane should open.

    The plan's own rendered row when it is in the list; otherwise (the plan sits in the
    other live/closed set, or is filtered out) a dispatch-only stand-in targeting its
    pane — gated by the SAME access check the pane route uses (get_buyplan_for_user), so
    an unknown/inaccessible id resolves to None and the caller falls back to the normal
    default silently.
    """
    row = next((r for r in rows if r.key == f"plan-{select}"), None)
    if row is not None:
        return row
    from ....dependencies import get_buyplan_for_user

    try:
        get_buyplan_for_user(db, user, select)
    except HTTPException:
        return None
    return WorkspaceRow(
        key=f"plan-{select}",
        pane_url=f"/v2/partials/approvals/plan/{select}/pane",
        title="",
        subtitle="",
        status="",
        status_label="",
        needs_approval=False,
    )


@router.get("/v2/partials/approvals/{tab}/list", response_class=HTMLResponse)
async def approvals_workspace_list(
    request: Request,
    tab: str,
    q: str = "",
    scope: str = "all",
    show_closed: bool = False,
    select: int | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render one tab's left work list (search + Mine/All + live/closed + age rows).

    Rows group "Needs your approval" first (oldest first — decision queues surface the
    stalest work); the rest render newest-first. The oldest needs-your-approval row is
    the default selection (dispatched to the pane on first load only). ``select`` (a
    plan id from a retired /v2/buy-plans/{id} deep link, Deals tab only) overrides
    that default with the selected plan's pane when the viewer may see the plan.
    """
    resolved = _resolve_tab(tab)
    if resolved is None:
        raise HTTPException(404, "Unknown approvals tab")
    scope = "mine" if scope == "mine" else "all"

    if resolved == "deals":
        rows = _plan_rows(db, user, q=q, scope=scope, show_closed=show_closed)
    elif resolved == "purchase-orders":
        rows = _po_rows(db, user, q=q, scope=scope, show_closed=show_closed)
    else:
        rows = _prepayment_rows(db, user, q=q, scope=scope, show_closed=show_closed)

    needs = [r for r in rows if r.needs_approval]
    rest = [r for r in rows if not r.needs_approval]
    default_row = needs[0] if needs else None
    if select is not None and resolved == "deals":
        default_row = _selected_plan_row(db, user, rows, select) or default_row

    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update(
        {
            "tab": resolved,
            "q": q,
            "scope": scope,
            "show_closed": show_closed,
            "needs_rows": needs,
            "other_rows": rest,
            "default_row": default_row,
            "list_url": f"/v2/partials/approvals/{resolved}/list",
        }
    )
    return template_response("htmx/partials/approvals/_workspace_list.html", ctx)


def _plan_rows(db: Session, user: User, *, q: str, scope: str, show_closed: bool) -> list[WorkspaceRow]:
    """Deals list rows — one per plan, decidable first (oldest first), then the rest
    newest-first."""
    tracking = buy_plan_tracking_rows(db, user, scope=scope)
    wanted = _CLOSED_PLAN_STATUSES if show_closed else _LIVE_PLAN_STATUSES
    tracking = [t for t in tracking if t.status in wanted]
    tracking = [t for t in tracking if _matches(q, t.customer_name, t.so_number, f"#{t.plan_id}", str(t.plan_id))]

    # Age source: submitted_at (time in the queue) falling back to created_at — one
    # batched query, keyed by plan id (PlanTrackingRow carries no timestamps).
    ages: dict[int, datetime] = {}
    if tracking:
        for pid, submitted_at, created_at in db.execute(
            select(BuyPlan.id, BuyPlan.submitted_at, BuyPlan.created_at).where(
                BuyPlan.id.in_([t.plan_id for t in tracking])
            )
        ).all():
            ages[pid] = submitted_at or created_at

    # Stall detection (2.5): a PENDING plan with no configured approver sits
    # invisibly — surface plan_needs_approver_reason on its row.
    stalled_ids: set[int] = set()
    from ....services.buyplan_workflow import plan_needs_approver_reason

    pending_ids = [t.plan_id for t in tracking if t.status == BuyPlanStatus.PENDING.value]
    if pending_ids:
        for plan in db.execute(select(BuyPlan).where(BuyPlan.id.in_(pending_ids))).scalars():
            if plan_needs_approver_reason(plan, db):
                stalled_ids.add(plan.id)

    rows = [
        WorkspaceRow(
            key=f"plan-{t.plan_id}",
            pane_url=f"/v2/partials/approvals/plan/{t.plan_id}/pane",
            title=t.customer_name or f"Plan #{t.plan_id}",
            subtitle=f"Plan #{t.plan_id} · {t.part_count} part{'s' if t.part_count != 1 else ''}",
            status=t.status,
            status_label=(t.status or "").replace("_", " ").capitalize(),
            needs_approval=t.can_decide,
            amount=float(t.amount) if t.amount is not None else None,
            age_at=ages.get(t.plan_id),
            copy_number=t.so_number,
            order_type=ORDER_TYPE_LABELS.get(t.order_type or "", t.order_type),
            closed=t.status in _CLOSED_PLAN_STATUSES,
            stalled=t.plan_id in stalled_ids,
        )
        for t in tracking
    ]
    # Decidable first, OLDEST first (spec §5); the rest newest-first.
    needs = sorted((r for r in rows if r.needs_approval), key=lambda r: int(r.key.split("-")[1]))
    rest = sorted((r for r in rows if not r.needs_approval), key=lambda r: -int(r.key.split("-")[1]))
    return needs + rest


def _po_line_row(line: BuyPlanLine, plan, *, needs: bool, closed: bool = False) -> WorkspaceRow:
    """Build one PO-tab row from an ORM line (+ its plan)."""
    mpn = None
    if line.requirement is not None:
        mpn = line.requirement.primary_mpn
    elif line.offer is not None:
        mpn = line.offer.mpn
    vendor = line.offer.vendor_name if line.offer is not None else None
    customer = None
    if plan is not None and plan.requisition is not None:
        customer = plan.requisition.customer_name
    amount = float(line.unit_cost or 0) * (line.quantity or 0)
    return WorkspaceRow(
        key=f"line-{line.id}",
        pane_url=f"/v2/partials/approvals/po/{line.id}/pane",
        title=" · ".join(x for x in (mpn, vendor) if x) or f"Line #{line.id}",
        subtitle=" · ".join(
            x
            for x in (
                customer,
                f"SO {plan.sales_order_number}" if plan is not None and plan.sales_order_number else None,
            )
            if x
        ),
        status=line.status,
        status_label=PO_DECISION_LABELS.get(line.status, line.status),
        needs_approval=needs,
        amount=amount,
        age_at=line.po_confirmed_at or line.created_at,
        copy_number=line.po_number,
        closed=closed,
    )


def _po_rows(db: Session, user: User, *, q: str, scope: str, show_closed: bool) -> list[WorkspaceRow]:
    """Purchase Orders list rows — one per buy-plan line.

    Live: PENDING_VERIFY (oldest first; needs-approval where can_verify_po_line), the
    viewer's assigned AWAITING_PO lines (their confirm-PO work), the claimable
    RESOURCING pool and flagged ISSUE lines. Closed: VERIFIED / CANCELLED lines.
    """
    rows: list[WorkspaceRow] = []

    if show_closed:
        closed_stmt = (
            select(BuyPlanLine)
            .options(
                joinedload(BuyPlanLine.offer),
                joinedload(BuyPlanLine.requirement),
                joinedload(BuyPlanLine.buy_plan).joinedload(BuyPlan.requisition),
            )
            .where(BuyPlanLine.status.in_(_CLOSED_LINE_STATUSES))
        )
        # Mine filters in SQL BEFORE the limit — filtering the 50 newest rows in
        # Python would hide the viewer's older closed lines entirely.
        if scope == "mine":
            closed_stmt = closed_stmt.join(BuyPlan, BuyPlan.id == BuyPlanLine.buy_plan_id).where(
                or_(BuyPlanLine.buyer_id == user.id, BuyPlan.submitted_by_id == user.id)
            )
        closed_lines = db.execute(closed_stmt.order_by(BuyPlanLine.id.desc()).limit(50)).unique().scalars().all()
        rows = [_po_line_row(ln, ln.buy_plan, needs=False, closed=True) for ln in closed_lines]
        return [r for r in rows if _matches(q, r.title, r.subtitle, r.copy_number)]

    # Pending approval — reuse the PO queue read model (oldest first by construction).
    view = build_po_queue_view(db, user, scope=scope)
    for pending in view.pending:
        rows.append(_po_line_row(pending.line, pending.plan, needs=can_verify_po_line(user, pending.line)))

    # The viewer's own confirm-PO work + the open re-sourcing pool + flagged issues.
    other_statuses = (
        BuyPlanLineStatus.AWAITING_PO.value,
        BuyPlanLineStatus.RESOURCING.value,
        BuyPlanLineStatus.ISSUE.value,
    )
    others = (
        db.execute(
            select(BuyPlanLine)
            .options(
                joinedload(BuyPlanLine.offer),
                joinedload(BuyPlanLine.requirement),
                joinedload(BuyPlanLine.buy_plan).joinedload(BuyPlan.requisition),
            )
            .join(BuyPlan, BuyPlan.id == BuyPlanLine.buy_plan_id)
            .where(
                BuyPlanLine.status.in_(other_statuses),
                BuyPlan.status == BuyPlanStatus.ACTIVE.value,
            )
            .order_by(BuyPlanLine.id.asc())
        )
        .unique()
        .scalars()
        .all()
    )
    if scope == "mine":
        others = [
            ln
            for ln in others
            if ln.buyer_id == user.id or (ln.buy_plan is not None and ln.buy_plan.submitted_by_id == user.id)
        ]
    for line in others:
        needs = line.status == BuyPlanLineStatus.AWAITING_PO.value and line.buyer_id == user.id
        rows.append(_po_line_row(line, line.buy_plan, needs=needs))

    return [r for r in rows if _matches(q, r.title, r.subtitle, r.copy_number)]


def _prepayment_rows(db: Session, user: User, *, q: str, scope: str, show_closed: bool) -> list[WorkspaceRow]:
    """Prepayments list rows — pending requests (decidable first) or, behind the Closed
    filter, the recently-resolved audit feed."""
    rows: list[WorkspaceRow] = []
    if show_closed:
        source = resolved_rows_for_gate(db, ApprovalGateType.PREPAYMENT, scope=scope, user=user)
    else:
        source = pending_rows_for_gate(db, user, ApprovalGateType.PREPAYMENT, scope=scope)

    for vm in source:
        if vm.subject_id is None:
            continue
        title = vm.beneficiary or vm.subject_label
        amount = float(vm.amount) if vm.amount is not None else None
        subtitle_bits = [
            (vm.payment_method or "").upper() or None,
            f"SO {vm.so_number}" if vm.so_number else None,
            f"req. {vm.requester_name}" if vm.requester_name and vm.requester_name != "—" else None,
        ]
        status = vm.prepay_status or vm.status
        rows.append(
            WorkspaceRow(
                key=f"prepay-{vm.subject_id}",
                pane_url=f"/v2/partials/approvals/prepayments/{vm.subject_id}/pane",
                title=title,
                subtitle=" · ".join(b for b in subtitle_bits if b),
                status=status,
                status_label=(status or "").replace("_", " ").capitalize(),
                needs_approval=vm.can_act,
                amount=amount,
                age_at=vm.created_at,
                copy_number=vm.po_number,
                closed=show_closed,
            )
        )
    return [r for r in rows if _matches(q, r.title, r.subtitle, r.copy_number)]
