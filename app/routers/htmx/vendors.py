"""routers/htmx/vendors.py — Vendor & vendor-contact partial views (HTMX + Alpine).

Server-rendered HTML partials for the vendor CRM surface: the vendor list, the
global vendor-contacts list, vendor create/edit/delete, the vendor detail shell
and tabs, vendor-contact CRUD, vendor ownership (claim/release/badge), vendor
custom fields, vendor reviews/nudges, and the AI contact finder (find/save/
promote/delete prospect contacts). Extracted verbatim from htmx_views.py (same
``/v2/partials/vendors`` + ``/v2/partials/vendor-contacts`` paths, same
``htmx-views`` tag) as part of the CRM-cluster domain split.

Called by: app/main.py (router mount); htmx_views.py re-imports ``vendor_tab`` for
    its vendor activity add-note route.
Depends on: app.models, app.dependencies, app.database, app.services.crm_service,
    app.services.strategic_vendor_service, app.services.tagging, ._shared
"""

import html as html_mod  # aliased: vendor_tab binds a local `html` string var that would shadow a plain `import html`
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from ...constants import ActivityType
from ...database import get_db
from ...dependencies import require_admin, require_prospect_site_access, require_user
from ...models import Offer, Sighting, SourcingLead, User, VendorCard
from ...models.enrichment import ProspectContact
from ...models.vendors import VendorContact
from ...services.crm_service import cadence_state as _cadence_state
from ...services.crm_service import next_best_touch as _next_best_touch
from ...services.crm_service import order_by_clock as _order_by_clock
from ...template_env import template_response
from ...utils.search_builder import SearchBuilder
from ...utils.sql_helpers import escape_like
from .._lookup_helpers import get_vendor_card_or_404
from ._shared import _DASH, _base_ctx, _sanitize_hx_params

router = APIRouter(tags=["htmx-views"])


# ── Vendor partials ─────────────────────────────────────────────────────


@router.get("/v2/partials/vendors", response_class=HTMLResponse)
async def vendors_list_partial(
    request: Request,
    q: str = "",
    hide_blacklisted: bool = True,
    include_archived: bool = False,
    sort: str = "sighting_count",
    dir: str = "desc",
    my_only: bool = False,
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    hx_target: str = Query("#main-content", alias="hx_target"),
    push_url_base: str = Query("/v2/vendors", alias="push_url_base"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return vendor list as HTML partial with blacklisted toggle and sorting."""
    hx_target, push_url_base = _sanitize_hx_params(hx_target, push_url_base, "/v2/vendors")
    from ...models.strategic import StrategicVendor

    query = db.query(VendorCard)

    # Filter to user's strategic vendors if "My Vendors" tab is active
    if my_only:
        my_vendor_ids = (
            db.query(StrategicVendor.vendor_card_id)
            .filter(StrategicVendor.user_id == user.id, StrategicVendor.released_at.is_(None))
            .subquery()
        )
        query = query.filter(VendorCard.id.in_(my_vendor_ids))

    if hide_blacklisted:
        query = query.filter(VendorCard.is_blacklisted.is_(False))

    # Soft-archive: archived vendors are hidden from the default list (mirrors the
    # customer/company is_active archive). "Show archived" lifts the filter.
    if not include_archived:
        query = query.filter(VendorCard.is_active.is_(True))

    if q.strip():
        from sqlalchemy import Text, cast

        sb = SearchBuilder(q.strip())
        term = f"%{escape_like(q.strip())}%"
        query = query.filter(
            or_(
                sb.ilike_filter(VendorCard.display_name, VendorCard.domain),
                cast(VendorCard.brand_tags, Text).ilike(term, escape="\\"),
                cast(VendorCard.commodity_tags, Text).ilike(term, escape="\\"),
            )
        )

    total = query.count()

    # Sorting — outbound_asc uses the generalized order_by_clock (VendorCard clocks)
    now_utc = datetime.now(timezone.utc)
    if sort == "outbound_asc":
        vendors = _order_by_clock(query, "outbound", model=VendorCard).offset(offset).limit(limit).all()
    else:
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

    # Attach cadence_state to each vendor (tier=None → standard/30d target)
    for v in vendors:
        v.cadence_state = _cadence_state(None, v.last_outbound_at, now_utc)

    ctx = _base_ctx(request, user, "vendors")
    ctx.update(
        {
            "vendors": vendors,
            "q": q,
            "hide_blacklisted": hide_blacklisted,
            "include_archived": include_archived,
            "sort": sort,
            "dir": dir,
            "total": total,
            "limit": limit,
            "offset": offset,
            "my_only": my_only,
            "hx_target": hx_target,
            "push_url_base": push_url_base,
            "now_utc": now_utc,
        }
    )
    return template_response("htmx/partials/vendors/list.html", ctx)


# ── Global vendor-contacts list ────────────────────────────────────────────
# View-open (require_user) — vendor data is not tenant-scoped, mirroring the
# /api/vendor-contacts/bulk endpoint this surfaces. Search/sort/paginate over all
# structured VendorContacts (blacklisted vendors excluded, as in the bulk route).


@router.get("/v2/partials/vendor-contacts", response_class=HTMLResponse)
async def vendor_contacts_partial(
    request: Request,
    search: str = "",
    sort: str = "name",
    dir: str = "asc",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the global vendor-contacts list as an HTML partial."""
    from ...models import VendorCard

    query = (
        db.query(VendorContact)
        .join(VendorCard, VendorContact.vendor_card_id == VendorCard.id)
        .filter(VendorCard.is_blacklisted.is_(False), VendorCard.is_active.is_(True))
        .options(joinedload(VendorContact.vendor_card))
    )
    if search.strip():
        sb = SearchBuilder(search.strip())
        query = query.filter(
            or_(
                sb.ilike_filter(VendorContact.full_name, VendorContact.email),
                sb.ilike_filter(VendorCard.display_name),
            )
        )

    sort_col_map = {
        "name": VendorContact.full_name,
        "email": VendorContact.email,
        "vendor": VendorCard.display_name,
        "score": VendorContact.relationship_score,
    }
    sort_col = sort_col_map.get(sort, VendorContact.full_name)
    order = sort_col.desc().nullslast() if dir == "desc" else sort_col.asc().nullslast()

    total = query.count()
    contacts = query.order_by(order, VendorContact.id).offset(offset).limit(limit).all()

    ctx = _base_ctx(request, user, "crm")
    ctx.update(
        {
            "contacts": contacts,
            "search": search,
            "sort": sort,
            "dir": dir,
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )
    return template_response("htmx/partials/vendors/contacts_list.html", ctx)


@router.get("/v2/partials/vendors/create-form", response_class=HTMLResponse)
async def vendor_create_form_early(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the create-vendor form partial (early route to precede /{vendor_id})."""
    return template_response(
        "htmx/partials/vendors/create_form.html",
        {"request": request},
    )


@router.post("/v2/partials/vendors/create", response_class=HTMLResponse)
async def create_vendor_partial_early(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a new VendorCard from the HTMX form (early route to precede
    /{vendor_id})."""
    from ...models import VendorCard
    from ...utils.vendor_helpers import find_vendor_card_by_name
    from ...vendor_utils import normalize_vendor_name

    form = await request.form()
    display_name = form.get("display_name", "").strip()
    if not display_name:
        raise HTTPException(400, "Vendor name is required")

    norm = normalize_vendor_name(display_name)
    existing = find_vendor_card_by_name(display_name, db)
    if existing:
        raise HTTPException(409, f"Vendor '{existing.display_name}' already exists (ID {existing.id})")

    emails_raw = form.get("emails", "").strip()
    emails = [e.strip() for e in emails_raw.split(",") if e.strip() and "@" in e] if emails_raw else []
    phones_raw = form.get("phones", "").strip()
    phones = [p.strip() for p in phones_raw.split(",") if p.strip()] if phones_raw else []

    card = VendorCard(
        normalized_name=norm,
        display_name=display_name,
        website=form.get("website", "").strip() or None,
        emails=emails,
        phones=phones,
        industry=form.get("industry", "").strip() or None,
        hq_city=form.get("hq_city", "").strip() or None,
        hq_country=form.get("hq_country", "").strip() or None,
        employee_size=form.get("employee_size", "").strip() or None,
        source="manual",
        is_blacklisted=False,
        is_new_vendor=True,
        sighting_count=0,
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    logger.info("VendorCard {} created by {}", card.id, user.email)
    return await vendor_detail_partial(request=request, vendor_id=card.id, user=user, db=db)


@router.delete("/v2/partials/vendors/{vendor_id}", response_class=HTMLResponse)
async def delete_vendor_partial(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Delete a vendor (admin-only) and return the refreshed vendor list."""
    from ...models import Offer, VendorCard

    card = db.get(VendorCard, vendor_id)
    if not card:
        raise HTTPException(404, "Vendor not found")
    active_offers = db.query(Offer).filter(Offer.vendor_card_id == card.id).count()
    if active_offers > 0:
        raise HTTPException(
            400,
            f"Cannot delete vendor with {active_offers} active offers. Archive instead.",
        )
    db.delete(card)
    db.commit()
    logger.info("VendorCard {} deleted by {}", vendor_id, user.email)
    # Return the vendor list using safe defaults
    return await vendors_list_partial(
        request=request,
        q="",
        hide_blacklisted=True,
        include_archived=False,
        sort="sighting_count",
        dir="desc",
        my_only=False,
        limit=30,
        offset=0,
        hx_target="#main-content",
        push_url_base="/v2/vendors",
        user=user,
        db=db,
    )


@router.get("/v2/partials/vendors/{vendor_id}", response_class=HTMLResponse)
async def vendor_detail_partial(
    request: Request,
    vendor_id: int,
    mpn: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return vendor detail as HTML partial with safety data and tabs."""
    vendor = get_vendor_card_or_404(db, vendor_id)

    contacts = (
        db.query(VendorContact)
        .filter(VendorContact.vendor_card_id == vendor_id)
        .order_by(VendorContact.interaction_count.desc().nullslast())
        .limit(20)
        .all()
    )

    sightings_query = db.query(Sighting).filter(Sighting.vendor_name_normalized == vendor.normalized_name)
    if mpn.strip():
        from app.utils.normalization import normalize_mpn

        norm = normalize_mpn(mpn)
        if norm:
            sightings_query = sightings_query.filter(Sighting.normalized_mpn == norm)

    recent_sightings = sightings_query.order_by(Sighting.created_at.desc().nullslast()).limit(10).all()

    # Load safety data from most recent SourcingLead
    safety_band = None
    safety_summary = None
    safety_flags = None
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

    now_utc = datetime.now(timezone.utc)
    vendor_cadence = _cadence_state(None, vendor.last_outbound_at, now_utc)
    vendor_nbt = _next_best_touch(None, vendor.last_outbound_at, now_utc)

    ctx = _base_ctx(request, user, "vendors")
    ctx.update(
        {
            "vendor": vendor,
            "contacts": contacts,
            "recent_sightings": recent_sightings,
            "safety_band": safety_band,
            "safety_summary": safety_summary,
            "safety_flags": safety_flags,
            "safety_score": None,
            "safety_available": False,
            "mpn_filter": mpn.strip().upper() if mpn.strip() else None,
            "cadence_state": vendor_cadence,
            "next_best_touch": vendor_nbt,
            "now_utc": now_utc,
        }
    )
    return template_response("htmx/partials/vendors/detail.html", ctx)


@router.get("/v2/partials/vendors/{vendor_id}/tab/{tab}", response_class=HTMLResponse)
async def vendor_tab(
    request: Request,
    vendor_id: int,
    tab: str,
    mpn: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return a specific tab partial for vendor detail."""
    vendor = get_vendor_card_or_404(db, vendor_id)

    valid_tabs = {
        "overview",
        "contacts",
        "find_contacts",
        "emails",
        "analytics",
        "offers",
        "reviews",
        "activity",
        "tasks",
        "files",
    }
    if tab not in valid_tabs:
        raise HTTPException(404, f"Unknown tab: {tab}")

    ctx = _base_ctx(request, user, "vendors")
    ctx["vendor"] = vendor

    if tab == "overview":
        sightings_query = db.query(Sighting).filter(Sighting.vendor_name_normalized == vendor.normalized_name)
        if mpn.strip():
            from app.utils.normalization import normalize_mpn

            norm = normalize_mpn(mpn)
            if norm:
                sightings_query = sightings_query.filter(Sighting.normalized_mpn == norm)

        recent_sightings = sightings_query.order_by(Sighting.created_at.desc().nullslast()).limit(10).all()
        # Safety data
        safety_band = None
        safety_summary = None
        safety_flags = None
        safety_score = None
        safety_available = False
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
            safety_available = True
        contacts = (
            db.query(VendorContact)
            .filter(VendorContact.vendor_card_id == vendor_id)
            .order_by(VendorContact.interaction_count.desc().nullslast())
            .limit(20)
            .all()
        )
        ctx.update(
            {
                "recent_sightings": recent_sightings,
                "contacts": contacts,
                "safety_band": safety_band,
                "safety_summary": safety_summary,
                "safety_flags": safety_flags,
                "safety_score": safety_score,
                "safety_available": safety_available,
                "mpn_filter": mpn.strip().upper() if mpn.strip() else None,
            }
        )
        # Re-use the inline overview from the detail template
        # by rendering just the overview portion
        return template_response("htmx/partials/vendors/overview_tab.html", ctx)

    elif tab == "contacts":
        contacts = (
            db.query(VendorContact)
            .filter(VendorContact.vendor_card_id == vendor_id)
            .order_by(VendorContact.interaction_count.desc().nullslast())
            .limit(50)
            .all()
        )
        ctx["contacts"] = contacts
        ctx["vendor"] = vendor
        return template_response("htmx/partials/vendors/tabs/contacts.html", ctx)

    elif tab == "find_contacts":
        prospects = (
            db.query(ProspectContact)
            .filter(ProspectContact.vendor_card_id == vendor_id)
            .order_by(ProspectContact.created_at.desc())
            .limit(50)
            .all()
        )
        ctx["prospects"] = prospects
        return template_response("htmx/partials/vendors/find_contacts_tab.html", ctx)

    elif tab == "emails":
        from ...models.offers import Contact as RfqContact
        from ...models.offers import VendorResponse

        norm = (vendor.normalized_name or "").lower().strip()
        contacts = (
            (
                db.query(RfqContact)
                .filter(RfqContact.vendor_name_normalized == norm)
                .order_by(RfqContact.created_at.desc())
                .limit(100)
                .all()
            )
            if norm
            else []
        )
        responses = (
            (
                db.query(VendorResponse)
                .filter(sqlfunc.lower(VendorResponse.vendor_name) == norm)
                .order_by(VendorResponse.received_at.desc().nullslast())
                .limit(100)
                .all()
            )
            if norm
            else []
        )
        ctx = _base_ctx(request, user, "vendors")
        ctx.update({"vendor": vendor, "contacts": contacts, "responses": responses})
        return template_response("htmx/partials/vendors/emails_tab.html", ctx)

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

    elif tab == "reviews":
        return await vendor_reviews(request=request, vendor_id=vendor_id, user=user, db=db)

    elif tab == "activity":
        from ...models.intelligence import ActivityLog as _ActivityLog

        activities = (
            db.query(_ActivityLog)
            .filter(_ActivityLog.vendor_card_id == vendor_id)
            .order_by(_ActivityLog.created_at.desc())
            .limit(50)
            .all()
        )
        activities_truncated = len(activities) >= 50

        # Bucket activities into type-sections (the template renders by section), mirroring
        # the account Activity tab. Vendors have no RFQ-contact merge (account-only), so
        # this is a straight type bucketing of the vendor's ActivityLog rows.
        _CALLS = frozenset({ActivityType.CALL_LOGGED})
        _EMAILS = frozenset({ActivityType.EMAIL_SENT, ActivityType.EMAIL_RECEIVED})
        _MEETINGS = frozenset({ActivityType.TEAMS_MESSAGE, ActivityType.WECHAT_MESSAGE, ActivityType.MEETING})
        _NOTES = frozenset({ActivityType.NOTE, ActivityType.SALES_NOTE, ActivityType.CONTACT_NOTE})

        sections: dict[str, list] = {"Calls": [], "Emails": [], "Meetings": [], "Notes": [], "Other": []}
        for a in activities:
            at = a.activity_type
            if at in _CALLS:
                sections["Calls"].append(a)
            elif at in _EMAILS:
                sections["Emails"].append(a)
            elif at in _MEETINGS:
                sections["Meetings"].append(a)
            elif at in _NOTES:
                sections["Notes"].append(a)
            else:
                sections["Other"].append(a)

        # has_any_activity: drives empty-state vs. sections in the template
        has_any_activity = bool(activities)

        ctx = _base_ctx(request, user, "vendors")
        ctx.update(
            {
                "vendor": vendor,
                "activities": activities,
                "sections": sections,
                "activities_truncated": activities_truncated,
                "has_any_activity": has_any_activity,
            }
        )
        return template_response("htmx/partials/vendors/tabs/activity_tab.html", ctx)

    elif tab == "tasks":
        from app.services.task_service import get_open_tasks_for_vendor_card

        vendor_tasks = get_open_tasks_for_vendor_card(db, vendor_id)
        ctx = _base_ctx(request, user, "vendors")
        ctx["vendor"] = vendor
        ctx["vendor_id"] = vendor_id
        ctx["vendor_tasks"] = vendor_tasks
        return template_response("htmx/partials/vendors/tabs/_vendor_tasks.html", ctx)

    elif tab == "files":
        ctx = _base_ctx(request, user, "vendors")
        ctx["vendor"] = vendor
        return template_response("htmx/partials/vendors/tabs/files_tab.html", ctx)

    else:  # offers
        offers = (
            db.query(Offer)
            .filter(Offer.vendor_name_normalized == vendor.normalized_name)
            .order_by(Offer.created_at.desc().nullslast())
            .limit(50)
            .all()
        )
        rows = []
        for o in offers:
            price_str = f"${o.unit_price:,.4f}" if o.unit_price else "RFQ"
            date_str = o.created_at.strftime("%b %d, %Y") if o.created_at else _DASH
            qty_str = f"{o.qty_available:,}" if o.qty_available else _DASH
            rows.append(f"""<tr class="hover:bg-brand-50">
              <td class="px-4 py-2 text-sm font-mono text-gray-900">{html_mod.escape(o.mpn or _DASH)}</td>
              <td class="px-4 py-2 text-sm text-gray-500 text-right">{qty_str}</td>
              <td class="px-4 py-2 text-sm text-right">{price_str}</td>
              <td class="px-4 py-2 text-sm text-gray-500">{html_mod.escape(o.lead_time or _DASH)}</td>
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


# ── Sprint 3: Vendor CRUD + Contact Management ────────────────────────


@router.get("/v2/partials/vendors/{vendor_id}/edit-form", response_class=HTMLResponse)
async def vendor_edit_form(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return inline edit form for vendor header fields."""
    vendor = get_vendor_card_or_404(db, vendor_id)
    return template_response(
        "htmx/partials/vendors/edit_vendor_form.html",
        {"request": request, "vendor": vendor},
    )


@router.post("/v2/partials/vendors/{vendor_id}/edit", response_class=HTMLResponse)
async def edit_vendor(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save vendor edits and return refreshed vendor detail."""
    vendor = get_vendor_card_or_404(db, vendor_id)

    form = await request.form()
    # VendorCard.display_name is NOT NULL. The edit form always submits it (required
    # input), so a submitted-but-blank value means the user cleared a required field —
    # reject it. A field that is ABSENT entirely is a partial edit (website/emails-only),
    # which must leave the existing name untouched.
    display_name_raw = form.get("display_name")
    if display_name_raw is not None:
        display_name = display_name_raw.strip()
        if not display_name:
            raise HTTPException(400, "Vendor name is required.")
        vendor.display_name = display_name
        from ...vendor_utils import normalize_vendor_name

        vendor.normalized_name = normalize_vendor_name(display_name)

    website = form.get("website", "").strip()
    vendor.website = website or vendor.website

    emails_raw = form.get("emails", "").strip()
    if emails_raw:
        emails = [e.strip() for e in emails_raw.split(",") if e.strip()]
        # Reject anything that isn't a plausible address — an entry without an
        # '@' is a data-entry mistake, not a contactable email.
        invalid = [e for e in emails if "@" not in e]
        if invalid:
            raise HTTPException(400, f"Invalid email address: {', '.join(invalid)}")
        vendor.emails = emails

    phones_raw = form.get("phones", "").strip()
    if phones_raw:
        vendor.phones = [p.strip() for p in phones_raw.split(",") if p.strip()]

    vendor.updated_at = datetime.now(timezone.utc)
    db.commit()
    logger.info("Vendor {} edited by {}", vendor_id, user.email)

    return await vendor_detail_partial(request=request, vendor_id=vendor_id, user=user, db=db)


@router.post("/v2/partials/vendors/{vendor_id}/toggle-blacklist", response_class=HTMLResponse)
async def toggle_vendor_blacklist(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Toggle blacklist status and return refreshed vendor detail."""
    vendor = get_vendor_card_or_404(db, vendor_id)

    vendor.is_blacklisted = not vendor.is_blacklisted
    vendor.updated_at = datetime.now(timezone.utc)
    db.commit()
    status = "blacklisted" if vendor.is_blacklisted else "un-blacklisted"
    logger.info("Vendor {} {} by {}", vendor_id, status, user.email)

    return await vendor_detail_partial(request=request, vendor_id=vendor_id, user=user, db=db)


@router.post("/v2/partials/vendors/{vendor_id}/archive", response_class=HTMLResponse)
async def archive_vendor(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Soft-archive a vendor — sets is_active=False; never deletes.

    Mirrors the customer/company archive (deactivate). Archived vendors drop out of the
    default vendor list/search; "Show archived" surfaces them again. require_user gate
    matches the vendor blacklist toggle (vendor data is not tenant-scoped).
    """
    vendor = get_vendor_card_or_404(db, vendor_id)
    vendor.is_active = False
    vendor.updated_at = datetime.now(timezone.utc)
    db.commit()
    logger.info("Vendor {} archived by {}", vendor_id, user.email)
    return await vendor_detail_partial(request=request, vendor_id=vendor_id, user=user, db=db)


@router.post("/v2/partials/vendors/{vendor_id}/unarchive", response_class=HTMLResponse)
async def unarchive_vendor(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Restore a soft-archived vendor — sets is_active=True.

    Mirrors company reactivate.
    """
    vendor = get_vendor_card_or_404(db, vendor_id)
    vendor.is_active = True
    vendor.updated_at = datetime.now(timezone.utc)
    db.commit()
    logger.info("Vendor {} unarchived by {}", vendor_id, user.email)
    return await vendor_detail_partial(request=request, vendor_id=vendor_id, user=user, db=db)


@router.get("/v2/partials/vendors/{vendor_id}/contacts/{contact_id}/timeline", response_class=HTMLResponse)
async def contact_timeline(
    request: Request,
    vendor_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return activity timeline for a vendor contact."""
    from ...models.intelligence import ActivityLog

    contact = (
        db.query(VendorContact)
        .filter(VendorContact.id == contact_id, VendorContact.vendor_card_id == vendor_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")

    activities = (
        (
            db.query(ActivityLog)
            .filter(ActivityLog.contact_email == contact.email)
            .order_by(ActivityLog.created_at.desc())
            .limit(20)
            .all()
        )
        if contact.email
        else []
    )

    return template_response(
        "htmx/partials/vendors/contact_timeline.html",
        {"request": request, "contact": contact, "activities": activities, "vendor_id": vendor_id},
    )


# ── Vendor Contact CRUD (HTMX, parity P1) ──────────────────────────────────


def _render_vendor_contacts(request: Request, vendor, contacts, user):
    """Re-render vendor contacts tab partial."""
    return template_response(
        "htmx/partials/vendors/tabs/contacts.html",
        {"request": request, "vendor": vendor, "contacts": contacts, "user": user},
    )


def _render_contact_row(request: Request, c, vendor):
    """Re-render a single vendor contact row partial."""
    return template_response(
        "htmx/partials/vendors/tabs/contact_row.html",
        {"request": request, "c": c, "vendor": vendor},
    )


@router.post("/v2/partials/vendors/{vendor_id}/contacts", response_class=HTMLResponse)
async def vendor_contact_add(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add a vendor contact (HTMX).

    require_user gate — mirrors vendor edit.
    """
    from ...models.vendors import VendorContact as VC

    vendor = get_vendor_card_or_404(db, vendor_id)
    form = await request.form()
    email = (form.get("email") or "").strip()
    if not email:
        raise HTTPException(400, "email is required")
    full_name = (form.get("full_name") or "").strip()
    title = (form.get("title") or "").strip()
    phone = (form.get("phone") or "").strip()

    # Deduplicate by (vendor_card_id, email)
    existing = db.query(VC).filter(VC.vendor_card_id == vendor_id, VC.email == email).first()
    if existing:
        raise HTTPException(409, "A contact with that email already exists")

    vc = VC(
        vendor_card_id=vendor_id,
        email=email,
        full_name=full_name or None,
        title=title or None,
        phone=phone or None,
        contact_type="individual" if full_name else "company",
        source="manual",
        is_verified=True,
        confidence=100,
        is_primary=False,
    )
    db.add(vc)
    db.commit()
    db.refresh(vc)
    logger.info("VendorContact {} added to vendor {} by {}", vc.id, vendor_id, user.email)
    return _render_contact_row(request, vc, vendor)


@router.put("/v2/partials/vendors/{vendor_id}/contacts/{contact_id}", response_class=HTMLResponse)
async def vendor_contact_edit(
    request: Request,
    vendor_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Edit a vendor contact (HTMX).

    require_user gate.
    """
    from ...models.vendors import VendorContact as VC

    vendor = get_vendor_card_or_404(db, vendor_id)
    vc = db.query(VC).filter(VC.id == contact_id, VC.vendor_card_id == vendor_id).first()
    if not vc:
        raise HTTPException(404, "Contact not found")

    form = await request.form()
    full_name = (form.get("full_name") or "").strip()
    title = (form.get("title") or "").strip()
    email = (form.get("email") or "").strip()
    phone = (form.get("phone") or "").strip()

    if full_name:
        vc.full_name = full_name
        vc.contact_type = "individual"
    if title:
        vc.title = title
    if email and email != vc.email:
        collision = db.query(VC).filter(VC.vendor_card_id == vendor_id, VC.email == email, VC.id != contact_id).first()
        if collision:
            raise HTTPException(409, "Another contact already has that email")
        vc.email = email
    if phone:
        vc.phone = phone

    db.commit()
    db.refresh(vc)
    logger.info("VendorContact {} updated by {}", contact_id, user.email)
    return _render_contact_row(request, vc, vendor)


@router.delete("/v2/partials/vendors/{vendor_id}/contacts/{contact_id}", response_class=HTMLResponse)
async def vendor_contact_delete(
    request: Request,
    vendor_id: int,
    contact_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Delete a vendor contact (HTMX).

    require_admin gate — matches vendor delete auth.
    """
    from ...models.vendors import VendorContact as VC

    get_vendor_card_or_404(db, vendor_id)
    vc = db.query(VC).filter(VC.id == contact_id, VC.vendor_card_id == vendor_id).first()
    if not vc:
        raise HTTPException(404, "Contact not found")

    db.delete(vc)
    db.commit()
    logger.info("VendorContact {} deleted by {}", contact_id, user.email)
    return HTMLResponse("")  # HTMX deletes the row via hx-swap="outerHTML"


@router.post(
    "/v2/partials/vendors/{vendor_id}/contacts/{contact_id}/set-primary",
    response_class=HTMLResponse,
)
async def vendor_contact_set_primary(
    request: Request,
    vendor_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Mark a contact as primary; clears is_primary on all other contacts for this
    vendor."""
    from ...models.vendors import VendorContact as VC

    vendor = get_vendor_card_or_404(db, vendor_id)
    vc = db.query(VC).filter(VC.id == contact_id, VC.vendor_card_id == vendor_id).first()
    if not vc:
        raise HTTPException(404, "Contact not found")

    # Clear all primaries for this vendor, then set this one
    db.query(VC).filter(VC.vendor_card_id == vendor_id).update({"is_primary": False})
    vc.is_primary = True
    db.commit()
    db.refresh(vc)
    logger.info("VendorContact {} set as primary by {}", contact_id, user.email)

    contacts = (
        db.query(VC)
        .filter(VC.vendor_card_id == vendor_id)
        .order_by(VC.interaction_count.desc().nullslast())
        .limit(50)
        .all()
    )
    return _render_vendor_contacts(request, vendor, contacts, user)


# ── Vendor Ownership UI (surface existing StrategicVendor) ─────────────────


@router.post("/v2/partials/vendors/{vendor_id}/claim", response_class=HTMLResponse)
async def vendor_claim(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Claim this vendor as strategic for the current user."""
    from ...services.strategic_vendor_service import claim_vendor, get_vendor_status

    get_vendor_card_or_404(db, vendor_id)
    _record, error = claim_vendor(db, user.id, vendor_id)
    if error:
        raise HTTPException(400, error)
    status = get_vendor_status(db, vendor_id)
    return _render_vendor_ownership_badge(request, vendor_id, status, user)


@router.post("/v2/partials/vendors/{vendor_id}/release", response_class=HTMLResponse)
async def vendor_release(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Release this vendor from the current user's strategic list."""
    from ...services.strategic_vendor_service import drop_vendor, get_vendor_status

    get_vendor_card_or_404(db, vendor_id)
    _ok, error = drop_vendor(db, user.id, vendor_id)
    if error:
        raise HTTPException(400, error)
    status = get_vendor_status(db, vendor_id)
    return _render_vendor_ownership_badge(request, vendor_id, status, user)


def _render_vendor_ownership_badge(request: Request, vendor_id: int, status, user):
    """Render the vendor ownership badge partial."""
    return template_response(
        "htmx/partials/vendors/_ownership_badge.html",
        {"request": request, "vendor_id": vendor_id, "ownership": status, "user": user},
    )


@router.get("/v2/partials/vendors/{vendor_id}/ownership", response_class=HTMLResponse)
async def vendor_ownership_badge(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the strategic ownership badge for a vendor (lazy-loaded)."""
    from ...services.strategic_vendor_service import get_vendor_status

    get_vendor_card_or_404(db, vendor_id)
    status = get_vendor_status(db, vendor_id)
    return _render_vendor_ownership_badge(request, vendor_id, status, user)


# ── Vendor Custom Fields (parity P1) ───────────────────────────────────────


def _render_vendor_custom_fields(request: Request, vendor):
    """Render vendor _custom_fields partial."""
    return template_response(
        "htmx/partials/vendors/_custom_fields.html",
        {"request": request, "vendor": vendor},
    )


@router.post("/v2/partials/vendors/{vendor_id}/custom-fields", response_class=HTMLResponse)
async def vendor_add_custom_field(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add or overwrite a custom field on a vendor card.

    require_user gate.
    """
    from sqlalchemy.orm.attributes import flag_modified

    from ...models.vendors import VendorCard as VC

    vendor = db.get(VC, vendor_id)
    if not vendor:
        raise HTTPException(404, "Vendor not found")

    form = await request.form()
    label = (form.get("label") or "").strip()
    value = (form.get("value") or "").strip()
    if not label:
        raise HTTPException(400, "label is required")

    existing = vendor.custom_fields or {}
    updated = {**existing, label: value}
    try:
        vendor.custom_fields = updated
    except ValueError as e:
        raise HTTPException(400, str(e))

    flag_modified(vendor, "custom_fields")
    db.commit()
    db.refresh(vendor)
    logger.info("Vendor {} custom field '{}' set by {}", vendor_id, label, user.email)
    return _render_vendor_custom_fields(request, vendor)


@router.delete(
    "/v2/partials/vendors/{vendor_id}/custom-fields/{label:path}",
    response_class=HTMLResponse,
)
async def vendor_delete_custom_field(
    request: Request,
    vendor_id: int,
    label: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Remove a custom field from a vendor card.

    require_user gate.
    """
    from sqlalchemy.orm.attributes import flag_modified

    from ...models.vendors import VendorCard as VC

    vendor = db.get(VC, vendor_id)
    if not vendor:
        raise HTTPException(404, "Vendor not found")

    existing = dict(vendor.custom_fields or {})
    existing.pop(label, None)
    vendor.custom_fields = existing
    flag_modified(vendor, "custom_fields")
    db.commit()
    db.refresh(vendor)
    logger.info("Vendor {} custom field '{}' removed by {}", vendor_id, label, user.email)
    return _render_vendor_custom_fields(request, vendor)


@router.get("/v2/partials/vendors/{vendor_id}/contact-nudges", response_class=HTMLResponse)
async def vendor_contact_nudges(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return nudge suggestions for dormant vendor contacts."""
    vendor = get_vendor_card_or_404(db, vendor_id)

    contacts = db.query(VendorContact).filter(VendorContact.vendor_card_id == vendor_id).all()
    # Contacts with no interaction in 30+ days are nudge candidates
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    nudges = []
    for c in contacts:
        if not c.last_interaction_at:
            nudges.append(c)
        else:
            # Handle both tz-aware and tz-naive datetimes
            last = c.last_interaction_at
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if last < cutoff:
                nudges.append(c)
    return template_response(
        "htmx/partials/vendors/contact_nudges.html",
        {"request": request, "nudges": nudges, "vendor": vendor},
    )


@router.get("/v2/partials/vendors/{vendor_id}/reviews", response_class=HTMLResponse)
async def vendor_reviews(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return reviews section for a vendor."""
    from ...models import VendorReview

    vendor = get_vendor_card_or_404(db, vendor_id)

    reviews = (
        db.query(VendorReview)
        .filter(VendorReview.vendor_card_id == vendor_id)
        .options(joinedload(VendorReview.user))
        .order_by(VendorReview.created_at.desc())
        .limit(20)
        .all()
    )
    return template_response(
        "htmx/partials/vendors/reviews.html",
        {"request": request, "reviews": reviews, "vendor": vendor, "user": user},
    )


@router.post("/v2/partials/vendors/{vendor_id}/reviews", response_class=HTMLResponse)
async def add_vendor_review(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add a review to a vendor and return refreshed reviews."""
    from ...models import VendorReview

    get_vendor_card_or_404(db, vendor_id)  # validates existence

    form = await request.form()
    try:
        rating = int(form.get("rating", "3"))
    except (ValueError, TypeError):
        rating = 3
    comment = form.get("comment", "").strip()

    review = VendorReview(
        vendor_card_id=vendor_id,
        user_id=user.id,
        rating=max(1, min(5, rating)),
        comment=comment or None,
    )
    db.add(review)
    db.commit()
    logger.info("Review added for vendor {} by {} (rating={})", vendor_id, user.email, rating)

    return await vendor_reviews(request=request, vendor_id=vendor_id, user=user, db=db)


@router.delete("/v2/partials/vendors/{vendor_id}/reviews/{review_id}", response_class=HTMLResponse)
async def delete_vendor_review(
    request: Request,
    vendor_id: int,
    review_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a vendor review (own reviews only) and return refreshed reviews."""
    from ...models import VendorReview

    review = (
        db.query(VendorReview).filter(VendorReview.id == review_id, VendorReview.vendor_card_id == vendor_id).first()
    )
    if not review:
        raise HTTPException(404, "Review not found")
    if review.user_id != user.id:
        raise HTTPException(403, "Can only delete your own reviews")

    db.delete(review)
    db.commit()

    return await vendor_reviews(request=request, vendor_id=vendor_id, user=user, db=db)


# ── AI Contact Finder actions (Phase 3A) ───────────────────────────────


@router.post("/v2/partials/vendors/{vendor_id}/ai/find-contacts", response_class=HTMLResponse)
async def vendor_find_contacts(
    request: Request,
    vendor_id: int,
    title_keywords: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Trigger AI web search for contacts at this vendor, return HTML results."""
    vendor = get_vendor_card_or_404(db, vendor_id)

    from ...config import settings as app_settings

    # Check AI feature gate
    ai_flag = app_settings.ai_features_enabled
    if ai_flag == "off":
        return HTMLResponse(
            '<div class="p-4 text-center text-sm text-amber-600 bg-amber-50 rounded-lg border border-amber-200">'
            "AI features are currently disabled. Contact your admin to enable them.</div>"
        )

    ctx = _base_ctx(request, user, "vendors")
    ctx["vendor"] = vendor

    try:
        from app.services.ai_service import enrich_contacts_websearch

        keywords = title_keywords.strip() if title_keywords else None
        web_results = await enrich_contacts_websearch(vendor.display_name, vendor.domain, keywords, limit=10)

        # Dedup and save as ProspectContact records
        seen: set[str] = set()
        new_count = 0
        for c in web_results:
            email = (c.get("email") or "").lower()
            key = email if email else c.get("full_name", "").lower()
            if key and key in seen:
                continue
            seen.add(key)

            pc = ProspectContact(
                vendor_card_id=vendor_id,
                full_name=c["full_name"],
                title=c.get("title"),
                email=c.get("email"),
                email_status=c.get("email_status"),
                phone=c.get("phone"),
                linkedin_url=c.get("linkedin_url"),
                source=c.get("source", "web_search"),
                confidence=c.get("confidence", "low"),
            )
            db.add(pc)
            new_count += 1

        db.commit()
    except Exception as exc:
        logger.error(f"AI contact finder error for vendor {vendor_id}: {exc}")
        return HTMLResponse(
            '<div class="p-4 text-center text-sm text-rose-600 bg-rose-50 rounded-lg border border-rose-200">'
            f"AI search failed: {exc}</div>"
        )

    # Reload all prospects for this vendor
    prospects = (
        db.query(ProspectContact)
        .filter(ProspectContact.vendor_card_id == vendor_id)
        .order_by(ProspectContact.created_at.desc())
        .limit(50)
        .all()
    )
    ctx["prospects"] = prospects
    ctx["search_count"] = new_count
    return template_response("htmx/partials/vendors/find_contacts_results.html", ctx)


@router.post(
    "/v2/partials/vendors/{vendor_id}/ai/prospect/{prospect_id}/save",
    response_class=HTMLResponse,
)
async def vendor_prospect_save(
    request: Request,
    vendor_id: int,
    prospect_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Mark a prospect contact as saved."""
    pc = db.query(ProspectContact).filter(ProspectContact.id == prospect_id).first()
    if not pc:
        raise HTTPException(404, "Prospect contact not found")
    require_prospect_site_access(db, user, pc)

    pc.is_saved = True
    pc.saved_by_id = user.id
    db.commit()

    vendor = db.query(VendorCard).filter(VendorCard.id == vendor_id).first()
    ctx = _base_ctx(request, user, "vendors")
    ctx["vendor"] = vendor
    ctx["p"] = pc
    return template_response("htmx/partials/vendors/prospect_card.html", ctx)


@router.post(
    "/v2/partials/vendors/{vendor_id}/ai/prospect/{prospect_id}/promote",
    response_class=HTMLResponse,
)
async def vendor_prospect_promote(
    request: Request,
    vendor_id: int,
    prospect_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Promote a prospect contact to a VendorContact."""
    pc = db.query(ProspectContact).filter(ProspectContact.id == prospect_id).first()
    if not pc:
        raise HTTPException(404, "Prospect contact not found")
    require_prospect_site_access(db, user, pc)

    # Dedup: check if email already exists on this vendor
    existing = None
    if pc.email:
        existing = db.query(VendorContact).filter_by(vendor_card_id=vendor_id, email=pc.email).first()

    if existing:
        if pc.full_name and not existing.full_name:
            existing.full_name = pc.full_name
        if pc.title and not existing.title:
            existing.title = pc.title
        if pc.phone and not existing.phone:
            existing.phone = pc.phone
        if pc.linkedin_url and not existing.linkedin_url:
            existing.linkedin_url = pc.linkedin_url
        vc = existing
    else:
        vc = VendorContact(
            vendor_card_id=vendor_id,
            full_name=pc.full_name,
            title=pc.title,
            email=pc.email,
            phone=pc.phone,
            linkedin_url=pc.linkedin_url,
            source="prospect_promote",
        )
        db.add(vc)
        db.flush()

    pc.promoted_to_type = "vendor_contact"
    pc.promoted_to_id = vc.id
    pc.is_saved = True
    pc.saved_by_id = user.id
    db.commit()

    vendor = db.query(VendorCard).filter(VendorCard.id == vendor_id).first()
    ctx = _base_ctx(request, user, "vendors")
    ctx["vendor"] = vendor
    ctx["p"] = pc
    return template_response("htmx/partials/vendors/prospect_card.html", ctx)


@router.delete(
    "/v2/partials/vendors/{vendor_id}/ai/prospect/{prospect_id}",
    response_class=HTMLResponse,
)
async def vendor_prospect_delete(
    request: Request,
    vendor_id: int,
    prospect_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a prospect contact."""
    pc = db.query(ProspectContact).filter(ProspectContact.id == prospect_id).first()
    if not pc:
        raise HTTPException(404, "Prospect contact not found")
    require_prospect_site_access(db, user, pc)
    db.delete(pc)
    db.commit()
    # Return empty string to remove the card from DOM
    return HTMLResponse("")
