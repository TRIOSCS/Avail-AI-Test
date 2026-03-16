"""
routers/htmx/vendors.py — Vendor list, detail, and tab partials.

Handles all /v2/partials/vendors/ routes: vendor listing with search/sort,
vendor detail with safety data, and per-vendor tab content (overview,
contacts, analytics, offers).

Called by: htmx router package (__init__.py includes this module's router)
Depends on: _helpers (router, templates, _base_ctx, _DASH, escape_like),
            models (VendorCard, VendorContact, Sighting, SourcingLead, User, Offer),
            dependencies (require_user), database (get_db)
"""

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ...database import get_db
from ...dependencies import require_user
from ...models import Sighting, SourcingLead, User, VendorCard
from ...models.vendors import VendorContact
from ._helpers import _DASH, _base_ctx, escape_like, router, templates


@router.get("/v2/partials/vendors", response_class=HTMLResponse)
async def vendors_list_partial(
    request: Request,
    q: str = "",
    hide_blacklisted: bool = True,
    sort: str = "sighting_count",
    dir: str = "desc",
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return vendor list as HTML partial with blacklisted toggle and sorting."""
    query = db.query(VendorCard)

    if hide_blacklisted:
        query = query.filter(VendorCard.is_blacklisted.is_(False))

    if q.strip():
        safe = escape_like(q.strip())
        query = query.filter(VendorCard.display_name.ilike(f"%{safe}%") | VendorCard.domain.ilike(f"%{safe}%"))

    total = query.count()

    # Sorting
    sort_col_map = {
        "display_name": VendorCard.display_name,
        "sighting_count": VendorCard.sighting_count,
        "overall_win_rate": VendorCard.overall_win_rate,
        "hq_country": VendorCard.hq_country,
        "industry": VendorCard.industry,
    }
    sort_col = sort_col_map.get(sort, VendorCard.sighting_count)
    order = sort_col.desc().nullslast() if dir == "desc" else sort_col.asc().nullslast()
    vendors = query.order_by(order).offset(offset).limit(limit).all()

    ctx = _base_ctx(request, user, "vendors")
    ctx.update(
        {
            "vendors": vendors,
            "q": q,
            "hide_blacklisted": hide_blacklisted,
            "sort": sort,
            "dir": dir,
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )
    return templates.TemplateResponse("htmx/partials/vendors/list.html", ctx)


@router.get("/v2/partials/vendors/{vendor_id}", response_class=HTMLResponse)
async def vendor_detail_partial(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return vendor detail as HTML partial with safety data and tabs."""
    vendor = db.query(VendorCard).filter(VendorCard.id == vendor_id).first()
    if not vendor:
        raise HTTPException(404, "Vendor not found")

    contacts = (
        db.query(VendorContact)
        .filter(VendorContact.vendor_card_id == vendor_id)
        .order_by(VendorContact.interaction_count.desc().nullslast())
        .limit(20)
        .all()
    )

    recent_sightings = (
        db.query(Sighting)
        .filter(Sighting.vendor_name_normalized == vendor.normalized_name)
        .order_by(Sighting.created_at.desc().nullslast())
        .limit(10)
        .all()
    )

    # Load safety data from most recent SourcingLead
    safety_band = None
    safety_summary = None
    safety_flags = None
    safety_score = None
    try:
        lead = (
            db.query(SourcingLead)
            .filter(SourcingLead.vendor_name_normalized == vendor.normalized_name)
            .order_by(SourcingLead.created_at.desc())
            .first()
        )
        if lead:
            safety_band = lead.vendor_safety_band
            safety_summary = lead.vendor_safety_summary
            safety_flags = lead.vendor_safety_flags
            safety_score = lead.vendor_safety_score
    except (SQLAlchemyError, ValueError) as exc:
        logger.warning("Failed to load SourcingLead for vendor {}: {}", vendor.normalized_name, exc)

    ctx = _base_ctx(request, user, "vendors")
    ctx.update(
        {
            "vendor": vendor,
            "contacts": contacts,
            "recent_sightings": recent_sightings,
            "safety_band": safety_band,
            "safety_summary": safety_summary,
            "safety_flags": safety_flags or [],
            "safety_score": safety_score,
        }
    )
    return templates.TemplateResponse("htmx/partials/vendors/detail.html", ctx)


@router.get("/v2/partials/vendors/{vendor_id}/tab/{tab}", response_class=HTMLResponse)
async def vendor_tab(
    request: Request,
    vendor_id: int,
    tab: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return a specific tab partial for vendor detail."""
    vendor = db.query(VendorCard).filter(VendorCard.id == vendor_id).first()
    if not vendor:
        raise HTTPException(404, "Vendor not found")

    valid_tabs = {"overview", "contacts", "analytics", "offers"}
    if tab not in valid_tabs:
        raise HTTPException(404, f"Unknown tab: {tab}")

    from ...services.vendor_detail_service import (
        get_vendor_contacts,
        get_vendor_offers,
        get_vendor_overview_data,
    )

    ctx = _base_ctx(request, user, "vendors")
    ctx["vendor"] = vendor

    if tab == "overview":
        ctx.update(get_vendor_overview_data(db, vendor))
        return templates.TemplateResponse("htmx/partials/vendors/overview_tab.html", ctx)

    elif tab == "contacts":
        ctx["contacts"] = get_vendor_contacts(db, vendor_id)
        ctx["vendor"] = vendor
        return templates.TemplateResponse("partials/vendors/tabs/contacts.html", ctx)

    elif tab == "analytics":
        html = f"""<div class="space-y-6">
          <div class="grid grid-cols-2 md:grid-cols-3 gap-4">
            <div class="bg-white rounded-lg border border-gray-200 p-4 text-center">
              <p class="text-2xl font-bold text-brand-500">{"{:.0f}%".format((vendor.overall_win_rate or 0) * 100)}</p>
              <p class="text-xs text-gray-500 mt-1">Win Rate</p>
            </div>
            <div class="bg-white rounded-lg border border-gray-200 p-4 text-center">
              <p class="text-2xl font-bold text-brand-500">{"{:.0f}%".format((vendor.response_rate or 0) * 100)}</p>
              <p class="text-xs text-gray-500 mt-1">Response Rate</p>
            </div>
            <div class="bg-white rounded-lg border border-gray-200 p-4 text-center">
              <p class="text-2xl font-bold text-brand-500">{"{:.0f}".format(vendor.vendor_score or 0)}</p>
              <p class="text-xs text-gray-500 mt-1">Vendor Score</p>
            </div>
            <div class="bg-white rounded-lg border border-gray-200 p-4 text-center">
              <p class="text-2xl font-bold text-gray-900">{vendor.sighting_count or 0}</p>
              <p class="text-xs text-gray-500 mt-1">Sightings</p>
            </div>
            <div class="bg-white rounded-lg border border-gray-200 p-4 text-center">
              <p class="text-2xl font-bold text-gray-900">{"{:.0f}".format(vendor.avg_response_hours or 0)}</p>
              <p class="text-xs text-gray-500 mt-1">Avg Response Hours</p>
            </div>
            <div class="bg-white rounded-lg border border-gray-200 p-4 text-center">
              <p class="text-2xl font-bold text-gray-900">{"{:.0f}".format(vendor.engagement_score or 0)}</p>
              <p class="text-xs text-gray-500 mt-1">Engagement Score</p>
            </div>
          </div>
          <p class="text-sm text-gray-500 text-center">Analytics data builds as you interact with this vendor.</p>
        </div>"""
        return HTMLResponse(html)

    else:  # offers
        offers = get_vendor_offers(db, vendor.display_name)
        rows = []
        for o in offers:
            price_str = f"${o.unit_price:,.4f}" if o.unit_price else "RFQ"
            date_str = o.created_at.strftime("%b %d, %Y") if o.created_at else _DASH
            qty_str = f"{o.qty_available:,}" if o.qty_available else _DASH
            rows.append(f"""<tr class="hover:bg-brand-50">
              <td class="px-4 py-2 text-sm font-mono text-gray-900">{o.mpn or _DASH}</td>
              <td class="px-4 py-2 text-sm text-gray-500 text-right">{qty_str}</td>
              <td class="px-4 py-2 text-sm text-right">{price_str}</td>
              <td class="px-4 py-2 text-sm text-gray-500">{o.lead_time or _DASH}</td>
              <td class="px-4 py-2 text-sm text-gray-500">{date_str}</td>
            </tr>""")
        if rows:
            html = f"""<div class="overflow-x-auto">
              <table class="min-w-full divide-y divide-gray-200">
                <thead class="bg-gray-50">
                  <tr>
                    <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">MPN</th>
                    <th class="px-4 py-2 text-right text-xs font-medium text-gray-500 uppercase">Qty</th>
                    <th class="px-4 py-2 text-right text-xs font-medium text-gray-500 uppercase">Price</th>
                    <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Lead Time</th>
                    <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Date</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-gray-200">{"".join(rows)}</tbody>
              </table>
            </div>"""
        else:
            html = '<div class="p-8 text-center"><p class="text-sm text-gray-500">No offers from this vendor yet.</p></div>'
        return HTMLResponse(html)
