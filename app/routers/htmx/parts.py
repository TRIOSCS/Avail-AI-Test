"""routers/htmx/parts.py — Parts-workspace partials (HTMX + Alpine).

Server-rendered HTML partials for the parts split-panel workspace body: the parts
list, the detail tabs (offers/sourcing/req-details/quotes/activity/comms/notes), the
header + inline cell + spec editors, notes save, per-part tasks, and the part
archive/unarchive (single + bulk) actions. Extracted verbatim from htmx_views.py
(same `/v2/partials/parts` paths, same `htmx-views` tag). The workspace SHELL entry
(`/v2/partials/parts/workspace`) stays in htmx_views.py.

Called by: app/main.py (router mount).
Depends on: app.models, app.dependencies, app.database, app.services, ._shared
"""

import json
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import case, or_, select
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session, joinedload

from ...constants import (
    ActivityType,
    OfferStatus,
    RequisitionStatus,
    SourcingStatus,
    TaskStatus,
)
from ...database import get_db
from ...dependencies import (
    require_requisition_access,
    require_user,
)
from ...models import (
    Offer,
    Quote,
    QuoteLine,
    Requirement,
    Requisition,
    RequisitionTask,
    Sighting,
    User,
)
from ...models.vendor_sighting_summary import VendorSightingSummary
from ...services import task_service
from ...services.activity_service import log_activity as _log_activity
from ...services.sighting_aggregation import get_vendor_tier_map
from ...template_env import template_response
from ...utils.search_builder import SearchBuilder
from ._shared import _base_ctx, _parse_task_due_date, _safe_int

router = APIRouter(tags=["htmx-views"])


@router.get("/v2/partials/parts", response_class=HTMLResponse)
async def parts_list_partial(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    q: str = "",
    requisition_name: str = "",
    customer: str = "",
    brand: str = "",
    status: str = "",
    owner: int = 0,
    date_from: str = "",
    date_to: str = "",
    include_archived: bool | None = None,
    sort: str = "created",
    dir: str = "desc",
    offset: int = 0,
    limit: int = 50,
):
    """Return parts (requirements) list as HTML partial with filters and sorting."""

    # Clamp pagination params
    offset = max(0, offset)
    limit = max(1, min(200, limit))

    # Validate + parse date filters into datetimes (bind real datetimes, not
    # raw strings, so UTCDateTime columns receive UTC-aware values)
    date_from_dt = None
    date_to_dt = None
    if date_from:
        try:
            date_from_dt = datetime.strptime(date_from, "%Y-%m-%d")
        except ValueError:
            date_from = ""
    if date_to:
        try:
            date_to_dt = datetime.strptime(date_to, "%Y-%m-%d")
        except ValueError:
            date_to = ""

    query = db.query(Requirement).join(Requisition, Requirement.requisition_id == Requisition.id)

    # Archive visibility logic:
    # - status=archived  → show only archived parts (any requisition status)
    # - include_archived → show everything (no filtering)
    # - default          → exclude archived parts AND archived requisitions
    if status == "archived":
        query = query.filter(Requirement.sourcing_status == SourcingStatus.ARCHIVED)
    elif not include_archived:
        query = query.filter(
            Requisition.status.in_(
                [
                    RequisitionStatus.OPEN,
                ]
            )
        )
        query = query.filter(Requirement.sourcing_status != SourcingStatus.ARCHIVED)

    # Filters
    if q:
        sb_q = SearchBuilder(q)
        query = query.filter(
            sb_q.ilike_filter(
                Requirement.primary_mpn,
                Requirement.customer_pn,
                Requirement.brand,
                Requirement.substitutes_text,
                Requisition.name,
                Requisition.customer_name,
            )
        )
    if requisition_name:
        query = query.filter(SearchBuilder(requisition_name).ilike_filter(Requisition.name))
    if customer:
        query = query.filter(SearchBuilder(customer).ilike_filter(Requisition.customer_name))
    if brand:
        query = query.filter(SearchBuilder(brand).ilike_filter(Requirement.brand))
    if status and status != "archived":
        query = query.filter(Requirement.sourcing_status == status)
    if owner:
        query = query.filter(Requisition.claimed_by_id == owner)
    if date_from_dt:
        query = query.filter(Requirement.created_at >= date_from_dt)
    if date_to_dt:
        query = query.filter(Requirement.created_at <= date_to_dt)

    total = query.count()

    # Sorting
    sort_map = {
        "mpn": Requirement.primary_mpn,
        "brand": Requirement.brand,
        "qty": Requirement.target_qty,
        "target_price": Requirement.target_price,
        "status": Requirement.sourcing_status,
        "requisition": Requisition.name,
        "customer": Requisition.customer_name,
        "created": Requirement.created_at,
    }
    sort_col = sort_map.get(sort) or Requirement.created_at
    query = query.order_by(sort_col.desc() if dir == "desc" else sort_col.asc())
    query = query.offset(offset).limit(limit)

    requirements = query.options(joinedload(Requirement.requisition).joinedload(Requisition.claimed_by)).all()

    # Aggregate offer count + best price per requirement
    req_ids = [r.id for r in requirements]
    offer_stats = {}
    if req_ids:
        stats = (
            db.query(
                Offer.requirement_id,
                sqlfunc.count(Offer.id).label("cnt"),
                sqlfunc.min(case((Offer.status == OfferStatus.ACTIVE, Offer.unit_price), else_=None)).label("best"),
            )
            .filter(Offer.requirement_id.in_(req_ids))
            .group_by(Offer.requirement_id)
            .all()
        )
        for row in stats:
            offer_stats[row.requirement_id] = {"count": row.cnt, "best_price": row.best}

    # Build display-MPN → material card ID mapping for click-through links
    from ...models.intelligence import MaterialCard
    from ...utils.normalization import normalize_mpn, normalize_mpn_key

    norm_to_mpns: dict[str, list[str]] = {}
    for r in requirements:
        raw_mpns = []
        if r.primary_mpn:
            raw_mpns.append(r.primary_mpn)
        for sub in r.substitutes or []:
            raw = sub["mpn"] if isinstance(sub, dict) else sub
            if raw:
                raw_mpns.append(raw)
        for raw in raw_mpns:
            display = normalize_mpn(raw) or raw.upper()
            nk = normalize_mpn_key(display)
            if nk:
                norm_to_mpns.setdefault(nk, []).append(display)

    sub_card_map: dict[str, int] = {}
    if norm_to_mpns:
        cards = (
            db.query(MaterialCard.normalized_mpn, MaterialCard.id)
            .filter(MaterialCard.normalized_mpn.in_(norm_to_mpns.keys()))
            .filter(MaterialCard.deleted_at.is_(None))
            .all()
        )
        for card_norm, card_id in cards:
            for display_mpn in norm_to_mpns[card_norm]:
                sub_card_map[display_mpn] = card_id

    # Team users for owner filter
    users_list = db.query(User).filter(User.is_active.is_(True)).order_by(User.name).all()

    # Spotlight markers: requirement rows carrying new confirmed offers the user hasn't seen.
    from ...services.alerts import markers_for_tab

    alert_markers = markers_for_tab(db, user, "requisitions")

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update(
        {
            "requirements": requirements,
            "offer_stats": offer_stats,
            "alert_markers": alert_markers,
            "q": q,
            "requisition_name": requisition_name,
            "customer": customer,
            "brand": brand,
            "status": status,
            "owner": owner,
            "date_from": date_from,
            "date_to": date_to,
            "include_archived": include_archived,
            "sort": sort,
            "dir": dir,
            "offset": offset,
            "limit": limit,
            "total": total,
            "users": users_list,
            "user_role": user.role,
            "sub_card_map": sub_card_map,
        }
    )
    return template_response("htmx/partials/parts/list.html", ctx)


@router.get("/v2/partials/parts/{requirement_id}/tab/offers", response_class=HTMLResponse)
async def part_tab_offers(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return offers table for a specific part number."""
    req = db.execute(
        select(Requirement).options(joinedload(Requirement.requisition)).where(Requirement.id == requirement_id)
    ).scalar_one_or_none()
    if not req:
        raise HTTPException(404, "Part not found")

    offers = db.query(Offer).filter(Offer.requirement_id == requirement_id).order_by(Offer.created_at.desc()).all()

    vendor_tier_map = get_vendor_tier_map(db, requirement_id)

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"requirement": req, "offers": offers, "vendor_tier_map": vendor_tier_map})
    return template_response("htmx/partials/parts/tabs/offers.html", ctx)


@router.get("/v2/partials/parts/{requirement_id}/tab/sourcing", response_class=HTMLResponse)
async def part_tab_sourcing(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return vendor-level sighting summaries for a specific part number."""
    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")

    summaries = (
        db.query(VendorSightingSummary)
        .filter(VendorSightingSummary.requirement_id == requirement_id)
        .order_by(VendorSightingSummary.score.desc(), VendorSightingSummary.id.desc())
        .all()
    )
    vendor_tier_map = get_vendor_tier_map(db, requirement_id)

    # Raw sightings grouped by vendor for popover breakdowns
    raw_sightings = (
        db.query(Sighting).filter(Sighting.requirement_id == requirement_id).order_by(Sighting.score.desc()).all()
    )
    raw_by_vendor: dict[str, list] = {}
    for s in raw_sightings:
        vn = (s.vendor_name or "unknown").strip()
        raw_by_vendor.setdefault(vn, []).append(s)

    # Derive vendor outreach statuses
    from app.services.sighting_status import compute_vendor_statuses

    vendor_statuses = compute_vendor_statuses(requirement_id, req.requisition_id, db)

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update(
        {
            "requirement": req,
            "summaries": summaries,
            "vendor_tier_map": vendor_tier_map,
            "raw_sightings_by_vendor": raw_by_vendor,
            "vendor_statuses": vendor_statuses,
        }
    )
    return template_response("htmx/partials/parts/tabs/sourcing.html", ctx)


@router.get("/v2/partials/parts/{requirement_id}/tab/req-details", response_class=HTMLResponse)
async def part_tab_req_details(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the Req Details tab showing parent requisition fields and sibling
    parts."""
    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")

    requisition = req.requisition
    sibling_parts = (
        db.query(Requirement)
        .options(joinedload(Requirement.offers))
        .filter(Requirement.requisition_id == requisition.id)
        .order_by(Requirement.primary_mpn)
        .all()
    )
    users_list = db.query(User).filter(User.is_active.is_(True)).order_by(User.name).all()

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update(
        {
            "requirement": req,
            "requisition": requisition,
            "sibling_parts": sibling_parts,
            "users": users_list,
        }
    )
    return template_response("htmx/partials/parts/tabs/req_details.html", ctx)


@router.get("/v2/partials/parts/{requirement_id}/tab/quotes", response_class=HTMLResponse)
async def part_tab_quotes(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Cross-requisition quote history for the selected part: every quote LINE
    whose MPN matches this part (primary + substitutes) OR whose material_card
    matches this part's canonical card — across ALL requisitions/customers."""
    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(404, "Part not found")

    from ...utils.normalization import parse_substitute_mpns

    mpns: set[str] = set()
    if requirement.primary_mpn:
        mpns.add(requirement.primary_mpn.upper())
    for sub in parse_substitute_mpns(requirement.substitutes or [], requirement.primary_mpn or ""):
        if sub.get("mpn"):
            mpns.add(sub["mpn"].upper())

    conds = []
    if mpns:
        conds.append(sqlfunc.upper(QuoteLine.mpn).in_(mpns))
    if requirement.material_card_id:
        conds.append(QuoteLine.material_card_id == requirement.material_card_id)

    quote_lines = []
    if conds:
        quote_lines = (
            db.query(QuoteLine)
            .join(Quote, QuoteLine.quote_id == Quote.id)
            .options(joinedload(QuoteLine.quote).joinedload(Quote.requisition))
            .filter(or_(*conds))
            .order_by(Quote.created_at.desc().nullslast())
            .all()
        )
    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"requirement": requirement, "quote_lines": quote_lines})
    return template_response("htmx/partials/parts/tabs/quotes.html", ctx)


@router.get("/v2/partials/parts/{requirement_id}/header", response_class=HTMLResponse)
async def part_header(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the part detail header strip (display mode)."""
    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")

    ctx = _base_ctx(request, user, "requisitions")
    ctx["requirement"] = req
    return template_response("htmx/partials/parts/header.html", ctx)


_PART_HEADER_EDITABLE = {
    "brand",
    "condition",
    "description",
    "manufacturer",
    "target_qty",
    "target_price",
    "sourcing_status",
    "notes",
    "substitutes",
}
_CONDITION_CHOICES = ["New", "Used", "Refurbished", "Any"]


@router.get("/v2/partials/parts/{requirement_id}/header/edit/{field}", response_class=HTMLResponse)
async def part_header_edit_cell(
    requirement_id: int,
    field: str,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return an inline edit input for a single header field."""
    if field not in _PART_HEADER_EDITABLE:
        return HTMLResponse("Invalid field", status_code=400)

    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")

    from markupsafe import escape

    context = request.query_params.get("context", "header")
    current = getattr(req, field, "") or ""
    safe_current = escape(current)
    cell_id = f"hdr-{field}"
    cancel_url = f"/v2/partials/parts/{requirement_id}/header"
    save_url = f"/v2/partials/parts/{requirement_id}/header"
    swap_target = "#part-header-wrap"
    if context == "tab":
        cell_id = f"reqd-{field}"
        cancel_url = f"/v2/partials/parts/{requirement_id}/tab/req-details"
        swap_target = "#part-detail"

    if field == "sourcing_status":
        statuses = ["open", "sourcing", "offered", "quoted", "won", "lost", "archived"]
        options = "".join(
            f'<option value="{s}" {"selected" if s == current else ""}>{s.capitalize()}</option>' for s in statuses
        )
        return HTMLResponse(
            f'<select name="value" id="{cell_id}" '
            f'hx-patch="{save_url}" hx-target="#part-header-wrap" hx-swap="innerHTML" '
            f'hx-vals=\'{{"field": "{field}"}}\' '
            f'class="text-xs px-1.5 py-0.5 rounded border border-brand-300 focus:ring-1 focus:ring-brand-500" '
            f"@keydown.escape=\"htmx.ajax('GET', '{cancel_url}', {{target: '#part-header-wrap', swap: 'innerHTML'}})\">"
            f"{options}</select>",
        )

    if field == "condition":
        options = "".join(
            f'<option value="{c}" {"selected" if c == current else ""}>{c}</option>' for c in _CONDITION_CHOICES
        )
        return HTMLResponse(
            f'<select name="value" id="{cell_id}" '
            f'hx-patch="{save_url}" hx-target="#part-header-wrap" hx-swap="innerHTML" '
            f'hx-vals=\'{{"field": "{field}"}}\' '
            f'class="text-xs px-1.5 py-0.5 rounded border border-brand-300 focus:ring-1 focus:ring-brand-500" '
            f"@keydown.escape=\"htmx.ajax('GET', '{cancel_url}', {{target: '#part-header-wrap', swap: 'innerHTML'}})\">"
            f"{options}</select>",
        )

    if field == "substitutes":
        import json as _json_enc

        subs_json = _json_enc.dumps(req.substitutes if req.substitutes else [])
        subs_json_escaped = subs_json.replace("'", "&#39;").replace('"', "&quot;")
        html = (
            f'<div id="{cell_id}"'
            f' x-data="{{ subs: JSON.parse($el.dataset.subs), saving: false }}"'
            f' data-subs="{subs_json_escaped}">'
            # hidden input serialises rows to JSON on submit
            f'<input type="hidden" name="value" x-bind:value="JSON.stringify(subs)">'
            f'<input type="hidden" name="field" value="{field}">'
            # sub rows
            f'<template x-for="(sub, idx) in subs" :key="idx">'
            f'<div class="flex gap-1 items-center mb-1">'
            f'<input :name="\'sub_mpn_\' + idx" x-model="sub.mpn" placeholder="Sub MPN"'
            f' class="px-2 py-0.5 text-xs font-mono border border-brand-300 rounded focus:ring-1 focus:ring-brand-500 w-32">'
            f'<input :name="\'sub_mfr_\' + idx" x-model="sub.manufacturer" placeholder="Manufacturer"'
            f' class="px-2 py-0.5 text-xs border border-brand-300 rounded focus:ring-1 focus:ring-brand-500 w-36">'
            f'<button type="button" @click="subs.splice(idx, 1)"'
            f' class="text-gray-400 hover:text-red-500 text-sm leading-none px-1">×</button>'
            f"</div>"
            f"</template>"
            f'<div class="flex items-center gap-2 mt-1">'
            f"<button type=\"button\" @click=\"subs.push({{mpn: '', manufacturer: ''}})\""
            f' class="text-[10px] text-brand-500 hover:text-brand-600 font-medium">+ Add</button>'
            f'<button type="button" :disabled="saving"'
            f" @click=\"saving=true; htmx.ajax('PATCH', '{save_url}', {{target: '#part-header-wrap', swap: 'innerHTML', values: {{field: '{field}', value: JSON.stringify(subs)}}}})\""
            f' class="text-[10px] px-2 py-0.5 bg-brand-500 text-white rounded hover:bg-brand-600 font-medium">Save</button>'
            f'<button type="button"'
            f" @click=\"htmx.ajax('GET', '{cancel_url}', {{target: '#part-header-wrap', swap: 'innerHTML'}})\""
            f' class="text-[10px] text-gray-500 hover:text-gray-700">Cancel</button>'
            f"</div>"
            f"</div>"
        )
        return HTMLResponse(html)

    if field == "description":
        return HTMLResponse(
            f'<div id="{cell_id}" class="w-full">'
            f'<textarea name="value" rows="2" '
            f'class="w-full text-xs px-1.5 py-0.5 rounded border border-brand-300 focus:ring-1 focus:ring-brand-500 resize-y" '
            f"@keydown.escape=\"htmx.ajax('GET', '{cancel_url}', {{target: '{swap_target}', swap: 'innerHTML'}})\" "
            f"autofocus>{safe_current}</textarea>"
            f'<div class="flex items-center gap-2 mt-1">'
            f'<button type="button" '
            f"onclick=\"htmx.ajax('PATCH', '{save_url}', {{target: '{swap_target}', swap: 'innerHTML', "
            f"values: {{field: '{field}', value: this.closest('div').parentElement.querySelector('textarea').value}}}})\" "
            f'class="text-[10px] px-2 py-0.5 bg-brand-500 text-white rounded hover:bg-brand-600 font-medium">Save</button>'
            f'<button type="button" '
            f"onclick=\"htmx.ajax('GET', '{cancel_url}', {{target: '{swap_target}', swap: 'innerHTML'}})\" "
            f'class="text-[10px] text-gray-500 hover:text-gray-700">Cancel</button>'
            f"</div></div>",
        )

    input_type = "number" if field in ("target_qty", "target_price") else "text"
    step = ' step="0.0001"' if field == "target_price" else ""

    return HTMLResponse(
        f'<input type="{input_type}" name="value" id="{cell_id}" value="{safe_current}" '
        f'hx-patch="{save_url}" hx-target="#part-header-wrap" hx-swap="innerHTML" '
        f'hx-vals=\'{{"field": "{field}"}}\' '
        f"hx-trigger=\"keyup[key=='Enter']\" "
        f"@keydown.escape=\"htmx.ajax('GET', '{cancel_url}', {{target: '#part-header-wrap', swap: 'innerHTML'}})\" "
        f'class="text-sm px-1.5 py-0.5 rounded border border-brand-300 focus:ring-1 focus:ring-brand-500 w-24"'
        f"{step} autofocus />",
    )


@router.patch("/v2/partials/parts/{requirement_id}/header", response_class=HTMLResponse)
async def part_header_save(
    requirement_id: int,
    request: Request,
    field: str = Form(...),
    value: str = Form(default=""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save an inline header field edit and return the refreshed header."""
    if field not in _PART_HEADER_EDITABLE:
        return HTMLResponse("Invalid field", status_code=400)

    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")
    require_requisition_access(db, req.requisition_id, user, label="Part")

    if field == "sourcing_status":
        from app.services.requirement_status import transition_requirement

        try:
            ok = transition_requirement(req, value, db, user)
        except ValueError:
            ok = False
        if not ok:
            logger.warning(
                "Status transition rejected: {} → {} for part {}", req.sourcing_status, value, requirement_id
            )
    elif field == "target_qty":
        try:
            req.target_qty = int(value) if value else None
        except (ValueError, TypeError):
            req.target_qty = None
    elif field == "target_price":
        from decimal import Decimal, InvalidOperation

        try:
            req.target_price = Decimal(value) if value else None
        except InvalidOperation:
            req.target_price = None
    elif field == "manufacturer":
        req.manufacturer = value.strip() if value else ""
    elif field == "substitutes":
        import json as _json

        from ...search_service import resolve_material_card
        from ...utils.normalization import parse_substitute_mpns

        try:
            subs_data = _json.loads(value) if value else []
        except (ValueError, TypeError):
            subs_data = []
        req.substitutes = parse_substitute_mpns(subs_data, req.primary_mpn)
        for sub in req.substitutes:
            resolve_material_card(sub["mpn"], db, manufacturer=sub.get("manufacturer", ""))
    else:
        setattr(req, field, value.strip() if value else None)

    db.commit()
    logger.info("Part {} header field '{}' updated by {}", requirement_id, field, user.email)

    ctx = _base_ctx(request, user, "requisitions")
    ctx["requirement"] = req
    response = template_response("htmx/partials/parts/header.html", ctx)
    response.headers["HX-Trigger"] = json.dumps({"part-updated": {"id": requirement_id}})
    return response


# ── Inline table-cell editing ────────────────────────────────────────

_CELL_EDITABLE = {"sourcing_status", "target_qty", "target_price"}
_SPEC_LABELS = {
    "customer_pn": "Customer PN",
    "condition": "Condition",
    "date_codes": "Date Codes",
    "packaging": "Packaging",
    "firmware": "Firmware",
    "hardware_codes": "Hardware",
}


@router.get("/v2/partials/parts/{requirement_id}/cell/edit/{field}", response_class=HTMLResponse)
async def part_cell_edit(
    requirement_id: int,
    field: str,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return an inline edit widget for a single table cell."""
    if field not in _CELL_EDITABLE:
        return HTMLResponse("Invalid field", status_code=400)

    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")

    cell_id = f"cell-{field}-{requirement_id}"
    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"requirement": req, "field": field, "cell_id": cell_id})
    return template_response("htmx/partials/parts/cell_edit.html", ctx)


@router.get("/v2/partials/parts/{requirement_id}/cell/display/{field}", response_class=HTMLResponse)
async def part_cell_display(
    requirement_id: int,
    field: str,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return display-mode table cell (used for Escape cancel)."""
    if field not in _CELL_EDITABLE:
        return HTMLResponse("Invalid field", status_code=400)

    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")

    cell_id = f"cell-{field}-{requirement_id}"
    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"requirement": req, "field": field, "cell_id": cell_id})
    return template_response("htmx/partials/parts/cell_display.html", ctx)


@router.patch("/v2/partials/parts/{requirement_id}/cell", response_class=HTMLResponse)
async def part_cell_save(
    requirement_id: int,
    request: Request,
    field: str = Form(...),
    value: str = Form(default=""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save an inline table-cell edit and return the display-mode cell."""
    if field not in _CELL_EDITABLE:
        return HTMLResponse("Invalid field", status_code=400)

    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")
    require_requisition_access(db, req.requisition_id, user, label="Part")

    if field == "sourcing_status":
        from app.services.requirement_status import transition_requirement

        try:
            transition_requirement(req, value, db, user)
        except ValueError:
            logger.warning(
                "Cell status transition rejected: {} → {} for part {}", req.sourcing_status, value, requirement_id
            )
    elif field == "target_qty":
        try:
            req.target_qty = int(value) if value else None
        except (ValueError, TypeError):
            req.target_qty = None
    elif field == "target_price":
        from decimal import Decimal, InvalidOperation

        try:
            req.target_price = Decimal(value) if value else None
        except InvalidOperation:
            req.target_price = None

    db.commit()
    logger.info("Part {} cell '{}' updated by {}", requirement_id, field, user.email)

    cell_id = f"cell-{field}-{requirement_id}"
    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"requirement": req, "field": field, "cell_id": cell_id})
    response = template_response("htmx/partials/parts/cell_display.html", ctx)
    response.headers["HX-Trigger"] = json.dumps({"part-updated": {"id": requirement_id}})
    return response


@router.get("/v2/partials/parts/{requirement_id}/edit-spec/{field}", response_class=HTMLResponse)
async def part_spec_edit(
    requirement_id: int,
    field: str,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return inline edit form for a requirement spec field."""
    if field not in _SPEC_LABELS:
        return HTMLResponse("Invalid field", status_code=400)

    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")

    if req.sourcing_status == SourcingStatus.ARCHIVED:
        return HTMLResponse("Cannot edit archived part", status_code=403)

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update(
        {
            "requirement": req,
            "field": field,
            "field_label": _SPEC_LABELS[field],
            "field_value": getattr(req, field, None) or "",
            "condition_choices": _CONDITION_CHOICES,
        }
    )
    return template_response("htmx/partials/parts/spec_edit.html", ctx)


@router.patch("/v2/partials/parts/{requirement_id}/save-spec", response_class=HTMLResponse)
async def part_spec_save(
    requirement_id: int,
    request: Request,
    field: str = Form(...),
    value: str = Form(default=""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save an inline spec field edit and return the updated display."""
    if field not in _SPEC_LABELS:
        return HTMLResponse("Invalid field", status_code=400)

    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")
    require_requisition_access(db, req.requisition_id, user, label="Part")

    if req.sourcing_status == SourcingStatus.ARCHIVED:
        return HTMLResponse("Cannot edit archived part", status_code=403)

    clean = (value or "").strip() or None
    setattr(req, field, clean)
    db.commit()

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"field_value": clean})
    response = template_response("htmx/partials/parts/spec_display.html", ctx)
    response.headers["HX-Trigger"] = json.dumps(
        {
            "part-updated": {"id": requirement_id},
            "showToast": {"message": f"{_SPEC_LABELS[field]} updated", "type": "success"},
        }
    )
    return response


@router.get("/v2/partials/parts/{requirement_id}/tab/activity", response_class=HTMLResponse)
async def part_tab_activity(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return activity timeline for the parent requisition of this part."""
    from ...models.intelligence import ActivityLog

    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")

    activities = (
        db.query(ActivityLog)
        .filter(ActivityLog.requisition_id == req.requisition_id)
        .order_by(ActivityLog.created_at.desc())
        .limit(50)
        .all()
    )

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"requirement": req, "activities": activities})
    return template_response("htmx/partials/parts/tabs/activity.html", ctx)


@router.get("/v2/partials/parts/{requirement_id}/tab/comms", response_class=HTMLResponse)
async def part_tab_comms(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return communications tab — notes and tasks for this part."""
    req = db.execute(
        select(Requirement).options(joinedload(Requirement.requisition)).where(Requirement.id == requirement_id)
    ).scalar_one_or_none()
    if not req:
        raise HTTPException(404, "Part not found")

    tasks = (
        db.query(RequisitionTask)
        .options(joinedload(RequisitionTask.assignee), joinedload(RequisitionTask.creator))
        .filter(
            (RequisitionTask.requisition_id == req.requisition_id)
            & ((RequisitionTask.requirement_id == requirement_id) | (RequisitionTask.requirement_id.is_(None)))
        )
        .order_by(
            RequisitionTask.status.asc(),  # pending before done
            RequisitionTask.due_at.asc().nullslast(),
            RequisitionTask.created_at.desc(),
        )
        .all()
    )

    users_list = db.query(User).filter(User.is_active.is_(True)).order_by(User.name).all()

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"requirement": req, "tasks": tasks, "users": users_list})
    return template_response("htmx/partials/parts/tabs/comms.html", ctx)


@router.get("/v2/partials/parts/{requirement_id}/tab/notes", response_class=HTMLResponse)
async def part_tab_notes(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return sales notes tab for a requirement."""
    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")
    ctx = _base_ctx(request, user, "requisitions")
    ctx["requirement"] = req
    return template_response("htmx/partials/parts/tabs/notes.html", ctx)


@router.patch("/v2/partials/parts/{requirement_id}/notes", response_class=HTMLResponse)
async def save_part_notes(
    requirement_id: int,
    request: Request,
    sale_notes: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save sales notes for a requirement."""
    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")
    require_requisition_access(db, req.requisition_id, user, label="Part")
    old_sale_notes = req.sale_notes
    req.sale_notes = sale_notes.strip() or None
    if (req.sale_notes or "") != (old_sale_notes or ""):
        _log_activity(
            db,
            activity_type=ActivityType.SALES_NOTE,
            requisition_id=req.requisition_id,
            requirement_id=req.id,
            user_id=user.id,
            description="Sales note updated",
            details={"requirement_id": req.id},
        )
    db.commit()
    ctx = _base_ctx(request, user, "requisitions")
    ctx["requirement"] = req
    return template_response("htmx/partials/parts/tabs/notes.html", ctx)


@router.post("/v2/partials/parts/{requirement_id}/tasks", response_class=HTMLResponse)
async def create_part_task(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a task for a specific part number."""
    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")
    require_requisition_access(db, req.requisition_id, user, label="Part")

    form = await request.form()
    title = (form.get("title") or "").strip()
    if not title:
        raise HTTPException(422, "Title is required")

    task = RequisitionTask(
        requisition_id=req.requisition_id,
        requirement_id=requirement_id,
        title=title,
        description=(form.get("notes") or "").strip() or None,
        assigned_to_id=_safe_int(form.get("assigned_to")),
        created_by=user.id,
        due_at=_parse_task_due_date(form.get("due_date")),
        status=TaskStatus.TODO,
        source="manual",
    )
    db.add(task)
    db.commit()
    logger.info("Task '{}' created for requirement {} by {}", title, requirement_id, user.email)

    # Return refreshed comms tab
    return await part_tab_comms(requirement_id, request, user, db)


@router.post("/v2/partials/parts/tasks/{task_id}/done", response_class=HTMLResponse)
async def mark_task_done(
    task_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Mark a task as done.

    Only the assignee may complete the task. An optional ``completion_note`` form field
    (a "how was this resolved?" note, submitted from the comms tab) is stored on the task.
    """
    form = await request.form()
    completion_note = (form.get("completion_note") or "").strip()
    try:
        task = task_service.complete_task(db, task_id, user.id, completion_note)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    if task is None:
        raise HTTPException(404, "Task not found")

    logger.info("Task {} marked done by {}", task_id, user.email)

    # Return refreshed comms tab for the requirement
    req_id = task.requirement_id
    if req_id:
        return await part_tab_comms(req_id, request, user, db)

    # Fallback — return just the updated task row
    return HTMLResponse('<div class="text-sm text-green-600">Task completed</div>')


@router.post("/v2/partials/parts/tasks/{task_id}/reopen", response_class=HTMLResponse)
async def reopen_task(
    task_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Reopen a completed task.

    Only the assignee may reopen the task.
    """
    try:
        task = task_service.reopen_task(db, task_id, user.id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    if task is None:
        raise HTTPException(404, "Task not found")

    logger.info("Task {} reopened by {}", task_id, user.email)

    req_id = task.requirement_id
    if req_id:
        return await part_tab_comms(req_id, request, user, db)
    return HTMLResponse('<div class="text-sm text-amber-600">Task reopened</div>')


# ── Archive system ────────────────────────────────────────────────────


@router.patch("/v2/partials/parts/{requirement_id}/archive", response_class=HTMLResponse)
async def archive_single_part(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Archive a single part (requirement) by setting sourcing_status to archived."""
    part = db.get(Requirement, requirement_id)
    if not part:
        raise HTTPException(404, "Part not found")
    require_requisition_access(db, part.requisition_id, user, label="Part")

    part.sourcing_status = SourcingStatus.ARCHIVED
    db.commit()
    logger.info("Part {} archived by {}", requirement_id, user.email)

    response = await parts_list_partial(request=request, user=user, db=db)
    response.headers["HX-Trigger"] = json.dumps({"part-archived": {"id": requirement_id}})
    return response


@router.patch("/v2/partials/parts/{requirement_id}/unarchive", response_class=HTMLResponse)
async def unarchive_single_part(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Unarchive a single part — restores sourcing_status to open."""
    part = db.get(Requirement, requirement_id)
    if not part:
        raise HTTPException(404, "Part not found")
    require_requisition_access(db, part.requisition_id, user, label="Part")

    part.sourcing_status = SourcingStatus.OPEN
    db.commit()
    logger.info("Part {} unarchived by {}", requirement_id, user.email)

    return await parts_list_partial(request=request, user=user, db=db)


@router.post("/v2/partials/parts/bulk-archive", response_class=HTMLResponse)
async def bulk_archive(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Bulk-archive parts and/or requisitions.

    Body: {"requirement_ids": [], "requisition_ids": []}.
    """
    body = await request.json()
    requirement_ids = body.get("requirement_ids", [])
    requisition_ids = body.get("requisition_ids", [])

    # Ownership guard (no-op for buyer/manager/admin; 404 for a restricted non-owner)
    for _rid in requisition_ids:
        require_requisition_access(db, _rid, user)
    if requirement_ids:
        for _r in db.query(Requirement).filter(Requirement.id.in_(requirement_ids)).all():
            require_requisition_access(db, _r.requisition_id, user, label="Requirement")

    # Bulk-update parts in a single query instead of N+1
    if requirement_ids:
        db.query(Requirement).filter(
            Requirement.id.in_(requirement_ids),
        ).update({"sourcing_status": SourcingStatus.ARCHIVED}, synchronize_session="fetch")

    # Archive every part belonging to the named requisitions (part-level
    # sourcing_status — there is no requisition-level archive/hide flag).
    if requisition_ids:
        db.query(Requirement).filter(
            Requirement.requisition_id.in_(requisition_ids),
        ).update({"sourcing_status": SourcingStatus.ARCHIVED}, synchronize_session="fetch")

    db.commit()
    logger.info("Bulk archive by {}: {} parts, {} requisitions", user.email, len(requirement_ids), len(requisition_ids))

    return await parts_list_partial(request=request, user=user, db=db)


@router.post("/v2/partials/parts/bulk-unarchive", response_class=HTMLResponse)
async def bulk_unarchive(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Bulk-unarchive parts and/or requisitions.

    Body: {"requirement_ids": [], "requisition_ids": []}.
    """
    body = await request.json()
    requirement_ids = body.get("requirement_ids", [])
    requisition_ids = body.get("requisition_ids", [])

    # Ownership guard (no-op for buyer/manager/admin; 404 for a restricted non-owner)
    for _rid in requisition_ids:
        require_requisition_access(db, _rid, user)
    if requirement_ids:
        for _r in db.query(Requirement).filter(Requirement.id.in_(requirement_ids)).all():
            require_requisition_access(db, _r.requisition_id, user, label="Requirement")

    # Bulk-update parts in a single query instead of N+1
    if requirement_ids:
        db.query(Requirement).filter(
            Requirement.id.in_(requirement_ids),
            Requirement.sourcing_status == SourcingStatus.ARCHIVED,
        ).update({"sourcing_status": SourcingStatus.OPEN}, synchronize_session="fetch")

    # Restore every archived part belonging to the named requisitions
    # (part-level sourcing_status — there is no requisition-level archive flag).
    if requisition_ids:
        db.query(Requirement).filter(
            Requirement.requisition_id.in_(requisition_ids),
            Requirement.sourcing_status == SourcingStatus.ARCHIVED,
        ).update({"sourcing_status": SourcingStatus.OPEN}, synchronize_session="fetch")

    db.commit()
    logger.info(
        "Bulk unarchive by {}: {} parts, {} requisitions", user.email, len(requirement_ids), len(requisition_ids)
    )

    return await parts_list_partial(request=request, user=user, db=db)
