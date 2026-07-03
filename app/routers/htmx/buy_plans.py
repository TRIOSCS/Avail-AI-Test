"""routers/htmx/buy_plans.py — Buy Plans / Approvals partial views (HTMX + Alpine).

Server-rendered HTML partials for the Approvals (Buy Plans) hub: the two-tab lens
shell (My Queue + Pipeline), sales-order new/create, buy-plan detail, and per-plan
lifecycle actions (submit, approve, halt, confirm-po, resource, claim, verify-po,
issue, cancel, reset). Plus the legacy /v2/buy-plans full-page redirect.

Called by: app/main.py (router mount).
Depends on: app.models, app.dependencies, app.database, app.services.approvals,
    ._shared (imports _is_ops_member shared with a staying quotes route).
"""

import json
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ...constants import (
    RESTRICTED_ROLES,
    AccessKey,
    BuyPlanStatus,
    UserRole,
)
from ...database import get_db
from ...dependencies import (
    get_buyplan_for_user,
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


def _can_see_all_deals(user: User, db: Session) -> bool:
    """True when the user may view every owner's deals on the Deal Hub board.

    PO-cutters (buyers + managers/admins) and ops verification-group members see the
    full deal flow; sales/traders are scoped to their own deals only. Broader than
    ``_can_supervise`` by including buyers, who need cross-owner visibility to cut POs.
    """
    return _can_resource(user) or _is_ops_member(user, db)


def _resolve_deal_scope(scope: str, can_see_all: bool) -> str:
    """Normalize a requested deal scope against the user's visibility.

    Empty/unknown → the role default (``all`` for can-see-all users, else ``mine``).
    ``all`` requested by a user without cross-owner visibility is forced to ``mine`` so
    no other rep's plans leak.
    """
    if scope not in ("mine", "all"):
        return "all" if can_see_all else "mine"
    if scope == "all" and not can_see_all:
        return "mine"
    return scope


def _require_po_cutter(user: User) -> None:
    """403 unless the user is an active PO-cutter (buyer/manager/admin)."""
    if not _can_resource(user) or not getattr(user, "is_active", True):
        raise HTTPException(403, "Only buyers and managers can re-source / claim lines")


_APPROVALS_TABS = ("my_queue", "pipeline")


def _default_lens(user: User, db: Session) -> str:
    """Pick the landing stage tab for the Approvals hub based on the user's role.

    - managers/admins/ops land on Pipeline — the 4-stage deal board (Phase C),
    - everyone else (buyers, sales, traders) lands on My Queue — their personal,
      role-aware "what needs YOU now" surface.
    """
    if _can_supervise(user, db):
        return "pipeline"
    return "my_queue"


@router.get("/v2/partials/buy-plans", response_class=HTMLResponse)
async def buy_plans_list_partial(
    request: Request,
    lens: str = "",
    user: User = Depends(require_access(AccessKey.BUY_PLANS)),
    db: Session = Depends(get_db),
):
    """Return the Buy Plans hub shell (My Queue + Pipeline tab switcher).

    The shell renders the two lens tabs + a lazy body that loads the active tab partial
    into ``#bp-hub-body``. Row data is fetched by the body, not here. This is the personal
    "what needs YOU" hub at /v2/buy-plans; the org-wide 3-tab decide console lives
    separately at /v2/approvals (routers/htmx/approvals_hub.py).
    """
    active_lens = lens if lens in _APPROVALS_TABS else _default_lens(user, db)

    # Spotlight markers: plan rows that carry an open step needing this user's action.
    # Buy Plans is its own primary nav tab, so the source is registered under "buy-plans".
    from ...services.alerts import markers_for_tab

    alert_markers = markers_for_tab(db, user, "buy-plans")

    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update(
        {
            "lens": active_lens,
            "alert_markers": alert_markers,
        }
    )
    return template_response("htmx/partials/buy_plans/hub.html", ctx)


@router.get("/v2/partials/buy-plans/sales-orders/new", response_class=HTMLResponse)
async def sales_order_new(
    request: Request,
    requisition_id: int | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """New Sales Order origination surface (requisition picker → offer/sell builder).

    Origination is deal CREATION, not a decide action — it lives under the Buy Plans hub
    prefix (/v2/partials/buy-plans/*), NOT the Approvals decide prefix. The two-segment
    ``sales-orders/new`` path does not collide with the ``{plan_id:int}`` detail route or
    the one-segment ``{tab}`` hub-lens converter. With no ``requisition_id`` it lists open
    (OPEN_PIPELINE) requisitions that carry at least one ACTIVE offer, scoped to what the
    user may see. With ``requisition_id`` it loads that requisition's per-requirement
    offer/sell-price form (``get_builder_data`` + ``apply_smart_defaults``), enforcing access
    via ``get_req_for_user`` (404 for a restricted role that does not own it).
    """
    from sqlalchemy import func

    from ...constants import OfferStatus, RequisitionStatus
    from ...dependencies import get_req_for_user
    from ...models import Offer, Requirement
    from ...services.quote_builder_service import apply_smart_defaults, get_builder_data

    ctx = _base_ctx(request, user, "buy-plans")

    if requisition_id is not None:
        req = get_req_for_user(db, user, requisition_id)
        lines = get_builder_data(req.id, db)
        apply_smart_defaults(lines)
        ctx.update({"selected_req": req, "lines": lines})
        return template_response("htmx/partials/approvals/_sales_order_new.html", ctx)

    # Picker mode: open requisitions with at least one active offer, scoped to the viewer.
    has_active_offer = (
        select(Offer.id)
        .join(Requirement, Offer.requirement_id == Requirement.id)
        .where(
            Requirement.requisition_id == Requisition.id,
            Offer.status == OfferStatus.ACTIVE,
        )
        .exists()
    )
    q = db.query(Requisition).filter(
        Requisition.status.in_(list(RequisitionStatus.OPEN_PIPELINE)),
        has_active_offer,
    )
    if user.role in RESTRICTED_ROLES:
        q = q.filter(Requisition.created_by == user.id)
    reqs = q.order_by(Requisition.id.desc()).all()

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
    """Originate a DRAFT buy plan (Sales Order) from the chosen offers, then render its
    detail.

    Parses ``requisition_id`` + per-requirement ``offer_<rid>`` / ``sell_<rid>`` form fields,
    enforces requisition access (``require_requisition_access`` — 404 for a restricted role
    that does not own it), and calls ``create_sales_order_from_offers``. On the builder's
    duplicate-open-SO ValueError it renders the existing open Sales Order's detail with a
    toast (never a 500); any other ValueError (e.g. no requirements) is a 400.
    """
    from ...dependencies import require_requisition_access
    from ...services.buyplan_builder import (
        DuplicateSalesOrderError,
        create_sales_order_from_offers,
    )

    form = await request.form()
    raw_req_id = form.get("requisition_id")
    if not raw_req_id:
        raise HTTPException(400, "Requisition is required")
    try:
        req_id = int(raw_req_id)
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid requisition")

    require_requisition_access(db, req_id, user)

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
        plan = create_sales_order_from_offers(req_id, selections, sell_prices, db, user)
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
    except ValueError:
        # Any other origination failure (e.g. requisition has no requirements). Return a
        # curated client message rather than echoing the raw builder error.
        raise HTTPException(400, "Could not build a buy plan from the selected offers.")

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
    then re-renders the caller's surface by ``origin``: ``my_queue`` → the My Queue body
    (``#bp-hub-body``); ``approvals_hub`` → the Approvals hub Prepayment tab
    (``#ap-hub-body``). Reject requires a non-blank comment (400 otherwise); a caller who
    holds no PENDING recipient slot is 403 (engine PermissionError); a stale/decided request
    is 400 (engine ValueError).
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
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))

    # Notify accounting/AP that the wire is authorized — OK TO WIRE. ONLY on approve (never
    # on reject). Fire-and-forget: the runner isolates every error so a failed notice never
    # breaks the approval that just committed.
    if action == "approve" and ar.subject_id is not None:
        from ...services.prepayment_notifications import (
            notify_prepayment_approved,
            run_prepayment_notify_bg,
        )

        await run_prepayment_notify_bg(notify_prepayment_approved, ar.subject_id)

    if origin == "approvals_hub":
        from .approvals_hub import render_tab_body

        return render_tab_body(request, user, db, "prepayment", hub_scope)
    return _render_my_queue_body(request, user, db)


def _render_my_queue_body(request: Request, user: User, db: Session) -> HTMLResponse:
    """Build + render the My Queue surface body for ``user`` into ``#bp-hub-body``.

    Shared by the ``my_queue`` lens dispatch and the my-queue-origin inline action returns
    (approve / verify-po), so a one-click action re-renders the refreshed queue in place
    rather than swapping in the single-plan detail. ``my_queue`` is already fully role-aware
    (it gates which kinds it emits by the viewer's rights / role / ownership), so no extra
    gating is needed here — Jinja consumes only the resolved ``QueueRow`` list.
    """
    from ...services.buyplan_hub import my_queue, open_avg_margin

    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update(
        {
            "queue": my_queue(db, user),
            "avg_margin": open_avg_margin(db),
            "user": user,
            # PO-cutter gate for the po_verify row's third (Cancel → re-source) action.
            "can_resource": _can_resource(user),
        }
    )
    return template_response("htmx/partials/approvals/_surface_my_queue.html", ctx)


def _render_pipeline_body(request: Request, user: User, db: Session, scope: str = "") -> HTMLResponse:
    """Build + render the Pipeline surface body for ``user`` into ``#bp-hub-body``.

    The Pipeline is the deal flow as cards in the four canonical stages: three visible
    columns Build (DRAFT) · Approve (PENDING) · Purchase (ACTIVE), plus a collapsed
    Done (COMPLETED) summary below. Each column is one ``deals_board`` call with an explicit
    status filter so the read model stays the single source of truth (see Phase B / the
    rework design's "Two surfaces via lens values"). Done comes from ``completed_archive``.

    Scope is role-resolved exactly like the standalone board: PO-cutters + ops may toggle
    All/Mine; sales/traders are locked to ``mine`` so no other rep's deals leak. The Mine/All
    toggle reloads THIS body in place (hx-target #bp-hub-body, hx-push-url="false").
    """
    from ...services.buyplan_hub import completed_archive, deals_board, open_avg_margin

    can_all = _can_see_all_deals(user, db)
    board_scope = _resolve_deal_scope(scope, can_all)

    build = deals_board(db, user, scope=board_scope, statuses=[BuyPlanStatus.DRAFT.value])
    approve = deals_board(db, user, scope=board_scope, statuses=[BuyPlanStatus.PENDING.value])
    purchase = deals_board(db, user, scope=board_scope, statuses=[BuyPlanStatus.ACTIVE.value])
    # HALTED is the off-ramp — buyers regain its visibility via a dedicated Halted column
    # (rework parity). HALTED maps to the "active" bucket in _STATUS_TO_COLUMN. The column
    # only renders for can_see_all_deals viewers (see the surface template).
    halted = deals_board(db, user, scope=board_scope, statuses=[BuyPlanStatus.HALTED.value])

    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update(
        {
            "build_col": build["draft"],
            "approve_col": approve["pending"],
            "purchase_col": purchase["active"],
            "halted_col": halted["active"],
            "archive": completed_archive(db, user, scope=board_scope),
            "scope": board_scope,
            "can_see_all_deals": can_all,
            "avg_margin": open_avg_margin(db),
            "user": user,
        }
    )
    return template_response("htmx/partials/approvals/_surface_pipeline.html", ctx)


@router.get("/v2/partials/buy-plans/{plan_id:int}", response_class=HTMLResponse)
async def buy_plan_detail_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return buy plan detail as HTML partial.

    The ``{plan_id:int}`` path convertor is load-bearing: it makes this route match ONLY
    integer segments, so the sibling hub-lens route ``/v2/partials/buy-plans/{tab}`` (str)
    and the literal ``/pipeline-archive`` never shadow a numeric plan id and vice-versa.
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

    from ...services.buyplan_workflow import plan_needs_approver_reason
    from ...services.prepayment_service import prepayment_state_for_lines

    lines = bp.lines or []
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
            # Most-urgent flag reason so the indicator states the issue at first glance.
            "top_flag": summarize_top_flag(bp.ai_flags),
            # Why the plan is silently stalled for lack of a configured approver (or None).
            "no_approver_reason": plan_needs_approver_reason(bp, db),
            # Live prepayment state per line (badge #11 + button→pill #10), one batch query.
            "prepay_state": prepayment_state_for_lines(db, [ln.id for ln in lines]),
        }
    )
    return template_response("htmx/partials/buy_plans/detail.html", ctx)


# ── Hub lens bodies (registered AFTER the {plan_id:int} detail route so a numeric plan id
#    is never captured by the {tab} converter; pipeline-archive is a literal and precedes
#    {tab} so it is not swallowed as an unknown lens). ──────────────────────────────────


@router.get("/v2/partials/buy-plans/pipeline-archive", response_class=HTMLResponse)
async def pipeline_archive_partial(
    request: Request,
    scope: str = "",
    offset: int = 0,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Lazy "load older" page of the Pipeline's Done (completed) deals.

    The Pipeline surface renders its Done cards via the shared archive-rows partial; this
    returns the next page of those cards (newest-completed first) so a "Load older" click
    appends in place. Scope is role-resolved exactly like the board so no other rep's
    completed deals leak. MUST be registered BEFORE the one-segment ``{tab}`` lens route
    below, or FastAPI routes "pipeline-archive" there as an unknown tab (404).
    """
    from ...services.buyplan_hub import completed_archive

    scope = _resolve_deal_scope(scope, _can_see_all_deals(user, db))

    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update(
        {
            "archive": completed_archive(db, user, scope=scope, offset=offset),
            "scope": scope,
        }
    )
    return template_response("htmx/partials/approvals/_pipeline_archive_rows.html", ctx)


@router.get("/v2/partials/buy-plans/{tab}", response_class=HTMLResponse)
async def buy_plans_tab_partial(
    request: Request,
    tab: str,
    scope: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render one Buy Plans hub lens body into ``#bp-hub-body``.

    Two lenses survive the Phase F retirement: ``my_queue`` (the role-aware "what needs YOU
    now" surface) and ``pipeline`` (the 4-stage deal board). ``tab`` arrives dash-cased
    (e.g. my-queue) and maps to the underscored lens key; any other value 404s.

    ``scope`` applies to the Pipeline board only: it is role-resolved (sales/traders locked
    to ``mine``), and its All/Mine toggle reloads THIS whole body in place.
    """
    lens = tab.replace("-", "_")
    if lens not in _APPROVALS_TABS:
        raise HTTPException(404, "Unknown buy-plans lens")

    if lens == "my_queue":
        return _render_my_queue_body(request, user, db)

    return _render_pipeline_body(request, user, db, scope)


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
        raise HTTPException(400, str(e))

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
    from ...services.approvals.service import decide as svc_decide
    from ...services.buyplan_notifications import (
        notify_approved,
        notify_rejected,
        run_notify_bg,
    )
    from ...services.buyplan_workflow import approve_buy_plan

    form = await request.form()
    action = form.get("action", "approve")
    origin = form.get("origin", "")
    hub_scope = form.get("hub_scope", "all")
    notes = form.get("notes")

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
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))

    if origin == "my_queue":
        return _render_my_queue_body(request, user, db)
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

    try:
        plan = halt_plan(plan_id, user, db, reason=form.get("reason"))
        db.commit()
        await run_notify_bg(notify_so_rejected, plan.id, action="halt")
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))

    if origin == "my_queue":
        return _render_my_queue_body(request, user, db)

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/lines/{line_id}/confirm-po", response_class=HTMLResponse)
async def buy_plan_confirm_po_partial(
    request: Request,
    plan_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Buyer confirms PO — returns the refreshed detail partial."""
    from ...services.buyplan_notifications import notify_po_confirmed, run_notify_bg
    from ...services.buyplan_workflow import confirm_po

    # Per-record ownership: non-owner SALES/TRADER → 404 before any mutation.
    get_buyplan_for_user(db, user, plan_id)

    form = await request.form()
    po_number = form.get("po_number", "").strip()
    ship_date_str = form.get("estimated_ship_date", "")

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

    try:
        confirm_po(plan_id, line_id, po_number, ship_date, user, db)
        db.commit()
        await run_notify_bg(notify_po_confirmed, plan_id, line_id=line_id)
    except ValueError as e:
        raise HTTPException(400, str(e))

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
        raise HTTPException(400, str(e))

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
    routes the re-render: ``my_queue`` → My Queue body, ``approvals_hub`` → the PO Approval
    tab body, else the full plan detail.
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

    if origin == "my_queue":
        return _render_my_queue_body(request, user, db)
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
    """
    from ...services.buyplan_workflow import claim_line

    _require_po_cutter(user)

    try:
        claim_line(plan_id, line_id, user, db)
        db.commit()
    except ValueError as e:
        logger.info("Claim lost/invalid for plan {} line {} by {}: {}", plan_id, line_id, user.id, e)
        raise HTTPException(409, str(e))

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
    from ...services.buyplan_notifications import (
        notify_completed,
        notify_po_rejected,
        run_notify_bg,
    )
    from ...services.buyplan_workflow import check_completion, verify_po

    form = await request.form()
    action = form.get("action", "approve")
    origin = form.get("origin", "")
    hub_scope = form.get("hub_scope", "all")

    try:
        verify_po(plan_id, line_id, action, user, db, rejection_note=form.get("rejection_note"))
        db.commit()
        if action == "reject":
            await run_notify_bg(notify_po_rejected, plan_id, line_id=line_id)
        updated = check_completion(plan_id, db)
        if updated and updated.status == BuyPlanStatus.COMPLETED:
            db.commit()
            await run_notify_bg(notify_completed, plan_id)
    except (ValueError, PermissionError) as e:
        raise HTTPException(400, str(e))

    if origin == "my_queue":
        return _render_my_queue_body(request, user, db)
    if origin == "approvals_hub":
        from .approvals_hub import render_tab_body

        return render_tab_body(request, user, db, "po-approval", hub_scope)

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
        raise HTTPException(400, str(e))

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
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/cancel", response_class=HTMLResponse)
async def buy_plan_cancel_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Cancel a buy plan — delegates to the service (line cascade + notification)."""
    from ...services.buyplan_notifications import notify_cancelled, run_notify_bg
    from ...services.buyplan_workflow import cancel_buy_plan

    # Per-record ownership: non-owner SALES/TRADER → 404 before any mutation.
    get_buyplan_for_user(db, user, plan_id)

    form = await request.form()
    try:
        plan = cancel_buy_plan(plan_id, user, db, reason=form.get("reason"))
        db.commit()
        await run_notify_bg(notify_cancelled, plan.id)
    except ValueError as e:
        raise HTTPException(400, str(e))

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
        raise HTTPException(400, str(e))

    return await buy_plan_detail_partial(request, plan_id, user, db)
