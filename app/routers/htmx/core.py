"""routers/htmx/core.py — Page shell, global search, dashboard, settings.

Handles full-page entry points (base.html with HTMX partial loading),
global search across entities, dashboard stats, and settings tabs.

Called by: main.py (via router from _helpers)
Depends on: models, dependencies, _helpers
"""

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ...database import get_db
from ...dependencies import get_user, require_user
from ...models import ApiSource, Company, Requisition, User, VendorCard
from ._helpers import _base_ctx, escape_like, router, templates

# ── Full page entry points ──────────────────────────────────────────────


@router.get("/requisitions", response_class=HTMLResponse)
@router.get("/requisitions/{req_id:int}", response_class=HTMLResponse)
@router.get("/search", response_class=HTMLResponse)
@router.get("/vendors", response_class=HTMLResponse)
@router.get("/vendors/{vendor_id:int}", response_class=HTMLResponse)
@router.get("/companies", response_class=HTMLResponse)
@router.get("/companies/{company_id:int}", response_class=HTMLResponse)
@router.get("/buy-plans", response_class=HTMLResponse)
@router.get("/buy-plans/{bp_id:int}", response_class=HTMLResponse)
@router.get("/quotes", response_class=HTMLResponse)
@router.get("/quotes/{quote_id:int}", response_class=HTMLResponse)
@router.get("/settings", response_class=HTMLResponse)
@router.get("/prospecting", response_class=HTMLResponse)
@router.get("/prospecting/{prospect_id:int}", response_class=HTMLResponse)
@router.get("/proactive", response_class=HTMLResponse)
@router.get("/strategic", response_class=HTMLResponse)
async def htmx_page(request: Request, db: Session = Depends(get_db)):
    """Full page load — serves base.html with initial content via HTMX."""
    user = get_user(request, db)
    if not user:
        return templates.TemplateResponse("htmx/login.html", {"request": request})

    # Determine which view to load based on URL path
    path = request.url.path
    if "/buy-plans" in path:
        current_view = "buy-plans"
    elif "/quotes" in path:
        current_view = "quotes"
    elif "/prospecting" in path:
        current_view = "prospecting"
    elif "/proactive" in path:
        current_view = "proactive"
    elif "/strategic" in path:
        current_view = "strategic"
    elif "/settings" in path:
        current_view = "settings"
    elif "/vendors" in path:
        current_view = "vendors"
    elif "/companies" in path:
        current_view = "companies"
    elif "/search" in path:
        current_view = "search"
    elif "/requisitions" in path:
        current_view = "requisitions"
    else:
        current_view = "dashboard"

    # Determine the correct partial URL for initial content load
    partial_url = f"/partials/{current_view}"
    # Pass path params for detail views
    if current_view == "requisitions" and "/requisitions/" in path:
        parts = path.split("/requisitions/")
        if len(parts) > 1 and parts[1].isdigit():
            partial_url = f"/partials/requisitions/{parts[1]}"
    elif current_view == "vendors" and "/vendors/" in path:
        parts = path.split("/vendors/")
        if len(parts) > 1 and parts[1].isdigit():
            partial_url = f"/partials/vendors/{parts[1]}"
    elif current_view == "companies" and "/companies/" in path:
        parts = path.split("/companies/")
        if len(parts) > 1 and parts[1].isdigit():
            partial_url = f"/partials/companies/{parts[1]}"
    elif current_view == "buy-plans" and "/buy-plans/" in path:
        parts = path.split("/buy-plans/")
        if len(parts) > 1 and parts[1].isdigit():
            partial_url = f"/partials/buy-plans/{parts[1]}"
    elif current_view == "quotes" and "/quotes/" in path:
        parts = path.split("/quotes/")
        if len(parts) > 1 and parts[1].isdigit():
            partial_url = f"/partials/quotes/{parts[1]}"
    elif current_view == "prospecting" and "/prospecting/" in path:
        parts = path.split("/prospecting/")
        if len(parts) > 1 and parts[1].isdigit():
            partial_url = f"/partials/prospecting/{parts[1]}"

    ctx = _base_ctx(request, user, current_view)
    ctx["partial_url"] = partial_url
    return templates.TemplateResponse("htmx/base_page.html", ctx)


# ── Global search ──────────────────────────────────────────────────────


@router.get("/partials/search/global", response_class=HTMLResponse)
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
            .filter(Requisition.name.ilike(f"%{safe}%") | Requisition.customer_name.ilike(f"%{safe}%"))
            .limit(5)
            .all()
        )
        results["companies"] = db.query(Company).filter(Company.name.ilike(f"%{safe}%")).limit(5).all()
        results["vendors"] = (
            db.query(VendorCard)
            .filter(VendorCard.display_name.ilike(f"%{safe}%") | VendorCard.normalized_name.ilike(f"%{safe}%"))
            .limit(5)
            .all()
        )
    return templates.TemplateResponse(
        "partials/shared/search_results.html",
        {**_base_ctx(request, user), "results": results, "query": q},
    )


# ── Dashboard partial ───────────────────────────────────────────────────


@router.get("/partials/dashboard", response_class=HTMLResponse)
async def dashboard_partial(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return dashboard stats partial."""
    open_reqs = (
        db.query(sqlfunc.count(Requisition.id))
        .filter(Requisition.status.in_(["open", "active", "sourcing", "draft"]))
        .scalar()
        or 0
    )
    vendor_count = db.query(sqlfunc.count(VendorCard.id)).scalar() or 0
    company_count = db.query(sqlfunc.count(Company.id)).filter(Company.is_active.is_(True)).scalar() or 0

    ctx = _base_ctx(request, user, "dashboard")
    ctx["stats"] = {"open_reqs": open_reqs, "vendor_count": vendor_count, "company_count": company_count}
    return templates.TemplateResponse("htmx/partials/dashboard.html", ctx)


# ── Settings partials ────────────────────────────────────────────────


@router.get("/partials/settings", response_class=HTMLResponse)
async def settings_partial(
    request: Request,
    tab: str = "sources",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Settings page — renders index with active tab."""
    ctx = _base_ctx(request, user, "settings")
    ctx["active_tab"] = tab
    ctx["is_admin"] = user.role == "admin"
    return templates.TemplateResponse("htmx/partials/settings/index.html", ctx)


@router.get("/partials/settings/sources", response_class=HTMLResponse)
async def settings_sources_tab(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Sources tab content."""
    sources = db.query(ApiSource).order_by(ApiSource.display_name).all()
    ctx = _base_ctx(request, user, "settings")
    ctx["sources"] = sources
    return templates.TemplateResponse("htmx/partials/settings/sources.html", ctx)


@router.get("/partials/settings/system", response_class=HTMLResponse)
async def settings_system_tab(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """System config tab — admin only."""
    if user.role != "admin":
        raise HTTPException(403, "Admin only")
    from ...services.admin_service import get_all_config

    config = get_all_config(db)
    ctx = _base_ctx(request, user, "settings")
    ctx["config"] = config
    return templates.TemplateResponse("htmx/partials/settings/system.html", ctx)


@router.get("/partials/settings/profile", response_class=HTMLResponse)
async def settings_profile_tab(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """User profile tab."""
    ctx = _base_ctx(request, user, "settings")
    ctx["profile_user"] = user
    return templates.TemplateResponse("htmx/partials/settings/profile.html", ctx)
