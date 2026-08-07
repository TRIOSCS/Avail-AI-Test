"""Approvals Workspace detail panes — the deal (SO/BP), PO-line, and prepayment right-
hand panes, their GET routes, and their pane-local POSTs (qp-sales save, sent-check,
method adjust).

W4.8 split of app/routers/htmx/approvals_hub.py — pure structural move: URLs and
behavior unchanged; routes attach to the shared router imported from .common.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ....constants import (
    SOURCING_ORDER_TYPES,
    ApprovalGateType,
    ApprovalRecipientStatus,
    ApprovalRequestStatus,
    BuyPlanLineStatus,
    BuyPlanStatus,
    SalesOrderType,
)
from ....database import get_db
from ....dependencies import can_verify_po_line, require_user
from ....models import BuyPlan, BuyPlanLine, User
from ....models.approvals import ApprovalRequest, ApprovalStep, ApprovalStepRecipient
from ....template_env import template_response
from .._shared import _base_ctx
from .common import ORDER_TYPE_LABELS, PO_DECISION_LABELS, _notes_ctx, router

# ── Deal (SO/BP) detail pane ────────────────────────────────────────────


def _viewer_can_decide_plan(db: Session, user: User, plan_id: int) -> bool:
    """True when *user* holds a PENDING recipient slot on the plan's open BUY_PLAN
    request (mirrors the engine's decide() gate — same predicate the queue uses)."""
    from ....constants import ApprovalSubjectType

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


def render_plan_pane(request: Request, user: User, db: Session, plan_id: int) -> HTMLResponse:
    """Build + render the deal (SO/BP) detail pane (shared by the pane GET route and the
    approve handler's origin=approvals_workspace re-render branch).

    One anatomy (spec §8): header → approval block → Quality (sales section) → lines →
    kanban → notes.
    """
    from ....dependencies import get_buyplan_for_user, is_manager_or_admin
    from ....models.quality_plan import QualityPlan
    from ....services.buyplan_workflow import plan_needs_approver_reason
    from ....services.field_audit import edits_since
    from ....services.kanban_lanes import build_kanban
    from ....services.qp_workspace import can_edit_qp_sales
    from ....services.stale_guard import stale_token

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

    is_sourcing = (bp.order_type or SalesOrderType.NEW.value) in {t.value for t in SOURCING_ORDER_TYPES}
    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update(
        {
            "bp": bp,
            "lines": bp.lines or [],
            "qp": qp,
            "can_decide": bp.status == BuyPlanStatus.PENDING.value and _viewer_can_decide_plan(db, user, bp.id),
            "is_sourcing": is_sourcing,
            # PO kanban (3.3, spec §6) — the centerpiece on ACTIVE sourcing
            # orders; None hides the board entirely (draft/pending/closed + lite plans).
            "kanban": (build_kanban(db, bp) if is_sourcing and bp.status == BuyPlanStatus.ACTIVE.value else None),
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
            # Notes + attachments (2.4): the plan-level thread.
            **_notes_ctx(db, user, plan_id=bp.id),
            # Lifecycle controls (2.5): manager-only halt/resume/cancel/reset on the
            # pane — same authority the existing buy_plans.py POSTs enforce.
            "can_lifecycle": is_manager_or_admin(user),
            # Why the plan is silently stalled for lack of a configured approver.
            "no_approver_reason": plan_needs_approver_reason(bp, db),
        }
    )
    return template_response("htmx/partials/approvals/_pane_sales_order.html", ctx)


@router.get("/v2/partials/approvals/plan/{plan_id:int}/pane", response_class=HTMLResponse)
async def approvals_plan_pane(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """The Deals right-hand detail pane for one plan.

    404s for a missing plan or a restricted non-owner (get_buyplan_for_user).
    """
    return render_plan_pane(request, user, db, plan_id)


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
    from ....dependencies import get_buyplan_for_user
    from ....services.field_audit import log_field_edits
    from ....services.qp_workspace import apply_qp_sales, can_edit_qp_sales, qp_sales_row
    from ....services.stale_guard import StaleEditError, ensure_not_stale, stale_conflict_response

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

    resp = render_plan_pane(request, user, db, plan_id)
    resp.headers["HX-Trigger"] = "awListRefresh"
    return resp


# ── Purchase-order line detail pane ─────────────────────────────────────


def render_po_pane(request: Request, user: User, db: Session, line_id: int) -> HTMLResponse:
    """Build + render the PO-line detail pane (shared by the pane GET route and the
    confirm-po / verify-po / resource handlers' origin=approvals_workspace branches).

    Buyer view (AWAITING_PO): the confirm-PO form (PO# + est ship + payment method + the
    QP-purchasing fields incl. AS9120B). Manager view (PENDING_VERIFY): line amount vs
    the viewer's limit, Approve / Send back / Cancel via the EXISTING routes, and the
    display-only sent-mail detection. Approved/re-sourcing/issue states render their
    stamps.
    """
    from ....constants import PO_LINE_PAYMENT_METHODS
    from ....dependencies import get_buyplan_for_user, is_manager_or_admin
    from ....services.buyplan_workflow import can_edit_buy_plan_lines
    from ....services.field_audit import manager_edited_line_ids
    from ....services.qp_workspace import qp_for_line
    from ....services.stale_guard import stale_token

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

    from ..buy_plans import _can_resource  # lazy: buy_plans lazily imports this module back

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
            # Manager edit-anything at verify (2.3): the pane shows the edit form with
            # the SAME predicate the /lines/{id}/edit service gate enforces (manager on
            # an editable plan, line at pending_verify), plus the edited-by marker.
            "can_manager_edit": (
                is_manager_or_admin(user)
                and can_edit_buy_plan_lines(user, plan)
                and line.status == BuyPlanLineStatus.PENDING_VERIFY.value
            ),
            "manager_edited": line.id in manager_edited_line_ids(db, plan),
            # Notes + attachments (2.4): the line's own thread.
            **_notes_ctx(db, user, plan_id=plan.id, line_id=line.id),
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
    """DISPLAY-ONLY sent-mail detection for one line's PO (spec §8: never auto-
    verifies).

    Runs the existing ``verify_po_sent`` Graph scan for the line's plan and reports
    whether the buyer's sent folder contains the PO email. Detection is a signal — the
    line only ever verifies through the gated verify_po action. Plan access rides
    get_buyplan_for_user (same gate as render_po_pane): a restricted non-owner 404s
    BEFORE any Graph scan runs.
    """
    from ....dependencies import get_buyplan_for_user
    from ....services.buyplan_workflow import verify_po_sent

    line = db.get(BuyPlanLine, line_id)
    if line is None:
        raise HTTPException(404, "PO line not found")
    plan = get_buyplan_for_user(db, user, line.buy_plan_id)

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
    renders on the approval card (adjustable by the approver before deciding — spec §7's
    ONE pre-approval edit); the approve button reads "OK to pay — {method}"; a paid
    prepayment shows its wire reference. Plan access rides get_buyplan_for_user (same
    gate as render_plan_pane / render_po_pane): a restricted non-owner 404s.
    """
    from ....constants import PREPAYMENT_METHODS, ApprovalSubjectType
    from ....dependencies import get_buyplan_for_user
    from ....models.quality_plan import Prepayment
    from ....services.approvals.queue import _beneficiary
    from ....services.stale_guard import stale_token

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
    get_buyplan_for_user(db, user, pp.buy_plan_id)  # restricted non-owner → 404

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
            # Notes + attachments (2.4): the prepayment's own thread.
            **_notes_ctx(db, user, plan_id=pp.buy_plan_id, prepayment_id=pp.id),
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
    (ensure_not_stale on the prepayment's updated_at token), method ∈ PREPAYMENT_METHODS
    (COD can never appear — nothing to pay in advance), and the change is field-audited
    (log_field_edits with prepayment_id). Re-renders the pane. prepayment_service.py is
    untouched — this edit never crosses into the engine.
    """
    from ....constants import PREPAYMENT_METHODS, PrepaymentStatus
    from ....models.quality_plan import Prepayment
    from ....services.field_audit import diff_fields, log_field_edits
    from ....services.stale_guard import StaleEditError, ensure_not_stale, stale_conflict_response

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
