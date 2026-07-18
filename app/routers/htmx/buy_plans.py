"""routers/htmx/buy_plans.py — Buy Plans / Approvals partial views (HTMX + Alpine).

Server-rendered HTML partials for the buy-plan workflow the Approvals Workspace
drives: sales-order new/create (origination), buy-plan detail, editable lines
(add/edit/remove — role×status gated), the SO-number field, and per-plan lifecycle
actions (submit, approve, halt, resume, confirm-po, resource, claim, verify-po,
issue, cancel, reset). The retired /v2/buy-plans hub's partial URLs 308 onto their
workspace equivalents (spec §11.1; docs/APPROVALS_PARITY_CHECKLIST.md).

Called by: app/main.py (router mount).
Depends on: app.models, app.dependencies, app.database, app.services.approvals,
    ._shared (imports _is_ops_member shared with a staying quotes route).
"""

import json
import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ...constants import (
    RESTRICTED_ROLES,
    AccessKey,
    BuyPlanStatus,
    PaymentMethod,
    UserRole,
)
from ...database import get_db
from ...dependencies import (
    get_buyplan_for_user,
    is_manager_or_admin,
    require_access,
    require_buyplan_approver,
    require_buyplan_po_approver,
    require_user,
)
from ...models import (
    BuyPlan,
    BuyPlanLine,
    Requisition,
    User,
)
from ...services.buyplan_naming import summarize_top_flag
from ...services.stale_guard import StaleEditError, ensure_not_stale, stale_conflict_response
from ...template_env import template_response
from ._shared import _base_ctx, _is_ops_member

router = APIRouter(tags=["htmx-views"])


def _can_supervise(user: User, db: Session) -> bool:
    """True when the user may see cross-user (scope=all) deal data.

    Managers/admins and ops verification-group members qualify.
    """
    return user.role in (UserRole.MANAGER, UserRole.ADMIN) or _is_ops_member(user, db)


_PO_CUTTER_ROLES = (UserRole.BUYER, UserRole.MANAGER, UserRole.ADMIN)


def _can_resource(user: User) -> bool:
    """True when the user may re-source / claim buy-plan lines (a PO-cutter)."""
    return user.role in _PO_CUTTER_ROLES


def _require_po_cutter(user: User) -> None:
    """403 unless the user is an active PO-cutter (buyer/manager/admin)."""
    if not _can_resource(user) or not getattr(user, "is_active", True):
        raise HTTPException(403, "Only buyers and managers can re-source / claim lines")


def _parse_optional_int(raw: str | None) -> int | None:
    """Parse an optional whole-number form field: blank → None; non-numeric → 400."""
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError) as e:
        raise HTTPException(400, "Expected a whole number.") from e


def _parse_optional_float(raw: str | None) -> float | None:
    """Parse an optional decimal form field: blank → None; non-numeric → 400."""
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError) as e:
        raise HTTPException(400, "Expected a number.") from e


async def _notify_if_completed(plan_id: int, just_completed: bool) -> None:
    """Fire the completion notification exactly once, driven by a caller-computed
    *just_completed* flag — NEVER by re-deriving it via a second ``check_completion``
    call.

    The auto-complete DECISION lives entirely at service depth: ``verify_po``,
    ``remove_buy_plan_line``, and ``bulk_edit_buy_plan_lines`` each call
    ``check_completion`` themselves right after mutating line state, so by the time
    control returns to the route the plan/line object already carries the answer in its
    (still in-session, pre-commit) ``.status``. Callers capture *just_completed* from
    that status BEFORE ``db.commit()`` (so an expired attribute after commit can't force
    a surprise re-fetch) and pass it here AFTER commit — re-scanning the plan's lines a
    second time here would be redundant work and, worse, a second opportunity to invoke
    completion side effects if the "already complete" short-circuit were ever weakened.
    Used identically by the verify-po, remove-line, and bulk-save-lines routes.
    """
    if not just_completed:
        return
    from ...services.buyplan_notifications import notify_completed, run_notify_bg

    await run_notify_bg(notify_completed, plan_id)


# The auto-filled note when a manager sends a plan back for sign-off without typing
# one — the engine requires a non-blank reject comment, and the change summary (the
# audit log since submission) always rides along on the pane (spec §7).
SEND_BACK_DEFAULT_NOTE = "Sent back for sign-off — see change summary"


def _workspace_pane_response(request: Request, user: User, db: Session, plan_id: int, form) -> HTMLResponse:
    """The shared origin=approvals_workspace re-render for plan lifecycle POSTs (halt /
    resume / cancel / reset — 2.5): the plan's SO/BP pane in place + an awListRefresh
    nudge so the left work list repaints its status."""
    from .approvals_hub import render_plan_pane

    resp = render_plan_pane(request, user, db, plan_id, lens=str(form.get("lens", "sales-orders")))
    resp.headers["HX-Trigger"] = "awListRefresh"
    return resp


@router.get("/v2/partials/buy-plans")
async def buy_plans_list_partial(
    new: bool = False,
    user: User = Depends(require_access(AccessKey.BUY_PLANS)),
) -> RedirectResponse:
    """Retired Buy Plans hub shell — 308 onto the Approvals Workspace.

    The personal My Queue + Pipeline hub retired into the workspace (spec §11.1;
    docs/APPROVALS_PARITY_CHECKLIST.md). ``new=1`` (the old origination entry point)
    308s straight to the Sales-Order origination picker, which now hosts itself in
    ``#main-content``; everything else lands on the workspace shell's Buy Plans tab.
    """
    if new:
        return RedirectResponse("/v2/partials/buy-plans/sales-orders/new", status_code=308)
    return RedirectResponse("/v2/partials/approvals?tab=buy-plans", status_code=308)


def _normalize_order_type(raw: str | None) -> str:
    """Normalize a picker/create order-type value: blank/unknown → NEW."""
    from ...constants import SalesOrderType

    value = (raw or "").strip().lower()
    return value if value in {t.value for t in SalesOrderType} else SalesOrderType.NEW.value


@router.get("/v2/partials/buy-plans/sales-orders/new", response_class=HTMLResponse)
async def sales_order_new(
    request: Request,
    requisition_id: int | None = None,
    order_type: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """New Sales Order origination surface (requisition picker → offer/sell builder).

    Origination is deal CREATION, not a decide action — it keeps the Buy Plans partial
    prefix (/v2/partials/buy-plans/*), NOT the Approvals decide prefix, and is the entry
    the workspace lists' "New sales order" button loads into ``#main-content`` (the
    surface hosts its own ``#so-origination`` swap container). The two-segment
    ``sales-orders/new`` path does not collide with the ``{plan_id:int}`` detail route or
    the one-segment retired-lens redirect ``{tab}`` converter.

    ``order_type`` drives the path (spec §3): SOURCING types (New / Revision) list open
    (OPEN_PIPELINE) requisitions carrying at least one ACTIVE offer and build via the
    per-requirement offer/sell form; NON-SOURCING types (Stock Sale / Testing Service /
    Comps) take the LITE path — any open requisition qualifies (no offers needed) and
    the builder collapses to a create-only confirm. Access via ``get_req_for_user``
    (404 for a restricted role that does not own the requisition).
    """
    from sqlalchemy import func

    from ...constants import (
        SOURCING_ORDER_TYPES,
        OfferStatus,
        RequisitionStatus,
        SalesOrderType,
    )
    from ...dependencies import get_req_for_user
    from ...models import Offer, Requirement
    from ...services.quote_builder_service import apply_smart_defaults, get_builder_data

    otype = _normalize_order_type(order_type)
    sourcing = otype in {t.value for t in SOURCING_ORDER_TYPES}
    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update(
        {
            "order_type": otype,
            "sourcing": sourcing,
            "order_type_choices": [(t.value, t.value.replace("_", " ").title()) for t in SalesOrderType],
        }
    )

    if requisition_id is not None:
        req = get_req_for_user(db, user, requisition_id)
        lines = []
        if sourcing:
            lines = get_builder_data(req.id, db)
            apply_smart_defaults(lines)
        ctx.update({"selected_req": req, "lines": lines})
        return template_response("htmx/partials/approvals/_sales_order_new.html", ctx)

    # Picker mode: open requisitions, scoped to the viewer. Sourcing types additionally
    # require at least one active offer (the plan is built FROM offers); non-sourcing
    # (lite) types list every open requisition.
    stmt = select(Requisition).where(Requisition.status.in_(list(RequisitionStatus.OPEN_PIPELINE)))
    if sourcing:
        has_active_offer = (
            select(Offer.id)
            .join(Requirement, Offer.requirement_id == Requirement.id)
            .where(
                Requirement.requisition_id == Requisition.id,
                Offer.status == OfferStatus.ACTIVE,
            )
            .exists()
        )
        stmt = stmt.where(has_active_offer)
    if user.role in RESTRICTED_ROLES:
        stmt = stmt.where(Requisition.created_by == user.id)
    reqs = db.scalars(stmt.order_by(Requisition.id.desc())).all()

    counts: dict[int, int] = {}
    if reqs:
        counts = dict(
            db.query(Requirement.requisition_id, func.count(Offer.id))
            .join(Offer, Offer.requirement_id == Requirement.id)
            .filter(
                Requirement.requisition_id.in_([r.id for r in reqs]),
                Offer.status == OfferStatus.ACTIVE,
            )
            .group_by(Requirement.requisition_id)
            .all()
        )

    picker_rows = [
        {"id": r.id, "name": r.name, "customer": r.customer_name or "", "offer_count": counts.get(r.id, 0)}
        for r in reqs
    ]
    ctx.update({"selected_req": None, "picker_rows": picker_rows})
    return template_response("htmx/partials/approvals/_sales_order_new.html", ctx)


@router.post("/v2/partials/buy-plans/sales-orders/create", response_class=HTMLResponse)
async def sales_order_create(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Originate a DRAFT buy plan (Sales Order), then render its detail.

    Parses ``requisition_id`` + ``order_type`` + per-requirement ``offer_<rid>`` /
    ``sell_<rid>`` form fields, enforces requisition access
    (``require_requisition_access`` — 404 for a restricted role that does not own it).
    SOURCING order types (New / Revision) build from the chosen offers
    (``create_sales_order_from_offers``); NON-SOURCING types (Stock Sale / Testing
    Service / Comps) take the LITE path (``create_lite_sales_order`` — zero lines, no
    kanban). On the builder's duplicate-open-SO ValueError it renders the existing open
    Sales Order's detail with a toast (never a 500); any other ValueError (e.g. no
    requirements) is a 400.
    """
    from ...constants import SOURCING_ORDER_TYPES
    from ...dependencies import require_requisition_access
    from ...services.buyplan_builder import (
        DuplicateSalesOrderError,
        create_lite_sales_order,
        create_sales_order_from_offers,
    )

    form = await request.form()
    raw_req_id = form.get("requisition_id")
    if not raw_req_id:
        raise HTTPException(400, "Requisition is required")
    try:
        req_id = int(raw_req_id)
    except (TypeError, ValueError) as e:
        raise HTTPException(400, "Invalid requisition") from e

    require_requisition_access(db, req_id, user)

    order_type = _normalize_order_type(form.get("order_type"))

    selections: dict[int, int] = {}
    sell_prices: dict[int, float] = {}
    for key, value in form.multi_items():
        if key.startswith("offer_"):
            try:
                selections[int(key[len("offer_") :])] = int(value)
            except (TypeError, ValueError):
                continue
        elif key.startswith("sell_"):
            if value in (None, ""):
                continue
            try:
                sell_prices[int(key[len("sell_") :])] = float(value)
            except (TypeError, ValueError):
                continue

    try:
        if order_type in {t.value for t in SOURCING_ORDER_TYPES}:
            plan = create_sales_order_from_offers(req_id, selections, sell_prices, db, user, order_type=order_type)
        else:
            plan = create_lite_sales_order(req_id, order_type, db, user)
    except DuplicateSalesOrderError as exc:
        # An open Sales Order already exists for this requisition — open it instead of
        # 500ing. The exception carries the existing plan id, so no re-query is needed.
        existing_id = exc.existing_plan_id
        resp = await buy_plan_detail_partial(request, existing_id, user, db)
        resp.headers["HX-Trigger"] = json.dumps(
            {
                "showToast": {
                    "message": f"There is already an open buy plan for this requisition (plan #{existing_id}).",
                    "type": "warning",
                }
            }
        )
        resp.headers["HX-Push-Url"] = f"/v2/buy-plans/{existing_id}"
        return resp
    except ValueError as e:
        # Any other origination failure (e.g. requisition has no requirements). Return a
        # curated client message rather than echoing the raw builder error.
        raise HTTPException(400, "Could not build a buy plan from the selected offers.") from e

    resp = await buy_plan_detail_partial(request, plan.id, user, db)
    resp.headers["HX-Push-Url"] = f"/v2/buy-plans/{plan.id}"
    return resp


@router.post("/v2/partials/approvals/prepay-requests/{request_id}/decide", response_class=HTMLResponse)
async def prepay_request_decide(
    request: Request,
    request_id: int,
    action: str = Form("approve"),
    comment: str | None = Form(None),
    origin: str = Form(""),
    hub_scope: str = Form("all"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Decide a prepayment ApprovalRequest from an inline action (HTML re-render).

    The standalone decision route (POST /v2/approvals/requests/{id}/decision) returns JSON;
    the inline callers instead need a refreshed body swapped in place. This thin sibling
    resolves the request via the SAME approvals-engine ``decide`` (no duplicated logic),
    then re-renders the caller's surface by ``origin``: ``approvals_workspace`` → the
    prepayment's pane + a list refresh; anything else → the workspace Prepayments tab
    body (``#ap-hub-body``). Reject requires a non-blank comment (400 otherwise); a caller
    who holds no PENDING recipient slot is 403 (engine PermissionError); a stale/decided
    request is 400 (engine ValueError).
    """
    from ...constants import ApprovalGateType
    from ...models.approvals import ApprovalRequest
    from ...services.approvals.service import decide as svc_decide

    if action == "reject" and not (comment or "").strip():
        raise HTTPException(400, "A reason is required to reject a prepayment.")

    # This route is prepayment-specific: refuse a non-PREPAYMENT request outright so it can
    # neither decide a foreign gate here nor mis-fire the OK-TO-WIRE notice against a wrong
    # subject_id (a buy-plan/quote subject_id is NOT a Prepayment id).
    ar = db.get(ApprovalRequest, request_id)
    if ar is None:
        raise HTTPException(404, "Prepayment request not found.")
    if ar.gate_type != ApprovalGateType.PREPAYMENT:
        raise HTTPException(400, "Not a prepayment approval request.")

    try:
        svc_decide(db, request_id, user, action, comment=comment or None)
        db.commit()
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    # Stamp the prepayment lifecycle + fan the accounting/AP notice. Fire-and-forget: the
    # runner isolates every error so a failed notice never breaks the decision that just
    # committed. APPROVE → approved + mint the single-use pay_token (the "OK TO WIRE" email
    # link); REJECT → void + the "DO NOT WIRE" stand-down.
    if ar.subject_id is not None and action in ("approve", "reject"):
        from ...constants import PrepaymentStatus
        from ...models.quality_plan import Prepayment
        from ...services.prepayment_notifications import (
            notify_prepayment_approved,
            notify_prepayment_voided,
            run_prepayment_notify_bg,
        )

        pp = db.get(Prepayment, ar.subject_id)
        if action == "approve":
            if pp is not None:
                pp.status = PrepaymentStatus.APPROVED.value
                pp.approved_by_id = user.id
                pp.approved_at = datetime.now(UTC)
                pp.pay_token = secrets.token_urlsafe(32)
                db.commit()
            await run_prepayment_notify_bg(notify_prepayment_approved, ar.subject_id)
        elif pp is not None:  # reject
            pp.status = PrepaymentStatus.VOID.value
            pp.void_reason = "rejected by approver"
            pp.voided_at = datetime.now(UTC)
            pp.voided_by_id = user.id
            # Note-to-the-fixer (2.2): the (required) reject reason lands on the
            # prepayment's notes thread tagged with the decision, and the requester
            # (the fixer) gets an in-app notification.
            from ...services.approvals.notifications import write_in_app
            from ...services.workspace_notes import add_note

            note_text = (comment or "").strip()
            if note_text:
                add_note(
                    db,
                    user=user,
                    body=note_text,
                    buy_plan_id=pp.buy_plan_id,
                    prepayment_id=pp.id,
                    decision="rejected",
                )
            if pp.created_by_id is not None:
                write_in_app(db, pp.created_by_id, "prepay_rejected", f"Prepayment #{pp.id} rejected", note_text)
            db.commit()
            await run_prepayment_notify_bg(notify_prepayment_voided, pp.id)

    if origin == "approvals_workspace" and ar.subject_id is not None:
        # Workspace pane decide: re-render the prepayment's pane in place + repaint
        # the left list (awListRefresh), mirroring the SO/PO pane branches.
        from .approvals_hub import render_prepayment_pane

        resp = render_prepayment_pane(request, user, db, int(ar.subject_id))
        resp.headers["HX-Trigger"] = "awListRefresh"
        return resp
    # Legacy-console origin ("approvals_hub") and originless posts both land on the
    # workspace Prepayments tab body — the only list surface these inline decisions
    # originate from since the Buy Plans hub retired (render_tab_body also resolves
    # the legacy "prepayment" tab key onto the same body).
    from .approvals_hub import render_tab_body

    return render_tab_body(request, user, db, "prepayments", hub_scope)


@router.get("/v2/partials/buy-plans/{plan_id:int}", response_class=HTMLResponse)
async def buy_plan_detail_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return buy plan detail as HTML partial.

    The ``{plan_id:int}`` path convertor is load-bearing: it makes this route match ONLY
    integer segments, so the sibling retired-lens redirect ``/v2/partials/buy-plans/{tab}``
    (str) and the literal ``/pipeline-archive`` never shadow a numeric plan id and
    vice-versa.
    """
    bp = get_buyplan_for_user(
        db,
        user,
        plan_id,
        options=[
            joinedload(BuyPlan.lines).joinedload(BuyPlanLine.offer),
            joinedload(BuyPlan.lines).joinedload(BuyPlanLine.requirement),
            joinedload(BuyPlan.lines).joinedload(BuyPlanLine.buyer),
            joinedload(BuyPlan.quote),
            joinedload(BuyPlan.requisition),
            joinedload(BuyPlan.submitted_by),
            joinedload(BuyPlan.approved_by),
        ],
    )

    from ...services.buyplan_workflow import can_edit_buy_plan_lines, plan_needs_approver_reason
    from ...services.prepayment_service import prepayment_state_for_lines

    lines = bp.lines or []

    # Editing surface (epics I/J/K). ``can_edit_lines`` is the SAME server-side gate the
    # add/edit/remove endpoints enforce, so the template hides the controls with the exact
    # predicate the POSTs check (never UI-only). ``can_manage_plan`` = owner-or-manager, the
    # gate the Cancel + SO-number endpoints enforce. Offers/requirements power the vendor
    # picker + add-line form; loaded only when the viewer can actually edit (no wasted query).
    can_edit_lines = can_edit_buy_plan_lines(user, bp)
    can_manage_plan = is_manager_or_admin(user) or (bp.requisition and bp.requisition.created_by == user.id)
    terminal = bp.status in (BuyPlanStatus.COMPLETED.value, BuyPlanStatus.CANCELLED.value)
    offers_by_requirement: dict[int, list] = {}
    plan_requirements: list = []
    if can_edit_lines:
        from ...constants import OfferStatus
        from ...models import Offer, Requirement

        plan_requirements = (
            db.query(Requirement).filter(Requirement.requisition_id == bp.requisition_id).order_by(Requirement.id).all()
        )
        active_offers = (
            db.query(Offer)
            .options(joinedload(Offer.vendor_card))
            .filter(Offer.requisition_id == bp.requisition_id, Offer.status == OfferStatus.ACTIVE.value)
            .order_by(Offer.unit_price)
            .all()
        )
        for off in active_offers:
            if off.requirement_id is not None:
                offers_by_requirement.setdefault(off.requirement_id, []).append(off)

    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update(
        {
            "bp": bp,
            "lines": lines,
            "is_ops_member": _is_ops_member(user, db),
            "can_resource": _can_resource(user),
            # Supervisors/ops resolve flagged-issue lines (the buyer who raised them can't).
            "can_supervise": _can_supervise(user, db),
            "user": user,
            # Line-editing gate (epic I) + owner/manager gate for Cancel + SO number (J/K).
            "can_edit_lines": can_edit_lines,
            "can_manage_plan": can_manage_plan,
            # Resume is manager-only and only meaningful on a halted plan (epic K).
            "can_resume": is_manager_or_admin(user) and bp.status == BuyPlanStatus.HALTED.value,
            # SO number is editable by owner/manager at any non-terminal status (epic J).
            "can_edit_so": can_manage_plan and not terminal,
            "offers_by_requirement": offers_by_requirement,
            "plan_requirements": plan_requirements,
            # Most-urgent flag reason so the indicator states the issue at first glance.
            "top_flag": summarize_top_flag(bp.ai_flags),
            # Why the plan is silently stalled for lack of a configured approver (or None).
            "no_approver_reason": plan_needs_approver_reason(bp, db),
            # Live prepayment state per line (badge #11 + button→pill #10), one batch query.
            "prepay_state": prepayment_state_for_lines(db, [ln.id for ln in lines]),
        }
    )
    return template_response("htmx/partials/buy_plans/detail.html", ctx)


# ── Retired hub lens redirects (registered AFTER the {plan_id:int} detail route so a
#    numeric plan id is never captured by the {tab} converter; pipeline-archive is a
#    literal and precedes {tab} so it is not swallowed as an unknown lens). The hub's
#    My Queue + Pipeline bodies retired into the Approvals Workspace (spec §11.1;
#    docs/APPROVALS_PARITY_CHECKLIST.md) — stale pushed URLs 308 onto their workspace
#    equivalents, matching the repo's retired-route precedent (routers/requisitions2.py).


@router.get("/v2/partials/buy-plans/pipeline-archive")
async def pipeline_archive_partial(
    user: User = Depends(require_user),
) -> RedirectResponse:
    """Retired Done-archive pager — 308 to the workspace Closed list (BP tab)."""
    return RedirectResponse("/v2/partials/approvals/buy-plans/list?show_closed=true", status_code=308)


@router.get("/v2/partials/buy-plans/{tab}")
async def buy_plans_tab_partial(
    tab: str,
    scope: str = "",
    user: User = Depends(require_user),
) -> RedirectResponse:
    """Retired hub lens bodies (``my-queue`` / ``pipeline``) — 308 to the workspace.

    Both lenses map onto the workspace's Buy Plans tab body: My Queue's role-aware rows
    live in every tab's "Needs your approval" group; the Pipeline board's stage story
    lives in the work list + SO-pane kanban. ``scope`` threads through to seed the
    list's Mine/All toggle. Any other value 404s (same contract as before retirement).
    """
    if tab.replace("-", "_") not in ("my_queue", "pipeline"):
        raise HTTPException(404, "Unknown buy-plans lens")
    target = "/v2/partials/approvals/buy-plans"
    if scope:
        target += f"?scope={scope}"
    return RedirectResponse(target, status_code=308)


@router.post("/v2/partials/buy-plans/{plan_id}/submit", response_class=HTMLResponse)
async def buy_plan_submit_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Submit a draft buy plan with SO# — returns refreshed detail partial."""
    from ...services.buyplan_notifications import (
        notify_approved,
        notify_submitted,
        run_notify_bg,
    )
    from ...services.buyplan_workflow import submit_buy_plan

    # Per-record ownership: non-owner SALES/TRADER → 404 before any mutation.
    get_buyplan_for_user(db, user, plan_id)

    form = await request.form()
    so = form.get("sales_order_number", "").strip()
    if not so:
        raise HTTPException(400, "Sales Order # is required")

    try:
        plan = submit_buy_plan(
            plan_id,
            so,
            user,
            db,
            customer_po_number=form.get("customer_po_number") or None,
            salesperson_notes=form.get("salesperson_notes") or None,
        )
        db.commit()
        if plan.auto_approved:
            await run_notify_bg(notify_approved, plan.id)
        else:
            await run_notify_bg(notify_submitted, plan.id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/approve", response_class=HTMLResponse)
async def buy_plan_approve_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_buyplan_approver),
    db: Session = Depends(get_db),
):
    """Approve or reject a pending buy plan — returns refreshed detail.

    Gated by ``require_buyplan_approver`` (403 unless the user holds the per-user
    can_approve_buy_plans right). Reject requires a reason (enforced in the service).

    QP Phase C1: the approval engine OWNS the gate. We look up the open BUY_PLAN
    ApprovalRequest for this plan and resolve it via the engine's ``decide`` — which drives
    the buy-plan side effects (ACTIVE + buyer tasks / DRAFT) in the SAME transaction. We let
    ``decide`` raise (no swallowing) so a side-effect failure rolls back the whole decision
    atomically (RISK 1). If NO open request exists — a plan that went PENDING before C1
    deployed — we fall back to the legacy ``approve_buy_plan`` and log a WARNING (RISK 3,
    transition window; the fallback is removed in a follow-up once no pre-C1 plans remain).
    """
    from sqlalchemy import select as _select

    from ...constants import ApprovalRequestStatus, ApprovalSubjectType
    from ...models.approvals import ApprovalRequest
    from ...services.approvals.notifications import write_in_app
    from ...services.approvals.service import decide as svc_decide
    from ...services.buyplan_notifications import (
        notify_approved,
        notify_rejected,
        run_notify_bg,
    )
    from ...services.buyplan_workflow import approve_buy_plan
    from ...services.field_audit import edits_since, format_change_summary
    from ...services.workspace_notes import add_note

    form = await request.form()
    action = form.get("action", "approve")
    origin = form.get("origin", "")
    hub_scope = form.get("hub_scope", "all")
    notes = (form.get("notes") or "").strip() or None

    # Two-part approve (spec §7 / workspace 2.2): the workspace approval block posts a
    # handoff instead of a bare action — proceed → the existing approve path; send_back
    # → the existing reject→draft transition ("send back for sign-off"). The engine
    # requires a non-blank reject comment, so a blank send-back note auto-fills.
    handoff = (form.get("handoff") or "").strip()
    if handoff == "proceed":
        action = "approve"
    elif handoff == "send_back":
        action = "reject"
        if not notes:
            notes = SEND_BACK_DEFAULT_NOTE
    decision_tag = None
    if action == "reject":
        decision_tag = "sent_back" if handoff == "send_back" else "rejected"

    # The submitter is the fixer (change-summary recipient / note-to-fixer target);
    # capture submitted_at BEFORE the decision so the summary window can't move.
    bp = db.get(BuyPlan, plan_id)
    fixer_id = bp.submitted_by_id if bp is not None else None
    submitted_at = bp.submitted_at if bp is not None else None

    open_request = (
        db.execute(
            _select(ApprovalRequest).where(
                ApprovalRequest.subject_type == ApprovalSubjectType.BUY_PLAN,
                ApprovalRequest.subject_id == plan_id,
                ApprovalRequest.status == ApprovalRequestStatus.REQUESTED,
            )
        )
        .scalars()
        .first()
    )

    try:
        if open_request is not None:
            # Engine path: decide() resolves the request AND drives the plan side effects.
            svc_decide(db, open_request.id, user, action, comment=notes or None)
        else:
            # RISK 3 fallback: plan pending pre-C1 with no engine request yet.
            logger.warning(
                "Buy plan {} approve/reject with no open engine request — falling back to legacy approve_buy_plan",
                plan_id,
            )
            approve_buy_plan(plan_id, action, user, db, notes=notes)
        db.commit()
        if action == "approve":
            await run_notify_bg(notify_approved, plan_id)
        else:
            await run_notify_bg(notify_rejected, plan_id)
    except PermissionError as e:
        # The dependency already 403s unauthorized callers; this maps the service's
        # defense-in-depth approval-right check to 403 (not 400) if it is ever reached.
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    # Post-decision fan-out (2.2) — after the decision committed, never inside it:
    #   approve → the submitter gets the change summary (the audit log since
    #   submission, "was X → now Y"; skipped when nothing changed);
    #   reject / send-back → the note-to-the-fixer lands on the plan's notes thread
    #   tagged with the decision, and the submitter gets an in-app notification.
    if action == "approve" and fixer_id is not None:
        summary = format_change_summary(edits_since(db, buy_plan_id=plan_id, since=submitted_at))
        if summary:
            write_in_app(db, fixer_id, "buy_plan_changes", f"Buy plan #{plan_id} approved with changes", summary)
            db.commit()
    elif action == "reject" and notes:
        add_note(db, user=user, body=notes, buy_plan_id=plan_id, decision=decision_tag)
        if fixer_id is not None:
            title = (
                f"Buy plan #{plan_id} sent back for sign-off"
                if decision_tag == "sent_back"
                else f"Buy plan #{plan_id} rejected"
            )
            write_in_app(db, fixer_id, f"buy_plan_{decision_tag}", title, notes)
        db.commit()

    if origin == "approvals_workspace":
        # Workspace pane decide: re-render THIS plan's pane in place and nudge the
        # left work list to repaint (awListRefresh — the split shell's list container
        # listens for it), so the decided row leaves the Needs-your-approval group.
        from .approvals_hub import render_plan_pane

        resp = render_plan_pane(request, user, db, plan_id, lens=form.get("lens", "sales-orders"))
        resp.headers["HX-Trigger"] = "awListRefresh"
        return resp
    if origin == "approvals_hub":
        from .approvals_hub import render_tab_body

        return render_tab_body(request, user, db, "buy-plan", hub_scope)

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/halt", response_class=HTMLResponse)
async def buy_plan_halt_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Halt an in-flight buy plan — the standalone off-ramp (Phase D).

    Auth is enforced in the service (``halt_plan`` raises PermissionError unless the user
    is a supervisor/ops member → mapped to 403 here). Reuses ``notify_so_rejected`` with
    ``action="halt"`` so the salesperson still gets the halt + reason notification.
    """
    from ...services.buyplan_notifications import notify_so_rejected, run_notify_bg
    from ...services.buyplan_workflow import halt_plan

    form = await request.form()
    origin = form.get("origin", "")

    # A halt is an off-ramp on a money-governing deal — the reason is required so the case
    # report + salesperson notification always say WHY (stored on so_rejection_note; no column).
    reason = (form.get("reason") or "").strip()
    if not reason:
        raise HTTPException(400, "A reason is required to halt a buy plan.")

    try:
        plan = halt_plan(plan_id, user, db, reason=reason)
        db.commit()
        await run_notify_bg(notify_so_rejected, plan.id, action="halt")
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    if origin == "approvals_workspace":
        return _workspace_pane_response(request, user, db, plan_id, form)

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/lines/{line_id}/confirm-po", response_class=HTMLResponse)
async def buy_plan_confirm_po_partial(
    request: Request,
    plan_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Buyer confirms PO — returns the refreshed detail partial (or, from the Approvals
    Workspace, the refreshed PO pane).

    Workspace additions: ``payment_method`` (validated against
    ``PO_LINE_PAYMENT_METHODS`` in the service) records the Acctivate PO terms;
    ``qp_purchasing_*`` fields fold the QP-purchasing answers (incl. AS9120B) onto the
    line's vendor QP row via ``qp_workspace.apply_qp_purchasing`` — the applied diff is
    field-audited. ``origin=approvals_workspace`` re-renders the PO pane + refreshes
    the work list.
    """
    from ...services.buyplan_notifications import notify_po_confirmed, run_notify_bg
    from ...services.buyplan_workflow import confirm_po
    from ...services.field_audit import diff_fields, log_field_edits
    from ...services.qp_workspace import apply_qp_purchasing

    # Per-record ownership: non-owner SALES/TRADER → 404 before any mutation.
    plan = get_buyplan_for_user(db, user, plan_id)

    form = await request.form()
    po_number = form.get("po_number", "").strip()
    ship_date_str = form.get("estimated_ship_date", "")
    payment_method = (form.get("payment_method") or "").strip() or None
    origin = form.get("origin", "")

    if not po_number:
        raise HTTPException(400, "PO number is required")

    ship_date = None
    if ship_date_str:
        try:
            ship_date = datetime.fromisoformat(ship_date_str)
        except ValueError:
            ship_date = datetime.now()
    else:
        ship_date = datetime.now()

    # Stale-edit guard (2.1): the narrowest edited object is the LINE being confirmed.
    target_line = db.get(BuyPlanLine, line_id)
    if target_line is not None and target_line.buy_plan_id == plan_id:
        try:
            ensure_not_stale(target_line, form.get("expected_updated_at"))
        except StaleEditError:
            return stale_conflict_response()

    # COD contradicts a live prepayment (money is already committed up front) — reject
    # here at the route so prepayment_service stays untouched by the confirm-PO flow.
    if payment_method == PaymentMethod.COD.value:
        from ...constants import PrepaymentStatus
        from ...models.quality_plan import Prepayment

        live_prepayment = db.scalars(
            select(Prepayment.id).where(
                Prepayment.buy_plan_line_id == line_id,
                Prepayment.status.in_(
                    (
                        PrepaymentStatus.REQUESTED.value,
                        PrepaymentStatus.APPROVED.value,
                        PrepaymentStatus.PAID.value,
                    )
                ),
            )
        ).first()
        if live_prepayment is not None:
            raise HTTPException(
                400,
                "This line has a prepayment in progress — COD terms would contradict it. "
                "Pick the prepaid method, or void the prepayment first.",
            )

    qp_fields = {key[len("qp_") :]: value for key, value in form.multi_items() if key.startswith("qp_")}

    # Field-audit (2.1): diff the line's PO fields BEFORE confirm_po mutates them, then
    # merge with the QP-purchasing diff into ONE row per save.
    line_updates: dict = {"po_number": po_number, "estimated_ship_date": ship_date}
    if payment_method is not None:
        line_updates["payment_method"] = payment_method
    line_edits = diff_fields(target_line, line_updates) if target_line is not None else []

    try:
        line = confirm_po(plan_id, line_id, po_number, ship_date, user, db, payment_method=payment_method)
        edits = list(line_edits)
        if qp_fields:
            _qp, qp_edits = apply_qp_purchasing(db, plan=plan, line=line, user=user, fields=qp_fields)
            edits.extend(qp_edits)
        log_field_edits(db, user=user, buy_plan_id=plan_id, buy_plan_line_id=line_id, edits=edits)
        db.commit()
        await run_notify_bg(notify_po_confirmed, plan_id, line_id=line_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    if origin == "approvals_workspace":
        from .approvals_hub import render_po_pane

        resp = render_po_pane(request, user, db, line_id)
        resp.headers["HX-Trigger"] = "awListRefresh"
        return resp

    return await buy_plan_detail_partial(request, plan_id, user, db)


async def _resource_lines_and_alert(
    plan_id: int,
    line_id: int,
    reason_code: str,
    reason_note: str | None,
    also_line_ids: list[int],
    user: User,
    db: Session,
) -> dict:
    """Shared fall-down → re-source core (vendor-cancel, including the completed-plan
    backorder case).

    Pools the target line(s) via the single ``resource_line`` engine, commits, and fans out
    one URGENT backfill alert per pooled line. Returns the service payload; raises HTTP 400
    on a service ValueError (with a server-side log first).

    Backorder emergency: ``resource_line`` reports ``was_completed`` when it had to reopen an
    already-COMPLETED plan (a vendor cancelled AFTER the deal closed). That flag is threaded
    into every ``notify_resource_requested`` dispatch so the broadcast forces email + Teams DM
    to ALL recipients regardless of their re-source-alert preference, with a BACKORDER subject.
    It MUST be passed (not re-derived): by notification time the plan is already reopened to
    ACTIVE, so the completed-at-cancel-time fact would be lost.
    """
    from ...services.buyplan_notifications import notify_resource_requested, run_notify_bg
    from ...services.buyplan_workflow import resource_line

    try:
        payload = resource_line(plan_id, line_id, reason_code, reason_note, user, db, also_line_ids=also_line_ids)
        db.commit()
    except ValueError as e:
        # Log before re-raising so a real failure (e.g. an un-keyable requirement deep in
        # the service) leaves a server trace instead of a silent, mislabeled 400.
        logger.warning("Re-source failed for plan {} line {}: {}", plan_id, line_id, e)
        raise HTTPException(400, str(e)) from e

    # Broadcast one urgent alert PER pooled line (scope=plan re-sources siblings too, and
    # each pooled line needs its own claim).
    was_completed = payload.get("was_completed", False)
    for resourced in payload["resourced_lines"]:
        await run_notify_bg(
            notify_resource_requested,
            plan_id,
            line_id=resourced["line_id"],
            actor_id=user.id,
            reason=reason_code,
            was_completed=was_completed,
        )
    return payload


@router.post("/v2/partials/buy-plans/{plan_id}/lines/{line_id}/resource", response_class=HTMLResponse)
async def buy_plan_resource_line_partial(
    request: Request,
    plan_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Re-source a line whose vendor PO was cancelled (vendor-cancel fall-down).

    Records the cancellation (vendor performance), marks the offer sold + the vendor
    unavailable, drops the line into the open claim pool, and fires the URGENT backfill
    alert to all other buyers. ``scope=plan`` re-sources the plan's other cut lines too.
    Also the completed-plan BACKORDER entry point: when the target line sits on an
    already-COMPLETED plan (a vendor cancelled AFTER the deal closed), ``resource_line``
    reopens it to ACTIVE and the broadcast escalates to a forced EMERGENCY alert. ``origin``
    routes the re-render: ``approvals_workspace`` → the line's PO pane, ``approvals_hub``
    → the PO Approval tab body, else the full plan detail.
    """
    # Per-record ownership (non-owner SALES/TRADER → 404) + PO-cutter role gate (403).
    get_buyplan_for_user(db, user, plan_id)
    _require_po_cutter(user)

    form = await request.form()
    reason_code = form.get("reason_code", "").strip()
    reason_note = (form.get("reason_note") or "").strip() or None
    scope = form.get("scope", "line")
    also_line_ids = [int(i) for i in form.getlist("also_line_ids")] if scope == "plan" else []

    if not reason_code:
        raise HTTPException(400, "A re-source reason is required")

    origin = form.get("origin", "")
    hub_scope = form.get("hub_scope", "all")

    await _resource_lines_and_alert(plan_id, line_id, reason_code, reason_note, also_line_ids, user, db)

    if origin == "approvals_workspace":
        from .approvals_hub import render_po_pane

        resp = render_po_pane(request, user, db, line_id)
        resp.headers["HX-Trigger"] = "awListRefresh"
        return resp
    if origin == "approvals_hub":
        from .approvals_hub import render_tab_body

        return render_tab_body(request, user, db, "po-approval", hub_scope)

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/lines/{line_id}/claim", response_class=HTMLResponse)
async def buy_plan_claim_line_partial(
    request: Request,
    plan_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Claim an open-pool (RESOURCING) line. First-to-claim wins.

    No per-record ownership gate: the open pool is intentionally claimable by ANY active
    PO-cutter regardless of who owns the parent requisition. The lost race → 409.
    ``origin=approvals_workspace`` re-renders the claimed line's PO pane in place.
    """
    from ...services.buyplan_workflow import claim_line

    _require_po_cutter(user)

    form = await request.form()
    origin = form.get("origin", "")

    try:
        claim_line(plan_id, line_id, user, db)
        db.commit()
    except ValueError as e:
        logger.info("Claim lost/invalid for plan {} line {} by {}: {}", plan_id, line_id, user.id, e)
        raise HTTPException(409, str(e)) from e

    if origin == "approvals_workspace":
        from .approvals_hub import render_po_pane

        resp = render_po_pane(request, user, db, line_id)
        resp.headers["HX-Trigger"] = "awListRefresh"
        return resp

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/lines/{line_id}/verify-po", response_class=HTMLResponse)
async def buy_plan_verify_po_partial(
    request: Request,
    plan_id: int,
    line_id: int,
    user: User = Depends(require_buyplan_po_approver),
    db: Session = Depends(get_db),
):
    """Ops verifies PO — returns refreshed detail."""
    from ...services.buyplan_notifications import notify_po_rejected, run_notify_bg
    from ...services.buyplan_workflow import verify_po

    form = await request.form()
    action = form.get("action", "approve")
    origin = form.get("origin", "")
    hub_scope = form.get("hub_scope", "all")

    rejection_note = (form.get("rejection_note") or "").strip() or None
    try:
        line = verify_po(plan_id, line_id, action, user, db, rejection_note=rejection_note)
        # verify_po's own internal (approve-only) check_completion call already mutated
        # the SAME identity-mapped BuyPlan object `line.buy_plan` resolves to (verify_po
        # loaded it via db.get(BuyPlan, plan_id) itself) — reading .status off it here
        # is a free identity-map hit, NOT a second completion scan.
        just_completed = line.buy_plan is not None and line.buy_plan.status == BuyPlanStatus.COMPLETED.value
        buyer_id = line.buyer_id
        db.commit()
        if action == "reject":
            await run_notify_bg(notify_po_rejected, plan_id, line_id=line_id)
        await _notify_if_completed(plan_id, just_completed)
    except (ValueError, PermissionError) as e:
        raise HTTPException(400, str(e)) from e

    # PO send-back note-to-the-fixer (2.2): the manager's note lands on the LINE's
    # notes thread tagged sent_back, and the assigned buyer (the fixer) gets an
    # in-app notification. The note is optional on a send-back (spec §7).
    if action == "reject":
        from ...services.approvals.notifications import write_in_app
        from ...services.workspace_notes import add_note

        if rejection_note:
            add_note(
                db,
                user=user,
                body=rejection_note,
                buy_plan_id=plan_id,
                buy_plan_line_id=line_id,
                decision="sent_back",
            )
        if buyer_id is not None:
            write_in_app(
                db,
                buyer_id,
                "po_sent_back",
                f"PO sent back on plan #{plan_id}",
                rejection_note,
            )
        db.commit()

    if origin == "approvals_workspace":
        from .approvals_hub import render_po_pane

        resp = render_po_pane(request, user, db, line_id)
        resp.headers["HX-Trigger"] = "awListRefresh"
        return resp
    if origin == "approvals_hub":
        from .approvals_hub import render_tab_body

        return render_tab_body(request, user, db, "po-approval", hub_scope)

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/lines/{line_id}/receive", response_class=HTMLResponse)
async def buy_plan_receive_line_partial(
    request: Request,
    plan_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Manually mark a line's goods received (Approvals Workspace 3.2 — the kanban
    Received column's backing action).

    Plain ``require_user`` here — the actor gate (line buyer / manager / admin) and
    the state gate (verified, or the paid-risk prepay state) live service-side in
    ``mark_line_received``; idempotent (an already-received line is a no-op). Never
    touches plan status machinery. ``origin=approvals_workspace`` re-renders the
    workspace pane in place: with a ``lens`` the SO/BP pane (the kanban card's Mark
    received), without one the PO-line pane.
    """
    from ...services.buyplan_workflow import mark_line_received

    form = await request.form()
    origin = form.get("origin", "")

    try:
        mark_line_received(plan_id, line_id, user, db)
        db.commit()
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    if origin == "approvals_workspace":
        from .approvals_hub import render_plan_pane, render_po_pane

        lens = str(form.get("lens") or "")
        if lens:
            resp = render_plan_pane(request, user, db, plan_id, lens=lens)
        else:
            resp = render_po_pane(request, user, db, line_id)
        resp.headers["HX-Trigger"] = "awListRefresh"
        return resp

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/lines/{line_id}/issue", response_class=HTMLResponse)
async def buy_plan_flag_issue_partial(
    request: Request,
    plan_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Buyer flags issue on a line — returns refreshed detail."""
    from ...services.buyplan_workflow import flag_line_issue

    # Per-record ownership: non-owner SALES/TRADER → 404 before any mutation.
    get_buyplan_for_user(db, user, plan_id)

    form = await request.form()
    issue_type = form.get("issue_type", "other")
    note = form.get("note", "")

    try:
        flag_line_issue(plan_id, line_id, issue_type, user, db, note=note)
        db.commit()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/lines/{line_id}/resolve-issue", response_class=HTMLResponse)
async def buy_plan_resolve_issue_partial(
    request: Request,
    plan_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Supervisor clears a flagged issue → line back to awaiting_po.

    Returns refreshed detail.
    """
    from ...services.buyplan_workflow import resolve_line_issue

    # Per-record ownership: non-owner SALES/TRADER → 404 before any mutation.
    get_buyplan_for_user(db, user, plan_id)

    try:
        resolve_line_issue(plan_id, line_id, user, db)
        db.commit()
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/cancel", response_class=HTMLResponse)
async def buy_plan_cancel_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Cancel a buy plan — delegates to the service (line cascade + notification).

    Gated to the plan owner (salesperson) or a manager/admin (epic K): a non-owner restricted
    role 404s at ``get_buyplan_for_user``; a non-owner non-manager (e.g. a buyer on someone
    else's plan) 403s. The cancellation reason is REQUIRED (400 if blank).
    """
    from ...services.buyplan_notifications import notify_cancelled, run_notify_bg
    from ...services.buyplan_workflow import cancel_buy_plan

    # Per-record ownership: non-owner SALES/TRADER → 404 before any mutation.
    plan = get_buyplan_for_user(db, user, plan_id, options=[joinedload(BuyPlan.requisition)])
    if not (is_manager_or_admin(user) or (plan.requisition and plan.requisition.created_by == user.id)):
        raise HTTPException(403, "Only the plan owner or a manager can cancel this buy plan.")

    form = await request.form()
    reason = (form.get("reason") or "").strip()
    if not reason:
        raise HTTPException(400, "A reason is required to cancel a buy plan.")

    try:
        plan = cancel_buy_plan(plan_id, user, db, reason=reason)
        db.commit()
        await run_notify_bg(notify_cancelled, plan.id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    if form.get("origin") == "approvals_workspace":
        return _workspace_pane_response(request, user, db, plan_id, form)

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/resume", response_class=HTMLResponse)
async def buy_plan_resume_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Resume a HALTED plan back to ACTIVE — manager-only (epic K).

    Unlike Reset (which returns to DRAFT and nulls the halt audit), Resume preserves
    ``halted_by/at`` as the halt→resume history. The service raises PermissionError for a
    non-manager (→ 403) and ValueError for a non-halted plan (→ 400).
    """
    from ...services.buyplan_workflow import resume_plan

    # Per-record ownership: non-owner SALES/TRADER → 404 before any mutation.
    get_buyplan_for_user(db, user, plan_id)

    try:
        resume_plan(plan_id, user, db)
        db.commit()
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    form = await request.form()
    if form.get("origin") == "approvals_workspace":
        return _workspace_pane_response(request, user, db, plan_id, form)

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/so-number", response_class=HTMLResponse)
async def buy_plan_set_so_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Set/edit the plan's active Sales Order number (epic J).

    Owner (salesperson) or manager, at any non-terminal status. A non-owner restricted
    role 404s; a non-owner non-manager 403s; a terminal plan 400s (service ValueError).
    """
    from ...services.buyplan_workflow import set_sales_order_number

    plan = get_buyplan_for_user(db, user, plan_id, options=[joinedload(BuyPlan.requisition)])
    if not (is_manager_or_admin(user) or (plan.requisition and plan.requisition.created_by == user.id)):
        raise HTTPException(403, "Only the plan owner or a manager can edit the Sales Order number.")

    form = await request.form()
    # Stale-edit guard (2.1): the narrowest edited object is the PLAN (SO# lives on it).
    try:
        ensure_not_stale(plan, form.get("expected_updated_at"))
    except StaleEditError:
        return stale_conflict_response()
    try:
        set_sales_order_number(plan_id, form.get("sales_order_number"), user, db)
        db.commit()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/lines/add", response_class=HTMLResponse)
async def buy_plan_add_line_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add a line (vendor offer + qty + sell) to an editable plan (epic I).

    Role×status gate is enforced in the service (PermissionError → 403). Bad input (non-
    numeric / missing offer / wrong requisition) → 400.
    """
    from ...services.buyplan_workflow import add_buy_plan_line

    # Per-record ownership: non-owner SALES/TRADER → 404 before any mutation. Same
    # loader options as add_buy_plan_line's own db.get() so the ownership pre-check's
    # load isn't silently wasted — a bare Session.get() on a PK already in the identity
    # map does NOT retroactively apply new loader options, so without this the service's
    # joinedload(BuyPlan.lines)/joinedload(BuyPlan.requisition) would do nothing and
    # plan.lines/plan.requisition would lazy-load one row at a time instead.
    plan = get_buyplan_for_user(db, user, plan_id, options=[joinedload(BuyPlan.lines), joinedload(BuyPlan.requisition)])

    form = await request.form()
    # Stale-edit guard (2.1): a new line's narrowest EXISTING object is the plan.
    try:
        ensure_not_stale(plan, form.get("expected_updated_at"))
    except StaleEditError:
        return stale_conflict_response()
    try:
        requirement_id = int(form.get("requirement_id") or 0)
        offer_id = int(form.get("offer_id") or 0)
        quantity = int(form.get("quantity") or 0)
    except (TypeError, ValueError) as e:
        raise HTTPException(400, "Requirement, vendor offer and a whole-number quantity are required.") from e
    unit_sell = _parse_optional_float(form.get("unit_sell"))

    try:
        add_buy_plan_line(plan_id, requirement_id, offer_id, quantity, user, db, unit_sell=unit_sell)
        db.commit()
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/lines/{line_id}/edit", response_class=HTMLResponse)
async def buy_plan_edit_line_partial(
    request: Request,
    plan_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Edit a line's qty / sell price / vendor(offer) on an editable plan (epic I).

    Only the submitted fields change (blank = unchanged). Role×status gate in the
    service (PermissionError → 403); a cut-PO vendor/qty change or bad input → 400.
    """
    from ...services.buyplan_workflow import edit_buy_plan_line

    # Per-record ownership: non-owner SALES/TRADER → 404 before any mutation. Matches
    # edit_buy_plan_line's own loader options (see buy_plan_add_line_partial for why).
    plan = get_buyplan_for_user(db, user, plan_id, options=[joinedload(BuyPlan.lines), joinedload(BuyPlan.requisition)])

    form = await request.form()
    # Stale-edit guard (2.1): the narrowest edited object is the LINE.
    target_line = next((ln for ln in (plan.lines or []) if ln.id == line_id), None)
    if target_line is not None:
        try:
            ensure_not_stale(target_line, form.get("expected_updated_at"))
        except StaleEditError:
            return stale_conflict_response()
    quantity = _parse_optional_int(form.get("quantity"))
    unit_sell = _parse_optional_float(form.get("unit_sell"))
    offer_id = _parse_optional_int(form.get("offer_id"))
    # Manager edit-anything-at-verify fields (2.3) — the service refuses them for
    # anyone but a manager/admin on a PENDING_VERIFY line. po_number keeps the
    # present-vs-absent distinction: the field ABSENT is a no-op (None → _UNSET in the
    # service), while present-but-EMPTY is an explicit clear of an erroneous number
    # (audited old→"" by the service; empty-on-empty stays a no-op).
    po_number_raw = form.get("po_number")
    po_number = str(po_number_raw).strip() if po_number_raw is not None else None
    unit_cost = _parse_optional_float(form.get("unit_cost"))
    ship_date_str = (form.get("estimated_ship_date") or "").strip()
    estimated_ship_date = None
    if ship_date_str:
        try:
            estimated_ship_date = datetime.fromisoformat(ship_date_str)
        except ValueError as e:
            raise HTTPException(400, "Expected an ISO date for the estimated ship date.") from e

    try:
        edit_buy_plan_line(
            plan_id,
            line_id,
            user,
            db,
            quantity=quantity,
            unit_sell=unit_sell,
            offer_id=offer_id,
            po_number=po_number,
            estimated_ship_date=estimated_ship_date,
            unit_cost=unit_cost,
        )
        db.commit()
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    if form.get("origin") == "approvals_workspace":
        from .approvals_hub import render_po_pane

        resp = render_po_pane(request, user, db, line_id)
        resp.headers["HX-Trigger"] = "awListRefresh"
        return resp

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/lines/{line_id}/remove", response_class=HTMLResponse)
async def buy_plan_remove_line_partial(
    request: Request,
    plan_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Remove a line from an editable plan (epic I).

    Role×status gate in the service (PermissionError → 403); removing a cut-PO line →
    400. ``remove_buy_plan_line`` already auto-completes at service depth (removing the
    plan's last open line can leave every remaining line terminal); the returned plan's
    ``.status`` is read BEFORE commit to drive ``_notify_if_completed`` without re-
    deriving the fact via a second ``check_completion`` scan.
    """
    from ...services.buyplan_workflow import remove_buy_plan_line

    # Per-record ownership: non-owner SALES/TRADER → 404 before any mutation. Matches
    # remove_buy_plan_line's own loader options (see buy_plan_add_line_partial).
    plan = get_buyplan_for_user(db, user, plan_id, options=[joinedload(BuyPlan.lines), joinedload(BuyPlan.requisition)])

    # Stale-edit guard (2.1): the narrowest edited object is the LINE being removed.
    form = await request.form()
    target_line = next((ln for ln in (plan.lines or []) if ln.id == line_id), None)
    if target_line is not None:
        try:
            ensure_not_stale(target_line, form.get("expected_updated_at"))
        except StaleEditError:
            return stale_conflict_response()

    try:
        updated = remove_buy_plan_line(plan_id, line_id, user, db)
        just_completed = updated.status == BuyPlanStatus.COMPLETED.value
        db.commit()
        await _notify_if_completed(plan_id, just_completed)
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/lines/bulk", response_class=HTMLResponse)
async def buy_plan_bulk_lines_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save the entire plan's lines (edited qty/sell/vendor, added lines, removed lines)
    in one POST (epic I "save all").

    Form field ``payload`` is a JSON object ``{"lines": [...], "known_line_ids": [...]}``
    (the Alpine editor posts it via an htmx ``hx-vals`` JSON blob). ``known_line_ids``
    (optional; a list of ints) is every line id the client's form actually rendered —
    it scopes removal-by-omission so a line added by someone else after the form loaded
    is left untouched instead of silently deleted; omitted entirely falls back to the
    legacy (unscoped) removal-by-omission behavior. Role×status gate and per-line rules
    are enforced in the service (PermissionError → 403); malformed JSON, a bad shape, or
    bad line data → 400. ``known_line_ids`` element-level validation (whole numbers,
    bools rejected) lives in the SERVICE now (``bulk_edit_buy_plan_lines`` owns the
    contract) — this route only checks the outer shape (a list, if present).
    ``bulk_edit_buy_plan_lines`` already auto-completes at service depth (removing the
    last open line can leave every remaining line terminal); the returned plan's
    ``.status`` is read BEFORE commit to drive ``_notify_if_completed`` without re-
    deriving the fact via a second ``check_completion`` scan.
    """
    from ...services.buyplan_workflow import bulk_edit_buy_plan_lines

    # Per-record ownership: non-owner SALES/TRADER → 404 before any mutation. Matches
    # bulk_edit_buy_plan_lines's own loader options (see buy_plan_add_line_partial).
    plan = get_buyplan_for_user(db, user, plan_id, options=[joinedload(BuyPlan.lines), joinedload(BuyPlan.requisition)])

    form = await request.form()
    # Stale-edit guard (2.1): a whole-plan save's narrowest object is the PLAN.
    try:
        ensure_not_stale(plan, form.get("expected_updated_at"))
    except StaleEditError:
        return stale_conflict_response()
    raw_payload = form.get("payload")
    try:
        parsed = json.loads(str(raw_payload))
    except (TypeError, ValueError) as e:
        raise HTTPException(400, "Malformed lines payload — expected JSON.") from e

    if not isinstance(parsed, dict) or not isinstance(parsed.get("lines"), list):
        raise HTTPException(400, 'Lines payload must be a JSON object shaped {"lines": [...]}.')

    known_line_ids = parsed.get("known_line_ids")
    if known_line_ids is not None and not isinstance(known_line_ids, list):
        raise HTTPException(400, "known_line_ids must be a list of whole-number line ids.")

    try:
        updated = bulk_edit_buy_plan_lines(plan_id, parsed["lines"], user, db, known_line_ids=known_line_ids)
        just_completed = updated.status == BuyPlanStatus.COMPLETED.value
        db.commit()
        await _notify_if_completed(plan_id, just_completed)
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/reset", response_class=HTMLResponse)
async def buy_plan_reset_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Reset halted/cancelled plan to draft — returns refreshed detail."""
    from ...services.buyplan_workflow import reset_buy_plan_to_draft

    # Per-record ownership: non-owner SALES/TRADER → 404 before any mutation.
    get_buyplan_for_user(db, user, plan_id)

    try:
        reset_buy_plan_to_draft(plan_id, user, db)
        db.commit()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    form = await request.form()
    if form.get("origin") == "approvals_workspace":
        return _workspace_pane_response(request, user, db, plan_id, form)

    return await buy_plan_detail_partial(request, plan_id, user, db)
