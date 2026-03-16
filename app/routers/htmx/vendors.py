"""
routers/htmx/vendors.py — Vendor list, detail, tab partials, and CRUD.

Handles all /partials/vendors/ routes: vendor listing with search/sort,
vendor detail with safety data, per-vendor tab content (overview, contacts,
analytics, offers), inline edit form, update, blacklist toggle, delete,
and typeahead search.

Called by: htmx router package (__init__.py includes this module's router)
Depends on: _helpers (router, templates, _base_ctx, _DASH, escape_like),
            models (VendorCard, VendorContact, Sighting, SourcingLead, User, Offer),
            dependencies (require_user, require_admin), database (get_db)
"""

from fastapi import Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from loguru import logger
from sqlalchemy import cast, or_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.types import String

from ...database import get_db
from ...dependencies import require_admin, require_user
from ...models import Sighting, SourcingLead, User, VendorCard
from ...models.vendors import VendorContact
from ._helpers import _DASH, _base_ctx, escape_like, router, templates


@router.get("/partials/vendors", response_class=HTMLResponse)
async def vendors_list_partial(
    request: Request,
    q: str = "",
    hide_blacklisted: bool = True,
    sort: str = "sighting_count",
    dir: str = "desc",
    view: str = "table",
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
        query = query.filter(
            or_(
                VendorCard.display_name.ilike(f"%{safe}%"),
                VendorCard.domain.ilike(f"%{safe}%"),
                cast(VendorCard.brand_tags, String).ilike(f"%{safe}%"),
                cast(VendorCard.commodity_tags, String).ilike(f"%{safe}%"),
                VendorCard.industry.ilike(f"%{safe}%"),
            )
        )

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

    # Build strategic claim map for card view badges
    claim_map = {}
    if view == "cards" and vendors:
        try:
            from ...models.strategic import StrategicVendor
            vendor_ids = [v.id for v in vendors]
            claims = (
                db.query(StrategicVendor, User.name)
                .join(User, StrategicVendor.user_id == User.id)
                .filter(
                    StrategicVendor.vendor_card_id.in_(vendor_ids),
                    StrategicVendor.released_at.is_(None),
                )
                .all()
            )
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            for sv, user_name in claims:
                days_left = (sv.expires_at - now).days if sv.expires_at else 0
                claim_map[sv.vendor_card_id] = {
                    "user_name": user_name,
                    "days_left": max(0, days_left),
                }
        except Exception as exc:
            logger.warning("Failed to load strategic claims: {}", exc)

    ctx = _base_ctx(request, user, "vendors")
    ctx.update(
        {
            "vendors": vendors,
            "q": q,
            "hide_blacklisted": hide_blacklisted,
            "sort": sort,
            "dir": dir,
            "view": view,
            "total": total,
            "limit": limit,
            "offset": offset,
            "claim_map": claim_map,
        }
    )
    return templates.TemplateResponse("htmx/partials/vendors/list.html", ctx)


@router.get("/partials/vendors/typeahead")
async def vendor_typeahead(
    q: str = Query("", min_length=2),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return JSON list of vendor name matches for typeahead autocomplete.

    Searches display_name and normalized_name, returns up to 15 results
    sorted by sighting_count descending.
    """
    safe = escape_like(q.strip())
    vendors = (
        db.query(VendorCard)
        .filter(
            VendorCard.display_name.ilike(f"%{safe}%")
            | VendorCard.normalized_name.ilike(f"%{safe}%")
        )
        .order_by(VendorCard.sighting_count.desc().nullslast())
        .limit(15)
        .all()
    )
    results = [
        {"id": v.id, "name": v.display_name, "type": "vendor"}
        for v in vendors
    ]
    return JSONResponse(results)


@router.get("/partials/vendors/{vendor_id}", response_class=HTMLResponse)
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


@router.get("/partials/vendors/{vendor_id}/tab/{tab}", response_class=HTMLResponse)
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


# ── Vendor CRUD endpoints ────────────────────────────────────────────


@router.get("/partials/vendors/{vendor_id}/edit", response_class=HTMLResponse)
async def vendor_edit_form(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return an inline edit form pre-filled with vendor data.

    The form uses hx-put to submit updates back to the vendor detail endpoint.
    """
    vendor = db.query(VendorCard).filter(VendorCard.id == vendor_id).first()
    if not vendor:
        raise HTTPException(404, "Vendor not found")

    emails_str = ", ".join(vendor.emails) if vendor.emails else ""
    phones_str = ", ".join(vendor.phones) if vendor.phones else ""

    html = f"""<form hx-put="/partials/vendors/{vendor.id}" hx-target="#main-content" class="space-y-4 p-6 bg-white rounded-lg border border-gray-200">
  <h3 class="text-lg font-semibold text-gray-900">Edit Vendor</h3>
  <div>
    <label class="block text-sm font-medium text-gray-700 mb-1">Display Name</label>
    <input type="text" name="display_name" value="{vendor.display_name or ""}"
           class="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-brand-500 focus:ring-brand-500" />
  </div>
  <div>
    <label class="block text-sm font-medium text-gray-700 mb-1">Website</label>
    <input type="text" name="website" value="{vendor.website or ""}"
           class="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-brand-500 focus:ring-brand-500" />
  </div>
  <div>
    <label class="block text-sm font-medium text-gray-700 mb-1">Emails (comma-separated)</label>
    <input type="text" name="emails" value="{emails_str}"
           class="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-brand-500 focus:ring-brand-500" />
  </div>
  <div>
    <label class="block text-sm font-medium text-gray-700 mb-1">Phones (comma-separated)</label>
    <input type="text" name="phones" value="{phones_str}"
           class="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-brand-500 focus:ring-brand-500" />
  </div>
  <div class="flex gap-3">
    <button type="submit"
            class="px-4 py-2 bg-brand-600 text-white text-sm font-medium rounded-md hover:bg-brand-700">
      Save Changes
    </button>
    <button type="button"
            hx-get="/partials/vendors/{vendor.id}" hx-target="#main-content"
            class="px-4 py-2 bg-gray-100 text-gray-700 text-sm font-medium rounded-md hover:bg-gray-200">
      Cancel
    </button>
  </div>
</form>"""
    return HTMLResponse(html)


@router.put("/partials/vendors/{vendor_id}", response_class=HTMLResponse)
async def vendor_update(
    request: Request,
    vendor_id: int,
    display_name: str = Form(""),
    website: str = Form(""),
    emails: str = Form(""),
    phones: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Update a vendor's editable fields and redirect to the detail view.

    Cleans email and phone lists: strips whitespace, lowercases emails,
    removes empty entries, and deduplicates.
    """
    vendor = db.query(VendorCard).filter(VendorCard.id == vendor_id).first()
    if not vendor:
        raise HTTPException(404, "Vendor not found")

    # Clean emails: strip, lowercase, filter empty, deduplicate
    clean_emails = list(
        dict.fromkeys(
            e.strip().lower() for e in emails.split(",") if e.strip()
        )
    )

    # Clean phones: strip, filter empty, deduplicate
    clean_phones = list(
        dict.fromkeys(
            p.strip() for p in phones.split(",") if p.strip()
        )
    )

    vendor.display_name = display_name.strip() or vendor.display_name
    vendor.website = website.strip() or None
    vendor.emails = clean_emails
    vendor.phones = clean_phones

    try:
        db.commit()
        logger.info("Vendor {} updated by {}", vendor_id, user.email)
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("Failed to update vendor {}: {}", vendor_id, exc)
        raise HTTPException(500, "Failed to update vendor")

    response = HTMLResponse("")
    response.headers["HX-Redirect"] = f"/partials/vendors/{vendor_id}"
    return response


@router.post("/partials/vendors/{vendor_id}/blacklist", response_class=HTMLResponse)
async def vendor_toggle_blacklist(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Toggle the is_blacklisted flag on a vendor and return an updated badge.

    Returns a small HTML snippet showing the new blacklist status.
    Sets HX-Trigger header so the page can optionally refresh related elements.
    """
    vendor = db.query(VendorCard).filter(VendorCard.id == vendor_id).first()
    if not vendor:
        raise HTTPException(404, "Vendor not found")

    vendor.is_blacklisted = not vendor.is_blacklisted

    try:
        db.commit()
        logger.info(
            "Vendor {} blacklist toggled to {} by {}",
            vendor_id,
            vendor.is_blacklisted,
            user.email,
        )
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("Failed to toggle blacklist for vendor {}: {}", vendor_id, exc)
        raise HTTPException(500, "Failed to update blacklist status")

    if vendor.is_blacklisted:
        badge = (
            '<span class="inline-flex items-center px-2.5 py-0.5 rounded-full '
            'text-xs font-medium bg-red-100 text-red-800">Blacklisted</span>'
        )
    else:
        badge = (
            '<span class="inline-flex items-center px-2.5 py-0.5 rounded-full '
            'text-xs font-medium bg-green-100 text-green-800">Active</span>'
        )

    response = HTMLResponse(badge)
    response.headers["HX-Trigger"] = "vendorStatusChanged"
    return response


@router.delete("/partials/vendors/{vendor_id}")
async def vendor_delete(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Delete a vendor card (admin only). Redirects to the vendor list via HX-Redirect."""
    vendor = db.query(VendorCard).filter(VendorCard.id == vendor_id).first()
    if not vendor:
        raise HTTPException(404, "Vendor not found")

    try:
        db.delete(vendor)
        db.commit()
        logger.info("Vendor {} deleted by admin {}", vendor_id, user.email)
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("Failed to delete vendor {}: {}", vendor_id, exc)
        raise HTTPException(500, "Failed to delete vendor")

    response = HTMLResponse("")
    response.headers["HX-Redirect"] = "/partials/vendors"
    return response


# ── Vendor Contact CRUD endpoints ────────────────────────────────────


@router.get("/partials/vendors/{vendor_id}/contacts/add-form", response_class=HTMLResponse)
async def vendor_contact_add_form(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return an inline HTML form for adding a new contact to a vendor."""
    vendor = db.query(VendorCard).filter(VendorCard.id == vendor_id).first()
    if not vendor:
        raise HTTPException(404, "Vendor not found")

    html = f"""<form hx-post="/partials/vendors/{vendor_id}/contacts"
      hx-target="#contacts-tab-content" hx-swap="innerHTML"
      class="space-y-4 p-6 bg-white rounded-lg border border-gray-200">
  <h3 class="text-lg font-semibold text-gray-900">Add Contact</h3>
  <div>
    <label class="block text-sm font-medium text-gray-700 mb-1">Full Name *</label>
    <input type="text" name="full_name" required
           class="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-brand-500 focus:ring-brand-500" />
  </div>
  <div>
    <label class="block text-sm font-medium text-gray-700 mb-1">Email</label>
    <input type="email" name="email"
           class="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-brand-500 focus:ring-brand-500" />
  </div>
  <div>
    <label class="block text-sm font-medium text-gray-700 mb-1">Phone</label>
    <input type="text" name="phone"
           class="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-brand-500 focus:ring-brand-500" />
  </div>
  <div>
    <label class="block text-sm font-medium text-gray-700 mb-1">Title</label>
    <input type="text" name="title"
           class="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-brand-500 focus:ring-brand-500" />
  </div>
  <div>
    <label class="block text-sm font-medium text-gray-700 mb-1">Label</label>
    <input type="text" name="label" placeholder="e.g. Sales, Purchasing"
           class="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-brand-500 focus:ring-brand-500" />
  </div>
  <div class="flex gap-3">
    <button type="submit"
            class="px-4 py-2 bg-brand-600 text-white text-sm font-medium rounded-md hover:bg-brand-700">
      Add Contact
    </button>
    <button type="button"
            hx-get="/partials/vendors/{vendor_id}/tab/contacts" hx-target="#contacts-tab-content"
            class="px-4 py-2 bg-gray-100 text-gray-700 text-sm font-medium rounded-md hover:bg-gray-200">
      Cancel
    </button>
  </div>
</form>"""
    return HTMLResponse(html)


@router.post("/partials/vendors/{vendor_id}/contacts", response_class=HTMLResponse)
async def vendor_contact_create(
    request: Request,
    vendor_id: int,
    full_name: str = Form(...),
    email: str = Form(""),
    phone: str = Form(""),
    title: str = Form(""),
    label: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a new vendor contact with source=manual, confidence=100, is_verified=True.

    Deduplicates by email. Syncs email into the vendor card's emails list.
    """
    vendor = db.query(VendorCard).filter(VendorCard.id == vendor_id).first()
    if not vendor:
        raise HTTPException(404, "Vendor not found")

    clean_email = email.strip().lower() if email else ""
    clean_name = full_name.strip()

    if not clean_name:
        error_html = (
            '<div class="p-3 bg-red-50 border border-red-200 rounded-md text-sm text-red-700">'
            "Full name is required.</div>"
        )
        return HTMLResponse(error_html, status_code=422)

    if clean_email:
        existing = (
            db.query(VendorContact)
            .filter(
                VendorContact.vendor_card_id == vendor_id,
                VendorContact.email == clean_email,
            )
            .first()
        )
        if existing:
            error_html = (
                '<div class="p-3 bg-red-50 border border-red-200 rounded-md text-sm text-red-700">'
                f"A contact with email {clean_email} already exists for this vendor.</div>"
            )
            return HTMLResponse(error_html, status_code=409)

    contact = VendorContact(
        vendor_card_id=vendor_id,
        full_name=clean_name,
        email=clean_email or None,
        phone=phone.strip() or None,
        title=title.strip() or None,
        label=label.strip() or None,
        source="manual",
        confidence=100,
        is_verified=True,
    )
    db.add(contact)

    if clean_email and vendor.emails is not None and clean_email not in vendor.emails:
        vendor.emails = list(vendor.emails) + [clean_email]
    elif clean_email and vendor.emails is None:
        vendor.emails = [clean_email]

    try:
        db.commit()
        logger.info(
            "Contact '{}' created for vendor {} by {}",
            clean_name,
            vendor_id,
            user.email,
        )
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("Failed to create contact for vendor {}: {}", vendor_id, exc)
        raise HTTPException(500, "Failed to create contact")

    success_html = (
        '<div class="p-3 bg-green-50 border border-green-200 rounded-md text-sm text-green-700">'
        f"Contact {clean_name} added successfully.</div>"
    )
    response = HTMLResponse(success_html)
    response.headers["HX-Trigger"] = "refreshContacts"
    return response


@router.get("/partials/vendors/{vendor_id}/contacts/{contact_id}/edit", response_class=HTMLResponse)
async def vendor_contact_edit_form(
    request: Request,
    vendor_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return an inline edit form pre-filled with a contact's current values."""
    contact = (
        db.query(VendorContact)
        .filter(
            VendorContact.id == contact_id,
            VendorContact.vendor_card_id == vendor_id,
        )
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")

    html = f"""<form hx-put="/partials/vendors/{vendor_id}/contacts/{contact_id}"
      hx-target="#contact-row-{contact_id}" hx-swap="outerHTML"
      class="space-y-4 p-6 bg-white rounded-lg border border-gray-200">
  <h3 class="text-lg font-semibold text-gray-900">Edit Contact</h3>
  <div>
    <label class="block text-sm font-medium text-gray-700 mb-1">Full Name *</label>
    <input type="text" name="full_name" value="{contact.full_name or ""}" required
           class="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-brand-500 focus:ring-brand-500" />
  </div>
  <div>
    <label class="block text-sm font-medium text-gray-700 mb-1">Email</label>
    <input type="email" name="email" value="{contact.email or ""}"
           class="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-brand-500 focus:ring-brand-500" />
  </div>
  <div>
    <label class="block text-sm font-medium text-gray-700 mb-1">Phone</label>
    <input type="text" name="phone" value="{contact.phone or ""}"
           class="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-brand-500 focus:ring-brand-500" />
  </div>
  <div>
    <label class="block text-sm font-medium text-gray-700 mb-1">Title</label>
    <input type="text" name="title" value="{contact.title or ""}"
           class="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-brand-500 focus:ring-brand-500" />
  </div>
  <div>
    <label class="block text-sm font-medium text-gray-700 mb-1">Label</label>
    <input type="text" name="label" value="{contact.label or ""}"
           class="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-brand-500 focus:ring-brand-500" />
  </div>
  <div class="flex gap-3">
    <button type="submit"
            class="px-4 py-2 bg-brand-600 text-white text-sm font-medium rounded-md hover:bg-brand-700">
      Save Changes
    </button>
    <button type="button"
            hx-get="/partials/vendors/{vendor_id}/tab/contacts" hx-target="#contacts-tab-content"
            class="px-4 py-2 bg-gray-100 text-gray-700 text-sm font-medium rounded-md hover:bg-gray-200">
      Cancel
    </button>
  </div>
</form>"""
    return HTMLResponse(html)


@router.put("/partials/vendors/{vendor_id}/contacts/{contact_id}", response_class=HTMLResponse)
async def vendor_contact_update(
    request: Request,
    vendor_id: int,
    contact_id: int,
    full_name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    title: str = Form(""),
    label: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Update a vendor contact's fields. Deduplicates email and syncs to vendor card."""
    contact = (
        db.query(VendorContact)
        .filter(
            VendorContact.id == contact_id,
            VendorContact.vendor_card_id == vendor_id,
        )
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")

    vendor = db.query(VendorCard).filter(VendorCard.id == vendor_id).first()
    if not vendor:
        raise HTTPException(404, "Vendor not found")

    clean_email = email.strip().lower() if email else ""
    old_email = contact.email

    if clean_email and clean_email != old_email:
        existing = (
            db.query(VendorContact)
            .filter(
                VendorContact.vendor_card_id == vendor_id,
                VendorContact.email == clean_email,
                VendorContact.id != contact_id,
            )
            .first()
        )
        if existing:
            error_html = (
                '<div class="p-3 bg-red-50 border border-red-200 rounded-md text-sm text-red-700">'
                f"A contact with email {clean_email} already exists for this vendor.</div>"
            )
            return HTMLResponse(error_html, status_code=409)

    contact.full_name = full_name.strip() or contact.full_name
    contact.email = clean_email or None
    contact.phone = phone.strip() or None
    contact.title = title.strip() or None
    contact.label = label.strip() or None

    if vendor.emails is None:
        vendor.emails = []
    emails_list = list(vendor.emails)
    if old_email and old_email in emails_list:
        emails_list.remove(old_email)
    if clean_email and clean_email not in emails_list:
        emails_list.append(clean_email)
    vendor.emails = emails_list

    try:
        db.commit()
        logger.info(
            "Contact {} updated for vendor {} by {}",
            contact_id,
            vendor_id,
            user.email,
        )
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("Failed to update contact {}: {}", contact_id, exc)
        raise HTTPException(500, "Failed to update contact")

    title_str = f" &middot; {contact.title}" if contact.title else ""
    email_str = f'<a href="mailto:{contact.email}" class="text-brand-600 hover:underline">{contact.email}</a>' if contact.email else ""
    phone_str = contact.phone or ""

    updated_html = f"""<tr id="contact-row-{contact.id}" class="hover:bg-brand-50">
  <td class="px-4 py-2 text-sm font-medium text-gray-900">{contact.full_name}{title_str}</td>
  <td class="px-4 py-2 text-sm">{email_str}</td>
  <td class="px-4 py-2 text-sm text-gray-500">{phone_str}</td>
  <td class="px-4 py-2 text-sm text-gray-500">{contact.label or ""}</td>
  <td class="px-4 py-2 text-sm">
    <button hx-get="/partials/vendors/{vendor_id}/contacts/{contact.id}/edit"
            hx-target="#contact-row-{contact.id}" hx-swap="outerHTML"
            class="text-brand-600 hover:text-brand-800 text-xs font-medium">Edit</button>
    <button hx-delete="/partials/vendors/{vendor_id}/contacts/{contact.id}"
            hx-confirm="Delete this contact?"
            class="text-red-600 hover:text-red-800 text-xs font-medium ml-2">Delete</button>
  </td>
</tr>"""
    return HTMLResponse(updated_html)


@router.delete("/partials/vendors/{vendor_id}/contacts/{contact_id}")
async def vendor_contact_delete(
    request: Request,
    vendor_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a vendor contact and remove its email from the vendor card's emails list."""
    contact = (
        db.query(VendorContact)
        .filter(
            VendorContact.id == contact_id,
            VendorContact.vendor_card_id == vendor_id,
        )
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")

    vendor = db.query(VendorCard).filter(VendorCard.id == vendor_id).first()
    if not vendor:
        raise HTTPException(404, "Vendor not found")

    contact_email = contact.email
    if contact_email and vendor.emails:
        emails_list = list(vendor.emails)
        if contact_email in emails_list:
            emails_list.remove(contact_email)
            vendor.emails = emails_list

    try:
        db.delete(contact)
        db.commit()
        logger.info(
            "Contact {} deleted from vendor {} by {}",
            contact_id,
            vendor_id,
            user.email,
        )
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("Failed to delete contact {}: {}", contact_id, exc)
        raise HTTPException(500, "Failed to delete contact")

    response = HTMLResponse("")
    response.headers["HX-Trigger"] = "refreshContacts"
    return response
