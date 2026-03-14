"""
routers/htmx_views.py — HTMX + Alpine.js MVP frontend views.

Serves server-rendered HTML partials for the HTMX-based frontend.
Full page loads render base.html; HTMX requests get just the partial.
All routes live under /v2 to coexist with the original SPA frontend.

Called by: main.py (router mount)
Depends on: models, dependencies, database, search_service
"""

import time

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session, joinedload

from ..database import get_db
from ..dependencies import get_user, require_user
from ..models import (
    Company,
    CustomerSite,
    Requirement,
    Requisition,
    Sighting,
    User,
    VendorCard,
)
from ..models.vendors import VendorContact
from ..utils.sql_helpers import escape_like

router = APIRouter(tags=["htmx-views"])
templates = Jinja2Templates(directory="app/templates")


def _is_htmx(request: Request) -> bool:
    """Check if this is an HTMX partial request (vs full page load)."""
    return request.headers.get("HX-Request") == "true"


def _base_ctx(request: Request, user: User, current_view: str = "") -> dict:
    """Shared template context for all views."""
    return {
        "request": request,
        "user_name": user.name if user else "",
        "user_email": user.email if user else "",
        "is_admin": user.role == "admin" if user else False,
        "current_view": current_view,
    }


# ── Full page entry points ──────────────────────────────────────────────


@router.get("/v2", response_class=HTMLResponse)
@router.get("/v2/requisitions", response_class=HTMLResponse)
@router.get("/v2/requisitions/{req_id:int}", response_class=HTMLResponse)
@router.get("/v2/search", response_class=HTMLResponse)
@router.get("/v2/vendors", response_class=HTMLResponse)
@router.get("/v2/vendors/{vendor_id:int}", response_class=HTMLResponse)
@router.get("/v2/companies", response_class=HTMLResponse)
@router.get("/v2/companies/{company_id:int}", response_class=HTMLResponse)
async def v2_page(request: Request, db: Session = Depends(get_db)):
    """Full page load — serves base.html with initial content via HTMX."""
    user = get_user(request, db)
    if not user:
        return templates.TemplateResponse("htmx/login.html", {"request": request})

    # Determine which view to load based on URL path
    path = request.url.path
    if "/vendors" in path:
        current_view = "vendors"
    elif "/companies" in path:
        current_view = "companies"
    elif "/search" in path:
        current_view = "search"
    else:
        current_view = "requisitions"

    # Determine the correct partial URL for initial content load
    partial_url = f"/v2/partials/{current_view}"
    # Pass path params for detail views
    if current_view == "requisitions" and "/requisitions/" in path:
        parts = path.split("/requisitions/")
        if len(parts) > 1 and parts[1].isdigit():
            partial_url = f"/v2/partials/requisitions/{parts[1]}"
    elif current_view == "vendors" and "/vendors/" in path:
        parts = path.split("/vendors/")
        if len(parts) > 1 and parts[1].isdigit():
            partial_url = f"/v2/partials/vendors/{parts[1]}"
    elif current_view == "companies" and "/companies/" in path:
        parts = path.split("/companies/")
        if len(parts) > 1 and parts[1].isdigit():
            partial_url = f"/v2/partials/companies/{parts[1]}"

    ctx = _base_ctx(request, user, current_view)
    ctx["partial_url"] = partial_url
    return templates.TemplateResponse("htmx/base_page.html", ctx)


# ── Requisition partials ────────────────────────────────────────────────


@router.get("/v2/partials/requisitions", response_class=HTMLResponse)
async def requisitions_list_partial(
    request: Request,
    q: str = "",
    status: str = "",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return requisitions list as HTML partial."""
    query = db.query(Requisition).options(joinedload(Requisition.creator))

    if q.strip():
        safe = escape_like(q.strip())
        query = query.filter(
            Requisition.name.ilike(f"%{safe}%")
            | Requisition.customer_name.ilike(f"%{safe}%")
        )
    if status:
        query = query.filter(Requisition.status == status)

    total = query.count()
    reqs = query.order_by(Requisition.created_at.desc()).offset(offset).limit(limit).all()

    # Attach requirement counts
    for req in reqs:
        req.req_count = len(req.requirements) if req.requirements else 0

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"requisitions": reqs, "q": q, "status": status, "total": total, "limit": limit, "offset": offset})
    return templates.TemplateResponse("htmx/partials/requisitions/list.html", ctx)


@router.get("/v2/partials/requisitions/{req_id}", response_class=HTMLResponse)
async def requisition_detail_partial(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return requisition detail as HTML partial."""
    req = (
        db.query(Requisition)
        .options(
            joinedload(Requisition.creator),
            joinedload(Requisition.requirements),
        )
        .filter(Requisition.id == req_id)
        .first()
    )
    if not req:
        raise HTTPException(404, "Requisition not found")

    requirements = req.requirements or []
    for r in requirements:
        r.sighting_count = len(r.sightings) if r.sightings else 0

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"req": req, "requirements": requirements})
    return templates.TemplateResponse("htmx/partials/requisitions/detail.html", ctx)


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
    """Create a new requisition and return updated list."""
    req = Requisition(
        name=name,
        customer_name=customer_name or None,
        deadline=deadline or None,
        urgency=urgency,
        status="active",
        created_by=user.id,
    )
    db.add(req)
    db.flush()

    # Parse parts text (format: "MPN, Qty" per line)
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
                r = Requirement(
                    requisition_id=req.id,
                    primary_mpn=mpn,
                    target_qty=qty,
                    sourcing_status="open",
                )
                db.add(r)

    db.commit()
    logger.info("Created requisition {} with parts from text", req.id)

    # Return the updated list
    return await requisitions_list_partial(request=request, q="", status="", limit=50, offset=0, user=user, db=db)


@router.post("/v2/partials/requisitions/{req_id}/requirements", response_class=HTMLResponse)
async def add_requirement(
    request: Request,
    req_id: int,
    primary_mpn: str = Form(...),
    target_qty: int = Form(1),
    brand: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add a requirement to a requisition, return the new row HTML."""
    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    if not req:
        raise HTTPException(404, "Requisition not found")

    r = Requirement(
        requisition_id=req_id,
        primary_mpn=primary_mpn,
        target_qty=target_qty,
        brand=brand or None,
        sourcing_status="open",
    )
    db.add(r)
    db.commit()
    db.refresh(r)

    # Return a table row for HTMX append
    r.sighting_count = 0
    html = f"""
    <tr class="hover:bg-gray-50">
      <td class="px-4 py-2 text-sm font-mono font-medium text-gray-900">{r.primary_mpn}</td>
      <td class="px-4 py-2 text-sm text-gray-600">{r.brand or '—'}</td>
      <td class="px-4 py-2 text-sm text-gray-600">{r.target_qty:,}</td>
      <td class="px-4 py-2 text-sm text-gray-600">—</td>
      <td class="px-4 py-2"><span class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full bg-gray-100 text-gray-700">Open</span></td>
      <td class="px-4 py-2 text-sm text-gray-600">0</td>
      <td class="px-4 py-2">
        <button hx-post="/v2/partials/search/run?requirement_id={r.id}&mpn={r.primary_mpn}"
                hx-target="#sightings-{r.id}"
                class="text-xs text-blue-600 hover:text-blue-500 font-medium">Search</button>
      </td>
    </tr>
    <tr id="sightings-{r.id}" class="bg-gray-50"></tr>
    """
    return HTMLResponse(html)


# ── Search partials ─────────────────────────────────────────────────────


@router.get("/v2/partials/search", response_class=HTMLResponse)
async def search_form_partial(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the search form partial."""
    ctx = _base_ctx(request, user, "search")
    return templates.TemplateResponse("htmx/partials/search/form.html", ctx)


@router.post("/v2/partials/search/run", response_class=HTMLResponse)
async def search_run(
    request: Request,
    mpn: str = Form(default=""),
    requirement_id: int = Query(default=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Run a part search and return results HTML.

    If requirement_id is provided, searches for that requirement's MPN.
    Otherwise uses the mpn form field.
    """
    search_mpn = mpn.strip()

    # If searching from a requirement row, get the MPN from query params
    if not search_mpn and requirement_id:
        req = db.query(Requirement).filter(Requirement.id == requirement_id).first()
        if req:
            search_mpn = req.primary_mpn or ""

    # Also check query params for mpn (when called from requirement detail)
    if not search_mpn:
        search_mpn = request.query_params.get("mpn", "").strip()

    if not search_mpn:
        return HTMLResponse('<div class="p-4 text-sm text-red-600">Please enter a part number.</div>')

    start = time.time()
    results = []
    error = None

    try:
        # Use the search service to find parts across all connectors
        from ..search_service import quick_search_mpn

        raw_results = await quick_search_mpn(search_mpn, db)
        results = raw_results if isinstance(raw_results, list) else raw_results.get("sightings", [])
    except Exception as exc:
        logger.error("Search failed for {}: {}", search_mpn, exc)
        error = f"Search error: {exc}"

    elapsed = time.time() - start

    ctx = _base_ctx(request, user, "search")
    ctx.update({
        "results": results,
        "mpn": search_mpn,
        "elapsed_seconds": elapsed,
        "error": error,
    })
    return templates.TemplateResponse("htmx/partials/search/results.html", ctx)


# ── Vendor partials ─────────────────────────────────────────────────────


@router.get("/v2/partials/vendors", response_class=HTMLResponse)
async def vendors_list_partial(
    request: Request,
    q: str = "",
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return vendor list as HTML partial."""
    query = db.query(VendorCard).filter(VendorCard.is_blacklisted.is_(False))

    if q.strip():
        safe = escape_like(q.strip())
        query = query.filter(
            VendorCard.display_name.ilike(f"%{safe}%")
            | VendorCard.domain.ilike(f"%{safe}%")
        )

    total = query.count()
    vendors = query.order_by(VendorCard.sighting_count.desc().nullslast()).offset(offset).limit(limit).all()

    ctx = _base_ctx(request, user, "vendors")
    ctx.update({"vendors": vendors, "q": q, "total": total, "limit": limit, "offset": offset})
    return templates.TemplateResponse("htmx/partials/vendors/list.html", ctx)


@router.get("/v2/partials/vendors/{vendor_id}", response_class=HTMLResponse)
async def vendor_detail_partial(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return vendor detail as HTML partial."""
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

    ctx = _base_ctx(request, user, "vendors")
    ctx.update({"vendor": vendor, "contacts": contacts, "recent_sightings": recent_sightings})
    return templates.TemplateResponse("htmx/partials/vendors/detail.html", ctx)


# ── Company partials ────────────────────────────────────────────────────


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
    """Return company detail as HTML partial."""
    company = (
        db.query(Company)
        .options(joinedload(Company.account_owner), joinedload(Company.sites))
        .filter(Company.id == company_id)
        .first()
    )
    if not company:
        raise HTTPException(404, "Company not found")

    sites = [s for s in (company.sites or []) if s.is_active]

    ctx = _base_ctx(request, user, "companies")
    ctx.update({"company": company, "sites": sites})
    return templates.TemplateResponse("htmx/partials/companies/detail.html", ctx)


# ── Dashboard partial ───────────────────────────────────────────────────


@router.get("/v2/partials/dashboard", response_class=HTMLResponse)
async def dashboard_partial(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return dashboard stats partial."""
    open_reqs = db.query(sqlfunc.count(Requisition.id)).filter(
        Requisition.status.in_(["open", "active", "sourcing", "draft"])
    ).scalar() or 0
    vendor_count = db.query(sqlfunc.count(VendorCard.id)).scalar() or 0
    company_count = db.query(sqlfunc.count(Company.id)).filter(Company.is_active.is_(True)).scalar() or 0

    ctx = _base_ctx(request, user, "dashboard")
    ctx["stats"] = {"open_reqs": open_reqs, "vendor_count": vendor_count, "company_count": company_count}
    return templates.TemplateResponse("htmx/partials/dashboard.html", ctx)
