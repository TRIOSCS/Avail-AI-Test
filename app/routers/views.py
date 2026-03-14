"""
views.py — HTMX view router for server-rendered pages.
Serves full-page HTML views when USE_HTMX is enabled.
Called by: app/main.py (registered when use_htmx=True)
Depends on: app/dependencies.py, app/templates/, app/database.py
"""

import math

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.templating import Jinja2Templates
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.dependencies import require_user
from app.models import (
    ActivityLog,
    BuyPlan,
    BuyPlanLine,
    Company,
    CustomerSite,
    MaterialCard,
    Offer,
    ProspectAccount,
    Quote,
    QuoteLine,
    Requirement,
    Requisition,
    RequisitionTask,
    Sighting,
    SiteContact,
    SourcingLead,
    LeadEvidence,
    LeadFeedbackEvent,
    User,
    VendorCard,
    VendorContact,
    VendorReview,
)
from app.utils.normalization import normalize_mpn_key
from app.utils.sql_helpers import escape_like
from app.vendor_utils import normalize_vendor_name

templates = Jinja2Templates(directory="app/templates")

router = APIRouter(tags=["views"])

PER_PAGE = 25


@router.get("/app")
async def app_shell(request: Request, user=Depends(require_user)):
    """Serve the main app shell."""
    return templates.TemplateResponse("base.html", {"request": request, "user": user})


@router.get("/search")
async def global_search(request: Request, q: str = "", user=Depends(require_user)):
    """Return search results partial for top bar global search.

    Accepts a query string and returns an HTML partial with matching results
    grouped by type (requisitions, companies, vendors). Used by the topbar
    search input via hx-get with debounce.
    """
    results = []  # TODO: aggregate search across requisitions, companies, vendors
    logger.debug("Global search query='{}' by user={}", q, user.email if user else "unknown")
    return templates.TemplateResponse(
        "partials/shared/search_results.html",
        {"request": request, "results": results, "query": q},
    )


# ── Requisitions views ──────────────────────────────────────────────


def _query_requisitions(db: Session, user: User, q: str, status: str, sort: str, dir: str, page: int):
    """Build a filtered, sorted, paginated requisition query.

    Returns (requisitions_list, page, total_pages) where each item is a
    lightweight dict suitable for the list template.
    """
    req_count_sq = (
        sqlfunc.count(Requirement.id)
    )
    base = (
        db.query(Requisition, req_count_sq)
        .outerjoin(Requirement, Requirement.requisition_id == Requisition.id)
        .group_by(Requisition.id)
    )

    # Sales sees own reqs only
    if user.role == "sales":
        base = base.filter(Requisition.created_by == user.id)

    # Search filter
    if q.strip():
        safe_q = escape_like(q.strip())
        base = base.filter(
            or_(
                Requisition.name.ilike(f"%{safe_q}%"),
                Requisition.customer_name.ilike(f"%{safe_q}%"),
            )
        )

    # Status filter
    if status == "archived":
        base = base.filter(Requisition.status.in_(["archived", "won", "lost", "closed"]))
    elif status:
        base = base.filter(Requisition.status == status)
    else:
        base = base.filter(Requisition.status.notin_(["archived", "won", "lost", "closed"]))

    # Sort
    allowed_sorts = {
        "created_at": Requisition.created_at,
        "name": Requisition.name,
        "status": Requisition.status,
        "customer_name": Requisition.customer_name,
    }
    sort_col = allowed_sorts.get(sort, Requisition.created_at)
    sort_expr = sort_col.asc() if dir == "asc" else sort_col.desc()

    total = base.count()
    total_pages = max(1, math.ceil(total / PER_PAGE))
    page = max(1, min(page, total_pages))
    offset = (page - 1) * PER_PAGE

    rows = base.order_by(sort_expr).offset(offset).limit(PER_PAGE).all()

    requisitions = [
        type("Req", (), {
            "id": r.id,
            "name": r.name,
            "customer_name": r.customer_name or "",
            "status": r.status,
            "requirement_count": cnt or 0,
            "created_at": r.created_at,
        })
        for r, cnt in rows
    ]
    return requisitions, page, total_pages


@router.get("/views/requisitions")
async def requisitions_page(
    request: Request,
    q: str = "",
    status: str = "",
    sort: str = "created_at",
    dir: str = "desc",
    page: int = Query(1, ge=1),
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Full requisitions list page."""
    logger.debug("Requisitions page view by user={}", user.email if user else "unknown")
    requisitions, page, total_pages = _query_requisitions(db, user, q, status, sort, dir, page)
    return templates.TemplateResponse(
        "partials/requisitions/list.html",
        {
            "request": request,
            "user": user,
            "requisitions": requisitions,
            "page": page,
            "total_pages": total_pages,
            "q": q,
            "status": status,
            "sort": sort,
            "dir": dir,
            "url": "/views/requisitions/rows",
            "target_id": "req-table-body",
            "message": "No requisitions found.",
            "action_url": "/views/requisitions/create-form",
            "action_label": "Create one",
        },
    )


@router.get("/views/requisitions/rows")
async def requisitions_rows(
    request: Request,
    q: str = "",
    status: str = "",
    sort: str = "created_at",
    dir: str = "desc",
    page: int = Query(1, ge=1),
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return requisition table rows partial (HTMX swap target)."""
    logger.debug("Requisitions rows partial q='{}' status='{}' sort={} dir={}", q, status, sort, dir)
    requisitions, page, total_pages = _query_requisitions(db, user, q, status, sort, dir, page)

    # Build rows HTML by rendering each row partial
    from fastapi.responses import HTMLResponse

    row_html_parts = []
    for req in requisitions:
        rendered = templates.get_template("partials/requisitions/req_row.html").render(req=req)
        row_html_parts.append(rendered)

    if not row_html_parts:
        empty = templates.get_template("partials/shared/empty_state.html").render(
            message="No requisitions found.",
            action_url="/views/requisitions/create-form",
            action_label="Create one",
        )
        row_html_parts.append(f'<tr><td colspan="5">{empty}</td></tr>')

    # Append pagination if needed
    if total_pages > 1:
        pagination = templates.get_template("partials/shared/pagination.html").render(
            page=page, total_pages=total_pages, url="/views/requisitions/rows", target_id="req-table-body",
        )
        row_html_parts.append(pagination)

    return HTMLResponse("\n".join(row_html_parts))


@router.get("/views/requisitions/create-form")
async def requisitions_create_form(request: Request, user=Depends(require_user)):
    """Return the create-requisition modal form partial."""
    logger.debug("Requisition create form requested by user={}", user.email if user else "unknown")
    return templates.TemplateResponse(
        "partials/requisitions/create_modal.html",
        {"request": request},
    )


# ── Requisition detail + drill-down ───────────────────────────────


@router.get("/views/requisitions/{req_id}")
async def requisition_detail(
    req_id: int,
    request: Request,
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Full requisition detail page with header, tab bar, and action buttons."""
    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Requisition not found")

    # Sales users can only see their own requisitions
    if user.role == "sales" and req.created_by != user.id:
        raise HTTPException(status_code=404, detail="Requisition not found")

    requirement_count = (
        db.query(sqlfunc.count(Requirement.id))
        .filter(Requirement.requisition_id == req_id)
        .scalar()
    ) or 0

    logger.debug("Requisition detail req_id={} by user={}", req_id, user.email)
    return templates.TemplateResponse(
        "partials/requisitions/detail.html",
        {
            "request": request,
            "user": user,
            "req": req,
            "requirement_count": requirement_count,
        },
    )


_VALID_TABS = {"parts", "offers", "quotes", "buy_plans", "activity", "tasks"}


@router.get("/views/requisitions/{req_id}/tab/{tab_name}")
async def requisition_tab(
    req_id: int,
    tab_name: str,
    request: Request,
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return a specific tab content partial for a requisition."""
    if tab_name not in _VALID_TABS:
        raise HTTPException(status_code=404, detail=f"Unknown tab: {tab_name}")

    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Requisition not found")
    if user.role == "sales" and req.created_by != user.id:
        raise HTTPException(status_code=404, detail="Requisition not found")

    logger.debug("Requisition tab req_id={} tab={} by user={}", req_id, tab_name, user.email)

    context = {"request": request, "req_id": req_id}

    if tab_name == "parts":
        requirements = (
            db.query(Requirement)
            .filter(Requirement.requisition_id == req_id)
            .order_by(Requirement.id)
            .all()
        )
        context["requirements"] = requirements

    elif tab_name == "offers":
        offers = (
            db.query(Offer)
            .filter(Offer.requisition_id == req_id)
            .order_by(Offer.created_at.desc())
            .all()
        )
        context["offers"] = offers

    elif tab_name == "quotes":
        quotes = (
            db.query(Quote)
            .filter(Quote.requisition_id == req_id)
            .order_by(Quote.created_at.desc())
            .all()
        )
        context["quotes"] = quotes

    elif tab_name == "buy_plans":
        buy_plans = (
            db.query(BuyPlan)
            .filter(BuyPlan.requisition_id == req_id)
            .order_by(BuyPlan.created_at.desc())
            .all()
        )
        context["buy_plans"] = buy_plans

    elif tab_name == "activity":
        activities = (
            db.query(ActivityLog)
            .filter(ActivityLog.requisition_id == req_id)
            .order_by(ActivityLog.created_at.desc())
            .all()
        )
        context["activities"] = activities

    elif tab_name == "tasks":
        tasks = (
            db.query(RequisitionTask)
            .filter(RequisitionTask.requisition_id == req_id)
            .order_by(RequisitionTask.created_at.desc())
            .all()
        )
        context["tasks"] = tasks

    return templates.TemplateResponse(
        f"partials/requisitions/tabs/{tab_name}.html",
        context,
    )


# ── Sourcing results ─────────────────────────────────────────────

# Source type to filter category mapping
_SOURCE_FILTERS = {
    "live": {"brokerbin", "nexar", "digikey", "mouser", "oemsecrets", "sourcengine", "ebay"},
    "historical": {"material_history", "sighting_history"},
    "affinity": {"vendor_affinity"},
}


def _filter_results(results: list, filter_value: str) -> list:
    """Filter sourcing results by source category or lead attribute."""
    if filter_value == "all" or not filter_value:
        return results
    # Source type filters
    allowed_types = _SOURCE_FILTERS.get(filter_value, set())
    if allowed_types:
        return [r for r in results if r.get("source_type", "") in allowed_types]
    # Lead attribute filters
    if filter_value == "high_confidence":
        return [r for r in results if r.get("confidence_band") == "high"]
    if filter_value == "safe_vendors":
        return [r for r in results if r.get("vendor_safety_band") in ("low_risk", None)]
    if filter_value == "has_lead":
        return [r for r in results if r.get("lead_id") is not None]
    return results


def _sort_results(results: list, sort_by: str) -> list:
    """Sort sourcing results by the given field."""
    if sort_by == "price_asc":
        return sorted(results, key=lambda r: (r.get("unit_price") is None, r.get("unit_price") or 0))
    elif sort_by == "price_desc":
        return sorted(results, key=lambda r: (r.get("unit_price") is None, -(r.get("unit_price") or 0)))
    elif sort_by == "qty_desc":
        return sorted(results, key=lambda r: (r.get("qty_available") is None, -(r.get("qty_available") or 0)))
    elif sort_by == "safest":
        return sorted(results, key=lambda r: (r.get("vendor_safety_score") is None, -(r.get("vendor_safety_score") or 0)))
    elif sort_by == "freshest":
        return sorted(results, key=lambda r: (r.get("score", 0)), reverse=True)
    # Default: confidence descending
    return sorted(results, key=lambda r: (r.get("confidence_pct", 0), r.get("score", 0)), reverse=True)


@router.get("/views/sourcing/{req_row_id}/results")
async def sourcing_results(
    req_row_id: int,
    request: Request,
    filter: str = "all",
    sort_by: str = "confidence",
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return sourcing results partial for a requirement row.

    Loads sightings from the DB for the given requirement, applies filter/sort,
    and renders the results panel with source progress pills.
    """
    requirement = db.query(Requirement).filter(Requirement.id == req_row_id).first()
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found")

    logger.debug("Sourcing results req_row_id={} filter={} sort={}", req_row_id, filter, sort_by)

    # Load sightings and convert to result dicts
    sightings = (
        db.query(Sighting)
        .filter(Sighting.requirement_id == req_row_id)
        .order_by(Sighting.score.desc())
        .all()
    )
    leads = db.query(SourcingLead).filter(SourcingLead.requirement_id == req_row_id).all()

    lead_map: dict[tuple[str, str], SourcingLead] = {}
    for lead in leads:
        vendor_key = normalize_vendor_name(lead.vendor_name_normalized or lead.vendor_name or "")
        part_key = normalize_mpn_key(lead.part_number_matched or "")
        if vendor_key and part_key:
            lead_map[(vendor_key, part_key)] = lead

    results = []
    for s in sightings:
        vendor_key = normalize_vendor_name(s.vendor_name_normalized or s.vendor_name or "")
        part_key = normalize_mpn_key((s.mpn_matched or requirement.primary_mpn or ""))
        lead = lead_map.get((vendor_key, part_key))
        results.append({
            "vendor_name": s.vendor_name,
            "mpn": s.mpn_matched or requirement.primary_mpn,
            "mpn_matched": s.mpn_matched,
            "manufacturer": s.manufacturer,
            "qty_available": s.qty_available,
            "unit_price": s.unit_price,
            "currency": s.currency or "USD",
            "source_type": s.source_type or "unknown",
            "source_badge": (s.source_type or "unknown").replace("_", " ").title(),
            "confidence_pct": round((s.confidence or 0) * 100),
            "confidence_color": "green" if (s.confidence or 0) >= 0.75 else ("amber" if (s.confidence or 0) >= 0.5 else "red"),
            "is_authorized": s.is_authorized or False,
            "score": s.score or 0,
            "material_card_id": s.material_card_id,
            "lead_id": lead.id if lead else None,
            "buyer_status": lead.buyer_status if lead else None,
            "confidence_band": lead.confidence_band if lead else None,
            "vendor_safety_band": lead.vendor_safety_band if lead else None,
            "vendor_safety_score": lead.vendor_safety_score if lead else None,
            "vendor_safety_summary": lead.vendor_safety_summary if lead else None,
        })

    # Build source progress from seen source types
    seen_sources = {}
    for r in results:
        st = r["source_type"]
        seen_sources[st] = seen_sources.get(st, 0) + 1
    sources = [{"name": name.replace("_", " ").title(), "done": True, "count": count}
               for name, count in seen_sources.items()]

    # Apply filter and sort
    results = _filter_results(results, filter)
    results = _sort_results(results, sort_by)

    return templates.TemplateResponse(
        "partials/sourcing/results.html",
        {
            "request": request,
            "results": results,
            "req_row_id": req_row_id,
            "sources": sources,
            "filter": filter,
            "sort_by": sort_by,
        },
    )


@router.get("/views/materials/{material_id}")
async def material_card_detail(
    material_id: int,
    request: Request,
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return material card detail partial with sighting history."""
    card = db.query(MaterialCard).filter(MaterialCard.id == material_id).first()
    if not card:
        raise HTTPException(status_code=404, detail="Material card not found")

    logger.debug("Material card detail id={} mpn={}", material_id, card.display_mpn)

    sightings = (
        db.query(Sighting)
        .filter(Sighting.material_card_id == material_id)
        .order_by(Sighting.created_at.desc())
        .limit(50)
        .all()
    )

    return templates.TemplateResponse(
        "partials/sourcing/material_card.html",
        {
            "request": request,
            "card": card,
            "sightings": sightings,
        },
    )


@router.get("/views/sourcing/follow-up-queue")
async def follow_up_queue_view(
    request: Request,
    status: str = "all",
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return follow-up queue partial showing all leads across requisitions."""
    from app.models import Requisition as Req

    q = (
        db.query(SourcingLead)
        .join(Req, SourcingLead.requisition_id == Req.id)
        .filter(Req.created_by == user.id)
    )
    if status and status != "all":
        q = q.filter(SourcingLead.buyer_status == status)
    leads = q.order_by(SourcingLead.updated_at.desc()).limit(200).all()

    return templates.TemplateResponse(
        "partials/sourcing/follow_up_queue.html",
        {"request": request, "leads": leads, "active_status": status},
    )


@router.get("/views/sourcing/leads/{lead_id}")
async def lead_detail_view(
    lead_id: int,
    request: Request,
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return lead detail partial with evidence, safety, contacts, and activity timeline."""
    lead = (
        db.query(SourcingLead)
        .options(
            joinedload(SourcingLead.evidence),
            joinedload(SourcingLead.feedback_events),
        )
        .filter(SourcingLead.id == lead_id)
        .first()
    )
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    return templates.TemplateResponse(
        "partials/sourcing/lead_detail.html",
        {"request": request, "lead": lead},
    )


@router.get("/views/sourcing/{req_row_id}/stream")
async def sourcing_stream(
    req_row_id: int,
    request: Request,
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """SSE stream placeholder for live sourcing progress.

    Returns the current search progress as a static HTML partial.
    Full SSE streaming (EventSourceResponse with async generator) will be
    implemented when the search service is wired for incremental results.
    """
    requirement = db.query(Requirement).filter(Requirement.id == req_row_id).first()
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found")

    logger.debug("SSE stream requested for req_row_id={}", req_row_id)

    # Build source progress from existing sightings
    sightings = (
        db.query(Sighting.source_type, sqlfunc.count(Sighting.id))
        .filter(Sighting.requirement_id == req_row_id)
        .group_by(Sighting.source_type)
        .all()
    )

    sources = [
        {"name": (st or "unknown").replace("_", " ").title(), "done": True, "count": cnt}
        for st, cnt in sightings
    ]

    return templates.TemplateResponse(
        "partials/sourcing/search_progress.html",
        {"request": request, "sources": sources},
    )


# ── Companies views ──────────────────────────────────────────────


def _query_companies(db: Session, q: str, owner: str, page: int):
    """Build a filtered, paginated company query.

    Returns (companies_list, page, total_pages, owners) where each item is a
    lightweight namespace suitable for the list template.
    """
    base = db.query(Company, User.name.label("owner_name")).outerjoin(
        User, Company.account_owner_id == User.id
    ).filter(Company.is_active.is_(True))

    # Search filter
    if q.strip():
        safe_q = escape_like(q.strip())
        base = base.filter(
            or_(
                Company.name.ilike(f"%{safe_q}%"),
                Company.industry.ilike(f"%{safe_q}%"),
            )
        )

    # Owner filter (match by owner name)
    if owner.strip():
        safe_owner = escape_like(owner.strip())
        base = base.filter(User.name.ilike(f"%{safe_owner}%"))

    total = base.count()
    total_pages = max(1, math.ceil(total / PER_PAGE))
    page = max(1, min(page, total_pages))
    offset = (page - 1) * PER_PAGE

    rows = base.order_by(Company.name.asc()).offset(offset).limit(PER_PAGE).all()

    # Distinct owner names for the filter dropdown
    owner_rows = (
        db.query(User.name)
        .join(Company, Company.account_owner_id == User.id)
        .filter(Company.is_active.is_(True))
        .distinct()
        .order_by(User.name)
        .all()
    )
    owners = [r[0] for r in owner_rows if r[0]]

    companies = [
        type("Co", (), {
            "id": c.id,
            "name": c.name,
            "owner_name": owner_name or "",
            "site_count": c.site_count or 0,
            "open_req_count": c.open_req_count or 0,
        })
        for c, owner_name in rows
    ]
    return companies, page, total_pages, owners


@router.get("/views/companies")
async def companies_page(
    request: Request,
    q: str = "",
    owner: str = "",
    page: int = Query(1, ge=1),
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Companies list page."""
    logger.debug("Companies page view by user={}", user.email if user else "unknown")
    companies, page, total_pages, owners = _query_companies(db, q, owner, page)
    return templates.TemplateResponse(
        "partials/companies/list.html",
        {
            "request": request,
            "user": user,
            "companies": companies,
            "page": page,
            "total_pages": total_pages,
            "q": q,
            "owner": owner,
            "owners": owners,
            "sort": "name",
            "dir": "asc",
            "message": "No companies found.",
        },
    )


@router.get("/views/companies/rows")
async def companies_rows(
    request: Request,
    q: str = "",
    owner: str = "",
    owner_filter: str = "",
    page: int = Query(1, ge=1),
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Company table rows partial with server-side filtering and pagination."""
    # Accept owner from either param name (direct or hx-include)
    effective_owner = owner or owner_filter
    logger.debug("Companies rows partial q='{}' owner='{}'", q, effective_owner)
    companies, page, total_pages, _owners = _query_companies(db, q, effective_owner, page)

    from fastapi.responses import HTMLResponse

    row_html_parts = []
    for company in companies:
        rendered = templates.get_template("partials/companies/company_row.html").render(company=company)
        row_html_parts.append(rendered)

    if not row_html_parts:
        empty = templates.get_template("partials/shared/empty_state.html").render(
            message="No companies found.",
        )
        row_html_parts.append(f'<tr><td colspan="5">{empty}</td></tr>')

    if total_pages > 1:
        pagination = templates.get_template("partials/shared/pagination.html").render(
            page=page, total_pages=total_pages, url="/views/companies/rows", target_id="company-table-body",
        )
        row_html_parts.append(pagination)

    return HTMLResponse("\n".join(row_html_parts))


@router.get("/views/companies/{company_id}")
async def company_detail(
    company_id: int,
    request: Request,
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Company detail drawer partial."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    logger.debug("Company detail id={} by user={}", company_id, user.email)
    return templates.TemplateResponse(
        "partials/companies/detail.html",
        {"request": request, "user": user, "company": company},
    )


_COMPANY_TABS = {"overview", "sites", "activity", "contacts", "pipeline"}


@router.get("/views/companies/{company_id}/tab/{tab_name}")
async def company_tab(
    company_id: int,
    tab_name: str,
    request: Request,
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Company tab content partial."""
    if tab_name not in _COMPANY_TABS:
        raise HTTPException(status_code=404, detail=f"Unknown tab: {tab_name}")

    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    logger.debug("Company tab id={} tab={} by user={}", company_id, tab_name, user.email)

    context: dict = {"request": request, "company": company, "company_id": company_id}

    if tab_name == "overview":
        pass  # company object has all overview fields

    elif tab_name == "sites":
        sites = (
            db.query(CustomerSite)
            .filter(CustomerSite.company_id == company_id)
            .order_by(CustomerSite.site_name)
            .all()
        )
        context["sites"] = sites

    elif tab_name == "activity":
        activities = (
            db.query(ActivityLog)
            .filter(ActivityLog.company_id == company_id)
            .order_by(ActivityLog.created_at.desc())
            .limit(50)
            .all()
        )
        context["activities"] = activities

    elif tab_name == "contacts":
        # Gather site contacts across all company sites
        contacts = (
            db.query(SiteContact, CustomerSite.site_name)
            .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
            .filter(CustomerSite.company_id == company_id)
            .order_by(SiteContact.full_name)
            .all()
        )
        context["contacts"] = [
            type("C", (), {
                "full_name": sc.full_name,
                "title": sc.title,
                "email": sc.email,
                "phone": sc.phone,
                "site_name": site_name,
                "contact_status": sc.contact_status,
            })
            for sc, site_name in contacts
        ]

    elif tab_name == "pipeline":
        # Requisitions linked to this company by customer_name match
        requisitions = (
            db.query(Requisition)
            .filter(
                Requisition.customer_name == company.name,
                Requisition.status.notin_(["archived", "won", "lost", "closed"]),
            )
            .order_by(Requisition.created_at.desc())
            .all()
        )
        context["requisitions"] = requisitions

        # Quotes linked through company's sites
        site_ids = [s.id for s in db.query(CustomerSite.id).filter(CustomerSite.company_id == company_id).all()]
        if site_ids:
            quotes = (
                db.query(Quote)
                .filter(Quote.customer_site_id.in_(site_ids))
                .order_by(Quote.created_at.desc())
                .all()
            )
        else:
            quotes = []
        context["quotes"] = quotes

    return templates.TemplateResponse(
        f"partials/companies/tabs/{tab_name}.html",
        context,
    )


# ── Quotes + Offers views ────────────────────────────────────────


def _query_quotes(db: Session, q: str, status: str, sort: str, page: int):
    """Build a filtered, sorted, paginated quote query.

    Returns (quotes_list, page, total_pages) where each item is a
    lightweight namespace suitable for the list template.
    """
    base = (
        db.query(Quote, CustomerSite.site_name)
        .outerjoin(CustomerSite, Quote.customer_site_id == CustomerSite.id)
    )

    # Search filter
    if q.strip():
        safe_q = escape_like(q.strip())
        base = base.filter(
            or_(
                Quote.quote_number.ilike(f"%{safe_q}%"),
                CustomerSite.site_name.ilike(f"%{safe_q}%"),
            )
        )

    # Status filter
    if status:
        base = base.filter(Quote.status == status)

    # Sort
    allowed_sorts = {
        "created_at": Quote.created_at,
        "total": Quote.subtotal,
        "status": Quote.status,
    }
    sort_col = allowed_sorts.get(sort, Quote.created_at)
    sort_expr = sort_col.desc() if sort in ("created_at", "total") else sort_col.asc()

    total = base.count()
    total_pages = max(1, math.ceil(total / PER_PAGE))
    page = max(1, min(page, total_pages))
    offset = (page - 1) * PER_PAGE

    rows = base.order_by(sort_expr).offset(offset).limit(PER_PAGE).all()

    quotes = []
    for qobj, site_name in rows:
        line_count = (
            db.query(sqlfunc.count(QuoteLine.id))
            .filter(QuoteLine.quote_id == qobj.id)
            .scalar()
        ) or 0
        quotes.append(
            type("Q", (), {
                "id": qobj.id,
                "quote_number": qobj.quote_number,
                "customer_name": site_name or "",
                "line_count": line_count,
                "total": float(qobj.subtotal) if qobj.subtotal else 0,
                "status": qobj.status or "draft",
                "created_at": qobj.created_at,
            })
        )
    return quotes, page, total_pages


@router.get("/views/quotes")
async def quotes_page(
    request: Request,
    q: str = "",
    status: str = "",
    sort: str = "created_at",
    page: int = Query(1, ge=1),
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Quotes list page."""
    logger.debug("Quotes page view by user={}", user.email if user else "unknown")
    quotes, page, total_pages = _query_quotes(db, q, status, sort, page)
    return templates.TemplateResponse(
        "partials/quotes/list.html",
        {
            "request": request,
            "user": user,
            "quotes": quotes,
            "page": page,
            "total_pages": total_pages,
            "q": q,
            "status": status,
            "sort": sort,
            "url": "/views/quotes/rows",
            "target_id": "quote-table-body",
            "message": "No quotes found.",
        },
    )


@router.get("/views/quotes/rows")
async def quotes_rows(
    request: Request,
    q: str = "",
    status: str = "",
    sort: str = "created_at",
    page: int = Query(1, ge=1),
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Quote table rows partial (HTMX swap target)."""
    logger.debug("Quotes rows partial q='{}' status='{}' sort={}", q, status, sort)
    quotes, page, total_pages = _query_quotes(db, q, status, sort, page)

    from fastapi.responses import HTMLResponse

    row_html_parts = []
    for quote in quotes:
        rendered = templates.get_template("partials/quotes/quote_row.html").render(quote=quote)
        row_html_parts.append(rendered)

    if not row_html_parts:
        empty = templates.get_template("partials/shared/empty_state.html").render(
            message="No quotes found.",
        )
        row_html_parts.append(f'<tr><td colspan="6">{empty}</td></tr>')

    if total_pages > 1:
        pagination = templates.get_template("partials/shared/pagination.html").render(
            page=page, total_pages=total_pages, url="/views/quotes/rows", target_id="quote-table-body",
        )
        row_html_parts.append(pagination)

    return HTMLResponse("\n".join(row_html_parts))


@router.get("/views/quotes/{quote_id}")
async def quote_detail(
    quote_id: int,
    request: Request,
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Quote detail page with line items editor."""
    quote = db.query(Quote).filter(Quote.id == quote_id).first()
    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")

    # Resolve customer name from site
    site = db.query(CustomerSite).filter(CustomerSite.id == quote.customer_site_id).first()
    quote.customer_name = site.site_name if site else ""

    line_items = (
        db.query(QuoteLine)
        .filter(QuoteLine.quote_id == quote_id)
        .order_by(QuoteLine.id)
        .all()
    )

    logger.debug("Quote detail id={} by user={}", quote_id, user.email)
    return templates.TemplateResponse(
        "partials/quotes/detail.html",
        {
            "request": request,
            "user": user,
            "quote": quote,
            "quote_id": quote_id,
            "line_items": line_items,
        },
    )


@router.get("/views/offers")
async def offers_page(
    request: Request,
    q: str = "",
    status: str = "",
    sort: str = "created_at",
    page: int = Query(1, ge=1),
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Offers gallery/list page."""
    logger.debug("Offers page view by user={}", user.email if user else "unknown")

    base = db.query(Offer)

    if q.strip():
        safe_q = escape_like(q.strip())
        base = base.filter(
            or_(
                Offer.vendor_name.ilike(f"%{safe_q}%"),
                Offer.mpn.ilike(f"%{safe_q}%"),
            )
        )

    if status:
        base = base.filter(Offer.status == status)

    total = base.count()
    total_pages = max(1, math.ceil(total / PER_PAGE))
    page = max(1, min(page, total_pages))
    offset = (page - 1) * PER_PAGE

    offers = base.order_by(Offer.created_at.desc()).offset(offset).limit(PER_PAGE).all()

    return templates.TemplateResponse(
        "partials/quotes/offers_list.html",
        {
            "request": request,
            "user": user,
            "offers": offers,
            "page": page,
            "total_pages": total_pages,
            "q": q,
            "status": status,
            "sort": sort,
        },
    )


# ── Vendors views ────────────────────────────────────────────────


def _query_vendors(db: Session, q: str, sort: str, dir: str, page: int):
    """Build a filtered, sorted, paginated vendor query.

    Returns (vendors_list, page, total_pages) where each item is a
    lightweight namespace suitable for the list template.
    """
    contact_count_sq = sqlfunc.count(VendorContact.id).label("contact_count")
    base = (
        db.query(VendorCard, contact_count_sq)
        .outerjoin(VendorContact, VendorContact.vendor_card_id == VendorCard.id)
        .group_by(VendorCard.id)
    )

    # Search filter
    if q.strip():
        safe_q = escape_like(q.strip())
        base = base.filter(
            or_(
                VendorCard.display_name.ilike(f"%{safe_q}%"),
                VendorCard.domain.ilike(f"%{safe_q}%"),
            )
        )

    # Sort
    allowed_sorts = {
        "name": VendorCard.display_name,
        "health_score": VendorCard.vendor_score,
        "last_activity": VendorCard.last_activity_at,
    }
    sort_col = allowed_sorts.get(sort, VendorCard.display_name)
    sort_expr = sort_col.asc() if dir == "asc" else sort_col.desc()

    total = base.count()
    total_pages = max(1, math.ceil(total / PER_PAGE))
    page = max(1, min(page, total_pages))
    offset = (page - 1) * PER_PAGE

    rows = base.order_by(sort_expr).offset(offset).limit(PER_PAGE).all()

    vendors = [
        type("V", (), {
            "id": v.id,
            "name": v.display_name,
            "health_score": round(v.vendor_score, 1) if v.vendor_score is not None else None,
            "contact_count": cnt or 0,
            "last_activity": v.last_activity_at.strftime("%b %d, %Y") if v.last_activity_at else None,
        })
        for v, cnt in rows
    ]
    return vendors, page, total_pages


@router.get("/views/vendors")
async def vendors_page(
    request: Request,
    q: str = "",
    sort: str = "name",
    dir: str = "asc",
    page: int = Query(1, ge=1),
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Vendors list page."""
    logger.debug("Vendors page view by user={}", user.email if user else "unknown")
    vendors, page, total_pages = _query_vendors(db, q, sort, dir, page)
    return templates.TemplateResponse(
        "partials/vendors/list.html",
        {
            "request": request,
            "user": user,
            "vendors": vendors,
            "page": page,
            "total_pages": total_pages,
            "q": q,
            "sort": sort,
            "dir": dir,
            "message": "No vendors found.",
        },
    )


@router.get("/views/vendors/rows")
async def vendors_rows(
    request: Request,
    q: str = "",
    sort: str = "name",
    dir: str = "asc",
    page: int = Query(1, ge=1),
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Vendor table rows partial (HTMX swap target)."""
    logger.debug("Vendors rows partial q='{}' sort={} dir={}", q, sort, dir)
    vendors, page, total_pages = _query_vendors(db, q, sort, dir, page)

    from fastapi.responses import HTMLResponse

    row_html_parts = []
    for vendor in vendors:
        rendered = templates.get_template("partials/vendors/vendor_row.html").render(vendor=vendor)
        row_html_parts.append(rendered)

    if not row_html_parts:
        empty = templates.get_template("partials/shared/empty_state.html").render(
            message="No vendors found.",
        )
        row_html_parts.append(f'<tr><td colspan="5">{empty}</td></tr>')

    if total_pages > 1:
        pagination = templates.get_template("partials/shared/pagination.html").render(
            page=page, total_pages=total_pages, url="/views/vendors/rows", target_id="vendor-table-body",
        )
        row_html_parts.append(pagination)

    return HTMLResponse("\n".join(row_html_parts))


@router.get("/views/vendors/{vendor_id}")
async def vendor_detail(
    vendor_id: int,
    request: Request,
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Vendor detail drawer partial."""
    vendor = db.query(VendorCard).filter(VendorCard.id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    contact_count = (
        db.query(sqlfunc.count(VendorContact.id))
        .filter(VendorContact.vendor_card_id == vendor_id)
        .scalar()
    ) or 0

    logger.debug("Vendor detail id={} by user={}", vendor_id, user.email)
    return templates.TemplateResponse(
        "partials/vendors/detail.html",
        {"request": request, "user": user, "vendor": vendor, "contact_count": contact_count},
    )


_VENDOR_TABS = {"overview", "contacts", "analytics", "offers"}


@router.get("/views/vendors/{vendor_id}/tab/{tab_name}")
async def vendor_tab(
    vendor_id: int,
    tab_name: str,
    request: Request,
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Vendor tab content partial."""
    if tab_name not in _VENDOR_TABS:
        raise HTTPException(status_code=404, detail=f"Unknown tab: {tab_name}")

    vendor = db.query(VendorCard).filter(VendorCard.id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    logger.debug("Vendor tab id={} tab={} by user={}", vendor_id, tab_name, user.email)

    context: dict = {"request": request, "vendor": vendor, "vendor_id": vendor_id}

    if tab_name == "overview":
        reviews = (
            db.query(VendorReview)
            .filter(VendorReview.vendor_card_id == vendor_id)
            .all()
        )
        if reviews:
            context["avg_rating"] = sum(r.rating for r in reviews) / len(reviews)
            context["review_count"] = len(reviews)
        else:
            context["avg_rating"] = None
            context["review_count"] = 0

    elif tab_name == "contacts":
        contacts = (
            db.query(VendorContact)
            .filter(VendorContact.vendor_card_id == vendor_id)
            .order_by(VendorContact.full_name)
            .all()
        )
        context["contacts"] = contacts

    elif tab_name == "analytics":
        pass  # vendor object has all analytics fields

    elif tab_name == "offers":
        offers = (
            db.query(Offer)
            .filter(Offer.vendor_name == vendor.display_name)
            .order_by(Offer.created_at.desc())
            .all()
        )
        context["offers"] = offers

    return templates.TemplateResponse(
        f"partials/vendors/tabs/{tab_name}.html",
        context,
    )


# ── Buy Plans views ─────────────────────────────────────────────


def _query_buy_plans(db: Session, user: User, q: str, status: str, mine: bool, sort: str, dir: str, page: int):
    """Build a filtered, sorted, paginated buy plan query.

    Returns (buy_plans_list, page, total_pages) where each item is a
    lightweight namespace suitable for the list template.
    """
    line_count_sq = sqlfunc.count(BuyPlanLine.id)
    base = (
        db.query(BuyPlan, line_count_sq, User.name.label("submitted_by_name"))
        .outerjoin(BuyPlanLine, BuyPlanLine.buy_plan_id == BuyPlan.id)
        .outerjoin(User, BuyPlan.submitted_by_id == User.id)
        .outerjoin(Requisition, BuyPlan.requisition_id == Requisition.id)
        .group_by(BuyPlan.id, User.name)
    )

    # "My Only" filter — show only plans submitted by this user
    if mine:
        base = base.filter(BuyPlan.submitted_by_id == user.id)

    # Search filter
    if q.strip():
        safe_q = escape_like(q.strip())
        base = base.filter(
            or_(
                Requisition.customer_name.ilike(f"%{safe_q}%"),
                BuyPlan.sales_order_number.ilike(f"%{safe_q}%"),
                BuyPlan.customer_po_number.ilike(f"%{safe_q}%"),
            )
        )

    # Status filter
    if status:
        base = base.filter(BuyPlan.status == status)

    # Sort
    allowed_sorts = {
        "created_at": BuyPlan.created_at,
        "total": BuyPlan.total_cost,
        "customer": Requisition.customer_name,
        "name": BuyPlan.id,
    }
    sort_col = allowed_sorts.get(sort, BuyPlan.created_at)
    sort_expr = sort_col.asc() if dir == "asc" else sort_col.desc()

    total = base.count()
    total_pages = max(1, math.ceil(total / PER_PAGE))
    page = max(1, min(page, total_pages))
    offset = (page - 1) * PER_PAGE

    rows = base.order_by(sort_expr).offset(offset).limit(PER_PAGE).all()

    buy_plans = [
        type("BP", (), {
            "id": bp.id,
            "name": f"BP-{bp.id}",
            "customer_name": bp.requisition.customer_name if bp.requisition else "",
            "line_count": cnt or 0,
            "total_cost": bp.total_cost,
            "status": bp.status,
            "submitted_by_name": submitted_name or "",
            "created_at": bp.created_at,
        })
        for bp, cnt, submitted_name in rows
    ]
    return buy_plans, page, total_pages


@router.get("/views/buy-plans")
async def buy_plans_page(
    request: Request,
    q: str = "",
    status: str = "",
    mine: bool = False,
    sort: str = "created_at",
    dir: str = "desc",
    page: int = Query(1, ge=1),
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Buy plans list page."""
    logger.debug("Buy plans page view by user={}", user.email if user else "unknown")
    buy_plans, page, total_pages = _query_buy_plans(db, user, q, status, mine, sort, dir, page)
    return templates.TemplateResponse(
        "partials/buy_plans/list.html",
        {
            "request": request,
            "user": user,
            "buy_plans": buy_plans,
            "page": page,
            "total_pages": total_pages,
            "q": q,
            "status": status,
            "mine": mine,
            "sort": sort,
            "dir": dir,
            "message": "No buy plans found.",
        },
    )


@router.get("/views/buy-plans/rows")
async def buy_plans_rows(
    request: Request,
    q: str = "",
    status: str = "",
    mine: bool = False,
    sort: str = "created_at",
    dir: str = "desc",
    page: int = Query(1, ge=1),
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Buy plan table rows partial (HTMX swap target)."""
    logger.debug("Buy plans rows partial q='{}' status='{}' mine={}", q, status, mine)
    buy_plans, page, total_pages = _query_buy_plans(db, user, q, status, mine, sort, dir, page)

    from fastapi.responses import HTMLResponse

    row_html_parts = []
    for bp in buy_plans:
        rendered = templates.get_template("partials/buy_plans/buy_plan_row.html").render(bp=bp)
        row_html_parts.append(rendered)

    if not row_html_parts:
        empty = templates.get_template("partials/shared/empty_state.html").render(
            message="No buy plans found.",
        )
        row_html_parts.append(f'<tr><td colspan="7">{empty}</td></tr>')

    if total_pages > 1:
        pagination = templates.get_template("partials/shared/pagination.html").render(
            page=page, total_pages=total_pages, url="/views/buy-plans/rows", target_id="bp-table-body",
        )
        row_html_parts.append(pagination)

    return HTMLResponse("\n".join(row_html_parts))


@router.get("/views/buy-plans/{bp_id}")
async def buy_plan_detail(
    bp_id: int,
    request: Request,
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Buy plan detail page with workflow actions."""
    bp = db.query(BuyPlan).filter(BuyPlan.id == bp_id).first()
    if not bp:
        raise HTTPException(status_code=404, detail="Buy plan not found")

    lines = (
        db.query(BuyPlanLine)
        .filter(BuyPlanLine.buy_plan_id == bp_id)
        .order_by(BuyPlanLine.id)
        .all()
    )

    logger.debug("Buy plan detail bp_id={} by user={}", bp_id, user.email)
    return templates.TemplateResponse(
        "partials/buy_plans/detail.html",
        {
            "request": request,
            "user": user,
            "bp": bp,
            "lines": lines,
        },
    )


# ── Prospecting views ────────────────────────────────────────────────


def _query_prospects(db: Session, q: str, industry: str, revenue: str,
                     region: str, source: str, page: int):
    """Build a filtered, paginated prospect query.

    Returns (prospects_list, page, total_pages, filter_options) where
    filter_options is a dict of distinct values for each filter dropdown.
    """
    base = db.query(ProspectAccount).filter(
        ProspectAccount.status.notin_(["dismissed", "converted"])
    )

    if q.strip():
        safe_q = escape_like(q.strip())
        base = base.filter(
            or_(
                ProspectAccount.name.ilike(f"%{safe_q}%"),
                ProspectAccount.domain.ilike(f"%{safe_q}%"),
                ProspectAccount.industry.ilike(f"%{safe_q}%"),
            )
        )

    if industry.strip():
        base = base.filter(ProspectAccount.industry == industry)
    if revenue.strip():
        base = base.filter(ProspectAccount.revenue_range == revenue)
    if region.strip():
        base = base.filter(ProspectAccount.region == region)
    if source.strip():
        base = base.filter(ProspectAccount.discovery_source == source)

    total = base.count()
    total_pages = max(1, math.ceil(total / PER_PAGE))
    page = max(1, min(page, total_pages))
    offset = (page - 1) * PER_PAGE

    prospects = (
        base.order_by(ProspectAccount.fit_score.desc())
        .offset(offset)
        .limit(PER_PAGE)
        .all()
    )

    # Distinct filter values for dropdowns
    all_base = db.query(ProspectAccount).filter(
        ProspectAccount.status.notin_(["dismissed", "converted"])
    )
    industries = sorted({
        r[0] for r in all_base.with_entities(ProspectAccount.industry).distinct().all()
        if r[0]
    })
    revenues = sorted({
        r[0] for r in all_base.with_entities(ProspectAccount.revenue_range).distinct().all()
        if r[0]
    })
    regions = sorted({
        r[0] for r in all_base.with_entities(ProspectAccount.region).distinct().all()
        if r[0]
    })
    sources = sorted({
        r[0] for r in all_base.with_entities(ProspectAccount.discovery_source).distinct().all()
        if r[0]
    })

    return prospects, page, total_pages, {
        "industries": industries,
        "revenues": revenues,
        "regions": regions,
        "sources": sources,
    }


@router.get("/views/prospecting")
async def prospecting_page(
    request: Request,
    q: str = "",
    industry: str = "",
    revenue: str = "",
    region: str = "",
    source: str = "",
    page: int = Query(1, ge=1),
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Prospect pool page."""
    logger.debug("Prospecting page view by user={}", user.email if user else "unknown")
    prospects, page, total_pages, filters = _query_prospects(
        db, q, industry, revenue, region, source, page
    )
    return templates.TemplateResponse(
        "partials/prospecting/list.html",
        {
            "request": request,
            "user": user,
            "prospects": prospects,
            "page": page,
            "total_pages": total_pages,
            "q": q,
            "industry": industry,
            "revenue": revenue,
            "region": region,
            "source": source,
            "message": "No prospects found.",
            **filters,
        },
    )


@router.get("/views/prospecting/rows")
async def prospecting_rows(
    request: Request,
    q: str = "",
    industry: str = "",
    industry_filter: str = "",
    revenue: str = "",
    revenue_filter: str = "",
    region: str = "",
    region_filter: str = "",
    source: str = "",
    source_filter: str = "",
    page: int = Query(1, ge=1),
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Prospect rows partial with server-side filtering and pagination."""
    eff_industry = industry or industry_filter
    eff_revenue = revenue or revenue_filter
    eff_region = region or region_filter
    eff_source = source or source_filter
    logger.debug(
        "Prospecting rows q='{}' industry='{}' revenue='{}' region='{}' source='{}'",
        q, eff_industry, eff_revenue, eff_region, eff_source,
    )
    prospects, page, total_pages, _filters = _query_prospects(
        db, q, eff_industry, eff_revenue, eff_region, eff_source, page
    )

    from fastapi.responses import HTMLResponse

    row_html_parts = []
    for prospect in prospects:
        rendered = templates.get_template(
            "partials/prospecting/prospect_row.html"
        ).render(prospect=prospect)
        row_html_parts.append(rendered)

    if not row_html_parts:
        empty = templates.get_template("partials/shared/empty_state.html").render(
            message="No prospects found.",
        )
        row_html_parts.append(f'<tr><td colspan="6">{empty}</td></tr>')

    if total_pages > 1:
        pagination = templates.get_template("partials/shared/pagination.html").render(
            page=page, total_pages=total_pages,
            url="/views/prospecting/rows", target_id="prospect-table-body",
        )
        row_html_parts.append(pagination)

    return HTMLResponse("\n".join(row_html_parts))


@router.get("/views/prospecting/{prospect_id}")
async def prospect_detail(
    prospect_id: int,
    request: Request,
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Prospect detail partial."""
    prospect = db.query(ProspectAccount).filter(ProspectAccount.id == prospect_id).first()
    if not prospect:
        raise HTTPException(status_code=404, detail="Prospect not found")

    logger.debug("Prospect detail id={} by user={}", prospect_id, user.email)
    return templates.TemplateResponse(
        "partials/prospecting/detail.html",
        {
            "request": request,
            "user": user,
            "prospect": prospect,
        },
    )


@router.post("/views/prospecting/{prospect_id}/claim")
async def prospect_claim(
    prospect_id: int,
    request: Request,
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Claim a prospect — returns updated prospect row."""
    prospect = db.query(ProspectAccount).filter(ProspectAccount.id == prospect_id).first()
    if not prospect:
        raise HTTPException(status_code=404, detail="Prospect not found")

    from datetime import datetime, timezone

    prospect.status = "claimed"
    prospect.claimed_by = user.id
    prospect.claimed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(prospect)

    logger.info("Prospect {} claimed by user={}", prospect_id, user.email)
    return templates.TemplateResponse(
        "partials/prospecting/prospect_row.html",
        {
            "request": request,
            "prospect": prospect,
        },
    )
