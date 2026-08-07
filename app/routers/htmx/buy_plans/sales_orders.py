"""Sales-Order origination — the requisition picker → offer/sell builder (sales-
orders/new + create) and the retired Buy Plans hub 308 redirect.

W4.8 split of the 1,543-line app/routers/htmx/buy_plans.py — pure structural
move: URLs and behavior unchanged; every route attaches to the shared router
imported from .common (registration assembled in __init__).
"""

import json

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ....constants import (
    RESTRICTED_ROLES,
    AccessKey,
)
from ....database import get_db
from ....dependencies import (
    require_access,
    require_user,
)
from ....models import (
    Requisition,
    User,
)
from ....template_env import template_response
from .._shared import _base_ctx
from .common import buy_plan_detail_partial, router


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
    return RedirectResponse("/v2/partials/approvals?tab=deals", status_code=308)


def _normalize_order_type(raw: str | None) -> str:
    """Normalize a picker/create order-type value: blank/unknown → NEW."""
    from ....constants import SalesOrderType

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

    from ....constants import (
        SOURCING_ORDER_TYPES,
        OfferStatus,
        RequisitionStatus,
        SalesOrderType,
    )
    from ....dependencies import get_req_for_user
    from ....models import Offer, Requirement
    from ....services.quote_builder_service import apply_smart_defaults, get_builder_data

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
    from ....constants import SOURCING_ORDER_TYPES
    from ....dependencies import require_requisition_access
    from ....services.buyplan_builder import (
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
