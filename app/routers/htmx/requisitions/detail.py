"""Requisition detail shell, requirement add, search-all, and the detail-tab
registration.

W4.8 split of the 1,473-line app/routers/htmx/requisitions.py — pure structural
move: URLs and behavior unchanged; every route attaches to the shared router
imported from .common (registration assembled in __init__).
"""

from fastapi import BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.orm import Session, joinedload, selectinload

from ....database import get_db
from ....dependencies import require_requisition_access, require_user
from ....models import (
    Requirement,
    Requisition,
    User,
)
from ....template_env import template_response
from ..._lookup_helpers import get_requisition_or_404
from .._shared import _base_ctx, _parse_date_safe
from .._shared_tabs import requisition_tab as _requisition_tab_impl
from .common import router


@router.get("/v2/partials/requisitions/{req_id}", response_class=HTMLResponse)
async def requisition_detail_partial(
    request: Request,
    req_id: int,
    tab: str | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return requisition detail as HTML partial with tabs.

    ``tab`` deep-links a starting tab (e.g. ``build_quote`` from the list "Build Quote"
    launch); it sets the Alpine active tab and auto-loads that tab's lazy body.
    """
    req = (
        db.query(Requisition)
        .options(
            joinedload(Requisition.creator),
            joinedload(Requisition.requirements).selectinload(Requirement.sightings),
            joinedload(Requisition.offers),
        )
        .filter(Requisition.id == req_id)
        .first()
    )
    if not req:
        raise HTTPException(404, "Requisition not found")
    require_requisition_access(db, req_id, user)

    requirements = req.requirements or []
    for r in requirements:
        r.sighting_count = len(r.sightings) if r.sightings else 0

    req.offer_count = len(req.offers) if req.offers else 0

    from ....services.requisition_state import attach_display_status

    attach_display_status(db, [req])

    # Fetch users for tasks tab assignee dropdown
    users = db.query(User).order_by(User.name).all()

    allowed_initial_tabs = {"parts", "offers", "responses", "quotes", "build_quote", "buy_plans"}
    initial_tab = tab if tab in allowed_initial_tabs else "parts"

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"req": req, "requirements": requirements, "users": users, "initial_tab": initial_tab})
    return template_response("htmx/partials/requisitions/detail.html", ctx)


# POST /v2/partials/requisitions/create (requisition_create) was deleted in W3 (spec §9):
# a UI-orphaned sibling of import-save (no template posted to it — the unified modal's
# create-form posts to /import-parse → /import-save). Its bare "MPN, Qty" text parsing
# bypassed the requirement pipeline (no normalization, dup detection, or task auto-gen).


@router.post("/v2/partials/requisitions/{req_id}/requirements", response_class=HTMLResponse)
async def add_requirement(
    request: Request,
    req_id: int,
    primary_mpn: str = Form(...),
    manufacturer: str = Form(""),
    target_qty: int = Form(1),
    brand: str = Form(""),
    substitutes: str = Form(""),
    target_price: float | None = Form(None),
    condition: str = Form(""),
    date_codes: str = Form(""),
    firmware: str = Form(""),
    hardware_codes: str = Form(""),
    packaging: str = Form(""),
    notes: str = Form(""),
    customer_pn: str = Form(""),
    need_by_date: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add a requirement to a requisition, return the new row HTML."""
    from datetime import date as date_type

    if not manufacturer.strip():
        raise HTTPException(422, "Manufacturer is required")

    req = get_requisition_or_404(db, req_id)
    require_requisition_access(db, req_id, user)

    form_data = await request.form()
    sub_mpns = form_data.getlist("sub_mpn")
    sub_mfrs = form_data.getlist("sub_manufacturer")
    subs_raw = [{"mpn": m.strip(), "manufacturer": mfr.strip()} for m, mfr in zip(sub_mpns, sub_mfrs) if m.strip()]

    # THE requirement-creation pipeline (services/requirement_service.py, spec §9).
    from ....services.requirement_service import create_requirements_ui

    result = await create_requirements_ui(
        db,
        req,
        [
            {
                "primary_mpn": primary_mpn,
                "target_qty": target_qty,
                "brand": brand or None,
                "manufacturer": manufacturer,
                "substitutes": subs_raw,
                "target_price": target_price,
                "condition": condition or None,
                "date_codes": date_codes or None,
                "firmware": firmware or None,
                "hardware_codes": hardware_codes or None,
                "packaging": packaging or None,
                "notes": notes or None,
                "customer_pn": customer_pn or None,
                "need_by_date": _parse_date_safe(need_by_date, date_type),
            }
        ],
        actor_id=user.id,
    )
    if not result.created:
        raise HTTPException(422, "primary_mpn is required")
    r = result.created[0]
    db.commit()
    db.refresh(r)

    # Return the new row via template for HTMX append
    r.sighting_count = 0
    ctx = _base_ctx(request, user, "requisitions")
    ctx["r"] = r
    ctx["req"] = req
    return template_response("htmx/partials/requisitions/tabs/req_row.html", ctx)


@router.post("/v2/partials/requisitions/{req_id}/search-all", response_class=HTMLResponse)
async def requisition_search_all(
    request: Request,
    req_id: int,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Trigger search for all requirements in a requisition, then refresh parts
    table."""
    req = get_requisition_or_404(db, req_id)
    require_requisition_access(db, req_id, user)
    requirements = db.query(Requirement).filter(Requirement.requisition_id == req_id).all()
    if not requirements:
        return HTMLResponse(
            "<div id='parts-table-wrapper'><p class='text-sm text-gray-500 p-4'>No requirements to search.</p></div>"
        )

    # Run searches in background
    import os

    if not os.environ.get("TESTING"):
        requirement_ids = [r.id for r in requirements]

        async def _bg_search(req_ids: list[int]):
            from app.database import SessionLocal
            from app.search_service import search_requirement as do_search

            bg_db = SessionLocal()
            try:
                for rid in req_ids:
                    try:
                        req_obj = bg_db.get(Requirement, rid)
                        if req_obj:
                            await do_search(req_obj, bg_db)
                    except Exception:
                        logger.warning("Manual search failed for requirement {}", rid, exc_info=True)
            finally:
                bg_db.close()

        background_tasks.add_task(_bg_search, requirement_ids)

    # Return the parts table with a searching indicator
    requirements = (
        db.query(Requirement)
        .options(selectinload(Requirement.sightings))
        .filter(Requirement.requisition_id == req_id)
        .all()
    )
    for r in requirements:
        r.sighting_count = len(r.sightings) if r.sightings else 0

    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req
    ctx["requirements"] = requirements
    ctx["search_triggered"] = True
    resp = template_response("htmx/partials/requisitions/tabs/parts.html", ctx)
    return resp


# Implementation lives in ._shared_tabs (P4.1 — offers.py / htmx_views.py reused this
# tab render by importing it straight off this sibling router module; it's now a
# shared home both import from). Registered here, unchanged, so the route/URL/tag and
# the `requisition_tab` name importable off this module are exactly as before.
requisition_tab = router.get("/v2/partials/requisitions/{req_id}/tab/{tab}", response_class=HTMLResponse)(
    _requisition_tab_impl
)
