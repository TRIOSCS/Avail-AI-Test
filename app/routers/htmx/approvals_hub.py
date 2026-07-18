"""routers/htmx/approvals_hub.py — Approvals Workspace (4-tab split-view console).

One page, four tabs — Sales Orders · Buy Plans · Purchase Orders · Prepayments — all
lenses on the same pipeline rooted at the sales order (specs/approvals-workspace.md).
Every tab is a split view: LEFT the work list (search, Mine/All, live/closed filter,
age on every row, "Needs your approval" grouped first, oldest default-selected), RIGHT
the detail pane with the action at the bottom. The approvals ENGINE is untouched —
decisions post the existing buy_plans.py / prepayments routes.

Legacy tab keys (buy-plan / po-approval / prepayment) alias onto the new tabs so old
pushed URLs and the existing origin=approvals_hub decide re-renders keep working.
``render_tab_body`` is shared: the tab GET route and the decide handlers both call it
so a one-click decision re-renders the refreshed tab in place.

Called by: app/main.py (router mount); routers/htmx/buy_plans.py + routers/prepayments.py
    (decide handlers' re-render branches).
Depends on: app.dependencies, app.database, app.services.approvals.{queue,po_queue},
    app.services.prepayment_service (read helpers), ._shared (_base_ctx), app.template_env.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from ...constants import (
    SOURCING_ORDER_TYPES,
    AccessKey,
    ApprovalGateType,
    ApprovalRecipientStatus,
    ApprovalRequestStatus,
    BuyPlanLineStatus,
    BuyPlanStatus,
    SalesOrderType,
)
from ...database import get_db
from ...dependencies import can_verify_po_line, require_access, require_user
from ...models import BuyPlan, BuyPlanLine, User
from ...models.approvals import ApprovalRequest, ApprovalStep, ApprovalStepRecipient
from ...services.approvals.po_queue import build_po_queue_view
from ...services.approvals.queue import (
    buy_plan_tracking_rows,
    pending_rows_for_gate,
    resolved_rows_for_gate,
)
from ...template_env import template_response
from ...utils.csv_export import stream_csv
from ._shared import _base_ctx

router = APIRouter(tags=["htmx-views"])

# The four workspace tabs (dash-cased URL segments), in display order. Sales Orders and
# Buy Plans are two LENSES on the same quote-less BuyPlan — same rows, same approve.
_TABS = ("sales-orders", "buy-plans", "purchase-orders", "prepayments")
DEFAULT_TAB = "sales-orders"

# Legacy 3-tab console keys → workspace tabs. Old pushed URLs (?tab=buy-plan) and the
# origin=approvals_hub decide handlers resolve through this map.
LEGACY_TAB_ALIASES = {
    "buy-plan": "buy-plans",
    "po-approval": "purchase-orders",
    "prepayment": "prepayments",
}

_TAB_LABELS = {
    "sales-orders": "Sales Orders",
    "buy-plans": "Buy Plans",
    "purchase-orders": "Purchase Orders",
    "prepayments": "Prepayments",
}

# PO decision vocabulary (spec §5): the UI says Approve / Approved / Pending approval
# everywhere users see a per-line PO state. Backend names stay pending_verify/verified —
# this is a DISPLAY map only, never a code rename.
PO_DECISION_LABELS = {
    BuyPlanLineStatus.AWAITING_PO.value: "Awaiting PO",
    BuyPlanLineStatus.PENDING_VERIFY.value: "Pending approval",
    BuyPlanLineStatus.VERIFIED.value: "Approved",
    BuyPlanLineStatus.ISSUE.value: "Issue",
    BuyPlanLineStatus.CANCELLED.value: "Cancelled",
    BuyPlanLineStatus.RESOURCING.value: "Re-sourcing",
}

# Order-type badge labels (SalesOrderType → short display).
ORDER_TYPE_LABELS = {
    SalesOrderType.NEW.value: "New",
    SalesOrderType.REVISION.value: "Revision",
    SalesOrderType.TESTING_SERVICE.value: "Testing Service",
    SalesOrderType.COMPS.value: "Comps",
    SalesOrderType.STOCK_SALE.value: "Stock Sale",
}

# Plan lifecycle statuses shown in the default (live) list vs behind the Closed filter.
_LIVE_PLAN_STATUSES = (
    BuyPlanStatus.DRAFT.value,
    BuyPlanStatus.PENDING.value,
    BuyPlanStatus.ACTIVE.value,
    BuyPlanStatus.INBOUND.value,
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


def _resolve_tab(tab: str) -> str | None:
    """Map *tab* (new key or legacy alias) to a canonical workspace tab, or None."""
    tab = LEGACY_TAB_ALIASES.get(tab, tab)
    return tab if tab in _TABS else None


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


def _matches(q: str, *fields: str | None) -> bool:
    """Case-insensitive substring match of *q* against any of *fields*."""
    if not q:
        return True
    needle = q.strip().lower()
    return any(needle in (f or "").lower() for f in fields)


# ── Per-viewer badges ───────────────────────────────────────────────────


def _decidable_gate_counts(db: Session, user: User) -> dict[str, int]:
    """Open engine requests the viewer can decide RIGHT NOW, counted per gate type.

    Mirrors the engine's decide() eligibility (REQUESTED + a PENDING recipient slot),
    same join as queue._actionable_request_ids but grouped by gate for the tab badges.
    """
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
    return {str(gate): int(cnt) for gate, cnt in rows}


def _po_waiting_on_viewer(db: Session, user: User) -> int:
    """PO-tab badge: lines waiting on THIS viewer.

    = PENDING_VERIFY lines the viewer may approve (can_verify_po_line — right + dollar
    limit) + the viewer's own assigned AWAITING_PO lines (their confirm-PO work).
    """
    pending = (
        db.execute(select(BuyPlanLine).where(BuyPlanLine.status == BuyPlanLineStatus.PENDING_VERIFY.value))
        .scalars()
        .all()
    )
    verifiable = sum(1 for line in pending if can_verify_po_line(user, line))
    own_awaiting = int(
        db.execute(
            select(func.count(BuyPlanLine.id)).where(
                BuyPlanLine.status == BuyPlanLineStatus.AWAITING_PO.value,
                BuyPlanLine.buyer_id == user.id,
            )
        ).scalar_one()
    )
    return verifiable + own_awaiting


def _viewer_badges(db: Session, user: User) -> dict[str, int]:
    """Per-viewer tab badges (spec §5: tab badges = items waiting on the viewer)."""
    gates = _decidable_gate_counts(db, user)
    plan_count = gates.get(ApprovalGateType.BUY_PLAN.value, 0)
    return {
        # Sales Orders and Buy Plans are lenses on the same object → same badge.
        "sales-orders": plan_count,
        "buy-plans": plan_count,
        "purchase-orders": _po_waiting_on_viewer(db, user),
        "prepayments": gates.get(ApprovalGateType.PREPAYMENT.value, 0),
    }


# ── Shell + tab body ────────────────────────────────────────────────────


@router.get("/v2/partials/approvals", response_class=HTMLResponse)
async def approvals_hub_shell(
    request: Request,
    tab: str = "",
    user: User = Depends(require_access(AccessKey.BUY_PLANS)),
    db: Session = Depends(get_db),
):
    """Return the Approvals Workspace shell (4-pill tab switcher + a lazy tab body).

    The shell renders the four tab pills with per-viewer "waiting on you" badges + a
    lazy body that loads the active tab's split view into ``#ap-hub-body``. ``?tab=``
    threads a deep-link / pushed tab URL; legacy 3-tab keys alias onto the new tabs.
    """
    active_tab = _resolve_tab(tab) or DEFAULT_TAB
    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update(
        {
            "active_tab": active_tab,
            "tabs": [(key, _TAB_LABELS[key]) for key in _TABS],
            "badges": _viewer_badges(db, user),
        }
    )
    return template_response("htmx/partials/approvals/approvals_hub.html", ctx)


@router.get("/v2/partials/approvals/{tab}", response_class=HTMLResponse)
async def approvals_hub_tab(
    request: Request,
    tab: str,
    scope: str = "all",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render one workspace tab body (the split view) into ``#ap-hub-body``.

    ``tab`` is one of the four workspace keys (legacy 3-tab keys alias); any other
    value 404s. ``scope`` seeds the list's Mine/All toggle.
    """
    if _resolve_tab(tab) is None:
        raise HTTPException(404, "Unknown approvals tab")
    return render_tab_body(request, user, db, tab, scope)


def render_tab_body(request: Request, user: User, db: Session, tab: str, scope: str = "all") -> HTMLResponse:
    """Build + render one workspace tab body (shared by the tab GET route and the
    decide handlers' origin=approvals_hub / legacy re-render branches).

    The body is the split view: the left list lazy-loads ``/{tab}/list`` (so a decide
    re-render always repaints a FRESH list), the right pane fills on row selection.
    """
    resolved = _resolve_tab(tab)
    if resolved is None:
        raise HTTPException(404, "Unknown approvals tab")
    scope = "mine" if scope == "mine" else "all"
    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update({"tab": resolved, "tab_label": _TAB_LABELS[resolved], "scope": scope})
    return template_response("htmx/partials/approvals/_workspace_split.html", ctx)


# ── Sales Order / Buy Plan detail pane (both lenses, one anatomy) ───────


def _viewer_can_decide_plan(db: Session, user: User, plan_id: int) -> bool:
    """True when *user* holds a PENDING recipient slot on the plan's open BUY_PLAN
    request (mirrors the engine's decide() gate — same predicate the queue uses)."""
    from ...constants import ApprovalSubjectType

    row = db.execute(
        select(ApprovalRequest.id)
        .join(ApprovalStep, ApprovalStep.request_id == ApprovalRequest.id)
        .join(ApprovalStepRecipient, ApprovalStepRecipient.step_id == ApprovalStep.id)
        .where(
            ApprovalRequest.gate_type == ApprovalGateType.BUY_PLAN,
            ApprovalRequest.subject_type == ApprovalSubjectType.BUY_PLAN,
            ApprovalRequest.subject_id == plan_id,
            ApprovalRequest.status == ApprovalRequestStatus.REQUESTED,
            ApprovalStepRecipient.user_id == user.id,
            ApprovalStepRecipient.status == ApprovalRecipientStatus.PENDING,
        )
        .limit(1)
    ).first()
    return row is not None


def render_plan_pane(
    request: Request, user: User, db: Session, plan_id: int, lens: str = "sales-orders"
) -> HTMLResponse:
    """Build + render the SO/BP detail pane (shared by the pane GET route and the
    approve handler's origin=approvals_workspace re-render branch).

    One anatomy for both lenses (spec §8): header → approval block → Quality (sales
    section) → lines → kanban placeholder → notes placeholder. ``lens`` only threads
    the decide form's re-render target back to the caller's tab.
    """
    from ...dependencies import get_buyplan_for_user
    from ...models.quality_plan import QualityPlan
    from ...services.field_audit import edits_since
    from ...services.qp_workspace import can_edit_qp_sales
    from ...services.stale_guard import stale_token

    lens = lens if lens in ("sales-orders", "buy-plans") else "sales-orders"
    bp = get_buyplan_for_user(
        db,
        user,
        plan_id,
        options=[
            joinedload(BuyPlan.lines).joinedload(BuyPlanLine.offer),
            joinedload(BuyPlan.lines).joinedload(BuyPlanLine.requirement),
            joinedload(BuyPlan.requisition),
            joinedload(BuyPlan.approved_by),
            joinedload(BuyPlan.submitted_by),
        ],
    )
    # The plan's QP row (spec §4: sales section lives on the SO; QP rows stay keyed per
    # (plan, vendor) — the SALES answers are plan-level, so the first row carries them).
    qp = db.execute(
        select(QualityPlan).where(QualityPlan.buy_plan_id == bp.id).order_by(QualityPlan.id.asc()).limit(1)
    ).scalar_one_or_none()

    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update(
        {
            "bp": bp,
            "lines": bp.lines or [],
            "lens": lens,
            "qp": qp,
            "can_decide": bp.status == BuyPlanStatus.PENDING.value and _viewer_can_decide_plan(db, user, bp.id),
            "is_sourcing": (bp.order_type or SalesOrderType.NEW.value) in {t.value for t in SOURCING_ORDER_TYPES},
            "order_type_label": ORDER_TYPE_LABELS.get(bp.order_type or "", bp.order_type),
            "po_labels": PO_DECISION_LABELS,
            # QP-sales inline editing (2.1): the pane hides the editor with the SAME
            # predicate the POST enforces (draft → owner/manager; pending → manager only).
            "can_edit_qp_sales": can_edit_qp_sales(user, bp),
            "qp_stale_token": stale_token(qp) if qp is not None else "",
            # Two-part approve (2.2): the audit-log change summary since submission,
            # embedded in the approval block ("was X → now Y"; empty = nothing changed).
            "change_edits": (
                edits_since(db, buy_plan_id=bp.id, since=bp.submitted_at)
                if bp.status == BuyPlanStatus.PENDING.value
                else []
            ),
        }
    )
    return template_response("htmx/partials/approvals/_pane_sales_order.html", ctx)


@router.get("/v2/partials/approvals/plan/{plan_id:int}/pane", response_class=HTMLResponse)
async def approvals_plan_pane(
    request: Request,
    plan_id: int,
    lens: str = "sales-orders",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """The Sales Orders / Buy Plans right-hand detail pane for one plan.

    404s for a missing plan or a restricted non-owner (get_buyplan_for_user).
    """
    return render_plan_pane(request, user, db, plan_id, lens)


@router.post("/v2/partials/approvals/plan/{plan_id:int}/qp-sales", response_class=HTMLResponse)
async def approvals_plan_qp_sales(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save the SO pane's QP-sales answers (spec §4/§7 — Approvals Workspace 2.1).

    Thin route → ``qp_workspace.apply_qp_sales``. Permission per the §7 matrix
    (``can_edit_qp_sales``): draft → owner or manager; pending → MANAGER ONLY; locked
    otherwise (403). Stale-guarded on the QP row's ``updated_at`` token (a plan with no
    QP row yet round-trips an empty token, which skips the check). The applied diff is
    field-audited (ONE row per save; a no-change save writes nothing). Re-renders the
    pane + refreshes the work list.
    """
    from ...dependencies import get_buyplan_for_user
    from ...services.field_audit import log_field_edits
    from ...services.qp_workspace import apply_qp_sales, can_edit_qp_sales, qp_sales_row
    from ...services.stale_guard import StaleEditError, ensure_not_stale, stale_conflict_response

    bp = get_buyplan_for_user(db, user, plan_id, options=[joinedload(BuyPlan.requisition)])
    if not can_edit_qp_sales(user, bp):
        raise HTTPException(403, "You cannot edit the Quality sales section in this plan's current status.")

    form = await request.form()
    qp = qp_sales_row(db, bp)
    if qp is not None:
        try:
            ensure_not_stale(qp, form.get("expected_updated_at"))
        except StaleEditError:
            return stale_conflict_response()

    fields = {key[len("qp_") :]: value for key, value in form.multi_items() if key.startswith("qp_")}
    try:
        _qp, edits = apply_qp_sales(db, plan=bp, user=user, fields=fields)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    log_field_edits(db, user=user, buy_plan_id=bp.id, edits=edits)
    db.commit()

    resp = render_plan_pane(request, user, db, plan_id, lens=str(form.get("lens", "sales-orders")))
    resp.headers["HX-Trigger"] = "awListRefresh"
    return resp


# ── Purchase-order line detail pane ─────────────────────────────────────


def render_po_pane(request: Request, user: User, db: Session, line_id: int) -> HTMLResponse:
    """Build + render the PO-line detail pane (shared by the pane GET route and the
    confirm-po / verify-po / resource handlers' origin=approvals_workspace branches).

    Buyer view (AWAITING_PO): the confirm-PO form (PO# + est ship + payment method +
    the QP-purchasing fields incl. AS9120B). Manager view (PENDING_VERIFY): line amount
    vs the viewer's limit, Approve / Send back / Cancel via the EXISTING routes, and
    the display-only sent-mail detection. Approved/re-sourcing/issue states render
    their stamps.
    """
    from ...constants import PO_LINE_PAYMENT_METHODS
    from ...dependencies import get_buyplan_for_user
    from ...services.qp_workspace import qp_for_line
    from ...services.stale_guard import stale_token

    line = db.get(
        BuyPlanLine,
        line_id,
        options=[
            joinedload(BuyPlanLine.offer),
            joinedload(BuyPlanLine.requirement),
            joinedload(BuyPlanLine.buyer),
            joinedload(BuyPlanLine.po_verified_by),
        ],
    )
    if line is None:
        raise HTTPException(404, "PO line not found")
    plan = get_buyplan_for_user(db, user, line.buy_plan_id, options=[joinedload(BuyPlan.requisition)])

    # "Line N of M · partial-ship yes/no" — the deferred-scope sibling flag (spec §12).
    sibling_ids = [
        lid
        for (lid,) in db.execute(
            select(BuyPlanLine.id).where(BuyPlanLine.buy_plan_id == plan.id).order_by(BuyPlanLine.id.asc())
        ).all()
    ]
    line_index = sibling_ids.index(line.id) + 1 if line.id in sibling_ids else 1

    qp = qp_for_line(db, plan, line)
    limit = getattr(user, "purchase_order_approval_limit", None)
    amount = float(line.unit_cost or 0) * (line.quantity or 0)

    from .buy_plans import _can_resource  # lazy: buy_plans lazily imports this module back

    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update(
        {
            "line": line,
            "plan": plan,
            "qp": qp,
            "user": user,
            "amount": amount,
            "approval_limit": limit,
            "over_limit": limit is not None and amount > limit,
            "can_verify": can_verify_po_line(user, line),
            "can_resource": _can_resource(user),
            "is_assigned_buyer": line.buyer_id == user.id,
            "line_index": line_index,
            "line_total": len(sibling_ids),
            "partial_ship": (qp.sales_authorized_ship_partial if qp is not None else None),
            "payment_methods": [
                (m.value, m.value.upper() if len(m.value) <= 3 else m.value.title()) for m in PO_LINE_PAYMENT_METHODS
            ],
            "po_labels": PO_DECISION_LABELS,
            "status_label": PO_DECISION_LABELS.get(line.status, line.status),
            # Stale-edit guard (2.1): the confirm-PO / line-edit forms round-trip the
            # LINE's token (narrowest edited object).
            "line_stale_token": stale_token(line),
        }
    )
    return template_response("htmx/partials/approvals/_pane_po_line.html", ctx)


@router.get("/v2/partials/approvals/po/{line_id:int}/pane", response_class=HTMLResponse)
async def approvals_po_pane(
    request: Request,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """The Purchase Orders right-hand detail pane for one buy-plan line."""
    return render_po_pane(request, user, db, line_id)


@router.get("/v2/partials/approvals/po/{line_id:int}/sent-check", response_class=HTMLResponse)
async def approvals_po_sent_check(
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """DISPLAY-ONLY sent-mail detection for one line's PO (spec §8: never auto-verifies).

    Runs the existing ``verify_po_sent`` Graph scan for the line's plan and reports
    whether the buyer's sent folder contains the PO email. Detection is a signal — the
    line only ever verifies through the gated verify_po action.
    """
    from ...services.buyplan_workflow import verify_po_sent

    line = db.get(BuyPlanLine, line_id)
    if line is None:
        raise HTTPException(404, "PO line not found")
    plan = db.get(BuyPlan, line.buy_plan_id)
    if plan is None:
        raise HTTPException(404, "Buy plan not found")

    try:
        results = await verify_po_sent(plan, db)
    except Exception:  # noqa: BLE001 — a detection failure must never break the pane
        results = []
    mine = next((r for r in results if r.get("line_id") == line_id), None)

    if mine and mine.get("found"):
        html = '<span class="text-xs text-emerald-600">PO email found in the buyer&#39;s sent mail (detection only — approve below).</span>'
    elif mine and not mine.get("skipped"):
        html = '<span class="text-xs text-gray-400">No PO email detected in the buyer&#39;s sent mail.</span>'
    else:
        html = '<span class="text-xs text-gray-400">Sent-mail detection unavailable.</span>'
    return HTMLResponse(html)


# ── Prepayment detail pane ──────────────────────────────────────────────


def render_prepayment_pane(request: Request, user: User, db: Session, prepayment_id: int) -> HTMLResponse:
    """Build + render the prepayment detail pane (shared by the pane GET route, the
    method-adjust POST, and the prepay-decide handler's origin=approvals_workspace
    branch).

    Amount + payee always visible; PO#/SO# as copy chips; the payment-method dropdown
    renders on the approval card (adjustable by the approver before deciding — spec
    §7's ONE pre-approval edit); the approve button reads "OK to pay — {method}"; a
    paid prepayment shows its wire reference.
    """
    from ...constants import PREPAYMENT_METHODS, ApprovalSubjectType
    from ...models.quality_plan import Prepayment
    from ...services.approvals.queue import _beneficiary
    from ...services.stale_guard import stale_token

    pp = db.get(
        Prepayment,
        prepayment_id,
        options=[
            joinedload(Prepayment.vendor_card),
            joinedload(Prepayment.buy_plan).joinedload(BuyPlan.requisition),
            joinedload(Prepayment.buy_plan_line),
            joinedload(Prepayment.created_by),
        ],
    )
    if pp is None:
        raise HTTPException(404, "Prepayment not found")

    open_request = db.execute(
        select(ApprovalRequest)
        .where(
            ApprovalRequest.gate_type == ApprovalGateType.PREPAYMENT,
            ApprovalRequest.subject_type == ApprovalSubjectType.PREPAYMENT,
            ApprovalRequest.subject_id == pp.id,
            ApprovalRequest.status == ApprovalRequestStatus.REQUESTED,
        )
        .limit(1)
    ).scalar_one_or_none()

    can_decide = False
    if open_request is not None:
        can_decide = (
            db.execute(
                select(ApprovalStepRecipient.id)
                .join(ApprovalStep, ApprovalStep.id == ApprovalStepRecipient.step_id)
                .where(
                    ApprovalStep.request_id == open_request.id,
                    ApprovalStepRecipient.user_id == user.id,
                    ApprovalStepRecipient.status == ApprovalRecipientStatus.PENDING,
                )
                .limit(1)
            ).first()
            is not None
        )

    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update(
        {
            "pp": pp,
            "plan": pp.buy_plan,
            "line": pp.buy_plan_line,
            "user": user,
            "open_request": open_request,
            "can_decide": can_decide,
            "beneficiary": _beneficiary(pp),
            "prepay_methods": [
                (m.value, m.value.upper() if len(m.value) <= 3 else m.value.title()) for m in PREPAYMENT_METHODS
            ],
            "method_label": (pp.payment_method or "").upper() or "—",
            "pp_stale_token": stale_token(pp),
        }
    )
    return template_response("htmx/partials/approvals/_pane_prepayment.html", ctx)


@router.get("/v2/partials/approvals/prepayments/{prepayment_id:int}/pane", response_class=HTMLResponse)
async def approvals_prepayment_pane(
    request: Request,
    prepayment_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """The Prepayments right-hand detail pane for one prepayment."""
    return render_prepayment_pane(request, user, db, prepayment_id)


@router.post("/v2/partials/approvals/prepayments/{prepayment_id:int}/method", response_class=HTMLResponse)
async def approvals_prepayment_method(
    request: Request,
    prepayment_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Adjust a REQUESTED prepayment's payment method on the approval card (spec §7's
    ONE pre-approval prepayment edit).

    Approver-only (User.can_approve_prepayments — the same flag the engine routes on),
    REQUESTED-only (a decided/paid/void prepayment is immutable), stale-guarded
    (ensure_not_stale on the prepayment's updated_at token), method ∈
    PREPAYMENT_METHODS (COD can never appear — nothing to pay in advance), and the
    change is field-audited (log_field_edits with prepayment_id). Re-renders the pane.
    prepayment_service.py is untouched — this edit never crosses into the engine.
    """
    from ...constants import PREPAYMENT_METHODS, PrepaymentStatus
    from ...models.quality_plan import Prepayment
    from ...services.field_audit import diff_fields, log_field_edits
    from ...services.stale_guard import StaleEditError, ensure_not_stale, stale_conflict_response

    if not getattr(user, "can_approve_prepayments", False):
        raise HTTPException(403, "Prepayment approval right required to adjust the payment method.")

    pp = db.get(Prepayment, prepayment_id)
    if pp is None:
        raise HTTPException(404, "Prepayment not found")
    if pp.status != PrepaymentStatus.REQUESTED.value:
        raise HTTPException(400, "Only a requested (undecided) prepayment's method can be adjusted.")

    form = await request.form()
    method = (form.get("payment_method") or "").strip().lower()
    if method not in {m.value for m in PREPAYMENT_METHODS}:
        raise HTTPException(400, "Invalid prepayment method.")

    try:
        ensure_not_stale(pp, form.get("expected_updated_at"))
    except StaleEditError:
        return stale_conflict_response()

    edits = diff_fields(pp, {"payment_method": method})
    if edits:
        pp.payment_method = method
        log_field_edits(db, user=user, buy_plan_id=pp.buy_plan_id, prepayment_id=pp.id, edits=edits)
        db.commit()

    resp = render_prepayment_pane(request, user, db, prepayment_id)
    resp.headers["HX-Trigger"] = "awListRefresh"
    return resp


# ── The left work list ──────────────────────────────────────────────────


@router.get("/v2/partials/approvals/{tab}/list", response_class=HTMLResponse)
async def approvals_workspace_list(
    request: Request,
    tab: str,
    q: str = "",
    scope: str = "all",
    show_closed: bool = False,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render one tab's left work list (search + Mine/All + live/closed + age rows).

    Rows group "Needs your approval" first (oldest first — decision queues surface the
    stalest work); the rest render newest-first. The oldest needs-your-approval row is
    the default selection (dispatched to the pane on first load only).
    """
    resolved = _resolve_tab(tab)
    if resolved is None:
        raise HTTPException(404, "Unknown approvals tab")
    scope = "mine" if scope == "mine" else "all"

    if resolved in ("sales-orders", "buy-plans"):
        rows = _plan_rows(db, user, lens=resolved, q=q, scope=scope, show_closed=show_closed)
    elif resolved == "purchase-orders":
        rows = _po_rows(db, user, q=q, scope=scope, show_closed=show_closed)
    else:
        rows = _prepayment_rows(db, user, q=q, scope=scope, show_closed=show_closed)

    needs = [r for r in rows if r.needs_approval]
    rest = [r for r in rows if not r.needs_approval]
    default_row = needs[0] if needs else None

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


def _plan_rows(db: Session, user: User, *, lens: str, q: str, scope: str, show_closed: bool) -> list[WorkspaceRow]:
    """Sales Orders / Buy Plans list rows — one per plan, decidable first (oldest
    first), then the rest newest-first. Both lenses read the same tracking rows."""
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

    rows = [
        WorkspaceRow(
            key=f"plan-{t.plan_id}",
            pane_url=f"/v2/partials/approvals/plan/{t.plan_id}/pane?lens={lens}",
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
        closed_lines = (
            db.execute(
                select(BuyPlanLine)
                .options(
                    joinedload(BuyPlanLine.offer),
                    joinedload(BuyPlanLine.requirement),
                    joinedload(BuyPlanLine.buy_plan).joinedload(BuyPlan.requisition),
                )
                .where(BuyPlanLine.status.in_(_CLOSED_LINE_STATUSES))
                .order_by(BuyPlanLine.id.desc())
                .limit(50)
            )
            .unique()
            .scalars()
            .all()
        )
        if scope == "mine":
            closed_lines = [
                ln
                for ln in closed_lines
                if ln.buyer_id == user.id or (ln.buy_plan is not None and ln.buy_plan.submitted_by_id == user.id)
            ]
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
    """Prepayments list rows — pending requests (decidable first) or, behind the
    Closed filter, the recently-resolved audit feed."""
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


# ── CSV export (kept from the 3-tab console; legacy tab keys alias) ─────


def _fmt_dt(dt: datetime | None) -> str:
    """Minute-precision timestamp for a CSV cell (empty string when missing)."""
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""


@router.get("/v2/partials/approvals/{tab}/export")
async def approvals_hub_export(
    tab: str,
    scope: str = "all",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Stream one workspace list as a CSV download (attachment).

    Same auth (require_user) and Mine/All scope as the tab list, reusing each tab's
    exact read model so the download can never drift from what the console shows:
      - sales-orders / buy-plans → the plan tracking list (buy_plan_tracking_rows);
      - prepayments → the resolved audit feed (resolved_rows_for_gate);
      - purchase-orders → the resolved PO decision feed (build_po_queue_view.history).
    Legacy 3-tab keys alias; anything else 404s.
    """
    resolved = _resolve_tab(tab)
    if resolved is None:
        raise HTTPException(404, "Unknown approvals tab")
    scope = "mine" if scope == "mine" else "all"

    if resolved in ("sales-orders", "buy-plans"):
        header = ["Plan ID", "Customer", "Sales Order", "Order Type", "Status", "Value"]
        rows = (
            [r.plan_id, r.customer_name, r.so_number, r.order_type, r.status, r.amount]
            for r in buy_plan_tracking_rows(db, user, scope=scope)
        )
        return stream_csv(f"approvals_sales_orders_{scope}.csv", header, rows)

    if resolved == "prepayments":
        header = [
            "Prepayment ID",
            "Beneficiary",
            "Plan ID",
            "PO Number",
            "SO Number",
            "Amount",
            "Currency",
            "Request Status",
            "Payment Status",
            "Decided By",
            "Wire Reference",
            "Resolution Note",
            "Resolved Date",
        ]
        rows = (
            [
                r.subject_id,
                r.beneficiary or r.subject_label,
                r.plan_id,
                r.po_number,
                r.so_number,
                r.amount,
                r.currency,
                r.status,
                r.prepay_status,
                r.decided_by,
                r.wire_reference,
                r.resolution_note,
                _fmt_dt(r.resolved_at),
            ]
            for r in resolved_rows_for_gate(db, ApprovalGateType.PREPAYMENT, scope=scope, user=user)
        )
        return stream_csv(f"approvals_prepayments_resolved_{scope}.csv", header, rows)

    # purchase-orders — the org-wide recently-resolved PO decision feed.
    header = ["Plan ID", "Outcome", "Description", "Actor", "Note", "Resolved Date"]
    rows = (
        [h.plan_id, h.kind, h.label, h.actor_name, h.note, _fmt_dt(h.when)]
        for h in build_po_queue_view(db, user, scope=scope).history
    )
    return stream_csv("approvals_po_resolved.csv", header, rows)
