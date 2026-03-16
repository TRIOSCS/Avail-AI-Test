"""
routers/htmx/companies.py — HTMX partials for the Companies section.

Serves server-rendered HTML partials for company list, detail, and tab views.
Routes: /v2/partials/companies, /v2/partials/companies/{id},
        /v2/partials/companies/{id}/tab/{tab}

Called by: main.py via the shared htmx router
Depends on: models (Company, Requisition, CustomerSite, User),
            services.company_detail_service, dependencies (require_user, get_db)
"""

from fastapi import Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session, joinedload

from ...database import get_db
from ...dependencies import require_user
from ...models import Company, CustomerSite, Requisition, User
from ._helpers import _DASH, _base_ctx, escape_like, router, templates


@router.get("/v2/partials/companies", response_class=HTMLResponse)
async def companies_list_partial(
    request: Request,
    search: str = "",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return companies list as HTML partial."""
    query = db.query(Company).filter(Company.is_active.is_(True)).options(joinedload(Company.account_owner))

    if search.strip():
        safe = escape_like(search.strip())
        query = query.filter(Company.name.ilike(f"%{safe}%"))

    total = query.count()
    companies = query.order_by(Company.name).offset(offset).limit(limit).all()

    ctx = _base_ctx(request, user, "companies")
    ctx.update({"companies": companies, "search": search, "total": total, "limit": limit, "offset": offset})
    return templates.TemplateResponse("htmx/partials/companies/list.html", ctx)


@router.get("/v2/partials/companies/create-form", response_class=HTMLResponse)
async def company_create_form(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return inline HTML form for creating a new company."""
    account_types = ["Customer", "Prospect", "Partner", "Competitor"]
    options = "\n".join(
        f'<option value="{t}">{t}</option>' for t in account_types
    )
    html = f"""
    <div class="max-w-2xl mx-auto p-6">
      <h2 class="text-xl font-semibold text-gray-900 mb-6">Create New Company</h2>
      <form hx-post="/v2/partials/companies/create" hx-target="#main-content"
            class="space-y-4">
        <div>
          <label class="block text-sm font-medium text-gray-700 mb-1">Company Name *</label>
          <input type="text" name="name" required
                 class="w-full rounded-md border-gray-300 shadow-sm focus:border-brand-500 focus:ring-brand-500 text-sm" />
        </div>
        <div class="grid grid-cols-2 gap-4">
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">Website</label>
            <input type="text" name="website"
                   class="w-full rounded-md border-gray-300 shadow-sm focus:border-brand-500 focus:ring-brand-500 text-sm" />
          </div>
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">Industry</label>
            <input type="text" name="industry"
                   class="w-full rounded-md border-gray-300 shadow-sm focus:border-brand-500 focus:ring-brand-500 text-sm" />
          </div>
        </div>
        <div class="grid grid-cols-2 gap-4">
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">Account Type</label>
            <select name="account_type"
                    class="w-full rounded-md border-gray-300 shadow-sm focus:border-brand-500 focus:ring-brand-500 text-sm">
              <option value="">-- Select --</option>
              {options}
            </select>
          </div>
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">Domain</label>
            <input type="text" name="domain"
                   class="w-full rounded-md border-gray-300 shadow-sm focus:border-brand-500 focus:ring-brand-500 text-sm" />
          </div>
        </div>
        <div>
          <label class="block text-sm font-medium text-gray-700 mb-1">Phone</label>
          <input type="text" name="phone"
                 class="w-full rounded-md border-gray-300 shadow-sm focus:border-brand-500 focus:ring-brand-500 text-sm" />
        </div>
        <div>
          <label class="block text-sm font-medium text-gray-700 mb-1">Notes</label>
          <textarea name="notes" rows="3"
                    class="w-full rounded-md border-gray-300 shadow-sm focus:border-brand-500 focus:ring-brand-500 text-sm"></textarea>
        </div>
        <div class="flex justify-end gap-3 pt-4">
          <button type="button"
                  hx-get="/v2/partials/companies" hx-target="#main-content"
                  class="px-4 py-2 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-md hover:bg-gray-50">
            Cancel
          </button>
          <button type="submit"
                  class="px-4 py-2 text-sm font-medium text-white bg-brand-600 border border-transparent rounded-md hover:bg-brand-700">
            Create Company
          </button>
        </div>
      </form>
    </div>
    """
    return HTMLResponse(html)


@router.get("/v2/partials/companies/typeahead", response_class=JSONResponse)
async def company_typeahead(
    request: Request,
    q: str = Query("", min_length=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return JSON list for company typeahead (name + sites)."""
    if len(q.strip()) < 2:
        return JSONResponse([])

    safe = escape_like(q.strip())
    companies = (
        db.query(Company)
        .filter(Company.is_active.is_(True), Company.name.ilike(f"%{safe}%"))
        .options(joinedload(Company.sites))
        .order_by(Company.name)
        .limit(20)
        .all()
    )

    results = []
    for c in companies:
        sites = [
            {"id": s.id, "site_name": s.site_name}
            for s in (c.sites or [])
            if s.is_active
        ]
        results.append({"id": c.id, "name": c.name, "sites": sites})

    return JSONResponse(results)


@router.get("/v2/partials/companies/{company_id}", response_class=HTMLResponse)
async def company_detail_partial(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return company detail as HTML partial with tabs."""
    company = (
        db.query(Company)
        .options(joinedload(Company.account_owner), joinedload(Company.sites))
        .filter(Company.id == company_id)
        .first()
    )
    if not company:
        raise HTTPException(404, "Company not found")

    sites = [s for s in (company.sites or []) if s.is_active]

    # Count open requisitions for this company
    open_req_count = (
        db.query(sqlfunc.count(Requisition.id))
        .filter(
            Requisition.customer_name == company.name,
            Requisition.status.in_(["open", "active", "sourcing", "draft"]),
        )
        .scalar()
        or 0
    )

    ctx = _base_ctx(request, user, "companies")
    ctx.update(
        {
            "company": company,
            "sites": sites,
            "open_req_count": open_req_count,
            "user": user,
        }
    )
    return templates.TemplateResponse("htmx/partials/companies/detail.html", ctx)


@router.get("/v2/partials/companies/{company_id}/tab/{tab}", response_class=HTMLResponse)
async def company_tab(
    request: Request,
    company_id: int,
    tab: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return a specific tab partial for company detail."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    valid_tabs = {"sites", "contacts", "requisitions", "activity"}
    if tab not in valid_tabs:
        raise HTTPException(404, f"Unknown tab: {tab}")

    from ...services.company_detail_service import (
        get_company_contacts,
        get_company_requisitions,
        get_company_sites,
    )

    if tab == "sites":
        sites = get_company_sites(db, company_id)
        rows = []
        for s in sites:
            rows.append(f"""<tr class="hover:bg-brand-50">
              <td class="px-4 py-2 text-sm font-medium text-gray-900">{s.site_name or _DASH}</td>
              <td class="px-4 py-2 text-sm text-gray-500">{s.site_type or _DASH}</td>
              <td class="px-4 py-2 text-sm text-gray-500">{s.city or _DASH}</td>
              <td class="px-4 py-2 text-sm text-gray-500">{s.country or _DASH}</td>
            </tr>""")
        if rows:
            html = f"""<div class="overflow-x-auto">
              <table class="min-w-full divide-y divide-gray-200">
                <thead class="bg-gray-50">
                  <tr>
                    <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Site Name</th>
                    <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Type</th>
                    <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">City</th>
                    <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Country</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-gray-200">{"".join(rows)}</tbody>
              </table>
            </div>"""
        else:
            html = '<div class="p-8 text-center"><p class="text-sm text-gray-500">No sites found.</p></div>'
        return HTMLResponse(html)

    elif tab == "contacts":
        contacts = get_company_contacts(db, company_id)
        rows = []
        for c in contacts:
            phone = c.get("contact_phone")
            phone_html = (
                f'<a href="tel:{phone}" class="text-brand-500 hover:text-brand-600">{phone}</a>'
                if phone
                else '<span class="text-gray-500">\u2014</span>'
            )
            rows.append(f"""<tr class="hover:bg-brand-50">
                  <td class="px-4 py-2 text-sm font-medium text-gray-900">{c.get("contact_name") or _DASH}</td>
                  <td class="px-4 py-2 text-sm text-gray-500">{c.get("site_name") or _DASH}</td>
                  <td class="px-4 py-2 text-sm text-gray-500">{c.get("contact_email") or _DASH}</td>
                  <td class="px-4 py-2 text-sm">{phone_html}</td>
                </tr>""")
        if rows:
            html = f"""<div class="overflow-x-auto">
              <table class="min-w-full divide-y divide-gray-200">
                <thead class="bg-gray-50">
                  <tr>
                    <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Name</th>
                    <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Site</th>
                    <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Email</th>
                    <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Phone</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-gray-200">{"".join(rows)}</tbody>
              </table>
            </div>"""
        else:
            html = '<div class="p-8 text-center"><p class="text-sm text-gray-500">No contacts found.</p></div>'
        return HTMLResponse(html)

    elif tab == "requisitions":
        reqs = get_company_requisitions(db, company.name)
        rows = []
        for r in reqs:
            date_str = r.created_at.strftime("%b %d, %Y") if r.created_at else "\u2014"
            rows.append(f"""<tr class="hover:bg-brand-50 cursor-pointer"
                hx-get="/v2/partials/requisitions/{r.id}"
                hx-target="#main-content"
                hx-push-url="/v2/requisitions/{r.id}">
              <td class="px-4 py-2 text-sm font-medium text-brand-500">{r.name}</td>
              <td class="px-4 py-2 text-sm text-gray-500">{r.status or _DASH}</td>
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

    else:  # activity
        html = '<div class="p-8 text-center"><p class="text-sm text-gray-500">No activity recorded yet.</p></div>'
        return HTMLResponse(html)


@router.post("/v2/partials/companies/create", response_class=HTMLResponse)
async def company_create(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    name: str = Form(""),
    website: str = Form(""),
    industry: str = Form(""),
    notes: str = Form(""),
    domain: str = Form(""),
    account_type: str = Form(""),
    phone: str = Form(""),
):
    """Create a new company with an auto-generated HQ site."""
    name = name.strip()
    if not name:
        html = """
        <div class="max-w-2xl mx-auto p-6">
          <div class="rounded-md bg-red-50 p-4">
            <p class="text-sm text-red-700">Company name is required.</p>
          </div>
        </div>
        """
        return HTMLResponse(html, status_code=422)

    company = Company(
        name=name,
        website=website.strip() or None,
        industry=industry.strip() or None,
        notes=notes.strip() or None,
        domain=domain.strip() or None,
        account_type=account_type.strip() or None,
        phone=phone.strip() or None,
        is_active=True,
    )
    db.add(company)
    db.flush()

    hq_site = CustomerSite(
        company_id=company.id,
        site_name="HQ",
        is_active=True,
    )
    db.add(hq_site)
    db.commit()
    db.refresh(company)

    logger.info("Created company id={} name={!r} with HQ site", company.id, company.name)

    html = f"""
    <div class="max-w-2xl mx-auto p-6">
      <div class="rounded-md bg-green-50 p-4 mb-4">
        <p class="text-sm text-green-700">Company <strong>{company.name}</strong> created successfully.</p>
      </div>
    </div>
    """
    response = HTMLResponse(html)
    response.headers["HX-Trigger"] = "refreshCompanyList"
    return response


@router.get("/v2/partials/companies/{company_id}/edit", response_class=HTMLResponse)
async def company_edit_form(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return inline HTML form pre-filled with current company values."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    account_types = ["Customer", "Prospect", "Partner", "Competitor"]

    def _opt(val):
        opts = ['<option value="">-- Select --</option>']
        for t in account_types:
            sel = " selected" if val and val == t else ""
            opts.append(f'<option value="{t}"{sel}>{t}</option>')
        return "\n".join(opts)

    def _v(val):
        """Escape a value for an HTML attribute."""
        if val is None:
            return ""
        return str(val).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")

    html = f"""
    <div class="max-w-2xl mx-auto p-6">
      <h2 class="text-xl font-semibold text-gray-900 mb-6">Edit Company: {_v(company.name)}</h2>
      <form hx-put="/v2/partials/companies/{company.id}" hx-target="#main-content"
            class="space-y-4">
        <div>
          <label class="block text-sm font-medium text-gray-700 mb-1">Company Name</label>
          <input type="text" name="name" value="{_v(company.name)}"
                 class="w-full rounded-md border-gray-300 shadow-sm focus:border-brand-500 focus:ring-brand-500 text-sm" />
        </div>
        <div class="grid grid-cols-2 gap-4">
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">Website</label>
            <input type="text" name="website" value="{_v(company.website)}"
                   class="w-full rounded-md border-gray-300 shadow-sm focus:border-brand-500 focus:ring-brand-500 text-sm" />
          </div>
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">Industry</label>
            <input type="text" name="industry" value="{_v(company.industry)}"
                   class="w-full rounded-md border-gray-300 shadow-sm focus:border-brand-500 focus:ring-brand-500 text-sm" />
          </div>
        </div>
        <div class="grid grid-cols-2 gap-4">
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">Account Type</label>
            <select name="account_type"
                    class="w-full rounded-md border-gray-300 shadow-sm focus:border-brand-500 focus:ring-brand-500 text-sm">
              {_opt(company.account_type)}
            </select>
          </div>
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">Domain</label>
            <input type="text" name="domain" value="{_v(company.domain)}"
                   class="w-full rounded-md border-gray-300 shadow-sm focus:border-brand-500 focus:ring-brand-500 text-sm" />
          </div>
        </div>
        <div class="grid grid-cols-2 gap-4">
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">Phone</label>
            <input type="text" name="phone" value="{_v(company.phone)}"
                   class="w-full rounded-md border-gray-300 shadow-sm focus:border-brand-500 focus:ring-brand-500 text-sm" />
          </div>
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">Credit Terms</label>
            <input type="text" name="credit_terms" value="{_v(company.credit_terms)}"
                   class="w-full rounded-md border-gray-300 shadow-sm focus:border-brand-500 focus:ring-brand-500 text-sm" />
          </div>
        </div>
        <div class="grid grid-cols-3 gap-4">
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">HQ City</label>
            <input type="text" name="hq_city" value="{_v(company.hq_city)}"
                   class="w-full rounded-md border-gray-300 shadow-sm focus:border-brand-500 focus:ring-brand-500 text-sm" />
          </div>
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">HQ State</label>
            <input type="text" name="hq_state" value="{_v(company.hq_state)}"
                   class="w-full rounded-md border-gray-300 shadow-sm focus:border-brand-500 focus:ring-brand-500 text-sm" />
          </div>
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">HQ Country</label>
            <input type="text" name="hq_country" value="{_v(company.hq_country)}"
                   class="w-full rounded-md border-gray-300 shadow-sm focus:border-brand-500 focus:ring-brand-500 text-sm" />
          </div>
        </div>
        <div>
          <label class="block text-sm font-medium text-gray-700 mb-1">Notes</label>
          <textarea name="notes" rows="3"
                    class="w-full rounded-md border-gray-300 shadow-sm focus:border-brand-500 focus:ring-brand-500 text-sm">{_v(company.notes)}</textarea>
        </div>
        <div class="flex justify-end gap-3 pt-4">
          <button type="button"
                  hx-get="/v2/partials/companies/{company.id}" hx-target="#main-content"
                  class="px-4 py-2 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-md hover:bg-gray-50">
            Cancel
          </button>
          <button type="submit"
                  class="px-4 py-2 text-sm font-medium text-white bg-brand-600 border border-transparent rounded-md hover:bg-brand-700">
            Save Changes
          </button>
        </div>
      </form>
    </div>
    """
    return HTMLResponse(html)


@router.put("/v2/partials/companies/{company_id}", response_class=HTMLResponse)
async def company_update(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    name: str = Form(None),
    website: str = Form(None),
    industry: str = Form(None),
    notes: str = Form(None),
    domain: str = Form(None),
    account_type: str = Form(None),
    phone: str = Form(None),
    credit_terms: str = Form(None),
    hq_city: str = Form(None),
    hq_state: str = Form(None),
    hq_country: str = Form(None),
):
    """Update an existing company and return the refreshed detail partial."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    fields = {
        "name": name,
        "website": website,
        "industry": industry,
        "notes": notes,
        "domain": domain,
        "account_type": account_type,
        "phone": phone,
        "credit_terms": credit_terms,
        "hq_city": hq_city,
        "hq_state": hq_state,
        "hq_country": hq_country,
    }

    updated = []
    for field_name, value in fields.items():
        if value is not None:
            cleaned = value.strip() if value else None
            setattr(company, field_name, cleaned or None)
            updated.append(field_name)

    if not company.name:
        html = """
        <div class="max-w-2xl mx-auto p-6">
          <div class="rounded-md bg-red-50 p-4">
            <p class="text-sm text-red-700">Company name cannot be blank.</p>
          </div>
        </div>
        """
        return HTMLResponse(html, status_code=422)

    db.commit()
    db.refresh(company)
    logger.info("Updated company id={} fields={}", company.id, updated)

    # Return the full detail partial so the page refreshes cleanly
    company = (
        db.query(Company)
        .options(joinedload(Company.account_owner), joinedload(Company.sites))
        .filter(Company.id == company_id)
        .first()
    )
    sites = [s for s in (company.sites or []) if s.is_active]
    open_req_count = (
        db.query(sqlfunc.count(Requisition.id))
        .filter(
            Requisition.customer_name == company.name,
            Requisition.status.in_(["open", "active", "sourcing", "draft"]),
        )
        .scalar()
        or 0
    )

    ctx = _base_ctx(request, user, "companies")
    ctx.update(
        {
            "company": company,
            "sites": sites,
            "open_req_count": open_req_count,
            "user": user,
        }
    )
    response = templates.TemplateResponse("htmx/partials/companies/detail.html", ctx)
    response.headers["HX-Trigger"] = "refreshCompanyDetail"
    return response
