"""routers/htmx/buy_plans.py — Buy Plans / Approvals partial views (HTMX + Alpine).

Server-rendered HTML partials for the Approvals (Buy Plans) hub: the stage-tab
lens shell, sales-order new/create, the resource/orders/board/archive/supervise
boards, and per-plan lifecycle actions (submit, approve, verify-so, confirm-po,
resource, claim, verify-po, issue, cancel, reset). Plus the legacy /v2/buy-plans
full-page redirect. Extracted verbatim from htmx_views.py (same `/v2` paths, same
`htmx-views` tag).

Called by: app/main.py (router mount).
Depends on: app.models, app.dependencies, app.database, app.services.approvals,
    ._shared (imports _is_ops_member shared with a staying quotes route).
"""

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
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
    can_approve_buy_plans,
    get_buyplan_for_user,
    require_access,
    require_buyplan_approver,
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


@router.get("/v2/buy-plans", response_class=HTMLResponse)
async def buy_plans_legacy_redirect(request: Request):
    """302 the legacy Buy Plans URL to the renamed Approvals module (query preserved).

    The hub was renamed Buy Plans → Approvals (SP-1); old bookmarks / pushed lens URLs
    keep working via this redirect. Detail URLs (/v2/buy-plans/{id}) are unchanged and
    still served directly by ``v2_page``.
    """
    from fastapi.responses import RedirectResponse

    qs = request.url.query
    return RedirectResponse(f"/v2/approvals?{qs}" if qs else "/v2/approvals", status_code=302)


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


_APPROVALS_TABS = ("sales_orders", "buy_plans", "purchase_orders", "prepayments", "supervise")


_TAB_APPROVE_ATTR = {
    "sales_orders": "can_approve_buy_plans",
    "purchase_orders": "can_approve_pos",
    "prepayments": "can_approve_prepayments",
}


def _default_lens(user: User, db: Session) -> str:
    """Pick the landing stage tab for the Approvals hub based on the user's role.

    - buyers land on the Purchase Orders stage (their PO cut queue),
    - managers/admins/ops land on Supervise,
    - everyone else (sales/trader) lands on the Buy Plans deal board.
    """
    if user.role == UserRole.BUYER:
        return "purchase_orders"
    if _can_supervise(user, db):
        return "supervise"
    return "buy_plans"


@router.get("/v2/partials/approvals", response_class=HTMLResponse)
@router.get("/v2/partials/buy-plans", response_class=HTMLResponse)
async def buy_plans_list_partial(
    request: Request,
    lens: str = "",
    user: User = Depends(require_access(AccessKey.BUY_PLANS)),
    db: Session = Depends(get_db),
):
    """Return the Approvals hub shell (stage-tab switcher).

    The shell renders the five lifecycle stage tabs + a lazy body that loads the active
    stage tab partial into ``#bp-hub-body``. Row data is fetched by the body, not here.
    ``/v2/partials/buy-plans`` is kept as a back-compat alias for in-flight htmx.
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
            # Only Supervise is gate-rendered in the shell; the four stage tabs are always
            # shown (their work surface + pinned approval section gate by role inside).
            "can_supervise": _can_supervise(user, db),
        }
    )
    return template_response("htmx/partials/buy_plans/hub.html", ctx)


@router.get("/v2/partials/approvals/{tab}", response_class=HTMLResponse)
async def approvals_tab_partial(
    request: Request,
    tab: str,
    scope: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render one Approvals stage-tab body into ``#bp-hub-body``.

    Composes the re-homed work surface for the stage (deal board / buyer orders +
    re-sourcing pool / neutral empty state) with a pinned per-gate "Pending approvals"
    section (services.approvals.queue.build_queue_view), shown only when the viewer holds
    that gate's approve right. ``supervise`` reuses the manager triage body. ``tab`` arrives
    dash-cased (e.g. purchase-orders) and maps to the underscored stage key.

    ``scope`` applies to the Buy Plans stage's deal board only: it is role-resolved exactly
    like the standalone board (sales/traders locked to ``mine``), and its All/Mine toggle
    reloads THIS whole tab body so the pinned approval section survives the swap.
    """
    lens = tab.replace("-", "_")
    if lens not in _APPROVALS_TABS:
        raise HTTPException(404, "Unknown approvals tab")

    if lens == "supervise":
        return _render_supervise_body(request, user, db)

    ctx = _base_ctx(request, user, "buy-plans")
    if lens in _TAB_APPROVE_ATTR:
        from ...services.approvals.queue import build_queue_view

        ctx["view"] = build_queue_view(db, user, lens)
        ctx["show_pending"] = bool(getattr(user, _TAB_APPROVE_ATTR[lens], False))

    if lens == "buy_plans":
        from ...services.buyplan_hub import completed_archive, deals_board

        # Role-resolve the deal-board scope exactly like the standalone /board route, but
        # point the All/Mine toggle at THIS tab URL so a toggle reloads the whole tab body
        # (pinned approval section + board) rather than swapping in the bare board.
        can_all = _can_see_all_deals(user, db)
        board_scope = _resolve_deal_scope(scope, can_all)
        ctx.update(
            {
                "board": deals_board(
                    db,
                    user,
                    scope=board_scope,
                    statuses=[BuyPlanStatus.ACTIVE.value, BuyPlanStatus.HALTED.value],
                ),
                "scope": board_scope,
                "archive": completed_archive(db, user, scope=board_scope),
                "can_see_all_deals": can_all,
                "scope_toggle_url": "/v2/partials/approvals/buy-plans",
            }
        )
        return template_response("htmx/partials/approvals/_tab_buy_plans.html", ctx)

    if lens == "purchase_orders":
        from ...services.buyplan_hub import buyer_line_queue, resourcing_pool_queue, team_line_queue

        ctx.update(
            {
                "orders_queue": buyer_line_queue(db, user),
                "team": team_line_queue(db, user),
                "resource_queue": resourcing_pool_queue(db),
                "can_claim": _can_resource(user),
            }
        )
        return template_response("htmx/partials/approvals/_tab_purchase_orders.html", ctx)

    if lens == "sales_orders":
        from ...services.buyplan_hub import deals_board

        can_all = _can_see_all_deals(user, db)
        board_scope = _resolve_deal_scope(scope, can_all)
        # DRAFT/PENDING work surface only. No `archive`: the Completed archive belongs to
        # the Buy Plans tab, and the shared board renders it only when `archive` is passed.
        ctx.update(
            {
                "board": deals_board(
                    db,
                    user,
                    scope=board_scope,
                    statuses=[BuyPlanStatus.DRAFT.value, BuyPlanStatus.PENDING.value],
                ),
                "scope": board_scope,
                "can_see_all_deals": can_all,
                "scope_toggle_url": "/v2/partials/approvals/sales-orders",
            }
        )
        return template_response("htmx/partials/approvals/_tab_sales_orders.html", ctx)

    # prepayments — approval-only stage (no work surface in SP-1)
    return template_response("htmx/partials/approvals/_tab_prepayments.html", ctx)


@router.get("/v2/partials/approvals/sales-orders/new", response_class=HTMLResponse)
async def sales_order_new(
    request: Request,
    requisition_id: int | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """New Sales Order origination surface (requisition picker → offer/sell builder).

    The two-segment path after ``approvals/`` does not collide with the one-segment
    ``/v2/partials/approvals/{tab}`` converter. With no ``requisition_id`` it lists open
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


@router.post("/v2/partials/approvals/sales-orders/create", response_class=HTMLResponse)
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


@router.get("/v2/partials/buy-plans/resource", response_class=HTMLResponse)
async def buy_plans_resource_partial(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Open-claim queue body for the "Needs Re-sourcing" lens (pool-wide).

    Lists every line whose cut PO was cancelled (vendor fell down) and is unassigned,
    awaiting any PO-cutter to claim + backfill.
    """
    from ...services.buyplan_hub import resourcing_pool_queue

    ctx = _base_ctx(request, user, "buy-plans")
    ctx["queue"] = resourcing_pool_queue(db)
    ctx["can_claim"] = _can_resource(user)
    return template_response("htmx/partials/buy_plans/_resource_queue.html", ctx)


@router.get("/v2/partials/buy-plans/orders", response_class=HTMLResponse)
async def buy_plans_orders_partial(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Buyer Orders body (re-homed under the Purchase Orders stage tab): the actionable
    per-line PO cut queue.

    Also includes a read-only "Team Orders" awareness section listing open lines
    assigned to OTHER buyers (see ``team_line_queue``).
    """
    from ...services.buyplan_hub import buyer_line_queue, team_line_queue

    ctx = _base_ctx(request, user, "buy-plans")
    ctx["queue"] = buyer_line_queue(db, user)
    ctx["team"] = team_line_queue(db, user)
    return template_response("htmx/partials/buy_plans/_orders_queue.html", ctx)


@router.get("/v2/partials/buy-plans/board", response_class=HTMLResponse)
async def buy_plans_board_partial(
    request: Request,
    scope: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Deal board body (re-homed under the Buy Plans / Supervise stage tabs): stage-
    grouped deal cards.

    Scope is role-defaulted: PO-cutters + ops (``_can_see_all_deals``) default to
    ``all`` and may toggle to ``mine``; sales/traders are locked to ``mine`` so no
    other rep's plans leak.
    """
    from ...services.buyplan_hub import completed_archive, deals_board

    can_all = _can_see_all_deals(user, db)
    scope = _resolve_deal_scope(scope, can_all)

    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update(
        {
            "board": deals_board(db, user, scope=scope),
            "scope": scope,
            "archive": completed_archive(db, user, scope=scope),
            "can_see_all_deals": can_all,
        }
    )
    return template_response("htmx/partials/buy_plans/_board.html", ctx)


@router.get("/v2/partials/buy-plans/archive", response_class=HTMLResponse)
async def buy_plans_archive_partial(
    request: Request,
    scope: str = "",
    offset: int = 0,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Completed-transactions archive page (lazy "load older" chunk).

    Returns just the rows partial (not the whole section) so an htmx "Load older" click
    can append the next page in place. Scope is role-resolved exactly like the board so
    no other rep's completed plans leak to a sales/trader user.
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
    return template_response("htmx/partials/buy_plans/_archive_rows.html", ctx)


def _render_supervise_body(request: Request, user: User, db: Session) -> HTMLResponse:
    """Build + render the supervise lens body for ``user``.

    Shared by the ``GET /supervise`` route and the supervise-origin action returns.
    Non-supervisors never see cross-user data: they get the mine-scope board instead
    (defense in depth — the hub also hides the Supervise button for them).
    """
    from ...services.buyplan_hub import completed_archive, deals_board, supervise_overview

    if not _can_supervise(user, db):
        ctx = _base_ctx(request, user, "buy-plans")
        ctx.update(
            {
                "board": deals_board(db, user, scope="mine"),
                "scope": "mine",
                "archive": completed_archive(db, user, scope="mine"),
            }
        )
        return template_response("htmx/partials/buy_plans/_board.html", ctx)

    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update(
        {
            "overview": supervise_overview(db),
            "board": deals_board(db, user, scope="all"),
            "archive": completed_archive(db, user, scope="all"),
            "is_ops": _is_ops_member(user, db),
            "is_manager": user.role in (UserRole.MANAGER, UserRole.ADMIN),
            "can_approve": can_approve_buy_plans(user),
            "user": user,
        }
    )
    return template_response("htmx/partials/buy_plans/_supervise.html", ctx)


@router.get("/v2/partials/buy-plans/supervise", response_class=HTMLResponse)
async def buy_plans_supervise_partial(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Manager/ops "Supervise" lens body: triage panel + all-scope deal board.

    Role-gated — a non-supervisor is served the mine-scope board so no other
    user's plans leak (see ``_render_supervise_body``).
    """
    return _render_supervise_body(request, user, db)


@router.get("/v2/partials/buy-plans/{plan_id}", response_class=HTMLResponse)
async def buy_plan_detail_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return buy plan detail as HTML partial."""
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

    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update(
        {
            "bp": bp,
            "lines": bp.lines or [],
            "is_ops_member": _is_ops_member(user, db),
            "can_resource": _can_resource(user),
            "user": user,
            # Most-urgent flag reason so the indicator states the issue at first glance.
            "top_flag": summarize_top_flag(bp.ai_flags),
        }
    )
    return template_response("htmx/partials/buy_plans/detail.html", ctx)


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

    if origin == "supervise":
        return _render_supervise_body(request, user, db)

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/verify-so", response_class=HTMLResponse)
async def buy_plan_verify_so_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Ops verifies SO — returns refreshed detail."""
    from ...services.buyplan_notifications import (
        notify_so_rejected,
        notify_so_verified,
        run_notify_bg,
    )
    from ...services.buyplan_workflow import verify_so

    form = await request.form()
    action = form.get("action", "approve")
    origin = form.get("origin", "")

    try:
        plan = verify_so(
            plan_id,
            action,
            user,
            db,
            rejection_note=form.get("rejection_note"),
        )
        db.commit()
        if action == "approve":
            await run_notify_bg(notify_so_verified, plan.id)
        else:
            await run_notify_bg(notify_so_rejected, plan.id, action=action)
    except (ValueError, PermissionError) as e:
        raise HTTPException(400, str(e))

    if origin == "supervise":
        return _render_supervise_body(request, user, db)

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/lines/{line_id}/confirm-po", response_class=HTMLResponse)
async def buy_plan_confirm_po_partial(
    request: Request,
    plan_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Buyer confirms PO.

    Returns the refreshed detail partial by default (``origin=""``, the original
    behavior). When ``origin == "queue"`` the call came from the buyer's Orders lens
    and we return the re-rendered orders queue so the confirmed line drops out.
    """

    from ...services.buyplan_notifications import notify_po_confirmed, run_notify_bg
    from ...services.buyplan_workflow import confirm_po

    # Per-record ownership: non-owner SALES/TRADER → 404 before any mutation.
    get_buyplan_for_user(db, user, plan_id)

    form = await request.form()
    po_number = form.get("po_number", "").strip()
    ship_date_str = form.get("estimated_ship_date", "")
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

    try:
        confirm_po(plan_id, line_id, po_number, ship_date, user, db)
        db.commit()
        await run_notify_bg(notify_po_confirmed, plan_id, line_id=line_id)
    except ValueError as e:
        raise HTTPException(400, str(e))

    if origin == "queue":
        return await buy_plans_orders_partial(request, user, db)

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/lines/{line_id}/resource", response_class=HTMLResponse)
async def buy_plan_resource_line_partial(
    request: Request,
    plan_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Re-source a line whose vendor PO was cancelled.

    Records the cancellation (vendor performance), marks the offer sold + the vendor
    unavailable, drops the line into the open claim pool, and fires the URGENT backfill
    alert to all other buyers. ``scope=plan`` re-sources the plan's other cut lines too.
    """
    from ...services.buyplan_notifications import notify_resource_requested, run_notify_bg
    from ...services.buyplan_workflow import resource_line

    # Per-record ownership (non-owner SALES/TRADER → 404) + PO-cutter role gate (403).
    get_buyplan_for_user(db, user, plan_id)
    _require_po_cutter(user)

    form = await request.form()
    reason_code = form.get("reason_code", "").strip()
    reason_note = (form.get("reason_note") or "").strip() or None
    scope = form.get("scope", "line")
    origin = form.get("origin", "")
    also_line_ids = [int(i) for i in form.getlist("also_line_ids")] if scope == "plan" else []

    if not reason_code:
        raise HTTPException(400, "A re-source reason is required")

    try:
        payload = resource_line(plan_id, line_id, reason_code, reason_note, user, db, also_line_ids=also_line_ids)
        db.commit()
    except ValueError as e:
        # Log before re-raising so a real failure (e.g. an un-keyable requirement deep in
        # the service) leaves a server trace instead of a silent, mislabeled 400.
        logger.warning("Re-source failed for plan {} line {}: {}", plan_id, line_id, e)
        raise HTTPException(400, str(e))

    # Broadcast one urgent alert PER re-sourced line (scope=plan re-sources siblings too,
    # and each pooled line needs its own claim).
    for resourced in payload["resourced_lines"]:
        await run_notify_bg(
            notify_resource_requested, plan_id, line_id=resourced["line_id"], actor_id=user.id, reason=reason_code
        )

    if origin == "resource":
        return await buy_plans_resource_partial(request, user, db)
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

    form = await request.form()
    origin = form.get("origin", "")

    try:
        claim_line(plan_id, line_id, user, db)
        db.commit()
    except ValueError as e:
        logger.info("Claim lost/invalid for plan {} line {} by {}: {}", plan_id, line_id, user.id, e)
        raise HTTPException(409, str(e))

    if origin == "resource":
        return await buy_plans_resource_partial(request, user, db)
    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/lines/{line_id}/verify-po", response_class=HTMLResponse)
async def buy_plan_verify_po_partial(
    request: Request,
    plan_id: int,
    line_id: int,
    user: User = Depends(require_user),
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

    if origin == "supervise":
        return _render_supervise_body(request, user, db)

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
