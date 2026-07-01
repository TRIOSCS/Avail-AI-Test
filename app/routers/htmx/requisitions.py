"""routers/htmx/requisitions.py — Requisition partial views (HTMX + Alpine).

Server-rendered HTML partials for the requisitions surface: the list, the
unified create/import modal + AI parse/save, the AI customer lookup/quick-create
used by that modal, the requisition detail shell, requirement add, search-all,
and the detail tabs. Extracted verbatim from htmx_views.py (same `/v2/partials`
paths, same `htmx-views` tag) as the first domain split.

Called by: app/main.py (router mount); htmx_views.py re-imports
    requisitions_list_partial / requisition_tab for its offer/response routes.
Depends on: app.models, app.dependencies, app.database, app.search_service,
    app.services.freeform_parser_service, ._shared
"""

import html as html_mod
import json
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import case, exists, or_, select
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session, joinedload, selectinload

from ...constants import RESTRICTED_ROLES, QuoteStatus, RequisitionStatus, SourcingStatus
from ...database import get_db
from ...dependencies import require_requisition_access, require_user
from ...models import (
    BuyPlan,
    Company,
    CustomerSite,
    Offer,
    Quote,
    Requirement,
    Requisition,
    RequisitionTask,
    User,
)
from ...services.freeform_parser_service import parse_freeform_rfq
from ...template_env import template_response
from ...utils.search_builder import SearchBuilder
from ...utils.sql_helpers import escape_like
from .._lookup_helpers import get_requisition_or_404
from ._shared import _base_ctx, _parse_date_safe

router = APIRouter(tags=["htmx-views"])

# Quote-status significance for the list's aggregate Quotes column — lower wins. Mirrors
# requisition_list_service._quote_priority (won > lost > sent > revised > everything else).
_QUOTE_STATUS_PRIORITY = {"won": 1, "lost": 2, "sent": 3, "revised": 4}


def _best_quote_status(quotes) -> str | None:
    """The most significant quote status across a requisition's quotes, or None if it
    has none — the value shown in the list's Quotes column."""
    if not quotes:
        return None
    return min(quotes, key=lambda qt: _QUOTE_STATUS_PRIORITY.get(qt.status, 5)).status


# ── Requisition partials ────────────────────────────────────────────────


@router.get("/v2/partials/requisitions", response_class=HTMLResponse)
async def requisitions_list_partial(
    request: Request,
    q: str = "",
    status: str = "",
    owner: int = Query(0, ge=0),
    urgency: str = "",
    date_from: str = "",
    date_to: str = "",
    sort: str = "created_at",
    sort_dir: Literal["asc", "desc"] = Query("desc", alias="dir"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return requisitions list as HTML partial with filters and sorting."""
    query = (
        db.query(Requisition)
        .filter(Requisition.is_scratch.is_(False))
        .options(
            joinedload(Requisition.creator),
            joinedload(Requisition.requirements),
            joinedload(Requisition.offers),
            joinedload(Requisition.quotes),
        )
    )

    search_term = q.strip()
    if search_term:
        sb = SearchBuilder(search_term)
        safe = f"%{sb.safe}%"
        mpn_match = exists(
            select(Requirement.id).where(
                Requirement.requisition_id == Requisition.id,
                or_(
                    Requirement.primary_mpn.ilike(safe, escape="\\"),
                    Requirement.customer_pn.ilike(safe, escape="\\"),
                    Requirement.substitutes_text.ilike(safe, escape="\\"),
                ),
            )
        )
        query = query.filter(
            or_(
                sb.ilike_filter(Requisition.name, Requisition.customer_name),
                mpn_match,
            )
        )
    if status:
        query = query.filter(Requisition.status == status)
    if owner:
        query = query.filter(Requisition.created_by == owner)
    if urgency:
        query = query.filter(Requisition.urgency == urgency)
    if date_from:
        try:
            dt = datetime.fromisoformat(date_from)
            query = query.filter(Requisition.created_at >= dt)
        except ValueError:
            pass
    if date_to:
        try:
            dt = datetime.fromisoformat(date_to)
            query = query.filter(Requisition.created_at <= dt)
        except ValueError:
            pass

    # Restricted roles (sales/trader) only see their own
    if user.role in RESTRICTED_ROLES:
        query = query.filter(Requisition.created_by == user.id)

    total = query.count()

    # Sorting — whitelist of sortable columns, including subqueries for computed counts
    req_count_sub = (
        select(sqlfunc.count(Requirement.id))
        .where(Requirement.requisition_id == Requisition.id)
        .correlate(Requisition)
        .scalar_subquery()
        .label("req_count_sort")
    )
    offer_count_sub = (
        select(sqlfunc.count(Offer.id))
        .where(Offer.requisition_id == Requisition.id)
        .correlate(Requisition)
        .scalar_subquery()
        .label("offer_count_sort")
    )
    # ASAP sorts before all dates (most urgent); nullslast() handles NULLs
    deadline_sort = case(
        (Requisition.deadline == "ASAP", "0000-00-00"),
        else_=Requisition.deadline,
    )
    sort_col_map = {
        "name": Requisition.name,
        "customer_name": Requisition.customer_name,
        "status": Requisition.status,
        "urgency": Requisition.urgency,
        "created_at": Requisition.created_at,
        "deadline": deadline_sort,
        "updated_at": Requisition.updated_at,
        "req_count": req_count_sub,
        "offer_count": offer_count_sub,
    }
    sort_col = sort_col_map.get(sort)
    if sort_col is None:
        logger.warning("Unknown sort key '{}', falling back to created_at", sort)
        sort_col = Requisition.created_at
        sort = "created_at"
    # nullslast: NULLs always sort to the bottom regardless of direction
    order = sort_col.desc().nullslast() if sort_dir == "desc" else sort_col.asc().nullslast()
    reqs = query.order_by(order).offset(offset).limit(limit).all()

    # Attach counts + match reason when searching
    for req in reqs:
        req.req_count = len(req.requirements) if req.requirements else 0
        req.offer_count = len(req.offers) if req.offers else 0
        # Aggregate quote status for the list's Quotes column — the most significant of the
        # req's quotes (won > lost > sent > revised > other), mirroring
        # requisition_list_service._quote_priority. None → the column shows a dash.
        req.quote_status = _best_quote_status(req.quotes)
        req.match_reason = None
        req.matched_mpn = None
        if search_term:
            term_lower = search_term.lower()
            if req.name and term_lower in req.name.lower():
                req.match_reason = "name"
            elif req.customer_name and term_lower in req.customer_name.lower():
                req.match_reason = "customer"
            else:
                matched_mpn = next(
                    (
                        r.primary_mpn
                        for r in (req.requirements or [])
                        if (r.primary_mpn and term_lower in r.primary_mpn.lower())
                        or (r.customer_pn and term_lower in r.customer_pn.lower())
                    ),
                    None,
                )
                if matched_mpn:
                    req.match_reason = "part"
                    req.matched_mpn = matched_mpn

    # Match stats for search scope indicators
    match_counts = None
    if search_term:
        match_counts = {"name": 0, "customer": 0, "part": 0}
        for req in reqs:
            reason = req.match_reason
            if reason and reason in match_counts:
                match_counts[reason] += 1

    # Fetch team users for owner dropdown (unrestricted roles only)
    users = []
    if user.role not in RESTRICTED_ROLES:
        users = db.query(User).order_by(User.name).all()

    from ...services.activity_service import get_inbox_sync_status

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update(
        {
            "requisitions": reqs,
            "q": q,
            "match_counts": match_counts,
            "status": status,
            "owner": owner,
            "urgency": urgency,
            "date_from": date_from,
            "date_to": date_to,
            "sort": sort,
            "dir": sort_dir,
            "total": total,
            "limit": limit,
            "offset": offset,
            "users": users,
            "user_role": user.role,
            "inbox_status": get_inbox_sync_status(db, user),
        }
    )
    return template_response("htmx/partials/requisitions/list.html", ctx)


@router.get("/v2/partials/requisitions/create-form", response_class=HTMLResponse)
async def requisition_create_form(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the create requisition modal form."""
    ctx = _base_ctx(request, user, "requisitions")
    return template_response("htmx/partials/requisitions/unified_modal.html", ctx)


@router.get("/v2/partials/requisitions/import-form", response_class=HTMLResponse)
async def requisition_import_form(
    request: Request,
    user: User = Depends(require_user),
):
    """Return the import requisition modal form."""
    ctx = _base_ctx(request, user, "requisitions")
    return template_response("htmx/partials/requisitions/unified_modal.html", ctx)


@router.post("/v2/partials/requisitions/import-parse", response_class=HTMLResponse)
async def requisition_import_parse(
    request: Request,
    name: str = Form(...),
    customer_name: str = Form(""),
    customer_site_id: str = Form(""),
    deadline: str = Form(""),
    urgency: str = Form("normal"),
    raw_text: str = Form(""),
    file: UploadFile | None = File(None),
    user: User = Depends(require_user),
):
    """Parse pasted text or uploaded file with AI, return editable preview."""
    # Extract text from file if uploaded
    text = raw_text.strip()
    if file and file.filename:
        content = await file.read()
        fname = file.filename.lower()
        if fname.endswith((".xlsx", ".xls")):
            from io import BytesIO

            import openpyxl

            wb = openpyxl.load_workbook(BytesIO(content), read_only=True, data_only=True)
            rows = []
            for ws in wb.worksheets:
                for row in ws.iter_rows(values_only=True):
                    cells = [str(c) if c is not None else "" for c in row]
                    if any(cells):
                        rows.append("\t".join(cells))
            text = "\n".join(rows)
        elif fname.endswith(".csv"):
            text = content.decode("utf-8", errors="replace")
        else:
            text = content.decode("utf-8", errors="replace")

    json_mode = request.query_params.get("format") == "json"

    if not text:
        if json_mode:
            from fastapi.responses import JSONResponse

            return JSONResponse({"error": "No data provided", "requirements": []})
        return HTMLResponse(
            '<div class="p-4 text-center text-sm text-rose-600 bg-rose-50 rounded-lg border border-rose-200">'
            "No data provided. Paste text or upload a file."
            "</div>"
        )

    # AI parse
    result = await parse_freeform_rfq(text)
    requirements = result.get("requirements", []) if result else []

    # Use AI-extracted name/customer as fallback if user left them blank
    if not name.strip() and result:
        name = result.get("name", "Untitled")
    if not customer_name.strip() and result:
        customer_name = result.get("customer_name", "")

    if json_mode:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            {
                "requirements": requirements,
                "inferred_name": name,
                "inferred_customer": customer_name,
            }
        )

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update(
        {
            "requirements": requirements,
            "req_name": name,
            "customer_name": customer_name,
            "customer_site_id": customer_site_id,
            "deadline": deadline,
            "urgency": urgency,
            "count": len(requirements),
        }
    )
    return template_response("htmx/partials/requisitions/unified_modal.html", ctx)


@router.post("/v2/partials/requisitions/import-save", response_class=HTMLResponse)
async def requisition_import_save(
    request: Request,
    name: str = Form(...),
    customer_name: str = Form(""),
    customer_site_id: str = Form(""),
    deadline: str = Form(""),
    urgency: str = Form("normal"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save AI-parsed requirements as a new requisition."""
    from app.utils.normalization import normalize_mpn_key

    form = await request.form()

    # Collect requirement rows from indexed form fields
    requirements = []
    idx = 0
    while f"reqs[{idx}].primary_mpn" in form:
        mpn = form.get(f"reqs[{idx}].primary_mpn", "").strip()
        if mpn:
            requirements.append(
                {
                    "primary_mpn": mpn,
                    "target_qty": int(form.get(f"reqs[{idx}].target_qty", "1") or "1"),
                    "brand": form.get(f"reqs[{idx}].brand", "").strip() or None,
                    "target_price": float(form.get(f"reqs[{idx}].target_price") or "0") or None,
                    "condition": form.get(f"reqs[{idx}].condition", "new").strip(),
                    "customer_pn": form.get(f"reqs[{idx}].customer_pn", "").strip() or None,
                    "date_codes": form.get(f"reqs[{idx}].date_codes", "").strip() or None,
                    "packaging": form.get(f"reqs[{idx}].packaging", "").strip() or None,
                    "manufacturer": form.get(f"reqs[{idx}].manufacturer", "").strip(),
                    "substitutes": [
                        s.strip() for s in form.get(f"reqs[{idx}].substitutes", "").split(",") if s.strip()
                    ],
                    "firmware": form.get(f"reqs[{idx}].firmware", "").strip() or None,
                    "hardware_codes": form.get(f"reqs[{idx}].hardware_codes", "").strip() or None,
                    "description": form.get(f"reqs[{idx}].description", "").strip() or None,
                    "package_type": form.get(f"reqs[{idx}].package_type", "").strip() or None,
                    "revision": form.get(f"reqs[{idx}].revision", "").strip() or None,
                    "need_by_date": form.get(f"reqs[{idx}].need_by_date", "").strip() or None,
                    "sale_notes": form.get(f"reqs[{idx}].sale_notes", "").strip() or None,
                }
            )
        idx += 1

    if not requirements:
        return HTMLResponse(
            '<div class="p-4 text-center text-sm text-rose-600 bg-rose-50 rounded-lg border border-rose-200">'
            "No valid parts to save."
            "</div>"
        )

    # Create requisition
    site_id = int(customer_site_id) if customer_site_id.strip() else None
    req = Requisition(
        name=name.strip() or "Untitled",
        customer_name=customer_name.strip() or None,
        customer_site_id=site_id,
        deadline=deadline.strip() or None,
        urgency=urgency,
        status=RequisitionStatus.OPEN,
        created_by=user.id,
        claimed_by_id=user.id,
    )
    db.add(req)
    db.flush()

    # Create requirements
    from ...search_service import resolve_material_card

    added = len(requirements)
    created_reqs = []
    for item in requirements:
        mpn = item["primary_mpn"]
        card = resolve_material_card(mpn, db)
        r = Requirement(
            requisition_id=req.id,
            primary_mpn=mpn,
            normalized_mpn=normalize_mpn_key(mpn),
            material_card_id=card.id if card else None,
            target_qty=item["target_qty"],
            target_price=item.get("target_price"),
            brand=item.get("brand"),
            manufacturer=item.get("manufacturer", ""),
            condition=item.get("condition", ""),
            substitutes=item.get("substitutes", []),
            customer_pn=item.get("customer_pn", ""),
            date_codes=item.get("date_codes", ""),
            packaging=item.get("packaging", ""),
            firmware=item.get("firmware", ""),
            hardware_codes=item.get("hardware_codes", ""),
            description=item.get("description"),
            package_type=item.get("package_type"),
            revision=item.get("revision"),
            need_by_date=item.get("need_by_date"),
            sale_notes=item.get("sale_notes", ""),
        )
        db.add(r)
        created_reqs.append(r)
        for sub in item.get("substitutes", []):
            sub_mpn = sub["mpn"] if isinstance(sub, dict) else sub
            sub_mfr = sub.get("manufacturer", "") if isinstance(sub, dict) else ""
            resolve_material_card(sub_mpn, db, manufacturer=sub_mfr)

    db.commit()

    # Return success — close modal + refresh parts list + toast
    safe_added = int(added)  # safe: server-computed int
    return HTMLResponse(
        "<div hx-trigger='load' hx-get='/v2/partials/parts' hx-target='#parts-list' hx-swap='innerHTML'>"
        "</div>"
        "<script>"
        "window.dispatchEvent(new CustomEvent('close-modal'));"
        f"Alpine.store('toast').message = 'Requisition created with {safe_added} parts';"
        "Alpine.store('toast').type = 'success';"
        "Alpine.store('toast').show = true;"
        "</script>"
    )


@router.post("/v2/partials/customers/lookup", response_class=HTMLResponse)
async def customer_lookup(
    request: Request,
    company_name: str = Form(...),
    location: str = Form(""),
    user: User = Depends(require_user),
):
    """AI-powered company lookup using Claude with web search."""
    from app.utils.claude_client import claude_json
    from app.utils.claude_errors import ClaudeError, ClaudeUnavailableError

    search_query = company_name.strip()
    if location.strip():
        search_query += f", {location.strip()}"

    try:
        result = await claude_json(
            prompt=f"Search the web for this company: {search_query}\n\n"
            f"Find their official website, main phone number, and physical address.\n\n"
            f"Return ONLY a JSON object with these fields:\n"
            f'{{"company_name": "...", "website": "...", "phone": "...", '
            f'"address_line1": "...", "city": "...", "state": "...", "zip": "...", "country": "..."}}\n\n'
            f"Use empty strings for any field you cannot verify from search results. "
            f"Do NOT guess or make up information — only include data you found online.",
            system="You look up company information using web search. "
            "ONLY return data you can verify from search results. "
            "If you cannot find a phone number or address, return empty strings — never guess.",
            model_tier="smart",
            max_tokens=512,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
            timeout=45,
        )
    except (ClaudeUnavailableError, ClaudeError):
        result = None

    if not result:
        return HTMLResponse(
            '<p class="text-xs text-rose-500 mt-1">Could not look up company. Enter details manually.</p>'
        )

    # Render an approval card — escape all AI-provided strings for XSS safety
    # html_mod.escape() for HTML display context
    name = html_mod.escape(result.get("company_name", company_name))
    website = html_mod.escape(result.get("website", ""))
    phone = html_mod.escape(result.get("phone", ""))
    addr_parts = [
        p
        for p in [
            result.get("address_line1", ""),
            result.get("city", ""),
            (result.get("state", "") + " " + result.get("zip", "")).strip(),
            result.get("country", ""),
        ]
        if p
    ]
    address_display = html_mod.escape(", ".join(addr_parts))

    # json.dumps() for values embedded in JavaScript — handles quotes,
    # backslashes, </script> injection, etc.  Produces a quoted string
    # like "O\u0027Brien Corp" that is safe inside JS.
    name_js = json.dumps(result.get("company_name", company_name))
    website_js = json.dumps(result.get("website", ""))
    phone_js = json.dumps(result.get("phone", ""))
    addr1_js = json.dumps(result.get("address_line1", ""))
    city_js = json.dumps(result.get("city", ""))
    state_js = json.dumps(result.get("state", ""))
    zip_js = json.dumps(result.get("zip", ""))
    country_js = json.dumps(result.get("country", "US"))

    html_out = f"""
    <div class="mt-2 p-3 bg-emerald-50 border border-emerald-200 rounded-lg text-xs space-y-1">
      <div class="flex items-center justify-between">
        <span class="font-semibold text-emerald-700">Found: {name}</span>
      </div>
      {"<div class='text-gray-600'>🌐 " + website + "</div>" if website else ""}
      {"<div class='text-gray-600'>📞 " + phone + "</div>" if phone else ""}
      {"<div class='text-gray-600'>📍 " + address_display + "</div>" if address_display else ""}
      <div class="flex gap-2 mt-2">
        <button type="button" onclick="(async function(btn){{
            btn.disabled=true; btn.textContent='Saving...';
            var fd=new FormData();
            fd.append('company_name',{name_js});
            fd.append('website',{website_js});
            fd.append('phone',{phone_js});
            fd.append('address_line1',{addr1_js});
            fd.append('city',{city_js});
            fd.append('state',{state_js});
            fd.append('zip',{zip_js});
            fd.append('country',{country_js});
            try{{
              var r=await fetch('/v2/partials/customers/quick-create',{{method:'POST',body:fd}});
              var html=await r.text();
              var el=btn.closest('.space-y-1');
              el.replaceChildren();
              el.insertAdjacentHTML('afterbegin',html);
              var meta=el.querySelector('[data-site-id]');
              if(meta)document.dispatchEvent(new CustomEvent('customer-created',{{
                detail:{{siteId:meta.dataset.siteId,displayName:meta.dataset.display}}
              }}));
            }}catch(e){{console.error('quick-create failed:',e);btn.textContent='Failed — retry';btn.disabled=false;}}
          }})(this)"
                class="px-3 py-1 text-xs font-semibold bg-emerald-600 text-white rounded hover:bg-emerald-700">
          Use This Customer
        </button>
      </div>
    </div>
    """
    return HTMLResponse(html_out)


@router.post("/v2/partials/customers/quick-create", response_class=HTMLResponse)
async def customer_quick_create(
    request: Request,
    company_name: str = Form(...),
    website: str = Form(""),
    phone: str = Form(""),
    address_line1: str = Form(""),
    city: str = Form(""),
    state: str = Form(""),
    zip: str = Form(""),
    country: str = Form("US"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create Company + default site from AI lookup, return JS to select it in
    picker."""
    from app.cache.decorators import invalidate_prefix

    # Check for duplicates
    existing = db.query(Company).filter(Company.name.ilike(escape_like(company_name.strip()), escape="\\")).first()
    if existing:
        site = existing.sites[0] if existing.sites else None
        site_id = site.id if site else ""
        display = html_mod.escape(f"{existing.name} — {site.site_name}" if site else existing.name)
        return HTMLResponse(
            f'<div class="mt-1 p-2 bg-amber-50 border border-amber-200 rounded text-xs text-amber-700">'
            f"Customer already exists. Selected automatically."
            f"</div>"
            f'<span class="hidden" data-site-id="{site_id}" data-display="{display}"></span>'
        )

    # Create company
    domain = ""
    if website:
        from urllib.parse import urlparse

        parsed = urlparse(website if "://" in website else f"https://{website}")
        domain = parsed.netloc.lower().replace("www.", "")

    company = Company(
        name=company_name.strip(),
        website=website.strip() or None,
        domain=domain or None,
        phone=phone.strip() or None,
        hq_city=city.strip() or None,
        hq_state=state.strip() or None,
        hq_country=country.strip() or "US",
        source="ai_lookup",
        is_active=True,
    )
    db.add(company)
    db.flush()

    # Create default site
    site_name = city.strip() or "HQ"
    site = CustomerSite(
        company_id=company.id,
        site_name=site_name,
        address_line1=address_line1.strip() or None,
        city=city.strip() or None,
        state=state.strip() or None,
        zip=zip.strip() or None,
        country=country.strip() or "US",
        contact_phone=phone.strip() or None,
    )
    db.add(site)
    db.commit()

    invalidate_prefix("companies_typeahead")
    invalidate_prefix("company_list")

    display = html_mod.escape(f"{company.name} — {site.site_name}")

    return HTMLResponse(
        f'<div class="mt-1 p-2 bg-emerald-50 border border-emerald-200 rounded text-xs text-emerald-700">'
        f"Created: {display}"
        f"</div>"
        f'<span class="hidden" data-site-id="{site.id}" data-display="{display}"></span>'
    )


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

    # Fetch users for tasks tab assignee dropdown
    users = db.query(User).order_by(User.name).all()

    allowed_initial_tabs = {"parts", "offers", "responses", "quotes", "build_quote", "buy_plans"}
    initial_tab = tab if tab in allowed_initial_tabs else "parts"

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"req": req, "requirements": requirements, "users": users, "initial_tab": initial_tab})
    return template_response("htmx/partials/requisitions/detail.html", ctx)


@router.post("/v2/partials/requisitions/create", response_class=HTMLResponse)
async def requisition_create(
    request: Request,
    name: str = Form(...),
    customer_name: str = Form(""),
    deadline: str = Form(""),
    urgency: str = Form("normal"),
    parts_text: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a new requisition and return the new row for HTMX prepend."""
    req = Requisition(
        name=name,
        customer_name=customer_name or None,
        deadline=deadline or None,
        urgency=urgency,
        status=RequisitionStatus.OPEN,
        created_by=user.id,
        claimed_by_id=user.id,
    )
    db.add(req)
    db.flush()

    # Parse parts text (format: "MPN, Qty" per line)
    from ...search_service import resolve_material_card
    from ...utils.normalization import normalize_mpn_key

    part_count = 0
    if parts_text.strip():
        for line in parts_text.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(",")]
            mpn = parts[0] if parts else ""
            qty = 1
            if len(parts) > 1:
                try:
                    qty = int(parts[1].strip().replace(",", ""))
                except ValueError:
                    qty = 1
            if mpn:
                card = resolve_material_card(mpn, db)
                r = Requirement(
                    requisition_id=req.id,
                    primary_mpn=mpn,
                    normalized_mpn=normalize_mpn_key(mpn),
                    material_card_id=card.id if card else None,
                    target_qty=qty,
                    sourcing_status=SourcingStatus.OPEN,
                )
                db.add(r)
                part_count += 1

    db.commit()
    db.refresh(req)
    logger.info("Created requisition {} with {} parts from text", req.id, part_count)

    # Attach counts for the row partial
    req.req_count = part_count
    req.offer_count = 0

    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req
    response = template_response("htmx/partials/requisitions/req_row.html", ctx)
    response.headers["HX-Trigger"] = "showToast"
    return response


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

    from ...utils.normalization import parse_substitute_mpns

    if not manufacturer.strip():
        raise HTTPException(422, "Manufacturer is required")

    req = get_requisition_or_404(db, req_id)
    require_requisition_access(db, req_id, user)

    form_data = await request.form()
    sub_mpns = form_data.getlist("sub_mpn")
    sub_mfrs = form_data.getlist("sub_manufacturer")
    subs_raw = [{"mpn": m.strip(), "manufacturer": mfr.strip()} for m, mfr in zip(sub_mpns, sub_mfrs) if m.strip()]
    sub_list = parse_substitute_mpns(subs_raw, primary_mpn)

    from ...search_service import resolve_material_card
    from ...utils.normalization import normalize_mpn_key

    card = resolve_material_card(primary_mpn, db)
    r = Requirement(
        requisition_id=req_id,
        primary_mpn=primary_mpn,
        normalized_mpn=normalize_mpn_key(primary_mpn),
        material_card_id=card.id if card else None,
        target_qty=target_qty,
        brand=brand or None,
        manufacturer=manufacturer.strip(),
        substitutes=sub_list,
        target_price=target_price,
        condition=condition or None,
        date_codes=date_codes or None,
        firmware=firmware or None,
        hardware_codes=hardware_codes or None,
        packaging=packaging or None,
        notes=notes or None,
        customer_pn=customer_pn or None,
        need_by_date=_parse_date_safe(need_by_date, date_type),
        sourcing_status=SourcingStatus.OPEN,
    )
    db.add(r)
    for sub in sub_list:
        resolve_material_card(sub["mpn"], db, manufacturer=sub.get("manufacturer", ""))
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


@router.get("/v2/partials/requisitions/{req_id}/tab/{tab}", response_class=HTMLResponse)
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
        # Check for existing draft quote to show "Add to Quote" button
        draft_quote = (
            db.query(Quote)
            .filter(Quote.requisition_id == req_id, Quote.status == QuoteStatus.DRAFT)
            .order_by(Quote.created_at.desc())
            .first()
        )
        ctx["offers"] = offers
        ctx["draft_quote"] = draft_quote
        ctx["qual"] = qual
        return template_response("htmx/partials/requisitions/tabs/offers.html", ctx)

    elif tab == "quotes":
        quotes = (
            db.query(Quote).filter(Quote.requisition_id == req_id).order_by(Quote.created_at.desc().nullslast()).all()
        )
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
