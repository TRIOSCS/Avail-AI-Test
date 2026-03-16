"""
routers/htmx/companies.py — HTMX partials for the Companies section.

Serves server-rendered HTML partials for company list, detail, and tab views.
Routes: /v2/partials/companies, /v2/partials/companies/{id},
        /v2/partials/companies/{id}/tab/{tab}

Called by: main.py via the shared htmx router
Depends on: models (Company, Requisition, CustomerSite, User),
            services.company_detail_service, dependencies (require_user, get_db)
"""

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session, joinedload

from ...database import get_db
from ...dependencies import require_user
from ...models import Company, Requisition, User
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
