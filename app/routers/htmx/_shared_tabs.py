"""routers/htmx/_shared_tabs.py — detail-tab partial renderers shared across routers.

``requisition_tab`` / ``company_tab`` / ``vendor_tab`` are each registered as the
detail-shell tab-swap route on their OWNING router (requisitions.py / companies.py /
vendors.py respectively — each does ``router.get(...)(...)`` on the function imported
from here so the route stays registered exactly where it always was), but a handful of
OTHER routers reuse the same rendered partial after finishing an unrelated action (e.g.
offers.py falls back to the requisition's "offers" tab after saving an offer; archive.py
renders a restored company/vendor's detail tab). Previously those callers imported the
function straight off the sibling router module (a router-importing-router inversion);
now everyone imports the single shared implementation from here instead.

These are genuinely HTTP-shaped (FastAPI ``Depends()`` defaults, ``Request``,
``HTTPException``) — each function IS the full route handler, not a plain data-assembly
helper a router calls into — so they live in ``routers/htmx`` rather than
``app/services/``. ``company_tab`` and ``vendor_tab`` still need a couple of names that
are genuinely local to their owning router module (``companies.py``'s
``FIELD_LABELS``/``CANONICAL_ROLES``/query helpers, ``vendors.py``'s ``vendor_reviews``);
those are imported lazily inside the function body — the same established
service/router reuse pattern used elsewhere in this codebase — to avoid a load-time
import cycle with the owning router (which imports this module).

Called by: app.routers.htmx.requisitions/companies/vendors (route registration),
    app.routers.htmx.offers (post-save requisition "offers" tab refresh),
    app.routers.htmx.archive (post-restore company/vendor detail tab)
Depends on: app.models, app.dependencies, app.database, app.services.quote_requisitions,
    app.services.activity_service, app.routers.htmx._shared, app.routers.htmx._lookup_helpers
"""

from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session, joinedload, selectinload

from ...constants import ActivityType, QuoteStatus
from ...database import get_db
from ...dependencies import require_requisition_access, require_user
from ...models import (
    BuyPlan,
    Offer,
    Quote,
    Requirement,
    RequisitionTask,
    Sighting,
    SourcingLead,
    User,
)
from ...models.enrichment import ProspectContact
from ...models.vendors import VendorContact
from ...services.quote_requisitions import quotes_for_requisition
from ...template_env import template_response
from .._lookup_helpers import get_requisition_or_404, get_vendor_card_or_404
from ._shared import _DASH, _base_ctx


async def requisition_tab(
    request: Request,
    req_id: int,
    tab: str,
    qual: str | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return a specific tab partial for requisition detail."""
    req = get_requisition_or_404(db, req_id)
    require_requisition_access(db, req_id, user)

    valid_tabs = {"parts", "offers", "quotes", "buy_plans", "tasks", "activity", "responses"}
    if tab not in valid_tabs:
        raise HTTPException(404, f"Unknown tab: {tab}")

    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req

    if tab == "parts":
        requirements = (
            db.query(Requirement)
            .options(selectinload(Requirement.sightings))
            .filter(Requirement.requisition_id == req_id)
            .all()
        )
        for r in requirements:
            r.sighting_count = len(r.sightings) if r.sightings else 0
        ctx["requirements"] = requirements
        return template_response("htmx/partials/requisitions/tabs/parts.html", ctx)

    elif tab == "offers":
        q = db.query(Offer).filter(Offer.requisition_id == req_id)
        if qual in ("unset", "incomplete", "essentials", "complete"):
            q = q.filter(Offer.qualification_status == qual)
        offers = q.order_by(Offer.created_at.desc().nullslast()).all()
        # Check for existing draft quote to show "Add to Quote" button — join-table scoped
        # so a combined draft quote is offered on every contributing requisition.
        draft_quote = (
            quotes_for_requisition(db, req_id)
            .filter(Quote.status == QuoteStatus.DRAFT)
            .order_by(Quote.created_at.desc())
            .first()
        )
        ctx["offers"] = offers
        ctx["draft_quote"] = draft_quote
        ctx["qual"] = qual
        return template_response("htmx/partials/requisitions/tabs/offers.html", ctx)

    elif tab == "quotes":
        # Join-table scoped so a combined quote appears on the Quotes tab of EVERY
        # contributing requisition, not just the one it anchors.
        quotes = quotes_for_requisition(db, req_id).order_by(Quote.created_at.desc().nullslast()).all()
        ctx["quotes"] = quotes
        return template_response("htmx/partials/requisitions/tabs/quotes.html", ctx)

    elif tab == "buy_plans":
        buy_plans = (
            db.query(BuyPlan)
            .options(joinedload(BuyPlan.lines))
            .filter(BuyPlan.requisition_id == req_id)
            .order_by(BuyPlan.created_at.desc().nullslast())
            .all()
        )
        ctx["buy_plans"] = buy_plans
        return template_response("htmx/partials/requisitions/tabs/buy_plans.html", ctx)

    elif tab == "tasks":
        tasks = (
            db.query(RequisitionTask)
            .options(joinedload(RequisitionTask.assignee))
            .filter(RequisitionTask.requisition_id == req_id)
            .order_by(RequisitionTask.priority.desc(), RequisitionTask.created_at.desc().nullslast())
            .all()
        )
        users = db.query(User).order_by(User.name).all()
        ctx["tasks"] = tasks
        ctx["users"] = users
        return template_response("htmx/partials/requisitions/tabs/tasks.html", ctx)

    elif tab == "responses":
        # Fetch vendor responses for this requisition
        from ...models.offers import VendorResponse

        responses = (
            db.query(VendorResponse)
            .filter(VendorResponse.requisition_id == req_id)
            .order_by(VendorResponse.received_at.desc().nullslast())
            .all()
        )
        ctx["responses"] = responses
        return template_response("htmx/partials/requisitions/tabs/responses.html", ctx)

    else:  # activity
        from ...services.activity_service import get_requisition_activities

        show_all = request.query_params.get("show_all") == "1"
        ctx["activities"] = get_requisition_activities(req_id, db, meaningful_only=not show_all)
        ctx["show_all"] = show_all
        ctx["req"] = req
        return template_response("htmx/partials/requisitions/tabs/activity.html", ctx)


async def company_tab(
    request: Request,
    company_id: int,
    tab: str,
    site_id: int | None = Query(None),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return a specific tab partial for company detail.

    ``FIELD_LABELS`` / ``CANONICAL_ROLES`` / ``_company_quotes_query`` /
    ``_company_buy_plans_query`` are companies.py module-level state used widely
    elsewhere in that router (not exclusive to this tab) — imported lazily below to
    avoid a load-time cycle with companies.py (which imports this function to register
    its route).
    """
    import html as html_mod

    from ...dependencies import can_manage_account
    from ...models import Company, CustomerSite, Requisition
    from ...models.intelligence import ActivityLog
    from ...models.offers import Contact as RfqContact
    from ...services.crm_field_history import ENTITY_COMPANY as _ENTITY_COMPANY
    from ...services.crm_field_history import field_history_for as _field_history_for
    from ...services.crm_service import company_contact_rows as _company_contact_rows
    from .companies import (
        CANONICAL_ROLES,
        FIELD_LABELS,
        _company_buy_plans_query,
        _company_quotes_query,
    )

    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(404, "Company not found")  # scope detail to match the contacts list

    valid_tabs = {"sites", "contacts", "requisitions", "activity", "quotes", "buy_plans", "files", "history"}
    if tab not in valid_tabs:
        raise HTTPException(404, f"Unknown tab: {tab}")

    if tab == "sites":
        sites = (
            db.query(CustomerSite)
            .options(joinedload(CustomerSite.owner), joinedload(CustomerSite.site_contacts))
            .filter(CustomerSite.company_id == company_id, CustomerSite.is_active.is_(True))
            .all()
        )
        users = db.query(User).order_by(User.name).all()
        ctx = _base_ctx(request, user, "customers")
        ctx["company"] = company
        ctx["sites"] = sites
        ctx["users"] = users
        return template_response("htmx/partials/customers/tabs/sites_tab.html", ctx)

    elif tab == "contacts":
        active_sites = (
            db.query(CustomerSite)
            .filter(CustomerSite.company_id == company_id, CustomerSite.is_active.is_(True))
            .order_by(CustomerSite.site_name)
            .all()
        )
        # IDOR-safe: only honor site_id when it belongs to this company's active sites.
        preselect_site_id = site_id if site_id and any(s.id == site_id for s in active_sites) else None
        ctx = _base_ctx(request, user, "customers")
        ctx.update(
            {
                "company": company,
                "contact_rows": _company_contact_rows(db, company_id, viewer=user),
                "now_utc": datetime.now(UTC),
                "active_sites": active_sites,
                "roles": CANONICAL_ROLES,
                "preselect_site_id": preselect_site_id,
            }
        )
        return template_response("htmx/partials/customers/tabs/contacts_tab.html", ctx)

    elif tab == "requisitions":
        from sqlalchemy import or_

        reqs = (
            db.query(Requisition)
            .filter(
                or_(
                    Requisition.company_id == company.id,
                    sqlfunc.lower(sqlfunc.trim(Requisition.customer_name)) == company.name.lower().strip(),
                )
            )
            .order_by(Requisition.created_at.desc().nullslast())
            .limit(50)
            .all()
        )
        rows = []
        for r in reqs:
            date_str = r.created_at.strftime("%b %d, %Y") if r.created_at else "—"
            rows.append(f"""<tr class="hover:bg-brand-50 cursor-pointer"
                hx-get="/v2/partials/requisitions/{r.id}"
                hx-target="#main-content"
                hx-push-url="/v2/requisitions/{r.id}">
              <td class="px-4 py-2 text-sm font-medium text-brand-500">{html_mod.escape(r.name or "")}</td>
              <td class="px-4 py-2 text-sm text-gray-500">{html_mod.escape(r.status or _DASH)}</td>
              <td class="px-4 py-2 text-sm text-gray-500">{date_str}</td>
            </tr>""")
        if rows:
            html = f"""<div class="overflow-x-auto">
              <table class="min-w-full divide-y divide-gray-200">
                <thead class="bg-gray-50">
                  <tr>
                    <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Name</th>
                    <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
                    <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Created</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-gray-200">{"".join(rows)}</tbody>
              </table>
            </div>"""
        else:
            html = '<div class="p-8 text-center"><p class="text-sm text-gray-500">No requisitions for this company.</p></div>'
        return HTMLResponse(html)

    elif tab == "quotes":
        cq = _company_quotes_query(db, company)
        quotes = cq.order_by(Quote.created_at.desc().nullslast()).all() if cq is not None else []
        ctx = _base_ctx(request, user, "customers")
        ctx.update({"company": company, "quotes": quotes})
        return template_response("htmx/partials/customers/tabs/quotes_tab.html", ctx)

    elif tab == "buy_plans":
        bpq = _company_buy_plans_query(db, company)
        buy_plans = bpq.order_by(BuyPlan.created_at.desc().nullslast()).all() if bpq is not None else []
        ctx = _base_ctx(request, user, "customers")
        ctx.update({"company": company, "buy_plans": buy_plans})
        return template_response("htmx/partials/customers/tabs/buy_plans_tab.html", ctx)

    elif tab == "files":
        ctx = _base_ctx(request, user, "customers")
        ctx["company"] = company
        return template_response("htmx/partials/customers/tabs/files_tab.html", ctx)

    elif tab == "history":
        history = _field_history_for(db, _ENTITY_COMPANY, company_id)
        ctx = _base_ctx(request, user, "customers")
        ctx.update(
            {
                "company": company,
                "history": history,
                "field_labels": FIELD_LABELS,
                "now_utc": datetime.now(UTC),
            }
        )
        return template_response("htmx/partials/customers/tabs/history_tab.html", ctx)

    else:  # activity
        from sqlalchemy import or_ as or_clause

        # Find all requisition IDs linked to this company (via FK or name match)
        req_ids = [
            r.id
            for r in db.query(Requisition.id)
            .filter(
                or_clause(
                    Requisition.company_id == company.id,
                    sqlfunc.lower(sqlfunc.trim(Requisition.customer_name)) == company.name.lower().strip(),
                )
            )
            .all()
        ]

        # RFQ contacts across company's requisitions (canonical RFQ source)
        rfq_contacts: list = []
        req_map: dict = {}
        if req_ids:
            rfq_contacts = (
                db.query(RfqContact)
                .filter(RfqContact.requisition_id.in_(req_ids))
                .order_by(RfqContact.created_at.desc())
                .limit(30)
                .all()
            )
            if rfq_contacts:
                linked_req_ids = {c.requisition_id for c in rfq_contacts}
                for r in db.query(Requisition).filter(Requisition.id.in_(linked_req_ids)).all():
                    req_map[r.id] = r

        # Direct activity logs on this company + its requisitions (newest-first).
        # Exclude rfq_sent: RfqContact rows are the canonical source; showing both
        # would double-show the same RFQ.
        _RFQ_SENT = ActivityType.RFQ_SENT
        activity_filters = [ActivityLog.company_id == company.id]
        if req_ids:
            activity_filters.append(ActivityLog.requisition_id.in_(req_ids))
        activities = (
            db.query(ActivityLog)
            .filter(or_clause(*activity_filters))
            .filter(ActivityLog.activity_type != _RFQ_SENT)
            .order_by(ActivityLog.created_at.desc())
            .limit(50)
            .all()
        )

        activities_truncated = len(activities) >= 50

        # Bucket activities into type-sections (template renders by section).
        # Emails section also carries RFQ contact items (tagged with _is_rfq=True);
        # they are merged and sorted newest-first in the template.
        _CALLS = frozenset({ActivityType.CALL_LOGGED})
        _EMAILS = frozenset({ActivityType.EMAIL_SENT, ActivityType.EMAIL_RECEIVED})
        _MEETINGS = frozenset({ActivityType.TEAMS_MESSAGE, ActivityType.WECHAT_MESSAGE, ActivityType.MEETING})
        _NOTES = frozenset({ActivityType.NOTE, ActivityType.SALES_NOTE, ActivityType.CONTACT_NOTE})

        sections: dict[str, list] = {"Calls": [], "Emails": [], "Meetings": [], "Notes": [], "Other": []}

        # Wrap RFQ contacts as tagged dicts so the template can branch on _is_rfq
        for c in rfq_contacts:
            sections["Emails"].append({"_is_rfq": True, "raw": c, "req": req_map.get(c.requisition_id)})

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

        # Sort Emails section: RFQ dicts use raw.created_at; ActivityLog uses created_at
        import datetime as _dt_mod

        _epoch = _dt_mod.datetime(1970, 1, 1, tzinfo=_dt_mod.UTC)

        def _email_ts(item):
            if isinstance(item, dict):
                c = item["raw"]
                return c.created_at or _epoch
            return item.created_at or _epoch

        sections["Emails"].sort(key=_email_ts, reverse=True)

        # has_any_activity: drives empty-state vs. sections in the template
        has_any_activity = bool(activities) or any(sections.values())

        ctx = _base_ctx(request, user, "customers")
        ctx.update(
            {
                "company": company,
                "activities": activities,
                "sections": sections,
                "activities_truncated": activities_truncated,
                "req_map": req_map,
                "has_any_activity": has_any_activity,
            }
        )
        return template_response("htmx/partials/customers/tabs/activity_tab.html", ctx)


async def vendor_tab(
    request: Request,
    vendor_id: int,
    tab: str,
    mpn: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return a specific tab partial for vendor detail.

    ``vendor_reviews`` is another registered route handler on vendors.py (the
    "reviews" tab has its own dedicated endpoint too) — imported lazily below to avoid
    a load-time cycle with vendors.py (which imports this function to register its
    route).
    """
    import html as html_mod

    from ...models.intelligence import ActivityLog as _ActivityLog
    from ...models.offers import Contact as RfqContact
    from ...models.offers import VendorResponse
    from ...services.task_service import get_open_tasks_for_vendor_card
    from ...utils.normalization import normalize_mpn
    from .vendors import vendor_reviews

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
              <p class="text-2xl font-bold text-brand-500">{f"{(vendor.overall_win_rate or 0) * 100:.0f}%"}</p>
              <p class="text-xs text-gray-500 mt-1">Win Rate</p>
            </div>
            <div class="bg-white rounded-lg border border-gray-200 p-4 text-center">
              <p class="text-2xl font-bold text-brand-500">{f"{(vendor.response_rate or 0) * 100:.0f}%"}</p>
              <p class="text-xs text-gray-500 mt-1">Response Rate</p>
            </div>
            <div class="bg-white rounded-lg border border-gray-200 p-4 text-center">
              <p class="text-2xl font-bold text-brand-500">{f"{vendor.vendor_score or 0:.0f}"}</p>
              <p class="text-xs text-gray-500 mt-1">Vendor Score</p>
            </div>
            <div class="bg-white rounded-lg border border-gray-200 p-4 text-center">
              <p class="text-2xl font-bold text-gray-900">{vendor.sighting_count or 0}</p>
              <p class="text-xs text-gray-500 mt-1">Sightings</p>
            </div>
            <div class="bg-white rounded-lg border border-gray-200 p-4 text-center">
              <p class="text-2xl font-bold text-gray-900">{f"{vendor.avg_response_hours or 0:.0f}"}</p>
              <p class="text-xs text-gray-500 mt-1">Avg Response Hours</p>
            </div>
            <div class="bg-white rounded-lg border border-gray-200 p-4 text-center">
              <p class="text-2xl font-bold text-gray-900">{f"{vendor.engagement_score or 0:.0f}"}</p>
              <p class="text-xs text-gray-500 mt-1">Engagement Score</p>
            </div>
          </div>
          <p class="text-sm text-gray-500 text-center">Analytics data builds as you interact with this vendor.</p>
        </div>"""
        return HTMLResponse(html)

    elif tab == "reviews":
        return await vendor_reviews(request=request, vendor_id=vendor_id, user=user, db=db)

    elif tab == "activity":
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
