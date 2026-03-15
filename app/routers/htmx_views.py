"""
routers/htmx_views.py — HTMX + Alpine.js MVP frontend views.

Serves server-rendered HTML partials for the HTMX-based frontend.
Full page loads render base.html; HTMX requests get just the partial.
All routes live under /v2 to coexist with the original SPA frontend.

Called by: main.py (router mount)
Depends on: models, dependencies, database, search_service
"""

import json
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session, joinedload

from ..database import get_db
from ..dependencies import get_user, require_user
from ..models import (
    BuyPlan,
    BuyPlanLine,
    Company,
    CustomerSite,
    Requirement,
    Requisition,
    Sighting,
    User,
    VendorCard,
    VerificationGroupMember,
)
from ..models.buy_plan import BuyPlanLineStatus, BuyPlanStatus, SOVerificationStatus
from ..models.vendors import VendorContact
from ..utils.sql_helpers import escape_like

router = APIRouter(tags=["htmx-views"])
templates = Jinja2Templates(directory="app/templates")

# Vite manifest for asset fingerprinting — read once at import time.
_MANIFEST_PATH = Path("app/static/dist/.vite/manifest.json")
_vite_manifest: dict = {}
if _MANIFEST_PATH.exists():
    _vite_manifest = json.loads(_MANIFEST_PATH.read_text())


def _vite_assets() -> dict:
    """Return Vite asset URLs for templates. Keys: js_file, css_files."""
    entry = _vite_manifest.get("htmx_app.js", {})
    js_file = entry.get("file", "assets/htmx_app.js")
    css_files = entry.get("css", [])
    # Also add standalone styles entry if not already in css list
    styles_entry = _vite_manifest.get("styles.css", {})
    if styles_entry.get("file") and styles_entry["file"] not in css_files:
        css_files = [styles_entry["file"]] + css_files
    return {"js_file": js_file, "css_files": css_files}


def _is_htmx(request: Request) -> bool:
    """Check if this is an HTMX partial request (vs full page load)."""
    return request.headers.get("HX-Request") == "true"


def _base_ctx(request: Request, user: User, current_view: str = "") -> dict:
    """Shared template context for all views."""
    assets = _vite_assets()
    return {
        "request": request,
        "user_name": user.name if user else "",
        "user_email": user.email if user else "",
        "is_admin": user.role == "admin" if user else False,
        "current_view": current_view,
        "vite_js": assets["js_file"],
        "vite_css": assets["css_files"],
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
@router.get("/v2/buy-plans", response_class=HTMLResponse)
@router.get("/v2/buy-plans/{bp_id:int}", response_class=HTMLResponse)
async def v2_page(request: Request, db: Session = Depends(get_db)):
    """Full page load — serves base.html with initial content via HTMX."""
    user = get_user(request, db)
    if not user:
        return templates.TemplateResponse("htmx/login.html", {"request": request})

    # Determine which view to load based on URL path
    path = request.url.path
    if "/buy-plans" in path:
        current_view = "buy-plans"
    elif "/vendors" in path:
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
    elif current_view == "buy-plans" and "/buy-plans/" in path:
        parts = path.split("/buy-plans/")
        if len(parts) > 1 and parts[1].isdigit():
            partial_url = f"/v2/partials/buy-plans/{parts[1]}"

    ctx = _base_ctx(request, user, current_view)
    ctx["partial_url"] = partial_url
    return templates.TemplateResponse("htmx/base_page.html", ctx)


# ── Global search ──────────────────────────────────────────────────────


@router.get("/v2/partials/search/global", response_class=HTMLResponse)
async def global_search(
    request: Request,
    q: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Global search across requisitions, companies, vendors."""
    results = {"requisitions": [], "companies": [], "vendors": []}
    if q and len(q) >= 2:
        safe = escape_like(q.strip())
        results["requisitions"] = (
            db.query(Requisition)
            .filter(
                Requisition.name.ilike(f"%{safe}%")
                | Requisition.customer_name.ilike(f"%{safe}%")
            )
            .limit(5)
            .all()
        )
        results["companies"] = (
            db.query(Company)
            .filter(Company.name.ilike(f"%{safe}%"))
            .limit(5)
            .all()
        )
        results["vendors"] = (
            db.query(VendorCard)
            .filter(VendorCard.name.ilike(f"%{safe}%"))
            .limit(5)
            .all()
        )
    return templates.TemplateResponse(
        "partials/shared/search_results.html",
        {**_base_ctx(request, user), "results": results, "query": q},
    )


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


# ── Buy Plans partials ─────────────────────────────────────────────────


def _is_ops_member(user: User, db: Session) -> bool:
    """Check if user is in the ops verification group."""
    return db.query(VerificationGroupMember).filter_by(
        user_id=user.id, is_active=True
    ).first() is not None


@router.get("/v2/partials/buy-plans", response_class=HTMLResponse)
async def buy_plans_list_partial(
    request: Request,
    q: str = "",
    status: str = "",
    mine: bool = False,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return buy plans list as HTML partial."""
    query = db.query(BuyPlan).options(
        joinedload(BuyPlan.quote),
        joinedload(BuyPlan.requisition),
        joinedload(BuyPlan.submitted_by),
        joinedload(BuyPlan.approved_by),
        joinedload(BuyPlan.lines),
    )

    if status:
        query = query.filter(BuyPlan.status == status)
    if mine:
        query = query.filter(BuyPlan.submitted_by_id == user.id)
    if q.strip():
        safe = escape_like(q.strip())
        query = query.filter(
            BuyPlan.sales_order_number.ilike(f"%{safe}%")
            | BuyPlan.customer_po_number.ilike(f"%{safe}%")
        )

    # Sales users only see their own
    if user.role == "sales":
        query = query.filter(BuyPlan.submitted_by_id == user.id)

    plans = query.order_by(BuyPlan.created_at.desc()).limit(200).all()

    # Build lightweight list items
    buy_plans = []
    for p in plans:
        customer_name = None
        if p.quote and p.quote.customer_site:
            site = p.quote.customer_site
            co = site.company if hasattr(site, "company") else None
            customer_name = co.name if co else getattr(site, "site_name", None)

        buy_plans.append({
            "id": p.id,
            "quote_id": p.quote_id,
            "quote_number": p.quote.quote_number if p.quote else None,
            "customer_name": customer_name,
            "sales_order_number": p.sales_order_number,
            "status": p.status,
            "so_status": p.so_status,
            "total_cost": float(p.total_cost) if p.total_cost else 0,
            "total_margin_pct": float(p.total_margin_pct) if p.total_margin_pct else 0,
            "line_count": len(p.lines) if p.lines else 0,
            "submitted_by_name": p.submitted_by.name if p.submitted_by else None,
            "auto_approved": p.auto_approved or False,
            "created_at": str(p.created_at) if p.created_at else None,
        })

    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update({
        "buy_plans": buy_plans,
        "q": q,
        "status": status,
        "mine": mine,
        "total": len(buy_plans),
    })
    return templates.TemplateResponse("htmx/partials/buy_plans/list.html", ctx)


@router.get("/v2/partials/buy-plans/{plan_id}", response_class=HTMLResponse)
async def buy_plan_detail_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return buy plan detail as HTML partial."""
    bp = (
        db.query(BuyPlan)
        .options(
            joinedload(BuyPlan.lines).joinedload(BuyPlanLine.offer),
            joinedload(BuyPlan.lines).joinedload(BuyPlanLine.requirement),
            joinedload(BuyPlan.lines).joinedload(BuyPlanLine.buyer),
            joinedload(BuyPlan.quote),
            joinedload(BuyPlan.requisition),
            joinedload(BuyPlan.submitted_by),
            joinedload(BuyPlan.approved_by),
        )
        .filter(BuyPlan.id == plan_id)
        .first()
    )
    if not bp:
        raise HTTPException(404, "Buy plan not found")

    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update({
        "bp": bp,
        "lines": bp.lines or [],
        "is_ops_member": _is_ops_member(user, db),
        "user": user,
    })
    return templates.TemplateResponse("htmx/partials/buy_plans/detail.html", ctx)


@router.post("/v2/partials/buy-plans/{plan_id}/submit", response_class=HTMLResponse)
async def buy_plan_submit_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Submit a draft buy plan with SO# — returns refreshed detail partial."""
    from ..services.buyplan_workflow import submit_buy_plan
    from ..services.buyplan_notifications import (
        notify_approved,
        notify_submitted,
        run_notify_bg,
    )

    form = await request.form()
    so = form.get("sales_order_number", "").strip()
    if not so:
        raise HTTPException(400, "Sales Order # is required")

    try:
        plan = submit_buy_plan(
            plan_id, so, user, db,
            customer_po_number=form.get("customer_po_number") or None,
            salesperson_notes=form.get("salesperson_notes") or None,
        )
        db.commit()
        if plan.auto_approved:
            run_notify_bg(notify_approved, plan.id)
        else:
            run_notify_bg(notify_submitted, plan.id)
    except ValueError as e:
        raise HTTPException(400, str(e))

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/approve", response_class=HTMLResponse)
async def buy_plan_approve_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Manager approves or rejects a pending buy plan — returns refreshed detail."""
    from ..services.buyplan_workflow import approve_buy_plan
    from ..services.buyplan_notifications import (
        notify_approved,
        notify_rejected,
        run_notify_bg,
    )

    form = await request.form()
    action = form.get("action", "approve")

    if user.role not in ("manager", "admin"):
        raise HTTPException(403, "Manager or admin role required")

    try:
        plan = approve_buy_plan(plan_id, action, user, db, notes=form.get("notes"))
        db.commit()
        if action == "approve":
            run_notify_bg(notify_approved, plan.id)
        else:
            run_notify_bg(notify_rejected, plan.id)
    except (ValueError, PermissionError) as e:
        raise HTTPException(400, str(e))

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/verify-so", response_class=HTMLResponse)
async def buy_plan_verify_so_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Ops verifies SO — returns refreshed detail."""
    from ..services.buyplan_workflow import verify_so
    from ..services.buyplan_notifications import (
        notify_so_rejected,
        notify_so_verified,
        run_notify_bg,
    )

    form = await request.form()
    action = form.get("action", "approve")

    try:
        plan = verify_so(
            plan_id, action, user, db,
            rejection_note=form.get("rejection_note"),
        )
        db.commit()
        if action == "approve":
            run_notify_bg(notify_so_verified, plan.id)
        else:
            run_notify_bg(notify_so_rejected, plan.id, action=action)
    except (ValueError, PermissionError) as e:
        raise HTTPException(400, str(e))

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/lines/{line_id}/confirm-po", response_class=HTMLResponse)
async def buy_plan_confirm_po_partial(
    request: Request,
    plan_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Buyer confirms PO — returns refreshed detail."""
    from datetime import datetime
    from ..services.buyplan_workflow import confirm_po
    from ..services.buyplan_notifications import notify_po_confirmed, run_notify_bg

    form = await request.form()
    po_number = form.get("po_number", "").strip()
    ship_date_str = form.get("estimated_ship_date", "")

    if not po_number:
        raise HTTPException(400, "PO number is required")

    ship_date = None
    if ship_date_str:
        try:
            ship_date = datetime.fromisoformat(ship_date_str)
        except ValueError:
            ship_date = datetime.now()
    else:
        ship_date = datetime.now()

    try:
        confirm_po(plan_id, line_id, po_number, ship_date, user, db)
        db.commit()
        run_notify_bg(notify_po_confirmed, plan_id, line_id=line_id)
    except ValueError as e:
        raise HTTPException(400, str(e))

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/lines/{line_id}/verify-po", response_class=HTMLResponse)
async def buy_plan_verify_po_partial(
    request: Request,
    plan_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Ops verifies PO — returns refreshed detail."""
    from ..services.buyplan_workflow import check_completion, verify_po
    from ..services.buyplan_notifications import notify_completed, run_notify_bg

    form = await request.form()
    action = form.get("action", "approve")

    try:
        verify_po(plan_id, line_id, action, user, db, rejection_note=form.get("rejection_note"))
        db.commit()
        updated = check_completion(plan_id, db)
        if updated and updated.status == "completed":
            db.commit()
            run_notify_bg(notify_completed, plan_id)
    except (ValueError, PermissionError) as e:
        raise HTTPException(400, str(e))

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/lines/{line_id}/issue", response_class=HTMLResponse)
async def buy_plan_flag_issue_partial(
    request: Request,
    plan_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Buyer flags issue on a line — returns refreshed detail."""
    from ..services.buyplan_workflow import flag_line_issue

    form = await request.form()
    issue_type = form.get("issue_type", "other")
    note = form.get("note", "")

    try:
        flag_line_issue(plan_id, line_id, issue_type, user, db, note=note)
        db.commit()
    except ValueError as e:
        raise HTTPException(400, str(e))

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/cancel", response_class=HTMLResponse)
async def buy_plan_cancel_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Cancel a buy plan — returns refreshed detail."""
    from datetime import datetime, timezone

    bp = db.get(BuyPlan, plan_id)
    if not bp:
        raise HTTPException(404, "Buy plan not found")
    if bp.status in ("completed", "cancelled"):
        raise HTTPException(400, f"Cannot cancel plan in '{bp.status}' status")

    form = await request.form()
    bp.status = BuyPlanStatus.cancelled.value
    bp.cancelled_at = datetime.now(timezone.utc)
    bp.cancelled_by_id = user.id
    bp.cancellation_reason = form.get("reason")
    db.commit()

    return await buy_plan_detail_partial(request, plan_id, user, db)


@router.post("/v2/partials/buy-plans/{plan_id}/reset", response_class=HTMLResponse)
async def buy_plan_reset_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Reset halted/cancelled plan to draft — returns refreshed detail."""
    from ..services.buyplan_workflow import reset_buy_plan_to_draft

    try:
        reset_buy_plan_to_draft(plan_id, user, db)
        db.commit()
    except ValueError as e:
        raise HTTPException(400, str(e))

    return await buy_plan_detail_partial(request, plan_id, user, db)
