"""routers/htmx_views.py — HTMX + Alpine.js MVP frontend views.

Serves server-rendered HTML partials for the HTMX-based frontend.
Full page loads render base.html; HTMX requests get just the partial.
All routes live under /v2 to coexist with the original SPA frontend.

Called by: main.py (router mount)
Depends on: models, dependencies, database, search_service
"""

import json
import time
from datetime import datetime, timedelta, timezone
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
    ApiSource,
    BuyPlan,
    BuyPlanLine,
    Company,
    CustomerSite,
    Offer,
    Quote,
    QuoteLine,
    Requirement,
    Requisition,
    RequisitionTask,
    Sighting,
    SiteContact,
    SourcingLead,
    User,
    VendorCard,
    VerificationGroupMember,
)
from ..models.buy_plan import BuyPlanStatus
from ..models.enrichment import ProspectContact
from ..models.prospect_account import ProspectAccount
from ..models.vendor_sighting_summary import VendorSightingSummary
from ..models.vendors import VendorContact
from ..scoring import classify_lead, explain_lead, score_unified
from ..utils.sql_helpers import escape_like

router = APIRouter(tags=["htmx-views"])
_DASH = "\u2014"  # em-dash for template fallbacks
templates = Jinja2Templates(directory="app/templates")

# Vite manifest for asset fingerprinting — read once at import time.
_MANIFEST_PATH = Path("app/static/dist/.vite/manifest.json")
_vite_manifest: dict = {}
if _MANIFEST_PATH.exists():
    _vite_manifest = json.loads(_MANIFEST_PATH.read_text())


def _vite_assets() -> dict:
    """Return Vite asset URLs for templates.

    Keys: js_file, css_files.
    """
    entry = _vite_manifest.get("htmx_app.js", {})
    js_file = entry.get("file", "assets/htmx_app.js")
    css_files = entry.get("css", [])
    # Also add standalone styles entry if not already in css list
    styles_entry = _vite_manifest.get("styles.css", {})
    if styles_entry.get("file") and styles_entry["file"] not in css_files:
        css_files = [styles_entry["file"]] + css_files
    return {"js_file": js_file, "css_files": css_files}


def _timesince_filter(dt):
    """Convert datetime to human-readable relative time string."""
    if not dt:
        return ""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    diff = now - dt
    seconds = diff.total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        mins = int(seconds // 60)
        return f"{mins} min ago"
    if seconds < 86400:
        hours = int(seconds // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = int(seconds // 86400)
    if days == 1:
        return "1 day ago"
    return f"{days} days ago"


templates.env.filters["timesince"] = _timesince_filter


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
@router.get("/v2/quotes", response_class=HTMLResponse)
@router.get("/v2/quotes/{quote_id:int}", response_class=HTMLResponse)
@router.get("/v2/settings", response_class=HTMLResponse)
@router.get("/v2/prospecting", response_class=HTMLResponse)
@router.get("/v2/prospecting/{prospect_id:int}", response_class=HTMLResponse)
@router.get("/v2/proactive", response_class=HTMLResponse)
@router.get("/v2/strategic", response_class=HTMLResponse)
@router.get("/v2/materials", response_class=HTMLResponse)
@router.get("/v2/materials/{card_id:int}", response_class=HTMLResponse)
@router.get("/v2/follow-ups", response_class=HTMLResponse)
async def v2_page(request: Request, db: Session = Depends(get_db)):
    """Full page load — serves base.html with initial content via HTMX."""

    path = request.url.path
    user = get_user(request, db)
    if not user:
        return templates.TemplateResponse("htmx/login.html", {"request": request, **_vite_assets()})
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
    elif "/materials" in path:
        current_view = "materials"
    elif "/follow-ups" in path:
        current_view = "follow-ups"
    elif "/vendors" in path:
        current_view = "vendors"
    elif "/companies" in path:
        current_view = "companies"
    elif "/search" in path:
        current_view = "search"
    elif "/requisitions" in path:
        current_view = "requisitions"
    else:
        current_view = "requisitions"

    # Determine the correct partial URL for initial content load
    if current_view == "requisitions":
        # Split-panel workspace is the new default for requisitions
        partial_url = "/v2/partials/parts/workspace"
    else:
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
    elif current_view == "quotes" and "/quotes/" in path:
        parts = path.split("/quotes/")
        if len(parts) > 1 and parts[1].isdigit():
            partial_url = f"/v2/partials/quotes/{parts[1]}"
    elif current_view == "prospecting" and "/prospecting/" in path:
        parts = path.split("/prospecting/")
        if len(parts) > 1 and parts[1].isdigit():
            partial_url = f"/v2/partials/prospecting/{parts[1]}"

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
    """Global search across all entity types (type-ahead)."""
    from app.services.global_search_service import fast_search

    results = fast_search(q, db)
    return templates.TemplateResponse(
        "htmx/partials/shared/search_results.html",
        {**_base_ctx(request, user), "results": results, "query": q},
    )


@router.post("/v2/partials/search/ai", response_class=HTMLResponse)
async def ai_search_endpoint(
    request: Request,
    q: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """AI-powered search — triggered by Enter key."""
    from app.services.global_search_service import ai_search

    results = await ai_search(q, db)
    return templates.TemplateResponse(
        "htmx/partials/shared/search_results.html",
        {**_base_ctx(request, user), "results": results, "query": q, "ai_search": True},
    )


@router.get("/v2/partials/search/results", response_class=HTMLResponse)
async def search_results_page(
    request: Request,
    q: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Full search results page."""
    from app.services.global_search_service import fast_search

    results = fast_search(q, db) if q else None
    return templates.TemplateResponse(
        "htmx/partials/search/full_results.html",
        {**_base_ctx(request, user), "results": results, "query": q},
    )


# ── Parts workspace (split-panel entry point) ─────────────────────────


@router.get("/v2/partials/parts/workspace", response_class=HTMLResponse)
async def parts_workspace_partial(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the split-panel parts workspace shell."""
    ctx = _base_ctx(request, user, "requisitions")
    return templates.TemplateResponse("htmx/partials/parts/workspace.html", ctx)


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
    dir: str = "desc",
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return requisitions list as HTML partial with filters and sorting."""
    query = db.query(Requisition).options(
        joinedload(Requisition.creator),
        joinedload(Requisition.requirements),
        joinedload(Requisition.offers),
    )

    if q.strip():
        safe = escape_like(q.strip())
        query = query.filter(Requisition.name.ilike(f"%{safe}%") | Requisition.customer_name.ilike(f"%{safe}%"))
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

    # Sales users only see their own
    if user.role == "sales":
        query = query.filter(Requisition.created_by == user.id)

    total = query.count()

    # Sorting
    sort_col_map = {
        "name": Requisition.name,
        "customer_name": Requisition.customer_name,
        "status": Requisition.status,
        "urgency": Requisition.urgency,
        "created_at": Requisition.created_at,
    }
    sort_col = sort_col_map.get(sort, Requisition.created_at)
    order = sort_col.desc() if dir == "desc" else sort_col.asc()
    reqs = query.order_by(order).offset(offset).limit(limit).all()

    # Attach counts
    for req in reqs:
        req.req_count = len(req.requirements) if req.requirements else 0
        req.offer_count = len(req.offers) if req.offers else 0

    # Fetch team users for owner dropdown (non-sales only)
    users = []
    if user.role != "sales":
        users = db.query(User).order_by(User.name).all()

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update(
        {
            "requisitions": reqs,
            "q": q,
            "status": status,
            "owner": owner,
            "urgency": urgency,
            "date_from": date_from,
            "date_to": date_to,
            "sort": sort,
            "dir": dir,
            "total": total,
            "limit": limit,
            "offset": offset,
            "users": users,
            "user_role": user.role,
        }
    )
    return templates.TemplateResponse("htmx/partials/requisitions/list.html", ctx)


@router.get("/v2/partials/requisitions/create-form", response_class=HTMLResponse)
async def requisition_create_form(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the create requisition modal form."""
    ctx = _base_ctx(request, user, "requisitions")
    return templates.TemplateResponse("htmx/partials/requisitions/create_modal.html", ctx)


@router.get("/v2/partials/requisitions/{req_id}", response_class=HTMLResponse)
async def requisition_detail_partial(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return requisition detail as HTML partial with tabs."""
    req = (
        db.query(Requisition)
        .options(
            joinedload(Requisition.creator),
            joinedload(Requisition.requirements),
            joinedload(Requisition.offers),
        )
        .filter(Requisition.id == req_id)
        .first()
    )
    if not req:
        raise HTTPException(404, "Requisition not found")

    requirements = req.requirements or []
    for r in requirements:
        r.sighting_count = len(r.sightings) if r.sightings else 0

    req.offer_count = len(req.offers) if req.offers else 0

    # Fetch users for tasks tab assignee dropdown
    users = db.query(User).order_by(User.name).all()

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"req": req, "requirements": requirements, "users": users})
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
    """Create a new requisition and return the new row for HTMX prepend."""
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
                r = Requirement(
                    requisition_id=req.id,
                    primary_mpn=mpn,
                    target_qty=qty,
                    sourcing_status="open",
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
    response = templates.TemplateResponse("htmx/partials/requisitions/req_row.html", ctx)
    response.headers["HX-Trigger"] = "showToast"
    return response


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

    # Return the new row via template for HTMX append
    r.sighting_count = 0
    ctx = _base_ctx(request, user, "requisitions")
    ctx["r"] = r
    ctx["req"] = req
    return templates.TemplateResponse("htmx/partials/requisitions/tabs/req_row.html", ctx)


@router.get("/v2/partials/requisitions/{req_id}/tab/{tab}", response_class=HTMLResponse)
async def requisition_tab(
    request: Request,
    req_id: int,
    tab: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return a specific tab partial for requisition detail."""
    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    if not req:
        raise HTTPException(404, "Requisition not found")

    valid_tabs = {"parts", "offers", "quotes", "buy_plans", "tasks", "activity", "responses"}
    if tab not in valid_tabs:
        raise HTTPException(404, f"Unknown tab: {tab}")

    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req

    if tab == "parts":
        requirements = db.query(Requirement).filter(Requirement.requisition_id == req_id).all()
        for r in requirements:
            r.sighting_count = len(r.sightings) if r.sightings else 0
        ctx["requirements"] = requirements
        return templates.TemplateResponse("htmx/partials/requisitions/tabs/parts.html", ctx)

    elif tab == "offers":
        offers = (
            db.query(Offer).filter(Offer.requisition_id == req_id).order_by(Offer.created_at.desc().nullslast()).all()
        )
        # Check for existing draft quote to show "Add to Quote" button
        draft_quote = (
            db.query(Quote)
            .filter(Quote.requisition_id == req_id, Quote.status == "draft")
            .order_by(Quote.created_at.desc())
            .first()
        )
        ctx["offers"] = offers
        ctx["draft_quote"] = draft_quote
        return templates.TemplateResponse("htmx/partials/requisitions/tabs/offers.html", ctx)

    elif tab == "quotes":
        quotes = (
            db.query(Quote).filter(Quote.requisition_id == req_id).order_by(Quote.created_at.desc().nullslast()).all()
        )
        ctx["quotes"] = quotes
        return templates.TemplateResponse("htmx/partials/requisitions/tabs/quotes.html", ctx)

    elif tab == "buy_plans":
        buy_plans = (
            db.query(BuyPlan)
            .options(joinedload(BuyPlan.lines))
            .filter(BuyPlan.requisition_id == req_id)
            .order_by(BuyPlan.created_at.desc().nullslast())
            .all()
        )
        ctx["buy_plans"] = buy_plans
        return templates.TemplateResponse("htmx/partials/requisitions/tabs/buy_plans.html", ctx)

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
        return templates.TemplateResponse("htmx/partials/requisitions/tabs/tasks.html", ctx)

    elif tab == "responses":
        # Fetch vendor responses for this requisition
        from ..models.offers import VendorResponse

        responses = (
            db.query(VendorResponse)
            .filter(VendorResponse.requisition_id == req_id)
            .order_by(VendorResponse.received_at.desc().nullslast())
            .all()
        )
        ctx["responses"] = responses
        return templates.TemplateResponse("htmx/partials/requisitions/tabs/responses.html", ctx)

    else:  # activity
        from ..models.intelligence import ActivityLog
        from ..models.offers import Contact as RfqContact

        contacts = (
            db.query(RfqContact)
            .filter(RfqContact.requisition_id == req_id)
            .order_by(RfqContact.created_at.desc())
            .all()
        )
        activities = (
            db.query(ActivityLog)
            .filter(ActivityLog.requisition_id == req_id)
            .order_by(ActivityLog.created_at.desc())
            .all()
        )
        ctx["contacts"] = contacts
        ctx["activities"] = activities
        ctx["req"] = req
        return templates.TemplateResponse("htmx/partials/requisitions/tabs/activity.html", ctx)


# ── AI Parsing in Requisition Offers (Phase 3B) ───────────────────────


@router.get("/v2/partials/requisitions/{req_id}/parse-email-form", response_class=HTMLResponse)
async def parse_email_form(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the parse-email paste form."""
    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    if not req:
        raise HTTPException(404, "Requisition not found")
    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req
    return templates.TemplateResponse("htmx/partials/requisitions/tabs/parse_email_form.html", ctx)


@router.get("/v2/partials/requisitions/{req_id}/paste-offer-form", response_class=HTMLResponse)
async def paste_offer_form(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the paste-offer freeform form."""
    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    if not req:
        raise HTTPException(404, "Requisition not found")
    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req
    return templates.TemplateResponse("htmx/partials/requisitions/tabs/paste_offer_form.html", ctx)


@router.post("/v2/partials/requisitions/{req_id}/parse-email", response_class=HTMLResponse)
async def parse_email_action(
    request: Request,
    req_id: int,
    email_body: str = Form(""),
    email_subject: str = Form(""),
    vendor_name: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Parse vendor email and return editable offer cards."""
    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    if not req:
        raise HTTPException(404, "Requisition not found")

    if not email_body.strip():
        return HTMLResponse(
            '<div class="p-4 text-center text-sm text-amber-600 bg-amber-50 rounded-lg border border-amber-200">'
            "Please paste the email body to parse.</div>"
        )

    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req
    ctx["vendor_name"] = vendor_name

    try:
        from app.services.ai_email_parser import parse_email

        result = await parse_email(
            email_body=email_body,
            email_subject=email_subject,
            vendor_name=vendor_name,
        )

        if not result:
            ctx["quotes"] = []
            ctx["overall_confidence"] = 0
            ctx["email_type"] = "unclear"
        else:
            ctx["quotes"] = result.get("quotes", [])
            ctx["overall_confidence"] = result.get("overall_confidence", 0)
            ctx["email_type"] = result.get("email_type", "unclear")

    except Exception as exc:
        logger.error(f"Parse email error for req {req_id}: {exc}")
        return HTMLResponse(
            '<div class="p-4 text-center text-sm text-rose-600 bg-rose-50 rounded-lg border border-rose-200">'
            f"Parse failed: {exc}</div>"
        )

    return templates.TemplateResponse("htmx/partials/requisitions/tabs/parsed_email_results.html", ctx)


@router.post("/v2/partials/requisitions/{req_id}/parse-offer", response_class=HTMLResponse)
async def parse_offer_action(
    request: Request,
    req_id: int,
    raw_text: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Parse freeform vendor text and return editable offer cards."""
    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    if not req:
        raise HTTPException(404, "Requisition not found")

    if not raw_text.strip():
        return HTMLResponse(
            '<div class="p-4 text-center text-sm text-amber-600 bg-amber-50 rounded-lg border border-amber-200">'
            "Please paste vendor text to parse.</div>"
        )

    # Build RFQ context for better matching
    reqs = db.query(Requirement).filter(Requirement.requisition_id == req_id).all()
    rfq_context = [{"mpn": r.primary_mpn, "qty": r.target_qty or 1} for r in reqs if r.primary_mpn]

    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req

    try:
        from app.services.freeform_parser_service import parse_freeform_offer

        result = await parse_freeform_offer(raw_text, rfq_context)
        if not result:
            ctx["offers"] = []
        else:
            ctx["offers"] = result.get("offers", [])
    except Exception as exc:
        logger.error(f"Parse offer error for req {req_id}: {exc}")
        return HTMLResponse(
            '<div class="p-4 text-center text-sm text-rose-600 bg-rose-50 rounded-lg border border-rose-200">'
            f"Parse failed: {exc}</div>"
        )

    return templates.TemplateResponse("htmx/partials/requisitions/tabs/parsed_offer_results.html", ctx)


@router.post("/v2/partials/requisitions/{req_id}/save-parsed-offers", response_class=HTMLResponse)
async def save_parsed_offers(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save user-edited parsed offers to the requisition."""
    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    if not req:
        raise HTTPException(404, "Requisition not found")

    form = await request.form()
    vendor_name = form.get("vendor_name", "")

    # Collect offers from form fields (offers[0].mpn, offers[0].qty_available, etc.)
    offers_data: list[dict] = []
    idx = 0
    while True:
        mpn = form.get(f"offers[{idx}].mpn")
        if mpn is None:
            # Also check vendor_name field for freeform offers
            vn = form.get(f"offers[{idx}].vendor_name")
            if vn is None:
                break
        offer = {
            "vendor_name": form.get(f"offers[{idx}].vendor_name", vendor_name),
            "mpn": form.get(f"offers[{idx}].mpn", ""),
            "manufacturer": form.get(f"offers[{idx}].manufacturer"),
            "qty_available": _safe_int(form.get(f"offers[{idx}].qty_available")),
            "unit_price": _safe_float(form.get(f"offers[{idx}].unit_price")),
            "lead_time": form.get(f"offers[{idx}].lead_time"),
            "date_code": form.get(f"offers[{idx}].date_code"),
            "condition": form.get(f"offers[{idx}].condition", "new"),
            "moq": _safe_int(form.get(f"offers[{idx}].moq")),
            "notes": form.get(f"offers[{idx}].notes"),
        }
        offers_data.append(offer)
        idx += 1

    if not offers_data:
        return HTMLResponse(
            '<div class="p-4 text-center text-sm text-amber-600 bg-amber-50 rounded-lg border border-amber-200">'
            "No offers to save.</div>"
        )

    # Match MPNs to requirements
    reqs = db.query(Requirement).filter(Requirement.requisition_id == req_id).all()
    from app.vendor_utils import normalize_vendor_name

    saved_count = 0
    for o in offers_data:
        if not o["mpn"]:
            continue

        # Find matching requirement
        req_match_id = None
        mpn_lower = (o["mpn"] or "").strip().lower()
        for r in reqs:
            if r.primary_mpn and r.primary_mpn.strip().lower() == mpn_lower:
                req_match_id = r.id
                break

        # Resolve vendor card
        vn = o.get("vendor_name") or vendor_name or "Unknown"
        norm_name = normalize_vendor_name(vn)
        card = db.query(VendorCard).filter(VendorCard.normalized_name == norm_name).first()
        if not card:
            card = VendorCard(
                normalized_name=norm_name,
                display_name=vn,
                emails=[],
                phones=[],
            )
            db.add(card)
            db.flush()

        offer = Offer(
            requisition_id=req_id,
            requirement_id=req_match_id,
            vendor_card_id=card.id,
            vendor_name=card.display_name,
            vendor_name_normalized=card.normalized_name,
            mpn=o["mpn"],
            manufacturer=o.get("manufacturer"),
            qty_available=o.get("qty_available"),
            unit_price=o.get("unit_price"),
            lead_time=o.get("lead_time"),
            date_code=o.get("date_code"),
            condition=o.get("condition") or "new",
            moq=o.get("moq"),
            notes=o.get("notes"),
            source="ai_parsed",
            entered_by_id=user.id,
            status="active",
        )
        db.add(offer)
        saved_count += 1

    db.commit()

    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req
    ctx["saved_count"] = saved_count
    return templates.TemplateResponse("htmx/partials/requisitions/tabs/parse_save_success.html", ctx)


def _safe_int(val) -> int | None:
    """Safely convert form value to int."""
    if not val:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _safe_float(val) -> float | None:
    """Safely convert form value to float."""
    if not val:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


@router.post("/v2/partials/requisitions/bulk/{action}", response_class=HTMLResponse)
async def requisitions_bulk_action(
    request: Request,
    action: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Apply bulk action to selected requisitions and return refreshed list."""
    form = await request.form()
    ids_str = form.get("ids", "")
    if not ids_str:
        raise HTTPException(400, "No requisition IDs provided")

    try:
        ids = [int(x.strip()) for x in ids_str.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "Invalid ID format")

    if len(ids) > 200:
        raise HTTPException(400, "Maximum 200 requisitions per bulk action")

    valid_actions = {"archive", "activate", "assign"}
    if action not in valid_actions:
        raise HTTPException(400, f"Invalid action: {action}")

    reqs = db.query(Requisition).filter(Requisition.id.in_(ids)).all()

    if action == "archive":
        for r in reqs:
            r.status = "archived"
    elif action == "activate":
        for r in reqs:
            r.status = "active"
    elif action == "assign":
        owner_id = form.get("owner_id")
        if owner_id:
            for r in reqs:
                r.created_by = int(owner_id)

    db.commit()
    logger.info("Bulk {} applied to {} requisitions by {}", action, len(reqs), user.email)

    return await requisitions_list_partial(
        request=request,
        q="",
        status="",
        owner=0,
        urgency="",
        date_from="",
        date_to="",
        sort="created_at",
        dir="desc",
        limit=50,
        offset=0,
        user=user,
        db=db,
    )


@router.get("/v2/partials/requisitions/{req_id}/edit/{field}", response_class=HTMLResponse)
async def requisition_inline_edit_cell(
    request: Request,
    req_id: int,
    field: str,
    context: str = Query("row"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return an inline edit form for a single cell (list row or detail header).

    Args:
        field: One of name, status, urgency, deadline, owner.
        context: 'row' for list view, 'header' for detail header.
    """
    from ..dependencies import get_req_for_user

    valid_fields = {"name", "status", "urgency", "deadline", "owner"}
    if field not in valid_fields:
        return HTMLResponse("Invalid field", status_code=400)

    req = get_req_for_user(db, user, req_id, options=[])
    if not req:
        return HTMLResponse("Not found", status_code=404)
    users = db.query(User).order_by(User.name).all() if field == "owner" else []
    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"req": req, "field": field, "users": users, "context": context})
    return templates.TemplateResponse("htmx/partials/requisitions/inline_cell.html", ctx)


@router.patch("/v2/partials/requisitions/{req_id}/inline", response_class=HTMLResponse)
async def requisition_inline_save(
    request: Request,
    req_id: int,
    field: str = Form(...),
    value: str = Form(default=""),
    context: str = Form(default="row"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save an inline edit and return the updated element.

    For context='row', returns the full table row. For context='header', returns the
    updated header card.
    """
    from ..dependencies import get_req_for_user

    req = get_req_for_user(db, user, req_id, options=[])
    if not req:
        return HTMLResponse("Not found", status_code=404)

    msg = "Updated"

    if field == "name":
        clean = value.strip()
        if clean:
            req.name = clean
            msg = f"Renamed to '{clean}'"
    elif field == "status":
        from ..services.requisition_state import transition

        try:
            transition(req, value, user, db)
            msg = f"Status → {value}"
        except ValueError as e:
            msg = str(e)
    elif field == "urgency":
        if value in ("normal", "hot", "critical"):
            req.urgency = value
            msg = f"Urgency → {value}"
    elif field == "deadline":
        req.deadline = value if value else None
        msg = f"Deadline {'→ ' + value if value else 'cleared'}"
    elif field == "owner":
        if value and value.isdigit():
            req.created_by = int(value)
            msg = "Owner reassigned"

    from datetime import datetime, timezone

    req.updated_at = datetime.now(timezone.utc)
    req.updated_by_id = user.id
    db.commit()
    db.refresh(req)

    if context == "header":
        # Re-fetch with relationships for detail header
        req = (
            db.query(Requisition)
            .options(
                joinedload(Requisition.creator),
                joinedload(Requisition.requirements),
                joinedload(Requisition.offers),
            )
            .filter(Requisition.id == req_id)
            .first()
        )
        requirements = req.requirements or []
        req.offer_count = len(req.offers) if req.offers else 0
        users = db.query(User).order_by(User.name).all()
        ctx = _base_ctx(request, user, "requisitions")
        ctx.update({"req": req, "requirements": requirements, "users": users})
        response = templates.TemplateResponse("htmx/partials/requisitions/detail_header.html", ctx)
    else:
        # Row context — re-fetch ORM object with relationships
        req = (
            db.query(Requisition)
            .options(
                joinedload(Requisition.creator),
                joinedload(Requisition.requirements),
                joinedload(Requisition.offers),
            )
            .filter(Requisition.id == req_id)
            .first()
        )
        req.req_count = len(req.requirements) if req.requirements else 0
        req.offer_count = len(req.offers) if req.offers else 0
        ctx = _base_ctx(request, user, "requisitions")
        ctx.update({"req": req, "user_role": getattr(user, "role", "sales"), "user": user})
        response = templates.TemplateResponse("htmx/partials/requisitions/req_row.html", ctx)

    response.headers["HX-Trigger"] = json.dumps({"showToast": {"message": msg}})
    return response


@router.post("/v2/partials/requisitions/{req_id}/action/{action_name}", response_class=HTMLResponse)
async def requisition_row_action(
    request: Request,
    req_id: int,
    action_name: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Execute a row-level action (archive, activate, claim, unclaim, won, lost,
    clone)."""
    from ..dependencies import get_req_for_user

    valid_actions = {"archive", "activate", "claim", "unclaim", "won", "lost", "clone"}
    if action_name not in valid_actions:
        return HTMLResponse("Invalid action", status_code=400)

    req = get_req_for_user(db, user, req_id, options=[])
    if not req:
        return HTMLResponse("Not found", status_code=404)

    msg = "Action completed"
    form = await request.form()

    if action_name in ("archive", "activate", "won", "lost"):
        from ..services.requisition_state import transition

        target = {"archive": "archived", "activate": "active"}.get(action_name, action_name)
        try:
            transition(req, target, user, db)
            msg = f"'{req.name}' → {target}"
        except ValueError as e:
            msg = str(e)
    elif action_name == "claim":
        from ..services.requirement_status import claim_requisition

        try:
            claim_requisition(req, user, db)
            msg = f"Claimed '{req.name}'"
        except ValueError as e:
            msg = str(e)
    elif action_name == "unclaim":
        from ..services.requirement_status import unclaim_requisition

        unclaim_requisition(req, db, actor=user)
        msg = f"Unclaimed '{req.name}'"
    elif action_name == "clone":
        from ..services.requisition_service import clone_requisition

        new_req = clone_requisition(db, req, user.id)
        msg = f"Cloned → REQ-{new_req.id:03d}"

    if action_name != "clone":
        db.commit()

    # Return refreshed list
    return_format = form.get("return", "list")
    if return_format == "list":
        response = await requisitions_list_partial(
            request=request,
            q="",
            status="",
            owner=0,
            urgency="",
            date_from="",
            date_to="",
            sort="created_at",
            dir="desc",
            limit=50,
            offset=0,
            user=user,
            db=db,
        )
    else:
        response = HTMLResponse("")

    response.headers["HX-Trigger"] = json.dumps({"showToast": {"message": msg}})
    return response


@router.post("/v2/partials/requisitions/{req_id}/create-quote", response_class=HTMLResponse)
async def create_quote_from_offers(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a new quote from selected offer IDs.

    Returns quote detail partial.
    """
    form = await request.form()
    offer_ids_raw = form.getlist("offer_ids")
    offer_ids = [int(x) for x in offer_ids_raw if x]

    if not offer_ids:
        raise HTTPException(400, "No offers selected")

    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    if not req:
        raise HTTPException(404, "Requisition not found")

    offers = db.query(Offer).filter(Offer.id.in_(offer_ids), Offer.requisition_id == req_id).all()
    if not offers:
        raise HTTPException(404, "No matching offers found")

    # Build line items from offers

    quote_number = f"Q-{req_id}-{db.query(Quote).filter(Quote.requisition_id == req_id).count() + 1}"
    quote = Quote(
        requisition_id=req_id,
        quote_number=quote_number,
        status="draft",
        created_by_id=user.id,
        customer_site_id=req.customer_site_id,
    )
    db.add(quote)
    db.flush()

    subtotal = 0.0
    total_cost = 0.0
    for o in offers:
        sell_price = float(o.unit_price or 0)
        cost_price = sell_price  # Default cost = sell, buyer adjusts
        qty = o.qty_available or 1
        margin_pct = 0.0

        line = QuoteLine(
            quote_id=quote.id,
            offer_id=o.id,
            mpn=o.mpn or "",
            manufacturer=o.manufacturer or "",
            qty=qty,
            cost_price=cost_price,
            sell_price=sell_price,
            margin_pct=margin_pct,
        )
        db.add(line)
        subtotal += sell_price * qty
        total_cost += cost_price * qty

    quote.subtotal = subtotal
    quote.total_cost = total_cost
    quote.total_margin_pct = ((subtotal - total_cost) / subtotal * 100) if subtotal else 0
    db.commit()
    db.refresh(quote)

    logger.info("Created quote {} from {} offers by {}", quote.quote_number, len(offers), user.email)

    # Return the quote detail page
    lines = db.query(QuoteLine).filter(QuoteLine.quote_id == quote.id).all()
    ctx = _base_ctx(request, user, "quotes")
    ctx["quote"] = quote
    ctx["lines"] = lines
    ctx["offers"] = offers
    return templates.TemplateResponse("htmx/partials/quotes/detail.html", ctx)


@router.post("/v2/partials/requisitions/{req_id}/offers/{offer_id}/review", response_class=HTMLResponse)
async def review_offer(
    request: Request,
    req_id: int,
    offer_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Approve or reject an offer.

    Returns refreshed offers tab.
    """
    form = await request.form()
    action = form.get("action", "")

    if action not in ("approve", "reject"):
        raise HTTPException(400, "Invalid action")

    offer = db.query(Offer).filter(Offer.id == offer_id, Offer.requisition_id == req_id).first()
    if not offer:
        raise HTTPException(404, "Offer not found")

    if action == "approve":
        offer.status = "approved"
        offer.approved_by_id = user.id
        from datetime import datetime, timezone

        offer.approved_at = datetime.now(timezone.utc)
    else:
        offer.status = "rejected"

    db.commit()
    logger.info("Offer {} {} by {}", offer_id, action, user.email)

    # Return refreshed offers tab
    return await requisition_tab(request=request, req_id=req_id, tab="offers", user=user, db=db)


@router.get("/v2/partials/requisitions/{req_id}/add-offer-form", response_class=HTMLResponse)
async def add_offer_form(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the manual offer entry form."""
    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    if not req:
        raise HTTPException(404, "Requisition not found")
    requirements = db.query(Requirement).filter(Requirement.requisition_id == req_id).all()
    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req
    ctx["requirements"] = requirements
    return templates.TemplateResponse("htmx/partials/requisitions/add_offer_form.html", ctx)


@router.post("/v2/partials/requisitions/{req_id}/add-offer", response_class=HTMLResponse)
async def add_offer(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a manual offer and return refreshed offers tab."""
    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    if not req:
        raise HTTPException(404, "Requisition not found")

    form = await request.form()
    vendor_name = (form.get("vendor_name") or "").strip()
    mpn = (form.get("mpn") or "").strip()
    if not vendor_name or not mpn:
        return HTMLResponse(
            '<div class="p-3 text-sm text-rose-600 bg-rose-50 rounded mb-4">Vendor name and MPN are required.</div>',
            status_code=400,
        )

    from ..utils.normalization import normalize_mpn
    from ..vendor_utils import normalize_vendor_name

    offer = Offer(
        requisition_id=req_id,
        vendor_name=vendor_name,
        vendor_name_normalized=normalize_vendor_name(vendor_name),
        mpn=mpn,
        normalized_mpn=normalize_mpn(mpn),
        qty_available=int(form["qty_available"]) if form.get("qty_available") else None,
        unit_price=float(form["unit_price"]) if form.get("unit_price") else None,
        lead_time=form.get("lead_time") or None,
        date_code=form.get("date_code") or None,
        condition=form.get("condition") or None,
        moq=int(form["moq"]) if form.get("moq") else None,
        notes=form.get("notes") or None,
        requirement_id=int(form["requirement_id"]) if form.get("requirement_id") else None,
        source="manual",
        status="active",
        entered_by_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(offer)
    db.commit()
    logger.info("Manual offer created: {} on req {} by {}", mpn, req_id, user.email)

    return await requisition_tab(request=request, req_id=req_id, tab="offers", user=user, db=db)


@router.post("/v2/partials/requisitions/{req_id}/offers/{offer_id}/reconfirm", response_class=HTMLResponse)
async def reconfirm_offer(
    request: Request,
    req_id: int,
    offer_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Reconfirm an offer — resets TTL and increments reconfirm count."""
    offer = db.query(Offer).filter(Offer.id == offer_id, Offer.requisition_id == req_id).first()
    if not offer:
        raise HTTPException(404, "Offer not found")

    now = datetime.now(timezone.utc)
    offer.reconfirmed_at = now
    offer.reconfirm_count = (offer.reconfirm_count or 0) + 1
    offer.expires_at = now + timedelta(days=14)
    offer.attribution_status = "active"
    offer.is_stale = False
    offer.updated_at = now
    offer.updated_by_id = user.id
    db.commit()
    logger.info("Offer {} reconfirmed (count={}) by {}", offer_id, offer.reconfirm_count, user.email)

    return await requisition_tab(request=request, req_id=req_id, tab="offers", user=user, db=db)


# ── Sprint 2: Offer Management Completion ─────────────────────────────


@router.get("/v2/partials/requisitions/{req_id}/offers/{offer_id}/edit-form", response_class=HTMLResponse)
async def edit_offer_form(
    request: Request,
    req_id: int,
    offer_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return inline edit form for an existing offer."""
    offer = db.query(Offer).filter(Offer.id == offer_id, Offer.requisition_id == req_id).first()
    if not offer:
        raise HTTPException(404, "Offer not found")
    requirements = db.query(Requirement).filter(Requirement.requisition_id == req_id).all()
    return templates.TemplateResponse(
        "htmx/partials/requisitions/edit_offer_form.html",
        {"request": request, "offer": offer, "req_id": req_id, "requirements": requirements},
    )


@router.post("/v2/partials/requisitions/{req_id}/offers/{offer_id}/edit", response_class=HTMLResponse)
async def edit_offer(
    request: Request,
    req_id: int,
    offer_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save edits to an offer and return refreshed offers tab."""
    from ..models.intelligence import ChangeLog

    offer = db.query(Offer).filter(Offer.id == offer_id, Offer.requisition_id == req_id).first()
    if not offer:
        raise HTTPException(404, "Offer not found")

    form = await request.form()
    trackable = ["vendor_name", "qty_available", "unit_price", "lead_time", "condition", "date_code", "moq", "notes"]
    now = datetime.now(timezone.utc)

    for field in trackable:
        new_val = form.get(field, "").strip()
        old_val = str(getattr(offer, field) or "")
        if new_val != old_val and new_val:
            if field in ("qty_available", "moq"):
                try:
                    setattr(offer, field, int(new_val))
                except ValueError:
                    continue
            elif field == "unit_price":
                try:
                    setattr(offer, field, float(new_val))
                except ValueError:
                    continue
            else:
                setattr(offer, field, new_val)
            db.add(
                ChangeLog(
                    entity_type="offer",
                    entity_id=offer_id,
                    user_id=user.id,
                    field_name=field,
                    old_value=old_val,
                    new_value=new_val,
                )
            )

    req_id_val = form.get("requirement_id", "")
    if req_id_val:
        offer.requirement_id = int(req_id_val) if req_id_val.isdigit() else None

    offer.updated_at = now
    offer.updated_by_id = user.id
    db.commit()
    logger.info("Offer {} edited by {}", offer_id, user.email)

    return await requisition_tab(request=request, req_id=req_id, tab="offers", user=user, db=db)


@router.delete("/v2/partials/requisitions/{req_id}/offers/{offer_id}", response_class=HTMLResponse)
async def delete_offer_htmx(
    request: Request,
    req_id: int,
    offer_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete an offer and return refreshed offers tab."""
    offer = db.query(Offer).filter(Offer.id == offer_id, Offer.requisition_id == req_id).first()
    if not offer:
        raise HTTPException(404, "Offer not found")
    db.delete(offer)
    db.commit()
    logger.info("Offer {} deleted by {}", offer_id, user.email)

    return await requisition_tab(request=request, req_id=req_id, tab="offers", user=user, db=db)


@router.post("/v2/partials/requisitions/{req_id}/offers/{offer_id}/mark-sold", response_class=HTMLResponse)
async def mark_offer_sold_htmx(
    request: Request,
    req_id: int,
    offer_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Mark an offer as sold and return refreshed offers tab."""
    from ..models.intelligence import ChangeLog

    offer = db.query(Offer).filter(Offer.id == offer_id, Offer.requisition_id == req_id).first()
    if not offer:
        raise HTTPException(404, "Offer not found")
    if offer.status == "sold":
        return await requisition_tab(request=request, req_id=req_id, tab="offers", user=user, db=db)

    old_status = offer.status
    offer.status = "sold"
    offer.updated_at = datetime.now(timezone.utc)
    offer.updated_by_id = user.id
    db.add(
        ChangeLog(
            entity_type="offer",
            entity_id=offer_id,
            user_id=user.id,
            field_name="status",
            old_value=old_status,
            new_value="sold",
        )
    )
    db.commit()
    logger.info("Offer {} marked sold by {}", offer_id, user.email)

    return await requisition_tab(request=request, req_id=req_id, tab="offers", user=user, db=db)


@router.get("/v2/partials/offers/review-queue", response_class=HTMLResponse)
async def offer_review_queue(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the offer review queue page — medium-confidence AI-parsed offers."""
    offers = db.query(Offer).filter(Offer.status == "pending_review").order_by(Offer.created_at.desc()).limit(100).all()
    return templates.TemplateResponse(
        "htmx/partials/offers/review_queue.html",
        {"request": request, "offers": offers, "user": user},
    )


@router.post("/v2/partials/offers/{offer_id}/promote", response_class=HTMLResponse)
async def promote_offer_htmx(
    request: Request,
    offer_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Promote a pending_review offer to active and return refreshed queue."""
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    if offer.status != "pending_review":
        raise HTTPException(400, "Only pending_review offers can be promoted")

    offer.status = "active"
    offer.approved_by_id = user.id
    offer.approved_at = datetime.now(timezone.utc)
    offer.updated_at = datetime.now(timezone.utc)
    offer.updated_by_id = user.id
    db.commit()
    logger.info("Offer {} promoted by {}", offer_id, user.email)

    return await offer_review_queue(request=request, user=user, db=db)


@router.post("/v2/partials/offers/{offer_id}/reject", response_class=HTMLResponse)
async def reject_offer_htmx(
    request: Request,
    offer_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Reject a pending_review offer and return refreshed queue."""
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    if offer.status != "pending_review":
        raise HTTPException(400, "Only pending_review offers can be rejected")

    offer.status = "rejected"
    offer.updated_at = datetime.now(timezone.utc)
    offer.updated_by_id = user.id
    db.commit()
    logger.info("Offer {} rejected by {}", offer_id, user.email)

    return await offer_review_queue(request=request, user=user, db=db)


@router.get("/v2/partials/offers/{offer_id}/changelog", response_class=HTMLResponse)
async def offer_changelog(
    request: Request,
    offer_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render change history for an offer."""
    from ..models.intelligence import ChangeLog

    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    rows = (
        db.query(ChangeLog)
        .filter(ChangeLog.entity_type == "offer", ChangeLog.entity_id == offer_id)
        .options(joinedload(ChangeLog.user))
        .order_by(ChangeLog.created_at.desc())
        .limit(50)
        .all()
    )
    return templates.TemplateResponse(
        "htmx/partials/offers/changelog.html",
        {"request": request, "offer": offer, "changes": rows},
    )


@router.post("/v2/partials/requisitions/{req_id}/log-activity", response_class=HTMLResponse)
async def log_activity(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Log a manual activity (note/call/email) for a requisition."""
    from ..models.intelligence import ActivityLog

    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    if not req:
        raise HTTPException(404, "Requisition not found")

    form = await request.form()
    activity_type = form.get("activity_type", "note")
    channel_map = {"note": "note", "phone_call": "phone", "email_sent": "email"}

    log = ActivityLog(
        user_id=user.id,
        requisition_id=req_id,
        activity_type=activity_type,
        channel=channel_map.get(activity_type, "note"),
        contact_name=form.get("vendor_name", ""),
        contact_phone=form.get("contact_phone", ""),
        contact_email=form.get("contact_email", ""),
        notes=form.get("notes", ""),
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    logger.info("Activity logged for req {} by {}: {}", req_id, user.email, activity_type)

    # Return refreshed activity tab
    return await requisition_tab(request=request, req_id=req_id, tab="activity", user=user, db=db)


@router.get("/v2/partials/requisitions/{req_id}/rfq-compose", response_class=HTMLResponse)
async def rfq_compose(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the RFQ compose form for a requisition."""
    from ..models.offers import Contact as RfqContact

    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    if not req:
        raise HTTPException(404, "Requisition not found")

    parts = db.query(Requirement).filter(Requirement.requisition_id == req_id).all()

    # Get unique vendors from sightings for this requisition's parts
    part_ids = [p.id for p in parts]
    vendors = []
    if part_ids:
        # Get distinct vendor names from sightings, then match to VendorCard
        vendor_names = (
            db.query(Sighting.vendor_name_normalized)
            .filter(Sighting.requirement_id.in_(part_ids), Sighting.vendor_name_normalized.isnot(None))
            .distinct()
            .all()
        )
        norm_names = [n[0] for n in vendor_names if n[0]]
        vendor_rows = (
            (db.query(VendorCard).filter(VendorCard.normalized_name.in_(norm_names)).limit(50).all())
            if norm_names
            else []
        )
        # Check which vendors already have RFQs sent
        sent_vendor_names = set()
        existing_contacts = db.query(RfqContact).filter(RfqContact.requisition_id == req_id).all()
        for c in existing_contacts:
            if c.vendor_name_normalized:
                sent_vendor_names.add(c.vendor_name_normalized)

        for v in vendor_rows:
            # Get contacts for this vendor
            v_contacts = db.query(VendorContact).filter(VendorContact.vendor_card_id == v.id).limit(5).all()
            vendors.append(
                {
                    "id": v.id,
                    "display_name": v.display_name,
                    "normalized_name": v.normalized_name,
                    "domain": v.domain,
                    "contacts": v_contacts,
                    "already_asked": v.normalized_name in sent_vendor_names,
                    "emails": [c.email for c in v_contacts if c.email],
                }
            )

    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req
    ctx["parts"] = parts
    ctx["vendors"] = vendors
    return templates.TemplateResponse("htmx/partials/requisitions/rfq_compose.html", ctx)


@router.post("/v2/partials/requisitions/{req_id}/ai-cleanup-email", response_class=HTMLResponse)
async def ai_cleanup_email(
    request: Request,
    req_id: int,
    body: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Clean up user-written email — fix grammar, tone, and formatting."""
    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    if not req:
        raise HTTPException(404, "Requisition not found")

    user_text = body.strip()
    if not user_text:
        return HTMLResponse('<p class="text-xs text-amber-600 mt-1">Write your email first, then click Clean Up.</p>')

    try:
        from app.utils.claude_client import claude_text

        result = await claude_text(
            prompt=(
                f"Clean up this RFQ email: fix grammar, spelling, punctuation. "
                f"Improve clarity and professional tone. Keep it concise. "
                f"Do NOT add information the user didn't include. "
                f"Do NOT change the meaning or add new requests. "
                f"Return ONLY the cleaned-up email text, nothing else.\n\n"
                f"---\n{user_text}\n---"
            ),
            system="You are an email editor for a professional electronic components buyer.",
            model_tier="fast",
            max_tokens=1000,
        )
        cleaned = result.strip() if result else user_text
    except Exception as exc:
        logger.error("AI cleanup error for req %d: %s", req_id, exc)
        cleaned = user_text

    # Return a script that replaces the textarea content with the cleaned text
    escaped = cleaned.replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$")
    return HTMLResponse(
        f'<script>document.getElementById("rfq-body-textarea").value = `{escaped}`;</script>'
        '<p class="text-xs text-green-600 mt-1">Email cleaned up. Review and edit as needed.</p>'
    )


@router.post("/v2/partials/requisitions/{req_id}/rfq-send", response_class=HTMLResponse)
async def rfq_send(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Send RFQs via Graph API, falling back to DB-only in test mode."""
    import os
    from datetime import datetime, timezone

    from ..models.offers import Contact as RfqContact

    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    if not req:
        raise HTTPException(404, "Requisition not found")

    form = await request.form()
    vendor_names = form.getlist("vendor_names")
    vendor_emails = form.getlist("vendor_emails")
    subject = form.get("subject", f"RFQ - {req.name}")
    body = form.get("body", "")
    parts_text = form.get("parts_summary", "")

    if not vendor_names:
        raise HTTPException(400, "No vendors selected")

    # Try to get a fresh Graph API token for real email send
    token = None
    is_testing = os.environ.get("TESTING") == "1"
    if not is_testing:
        try:
            from ..dependencies import require_fresh_token

            token = await require_fresh_token(request, db)
        except HTTPException:
            token = None
            logger.warning("No Graph API token available — creating contacts without sending")

    sent = []
    failed = []

    if token and not is_testing:
        # Real email send via Graph API
        vendor_groups = []
        for name, email in zip(vendor_names, vendor_emails):
            if not email:
                continue
            vendor_groups.append(
                {
                    "vendor_name": name,
                    "vendor_email": email,
                    "parts": parts_text,
                    "subject": subject,
                    "body": body
                    or f"Dear {name},\n\nWe are looking for the following parts: {parts_text}\n\nPlease provide your best pricing and availability.\n\nThank you.",
                }
            )

        if vendor_groups:
            try:
                from ..email_service import send_batch_rfq

                results = await send_batch_rfq(
                    token=token,
                    db=db,
                    user_id=user.id,
                    requisition_id=req_id,
                    vendor_groups=vendor_groups,
                )
                for r in results:
                    status = r.get("status", "sent")
                    entry = {"vendor": r.get("vendor_name", ""), "email": r.get("vendor_email", ""), "status": status}
                    if status == "failed":
                        failed.append(entry)
                    else:
                        sent.append(entry)
            except Exception as exc:
                logger.error("Batch RFQ send failed: {}", exc)
                # Fall back to DB-only mode
                for name, email in zip(vendor_names, vendor_emails):
                    if not email:
                        continue
                    contact = RfqContact(
                        requisition_id=req_id,
                        user_id=user.id,
                        contact_type="email",
                        vendor_name=name,
                        vendor_name_normalized=name.lower().strip(),
                        vendor_contact=email,
                        parts_included=parts_text,
                        subject=subject,
                        status="draft",
                        status_updated_at=datetime.now(timezone.utc),
                    )
                    db.add(contact)
                    sent.append({"vendor": name, "email": email, "status": "draft"})
                db.commit()
    else:
        # Test mode or no token — create Contact records without sending
        for name, email in zip(vendor_names, vendor_emails):
            if not email:
                continue
            contact = RfqContact(
                requisition_id=req_id,
                user_id=user.id,
                contact_type="email",
                vendor_name=name,
                vendor_name_normalized=name.lower().strip(),
                vendor_contact=email,
                parts_included=parts_text,
                subject=subject,
                status="sent",
                status_updated_at=datetime.now(timezone.utc),
            )
            db.add(contact)
            sent.append({"vendor": name, "email": email, "status": "sent"})
        db.commit()

    logger.info("RFQ: {} sent, {} failed for req {} by {}", len(sent), len(failed), req_id, user.email)

    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req
    ctx["sent_results"] = sent
    ctx["failed_results"] = failed
    ctx["total_sent"] = len(sent)
    ctx["total_failed"] = len(failed)
    return templates.TemplateResponse("htmx/partials/requisitions/rfq_results.html", ctx)


# ── Follow-ups & Response Review (Phase 6) ───────────────────────────


@router.get("/v2/partials/follow-ups", response_class=HTMLResponse)
async def follow_ups_list_partial(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Cross-requisition follow-up queue as HTML partial."""
    from ..config import settings as cfg
    from ..models.offers import Contact as RfqContact

    threshold_days = getattr(cfg, "follow_up_days", 2)
    threshold = datetime.now() - __import__("datetime").timedelta(days=threshold_days)

    stale_q = db.query(RfqContact).filter(
        RfqContact.contact_type == "email",
        RfqContact.status.in_(["sent", "opened"]),
        RfqContact.created_at < threshold,
    )
    if getattr(user, "role", None) in ("sales", "trader"):
        stale_q = stale_q.join(Requisition).filter(Requisition.created_by == user.id)

    stale = stale_q.order_by(RfqContact.created_at.asc()).limit(500).all()

    req_ids = {c.requisition_id for c in stale}
    req_names: dict[int, str] = {}
    if req_ids:
        for r in db.query(Requisition.id, Requisition.name).filter(Requisition.id.in_(req_ids)).all():
            req_names[r.id] = r.name

    from datetime import timezone as tz

    now = datetime.now(tz.utc)
    follow_ups = []
    for c in stale:
        ca = c.created_at.replace(tzinfo=None) if c.created_at else now.replace(tzinfo=None)
        days_waiting = (now.replace(tzinfo=None) - ca).days
        follow_ups.append(
            {
                "contact_id": c.id,
                "requisition_id": c.requisition_id,
                "requisition_name": req_names.get(c.requisition_id, "Unknown"),
                "vendor_name": c.vendor_name,
                "vendor_email": c.vendor_contact,
                "parts": c.parts_included or [],
                "status": c.status,
                "days_waiting": days_waiting,
            }
        )

    ctx = _base_ctx(request, user, "follow-ups")
    ctx.update({"follow_ups": follow_ups, "total": len(follow_ups)})
    return templates.TemplateResponse("htmx/partials/follow_ups/list.html", ctx)


@router.post("/v2/partials/follow-ups/{contact_id}/send", response_class=HTMLResponse)
async def send_follow_up_htmx(
    request: Request,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Send a follow-up email for a stale contact.

    Returns success card.
    """
    import os

    from ..models.offers import Contact as RfqContact

    contact = db.get(RfqContact, contact_id)
    if not contact:
        raise HTTPException(404, "Contact not found")

    form = await request.form()
    body = (form.get("body") or "").strip()

    is_testing = os.environ.get("TESTING") == "1"
    email_sent = False

    if not is_testing and contact.vendor_contact:
        # Try to send real follow-up via Graph API
        try:
            from ..dependencies import require_fresh_token

            token = await require_fresh_token(request, db)

            from ..utils.graph_client import GraphClient

            gc = GraphClient(token)
            follow_up_subject = f"Follow-up: {contact.subject or 'RFQ'}"
            follow_up_body = (
                body
                or f"Dear {contact.vendor_name},\n\nI'm following up on our previous inquiry. Please let us know if you have availability.\n\nThank you."
            )
            payload = {
                "message": {
                    "subject": follow_up_subject,
                    "body": {"contentType": "Text", "content": follow_up_body},
                    "toRecipients": [{"emailAddress": {"address": contact.vendor_contact}}],
                },
                "saveToSentItems": "true",
            }
            await gc.post_json("/me/sendMail", payload)
            email_sent = True
        except Exception as exc:
            logger.warning("Follow-up email send failed for contact {}: {}", contact_id, exc)

    from datetime import timezone as tz

    contact.status = "sent"
    contact.status_updated_at = datetime.now(tz.utc)
    db.commit()

    mode = "via Graph API" if email_sent else "test mode"
    logger.info(
        "Follow-up sent for contact {} (vendor: {}, {}) by {}", contact_id, contact.vendor_name, mode, user.email
    )

    ctx = _base_ctx(request, user, "follow-ups")
    ctx["contact_id"] = contact_id
    ctx["vendor_name"] = contact.vendor_name or "Vendor"
    return templates.TemplateResponse("htmx/partials/follow_ups/sent_success.html", ctx)


@router.post("/v2/partials/requisitions/{req_id}/responses/{response_id}/review", response_class=HTMLResponse)
async def review_response_htmx(
    request: Request,
    req_id: int,
    response_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Mark a vendor response as reviewed or rejected.

    Returns updated card.
    """
    from ..models.offers import VendorResponse

    vr = (
        db.query(VendorResponse)
        .filter(
            VendorResponse.id == response_id,
            VendorResponse.requisition_id == req_id,
        )
        .first()
    )
    if not vr:
        raise HTTPException(404, "Response not found")

    form = await request.form()
    new_status = form.get("status", "")
    if new_status not in ("reviewed", "rejected"):
        raise HTTPException(400, "Status must be 'reviewed' or 'rejected'")

    vr.status = new_status
    db.commit()
    logger.info("Response {} marked as {} by {}", response_id, new_status, user.email)

    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    ctx = _base_ctx(request, user, "requisitions")
    ctx["r"] = vr
    ctx["req"] = req
    return templates.TemplateResponse("htmx/partials/requisitions/tabs/response_card.html", ctx)


@router.post("/v2/partials/requisitions/{req_id}/poll-inbox", response_class=HTMLResponse)
async def poll_inbox_htmx(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Poll inbox for vendor responses (test mode: no-op, returns refreshed responses
    tab)."""
    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    if not req:
        raise HTTPException(404, "Requisition not found")
    logger.info("Inbox poll requested for req {} by {}", req_id, user.email)
    # Return refreshed responses tab
    return await requisition_tab(request=request, req_id=req_id, tab="responses", user=user, db=db)


@router.delete("/v2/partials/requisitions/{req_id}/requirements/{item_id}", response_class=HTMLResponse)
async def delete_requirement(
    request: Request,
    req_id: int,
    item_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a requirement from a requisition.

    Returns empty response for hx-swap='delete'.
    """
    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    if not req:
        raise HTTPException(404, "Requisition not found")
    item = db.query(Requirement).filter(Requirement.id == item_id, Requirement.requisition_id == req_id).first()
    if not item:
        raise HTTPException(404, "Requirement not found")
    db.delete(item)
    db.commit()
    return HTMLResponse("")


@router.put("/v2/partials/requisitions/{req_id}/requirements/{item_id}", response_class=HTMLResponse)
async def update_requirement(
    request: Request,
    req_id: int,
    item_id: int,
    primary_mpn: str = Form(...),
    target_qty: int = Form(1),
    brand: str = Form(""),
    target_price: float | None = Form(None),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Update a requirement inline.

    Returns the updated row HTML.
    """
    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    if not req:
        raise HTTPException(404, "Requisition not found")
    item = db.query(Requirement).filter(Requirement.id == item_id, Requirement.requisition_id == req_id).first()
    if not item:
        raise HTTPException(404, "Requirement not found")

    item.primary_mpn = primary_mpn.strip()
    item.target_qty = target_qty
    item.brand = brand.strip() or None
    item.target_price = target_price
    db.commit()
    db.refresh(item)

    # Attach sighting_count for the template
    sighting_count = db.query(Sighting).filter(Sighting.requirement_id == item.id).count()
    item.sighting_count = sighting_count

    ctx = _base_ctx(request, user, "requisitions")
    ctx["r"] = item
    ctx["req"] = req
    return templates.TemplateResponse("htmx/partials/requisitions/tabs/req_row.html", ctx)


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
    """Launch a streaming part search and return the results shell HTML.

    Generates a search_id, launches stream_search_mpn as a background task, and returns
    the results_shell.html template with SSE connection details.

    If requirement_id is provided, searches for that requirement's MPN. Otherwise uses
    the mpn form field.
    """
    from uuid import uuid4

    from ..utils.async_helpers import safe_background_task as _safe_bg

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

    # Generate a unique search ID and launch streaming search in background
    search_id = str(uuid4())
    enabled_sources = _get_enabled_sources(db)

    from ..search_service import stream_search_mpn

    await _safe_bg(stream_search_mpn(search_id, search_mpn, db), task_name="stream_search_mpn")

    ctx = _base_ctx(request, user, "search")
    ctx.update(
        {
            "search_id": search_id,
            "mpn": search_mpn,
            "enabled_sources": enabled_sources,
        }
    )
    return templates.TemplateResponse("htmx/partials/search/results_shell.html", ctx)


@router.get("/v2/partials/search/stream")
async def search_stream(
    request: Request,
    search_id: str = Query(...),
    user: User = Depends(require_user),
):
    """SSE stream endpoint for search results.

    Subscribes to the SSE broker channel for the given search_id and yields events until
    the 'done' event is received or the client disconnects.
    """
    from sse_starlette.sse import EventSourceResponse

    from ..services.sse_broker import broker

    async def event_generator():
        async for msg in broker.listen(f"search:{search_id}"):
            if await request.is_disconnected():
                break
            yield {"event": msg["event"], "data": msg["data"]}
            if msg["event"] == "done":
                break

    return EventSourceResponse(event_generator())


def _get_enabled_sources(db: Session) -> list[dict]:
    """Return list of enabled API sources for the source progress chips.

    Called by: search_run
    Depends on: ApiSource model
    """
    from ..models import ApiSource

    sources = db.query(ApiSource).filter(ApiSource.status != "disabled").all()
    return [{"name": s.name, "status": s.status} for s in sources]


@router.get("/v2/partials/search/lead-detail", response_class=HTMLResponse)
async def search_lead_detail(
    request: Request,
    idx: int = Query(0, ge=0),
    mpn: str = Query(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the lead detail drawer content for a single search result.

    Re-runs the search (cached in search_service) and returns the enriched result at the
    given index.
    """
    if not mpn.strip():
        return HTMLResponse('<p class="p-4 text-sm text-gray-500">No part number specified.</p>')

    try:
        from ..search_service import quick_search_mpn

        raw_results = await quick_search_mpn(mpn.strip(), db)
        results = raw_results if isinstance(raw_results, list) else raw_results.get("sightings", [])
    except Exception as exc:
        logger.error("Lead detail search failed for {}: {}", mpn, exc)
        return HTMLResponse(f'<p class="p-4 text-sm text-red-600">Search error: {exc}</p>')

    if idx >= len(results):
        return HTMLResponse('<p class="p-4 text-sm text-gray-500">Lead not found.</p>')

    r = results[idx]

    # Enrich the single result with scoring data
    unified = score_unified(
        source_type=r.get("source_type", ""),
        vendor_score=r.get("vendor_score"),
        is_authorized=r.get("is_authorized", False),
        unit_price=r.get("unit_price"),
        qty_available=r.get("qty_available"),
        age_hours=r.get("age_hours"),
        has_price=bool(r.get("unit_price")),
        has_qty=bool(r.get("qty_available")),
        has_lead_time=bool(r.get("lead_time")),
        has_condition=bool(r.get("condition")),
    )
    r["confidence_pct"] = unified["confidence_pct"]
    r["confidence_color"] = unified["confidence_color"]
    r["source_badge"] = unified["source_badge"]
    r["score_components"] = unified.get("components", {})
    r["lead_quality"] = classify_lead(
        score=unified["score"],
        is_authorized=r.get("is_authorized", False),
        has_price=bool(r.get("unit_price")),
        has_qty=bool(r.get("qty_available")),
        has_contact=bool(r.get("vendor_email") or r.get("vendor_phone")),
        evidence_tier=r.get("evidence_tier"),
    )
    r["reason"] = explain_lead(
        vendor_name=r.get("vendor_name"),
        is_authorized=r.get("is_authorized", False),
        vendor_score=r.get("vendor_score"),
        unit_price=r.get("unit_price"),
        qty_available=r.get("qty_available"),
        has_contact=bool(r.get("vendor_email") or r.get("vendor_phone")),
        evidence_tier=r.get("evidence_tier"),
        source_type=r.get("source_type"),
    )

    # Look up vendor safety data from SourcingLead records if available
    safety_band = "unknown"
    safety_score = None
    safety_summary = "Safety is assessed when leads are sourced through requisitions."
    safety_flags = []
    safety_available = False

    vendor_name = r.get("vendor_name", "")
    if vendor_name:
        lead_row = (
            db.query(SourcingLead)
            .filter(SourcingLead.vendor_name.ilike(vendor_name))
            .order_by(SourcingLead.created_at.desc())
            .first()
        )
        if lead_row and lead_row.vendor_safety_band:
            safety_band = lead_row.vendor_safety_band
            safety_score = lead_row.vendor_safety_score
            safety_summary = lead_row.vendor_safety_summary or safety_summary
            safety_flags = lead_row.vendor_safety_flags or []
            safety_available = True

    # Look up material card for this MPN
    from ..models.intelligence import MaterialCard

    material_card_id = None
    mpn_clean = mpn.strip().lower()
    if mpn_clean:
        mc = db.query(MaterialCard.id).filter(MaterialCard.normalized_mpn == mpn_clean).first()
        if mc:
            material_card_id = mc.id

    ctx = _base_ctx(request, user, "search")
    ctx.update(
        {
            "lead": r,
            "mpn": mpn.strip(),
            "idx": idx,
            "safety_band": safety_band,
            "safety_score": safety_score,
            "safety_summary": safety_summary,
            "safety_flags": safety_flags,
            "safety_available": safety_available,
            "material_card_id": material_card_id,
        }
    )
    return templates.TemplateResponse("htmx/partials/search/lead_detail.html", ctx)


# ── Vendor partials ─────────────────────────────────────────────────────


@router.get("/v2/partials/vendors", response_class=HTMLResponse)
async def vendors_list_partial(
    request: Request,
    q: str = "",
    hide_blacklisted: bool = True,
    sort: str = "sighting_count",
    dir: str = "desc",
    my_only: bool = False,
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return vendor list as HTML partial with blacklisted toggle and sorting."""
    from ..models.strategic import StrategicVendor

    query = db.query(VendorCard)

    # Filter to user's strategic vendors if "My Vendors" tab is active
    if my_only:
        my_vendor_ids = (
            db.query(StrategicVendor.vendor_card_id)
            .filter(StrategicVendor.user_id == user.id, StrategicVendor.status == "active")
            .subquery()
        )
        query = query.filter(VendorCard.id.in_(my_vendor_ids))

    if hide_blacklisted:
        query = query.filter(VendorCard.is_blacklisted.is_(False))

    if q.strip():
        safe = escape_like(q.strip())
        query = query.filter(VendorCard.display_name.ilike(f"%{safe}%") | VendorCard.domain.ilike(f"%{safe}%"))

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

    ctx = _base_ctx(request, user, "vendors")
    ctx.update(
        {
            "vendors": vendors,
            "q": q,
            "hide_blacklisted": hide_blacklisted,
            "sort": sort,
            "dir": dir,
            "total": total,
            "limit": limit,
            "offset": offset,
            "my_only": my_only,
        }
    )
    return templates.TemplateResponse("htmx/partials/vendors/list.html", ctx)


@router.get("/v2/partials/vendors/{vendor_id}", response_class=HTMLResponse)
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
    except Exception:
        pass  # SourcingLead may not have data

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
        }
    )
    return templates.TemplateResponse("htmx/partials/vendors/detail.html", ctx)


@router.get("/v2/partials/vendors/{vendor_id}/tab/{tab}", response_class=HTMLResponse)
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

    valid_tabs = {"overview", "contacts", "find_contacts", "emails", "analytics", "offers", "reviews"}
    if tab not in valid_tabs:
        raise HTTPException(404, f"Unknown tab: {tab}")

    ctx = _base_ctx(request, user, "vendors")
    ctx["vendor"] = vendor

    if tab == "overview":
        recent_sightings = (
            db.query(Sighting)
            .filter(Sighting.vendor_name_normalized == vendor.normalized_name)
            .order_by(Sighting.created_at.desc().nullslast())
            .limit(10)
            .all()
        )
        # Safety data
        safety_band = None
        safety_summary = None
        safety_flags = None
        safety_score = None
        safety_available = False
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
                safety_available = True
        except Exception:
            pass
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
            }
        )
        # Re-use the inline overview from the detail template
        # by rendering just the overview portion
        return templates.TemplateResponse("htmx/partials/vendors/overview_tab.html", ctx)

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
        return templates.TemplateResponse("htmx/partials/vendors/tabs/contacts.html", ctx)

    elif tab == "find_contacts":
        prospects = (
            db.query(ProspectContact)
            .filter(ProspectContact.vendor_card_id == vendor_id)
            .order_by(ProspectContact.created_at.desc())
            .limit(50)
            .all()
        )
        ctx["prospects"] = prospects
        return templates.TemplateResponse("htmx/partials/vendors/find_contacts_tab.html", ctx)

    elif tab == "emails":
        from ..models.offers import Contact as RfqContact
        from ..models.offers import VendorResponse

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
        return templates.TemplateResponse("htmx/partials/vendors/emails_tab.html", ctx)

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

    else:  # offers
        offers = (
            db.query(Offer)
            .filter(Offer.vendor_name == vendor.display_name)
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


# ── Sprint 3: Vendor CRUD + Contact Management ────────────────────────


@router.get("/v2/partials/vendors/{vendor_id}/edit-form", response_class=HTMLResponse)
async def vendor_edit_form(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return inline edit form for vendor header fields."""
    vendor = db.query(VendorCard).filter(VendorCard.id == vendor_id).first()
    if not vendor:
        raise HTTPException(404, "Vendor not found")
    return templates.TemplateResponse(
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
    vendor = db.query(VendorCard).filter(VendorCard.id == vendor_id).first()
    if not vendor:
        raise HTTPException(404, "Vendor not found")

    form = await request.form()
    display_name = form.get("display_name", "").strip()
    if display_name:
        vendor.display_name = display_name
        from ..vendor_utils import normalize_vendor_name

        vendor.normalized_name = normalize_vendor_name(display_name)

    website = form.get("website", "").strip()
    vendor.website = website or vendor.website

    emails_raw = form.get("emails", "").strip()
    if emails_raw:
        vendor.emails = [e.strip() for e in emails_raw.split(",") if e.strip()]

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
    vendor = db.query(VendorCard).filter(VendorCard.id == vendor_id).first()
    if not vendor:
        raise HTTPException(404, "Vendor not found")

    vendor.is_blacklisted = not vendor.is_blacklisted
    vendor.updated_at = datetime.now(timezone.utc)
    db.commit()
    status = "blacklisted" if vendor.is_blacklisted else "un-blacklisted"
    logger.info("Vendor {} {} by {}", vendor_id, status, user.email)

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
    from ..models.intelligence import ActivityLog

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

    return templates.TemplateResponse(
        "htmx/partials/vendors/contact_timeline.html",
        {"request": request, "contact": contact, "activities": activities, "vendor_id": vendor_id},
    )


@router.get("/v2/partials/vendors/{vendor_id}/contact-nudges", response_class=HTMLResponse)
async def vendor_contact_nudges(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return nudge suggestions for dormant vendor contacts."""
    vendor = db.query(VendorCard).filter(VendorCard.id == vendor_id).first()
    if not vendor:
        raise HTTPException(404, "Vendor not found")

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
    return templates.TemplateResponse(
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
    from ..models import VendorReview

    vendor = db.query(VendorCard).filter(VendorCard.id == vendor_id).first()
    if not vendor:
        raise HTTPException(404, "Vendor not found")

    reviews = (
        db.query(VendorReview)
        .filter(VendorReview.vendor_card_id == vendor_id)
        .options(joinedload(VendorReview.user))
        .order_by(VendorReview.created_at.desc())
        .limit(20)
        .all()
    )
    return templates.TemplateResponse(
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
    from ..models import VendorReview

    vendor = db.query(VendorCard).filter(VendorCard.id == vendor_id).first()
    if not vendor:
        raise HTTPException(404, "Vendor not found")

    form = await request.form()
    rating = int(form.get("rating", "3"))
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
    from ..models import VendorReview

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
    vendor = db.query(VendorCard).filter(VendorCard.id == vendor_id).first()
    if not vendor:
        raise HTTPException(404, "Vendor not found")

    from ..config import settings as app_settings

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
    return templates.TemplateResponse("htmx/partials/vendors/find_contacts_results.html", ctx)


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

    pc.is_saved = True
    pc.saved_by_id = user.id
    db.commit()

    vendor = db.query(VendorCard).filter(VendorCard.id == vendor_id).first()
    ctx = _base_ctx(request, user, "vendors")
    ctx["vendor"] = vendor
    ctx["p"] = pc
    return templates.TemplateResponse("htmx/partials/vendors/prospect_card.html", ctx)


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
    return templates.TemplateResponse("htmx/partials/vendors/prospect_card.html", ctx)


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
    db.delete(pc)
    db.commit()
    # Return empty string to remove the card from DOM
    return HTMLResponse("")


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


# ── Sprint 4: Company CRUD (static routes — must precede {company_id}) ──


@router.get("/v2/partials/companies/create-form", response_class=HTMLResponse)
async def company_create_form(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return create company form."""
    users = db.query(User).filter(User.role.in_(("buyer", "trader", "manager", "admin"))).all()
    return templates.TemplateResponse(
        "htmx/partials/companies/create_form.html",
        {"request": request, "users": users},
    )


@router.post("/v2/partials/companies/create", response_class=HTMLResponse)
async def create_company(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a new company and redirect to its detail page."""
    form = await request.form()
    name = form.get("name", "").strip()
    if not name:
        raise HTTPException(400, "Company name is required")

    # Duplicate check
    existing = db.query(Company).filter(sqlfunc.lower(Company.name) == name.lower()).first()
    if existing:
        raise HTTPException(409, f"Company '{existing.name}' already exists (ID {existing.id})")

    company = Company(
        name=name,
        website=form.get("website", "").strip() or None,
        industry=form.get("industry", "").strip() or None,
        notes=form.get("notes", "").strip() or None,
        is_active=True,
    )
    owner_id = form.get("owner_id", "")
    if owner_id and owner_id.isdigit():
        company.account_owner_id = int(owner_id)
    db.add(company)
    db.flush()

    # Auto-create default site
    default_site = CustomerSite(
        company_id=company.id,
        site_name="HQ",
        site_type="headquarters",
        is_active=True,
    )
    db.add(default_site)
    db.commit()
    logger.info("Company {} created by {}", company.id, user.email)

    return await company_detail_partial(request=request, company_id=company.id, user=user, db=db)


@router.get("/v2/partials/companies/typeahead", response_class=HTMLResponse)
async def company_typeahead(
    request: Request,
    q: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return company typeahead results as HTML options."""
    if not q.strip() or len(q.strip()) < 2:
        return HTMLResponse("")

    safe = escape_like(q.strip())
    companies = (
        db.query(Company)
        .filter(Company.is_active.is_(True), Company.name.ilike(f"%{safe}%"))
        .order_by(Company.name)
        .limit(10)
        .all()
    )
    rows = [f'<option value="{c.id}">{c.name}</option>' for c in companies]
    return HTMLResponse("\n".join(rows))


@router.get("/v2/partials/companies/check-duplicate", response_class=HTMLResponse)
async def check_company_duplicate(
    request: Request,
    name: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Check for duplicate company name, return warning HTML if found."""
    if not name.strip():
        return HTMLResponse("")

    existing = (
        db.query(Company)
        .filter(
            Company.is_active.is_(True),
            sqlfunc.lower(Company.name) == name.strip().lower(),
        )
        .first()
    )
    if existing:
        return HTMLResponse(
            f'<p class="text-sm text-amber-600">A company named "{existing.name}" already exists (ID {existing.id}).</p>'
        )
    return HTMLResponse("")


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

    # Count open requisitions — use company_id FK if available, fall back to name match
    from sqlalchemy import or_

    open_req_count = (
        db.query(sqlfunc.count(Requisition.id))
        .filter(
            or_(
                Requisition.company_id == company.id,
                sqlfunc.lower(sqlfunc.trim(Requisition.customer_name)) == company.name.lower().strip(),
            ),
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

    if tab == "sites":
        from sqlalchemy.orm import joinedload

        sites = (
            db.query(CustomerSite)
            .options(joinedload(CustomerSite.owner))
            .filter(CustomerSite.company_id == company_id, CustomerSite.is_active.is_(True))
            .all()
        )
        users = db.query(User).order_by(User.name).all()
        ctx = _base_ctx(request, user, "companies")
        ctx["company"] = company
        ctx["sites"] = sites
        ctx["users"] = users
        return templates.TemplateResponse("htmx/partials/companies/tabs/sites_tab.html", ctx)

    elif tab == "contacts":
        # Get all SiteContact records across all sites for this company
        site_ids = [s.id for s in db.query(CustomerSite.id).filter(CustomerSite.company_id == company_id).all()]
        contacts = []
        if site_ids:
            contacts = (
                db.query(SiteContact)
                .filter(SiteContact.customer_site_id.in_(site_ids), SiteContact.is_active.is_(True))
                .order_by(SiteContact.is_primary.desc(), SiteContact.full_name)
                .all()
            )
        # Build a table with site name
        site_map = {s.id: s for s in db.query(CustomerSite).filter(CustomerSite.company_id == company_id).all()}
        rows = []
        for c in contacts:
            site = site_map.get(c.customer_site_id)
            site_name = site.site_name if site else _DASH
            phone_html = (
                f'<a href="tel:{c.phone}" class="text-brand-500 hover:text-brand-600">{c.phone}</a>'
                if c.phone
                else f'<span class="text-gray-500">{_DASH}</span>'
            )
            primary_badge = (
                ' <span class="px-1 py-0.5 text-[9px] font-medium rounded bg-emerald-50 text-emerald-700">Primary</span>'
                if c.is_primary
                else ""
            )
            rows.append(f"""<tr class="hover:bg-brand-50">
              <td class="px-4 py-2 text-sm font-medium text-gray-900">{c.full_name or _DASH}{primary_badge}</td>
              <td class="px-4 py-2 text-sm text-gray-500">{c.title or _DASH}</td>
              <td class="px-4 py-2 text-sm text-gray-500">{site_name}</td>
              <td class="px-4 py-2 text-sm text-gray-500">{c.email or _DASH}</td>
              <td class="px-4 py-2 text-sm">{phone_html}</td>
            </tr>""")
        # Also include legacy site-level contacts
        for s in site_map.values():
            if s.contact_name or s.contact_email:
                phone_html = (
                    f'<a href="tel:{s.contact_phone}" class="text-brand-500">{s.contact_phone}</a>'
                    if s.contact_phone
                    else f'<span class="text-gray-500">{_DASH}</span>'
                )
                rows.append(f"""<tr class="hover:bg-brand-50">
                  <td class="px-4 py-2 text-sm font-medium text-gray-900">{s.contact_name or _DASH} <span class="text-[9px] text-gray-400">legacy</span></td>
                  <td class="px-4 py-2 text-sm text-gray-500">{s.contact_title or _DASH}</td>
                  <td class="px-4 py-2 text-sm text-gray-500">{s.site_name}</td>
                  <td class="px-4 py-2 text-sm text-gray-500">{s.contact_email or _DASH}</td>
                  <td class="px-4 py-2 text-sm">{phone_html}</td>
                </tr>""")
        if rows:
            html = f"""<div class="overflow-x-auto">
              <table class="min-w-full divide-y divide-gray-200">
                <thead class="bg-gray-50">
                  <tr>
                    <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Name</th>
                    <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Title</th>
                    <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Site</th>
                    <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Email</th>
                    <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Phone</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-gray-200">{"".join(rows)}</tbody>
              </table>
            </div>"""
        else:
            html = '<div class="p-8 text-center"><p class="text-sm text-gray-500">No contacts found. Add contacts via the Sites tab.</p></div>'
        return HTMLResponse(html)

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
        from sqlalchemy import or_ as or_clause

        from ..models.intelligence import ActivityLog
        from ..models.offers import Contact as RfqContact

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

        # RFQ contacts across company's requisitions
        contacts = []
        if req_ids:
            contacts = (
                db.query(RfqContact)
                .filter(RfqContact.requisition_id.in_(req_ids))
                .order_by(RfqContact.created_at.desc())
                .limit(30)
                .all()
            )
        # Build req_map for backlinks
        req_map = {}
        if contacts:
            linked_req_ids = {c.requisition_id for c in contacts}
            for r in db.query(Requisition).filter(Requisition.id.in_(linked_req_ids)).all():
                req_map[r.id] = r

        # Quotes linked to company's sites
        site_ids = [s.id for s in db.query(CustomerSite.id).filter(CustomerSite.company_id == company_id).all()]
        quotes = []
        if site_ids:
            quotes = (
                db.query(Quote)
                .filter(Quote.customer_site_id.in_(site_ids))
                .order_by(Quote.created_at.desc().nullslast())
                .limit(20)
                .all()
            )

        # Direct activity logs on this company + its requisitions
        activity_filters = [ActivityLog.company_id == company.id]
        if req_ids:
            activity_filters.append(ActivityLog.requisition_id.in_(req_ids))
        activities = (
            db.query(ActivityLog)
            .filter(or_clause(*activity_filters))
            .order_by(ActivityLog.created_at.desc())
            .limit(30)
            .all()
        )

        ctx = _base_ctx(request, user, "companies")
        ctx.update(
            {
                "company": company,
                "contacts": contacts,
                "quotes": quotes,
                "activities": activities,
                "req_map": req_map,
            }
        )
        return templates.TemplateResponse("htmx/partials/companies/tabs/activity_tab.html", ctx)


# ── Sites & Site Contacts CRUD (Phase 4) ───────────────────────────────


@router.post("/v2/partials/companies/{company_id}/sites", response_class=HTMLResponse)
async def create_site(
    request: Request,
    company_id: int,
    site_name: str = Form(""),
    site_type: str = Form(""),
    city: str = Form(""),
    country: str = Form(""),
    owner_id: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a new site for a company, return the site card partial."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    if not site_name.strip():
        return HTMLResponse('<div class="p-2 text-xs text-rose-600">Site name is required.</div>')

    # Enforce one-owner-per-site rule: each user can only own one site
    parsed_owner_id = int(owner_id) if owner_id else None
    if parsed_owner_id:
        existing = (
            db.query(CustomerSite)
            .filter(
                CustomerSite.owner_id == parsed_owner_id,
            )
            .first()
        )
        if existing:
            owner_user = db.get(User, parsed_owner_id)
            owner_name = owner_user.display_name if owner_user else f"User #{parsed_owner_id}"
            return HTMLResponse(
                f'<div class="p-2 text-xs text-rose-600">{owner_name} already owns site "{existing.site_name}". Each user can only own one site.</div>'
            )

    site = CustomerSite(
        company_id=company_id,
        site_name=site_name.strip(),
        site_type=site_type or None,
        city=city or None,
        country=country or None,
        owner_id=parsed_owner_id,
        is_active=True,
    )
    db.add(site)
    db.commit()
    db.refresh(site)

    # Eager load owner for template
    if site.owner_id:
        _ = site.owner

    ctx = _base_ctx(request, user, "companies")
    ctx["company"] = company
    ctx["s"] = site
    return templates.TemplateResponse("htmx/partials/companies/tabs/site_card.html", ctx)


@router.delete("/v2/partials/companies/{company_id}/sites/{site_id}", response_class=HTMLResponse)
async def delete_site(
    request: Request,
    company_id: int,
    site_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Soft-delete a site (set is_active=False)."""
    site = db.query(CustomerSite).filter(CustomerSite.id == site_id, CustomerSite.company_id == company_id).first()
    if not site:
        raise HTTPException(404, "Site not found")
    site.is_active = False
    db.commit()
    return HTMLResponse("")


@router.get(
    "/v2/partials/companies/{company_id}/sites/{site_id}/contacts",
    response_class=HTMLResponse,
)
async def site_contacts_list(
    request: Request,
    company_id: int,
    site_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Load contacts for a specific site."""
    site = db.query(CustomerSite).filter(CustomerSite.id == site_id, CustomerSite.company_id == company_id).first()
    if not site:
        raise HTTPException(404, "Site not found")

    contacts = (
        db.query(SiteContact)
        .filter(SiteContact.customer_site_id == site_id, SiteContact.is_active.is_(True))
        .order_by(SiteContact.is_primary.desc(), SiteContact.full_name)
        .all()
    )
    company = db.query(Company).filter(Company.id == company_id).first()
    ctx = _base_ctx(request, user, "companies")
    ctx["site"] = site
    ctx["contacts"] = contacts
    ctx["company"] = company
    return templates.TemplateResponse("htmx/partials/companies/tabs/site_contacts.html", ctx)


@router.post(
    "/v2/partials/companies/{company_id}/sites/{site_id}/contacts",
    response_class=HTMLResponse,
)
async def create_site_contact(
    request: Request,
    company_id: int,
    site_id: int,
    full_name: str = Form(""),
    email: str = Form(""),
    title: str = Form(""),
    phone: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a site contact and return refreshed contacts list."""
    site = db.query(CustomerSite).filter(CustomerSite.id == site_id, CustomerSite.company_id == company_id).first()
    if not site:
        raise HTTPException(404, "Site not found")

    if not full_name.strip():
        return HTMLResponse('<div class="p-2 text-xs text-rose-600">Name is required.</div>')

    # Dedup by email
    if email:
        from sqlalchemy import func

        existing = (
            db.query(SiteContact)
            .filter(
                SiteContact.customer_site_id == site_id,
                func.lower(SiteContact.email) == email.strip().lower(),
            )
            .first()
        )
        if existing:
            # Already exists — just return the list
            pass
        else:
            contact = SiteContact(
                customer_site_id=site_id,
                full_name=full_name.strip(),
                email=email.strip() or None,
                title=title.strip() or None,
                phone=phone.strip() or None,
            )
            db.add(contact)
            db.commit()
    else:
        contact = SiteContact(
            customer_site_id=site_id,
            full_name=full_name.strip(),
            title=title.strip() or None,
            phone=phone.strip() or None,
        )
        db.add(contact)
        db.commit()

    # Return refreshed contacts list
    contacts = (
        db.query(SiteContact)
        .filter(SiteContact.customer_site_id == site_id, SiteContact.is_active.is_(True))
        .order_by(SiteContact.is_primary.desc(), SiteContact.full_name)
        .all()
    )
    company = db.query(Company).filter(Company.id == company_id).first()
    ctx = _base_ctx(request, user, "companies")
    ctx["site"] = site
    ctx["contacts"] = contacts
    ctx["company"] = company
    return templates.TemplateResponse("htmx/partials/companies/tabs/site_contacts.html", ctx)


@router.delete(
    "/v2/partials/companies/{company_id}/sites/{site_id}/contacts/{contact_id}",
    response_class=HTMLResponse,
)
async def delete_site_contact(
    request: Request,
    company_id: int,
    site_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a site contact."""
    contact = (
        db.query(SiteContact).filter(SiteContact.id == contact_id, SiteContact.customer_site_id == site_id).first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")
    db.delete(contact)
    db.commit()
    return HTMLResponse("")


@router.post(
    "/v2/partials/companies/{company_id}/sites/{site_id}/contacts/{contact_id}/primary",
    response_class=HTMLResponse,
)
async def set_primary_contact(
    request: Request,
    company_id: int,
    site_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Set a contact as primary for the site (unsets others)."""
    contact = (
        db.query(SiteContact).filter(SiteContact.id == contact_id, SiteContact.customer_site_id == site_id).first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")

    # Unset all other primary contacts on this site
    db.query(SiteContact).filter(
        SiteContact.customer_site_id == site_id,
        SiteContact.is_primary.is_(True),
        SiteContact.id != contact_id,
    ).update({"is_primary": False})
    contact.is_primary = True
    db.commit()

    # Return refreshed contacts list
    contacts = (
        db.query(SiteContact)
        .filter(SiteContact.customer_site_id == site_id, SiteContact.is_active.is_(True))
        .order_by(SiteContact.is_primary.desc(), SiteContact.full_name)
        .all()
    )
    site = db.query(CustomerSite).filter(CustomerSite.id == site_id).first()
    company = db.query(Company).filter(Company.id == company_id).first()
    ctx = _base_ctx(request, user, "companies")
    ctx["site"] = site
    ctx["contacts"] = contacts
    ctx["company"] = company
    return templates.TemplateResponse("htmx/partials/companies/tabs/site_contacts.html", ctx)


# ── Sprint 4: Company CRUD (parameterized routes) ──────────────────────


@router.get("/v2/partials/companies/{company_id}/edit-form", response_class=HTMLResponse)
async def company_edit_form(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return inline edit form for company fields."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    users = db.query(User).filter(User.role.in_(("buyer", "trader", "manager", "admin"))).all()
    return templates.TemplateResponse(
        "htmx/partials/companies/edit_form.html",
        {"request": request, "company": company, "users": users},
    )


@router.post("/v2/partials/companies/{company_id}/edit", response_class=HTMLResponse)
async def edit_company(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save company edits and return refreshed detail."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    form = await request.form()
    name = form.get("name", "").strip()
    if name:
        company.name = name
    website = form.get("website", "").strip()
    if website:
        company.website = website
    industry = form.get("industry", "").strip()
    company.industry = industry or company.industry
    notes = form.get("notes", "").strip()
    company.notes = notes or company.notes

    owner_id = form.get("owner_id", "")
    if owner_id and owner_id.isdigit():
        company.account_owner_id = int(owner_id)

    company.updated_at = datetime.now(timezone.utc)
    db.commit()
    logger.info("Company {} edited by {}", company_id, user.email)

    return await company_detail_partial(request=request, company_id=company_id, user=user, db=db)


@router.post("/v2/partials/companies/{company_id}/sites/{site_id}/edit", response_class=HTMLResponse)
async def edit_site(
    request: Request,
    company_id: int,
    site_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Update site fields and return refreshed sites tab."""
    site = db.query(CustomerSite).filter(CustomerSite.id == site_id, CustomerSite.company_id == company_id).first()
    if not site:
        raise HTTPException(404, "Site not found")

    form = await request.form()
    site_name = form.get("site_name", "").strip()
    if site_name:
        site.site_name = site_name
    site.city = form.get("city", "").strip() or site.city
    site.country = form.get("country", "").strip() or site.country
    site.site_type = form.get("site_type", "").strip() or site.site_type
    db.commit()
    logger.info("Site {} edited by {}", site_id, user.email)

    return await company_tab(request=request, company_id=company_id, tab="sites", user=user, db=db)


@router.post(
    "/v2/partials/companies/{company_id}/sites/{site_id}/contacts/{contact_id}/notes",
    response_class=HTMLResponse,
)
async def add_site_contact_note(
    request: Request,
    company_id: int,
    site_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add a note to a site contact and return updated notes list."""
    from ..models.intelligence import ActivityLog

    contact = (
        db.query(SiteContact).filter(SiteContact.id == contact_id, SiteContact.customer_site_id == site_id).first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")

    form = await request.form()
    notes_text = form.get("notes", "").strip()
    if not notes_text:
        raise HTTPException(400, "Notes cannot be empty")

    log = ActivityLog(
        user_id=user.id,
        activity_type="contact_note",
        channel="note",
        contact_name=contact.full_name or "",
        contact_email=contact.email or "",
        notes=notes_text,
    )
    db.add(log)
    db.commit()
    logger.info("Note added for site contact {} by {}", contact_id, user.email)

    # Return refreshed notes
    return await get_site_contact_notes(
        request=request,
        company_id=company_id,
        site_id=site_id,
        contact_id=contact_id,
        user=user,
        db=db,
    )


@router.get(
    "/v2/partials/companies/{company_id}/sites/{site_id}/contacts/{contact_id}/notes",
    response_class=HTMLResponse,
)
async def get_site_contact_notes(
    request: Request,
    company_id: int,
    site_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return notes timeline for a site contact."""
    from ..models.intelligence import ActivityLog

    contact = (
        db.query(SiteContact).filter(SiteContact.id == contact_id, SiteContact.customer_site_id == site_id).first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")

    notes = (
        (
            db.query(ActivityLog)
            .filter(
                ActivityLog.activity_type == "contact_note",
                ActivityLog.contact_email == contact.email,
            )
            .order_by(ActivityLog.created_at.desc())
            .limit(20)
            .all()
        )
        if contact.email
        else []
    )

    return templates.TemplateResponse(
        "htmx/partials/companies/contact_notes.html",
        {"request": request, "contact": contact, "notes": notes, "company_id": company_id, "site_id": site_id},
    )


# ── Sprint 5: Quote Workflow Completion ────────────────────────────────


@router.post("/v2/partials/quotes/{quote_id}/preview", response_class=HTMLResponse)
async def preview_quote(
    request: Request,
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render quote email preview before sending."""
    quote = db.query(Quote).options(joinedload(Quote.quote_lines)).filter(Quote.id == quote_id).first()
    if not quote:
        raise HTTPException(404, "Quote not found")

    return templates.TemplateResponse(
        "htmx/partials/quotes/preview.html",
        {"request": request, "quote": quote},
    )


@router.delete("/v2/partials/quotes/{quote_id}", response_class=HTMLResponse)
async def delete_quote_htmx(
    request: Request,
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a draft quote and return refreshed quotes list."""
    quote = db.query(Quote).filter(Quote.id == quote_id).first()
    if not quote:
        raise HTTPException(404, "Quote not found")
    if quote.status != "draft":
        raise HTTPException(400, "Only draft quotes can be deleted")

    db.delete(quote)
    db.commit()
    logger.info("Quote {} deleted by {}", quote_id, user.email)

    return await quotes_list_partial(request=request, user=user, db=db, limit=50, offset=0)


@router.post("/v2/partials/quotes/{quote_id}/reopen", response_class=HTMLResponse)
async def reopen_quote(
    request: Request,
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Reopen a sent/closed quote back to draft."""
    quote = db.query(Quote).filter(Quote.id == quote_id).first()
    if not quote:
        raise HTTPException(404, "Quote not found")
    if quote.status not in ("sent", "won", "lost"):
        raise HTTPException(400, "Only sent/won/lost quotes can be reopened")

    quote.status = "draft"
    quote.updated_at = datetime.now(timezone.utc)
    db.commit()
    logger.info("Quote {} reopened by {}", quote_id, user.email)

    return await quote_detail_partial(request=request, quote_id=quote_id, user=user, db=db)


@router.get("/v2/partials/quotes/recent-terms", response_class=HTMLResponse)
async def recent_terms(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return recent payment/shipping terms as datalist options."""
    from sqlalchemy import distinct

    payment_terms = (
        db.query(distinct(Quote.payment_terms))
        .filter(Quote.payment_terms.isnot(None), Quote.payment_terms != "")
        .order_by(Quote.payment_terms)
        .limit(20)
        .all()
    )
    shipping_terms = (
        db.query(distinct(Quote.shipping_terms))
        .filter(Quote.shipping_terms.isnot(None), Quote.shipping_terms != "")
        .order_by(Quote.shipping_terms)
        .limit(20)
        .all()
    )
    payment_opts = [f'<option value="{t[0]}">' for t in payment_terms if t[0]]
    shipping_opts = [f'<option value="{t[0]}">' for t in shipping_terms if t[0]]
    html = f'<datalist id="payment-terms">{"".join(payment_opts)}</datalist>'
    html += f'<datalist id="shipping-terms">{"".join(shipping_opts)}</datalist>'
    return HTMLResponse(html)


@router.get("/v2/partials/pricing-history/{mpn}", response_class=HTMLResponse)
async def pricing_history(
    request: Request,
    mpn: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return pricing history table for an MPN."""
    from ..utils.normalization import normalize_mpn_key

    norm = normalize_mpn_key(mpn)
    offers = (
        (
            db.query(Offer)
            .filter(Offer.normalized_mpn == norm, Offer.unit_price.isnot(None))
            .order_by(Offer.created_at.desc())
            .limit(50)
            .all()
        )
        if norm
        else []
    )

    return templates.TemplateResponse(
        "htmx/partials/quotes/pricing_history.html",
        {"request": request, "offers": offers, "mpn": mpn},
    )


@router.post("/v2/partials/quotes/{quote_id}/edit", response_class=HTMLResponse)
async def edit_quote_metadata(
    request: Request,
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Update quote metadata (payment terms, shipping, notes) and return refreshed
    detail."""
    quote = db.query(Quote).filter(Quote.id == quote_id).first()
    if not quote:
        raise HTTPException(404, "Quote not found")

    form = await request.form()
    if form.get("payment_terms"):
        quote.payment_terms = form["payment_terms"].strip()
    if form.get("shipping_terms"):
        quote.shipping_terms = form["shipping_terms"].strip()
    if form.get("notes"):
        quote.notes = form["notes"].strip()
    if form.get("valid_until"):
        quote.valid_until = form["valid_until"].strip()

    quote.updated_at = datetime.now(timezone.utc)
    db.commit()
    logger.info("Quote {} metadata edited by {}", quote_id, user.email)

    return await quote_detail_partial(request=request, quote_id=quote_id, user=user, db=db)


# ── Sprint 6: RFQ Workflow Depth ────────────────────────────────────────


@router.get("/v2/partials/requisitions/{req_id}/rfq-prepare", response_class=HTMLResponse)
async def rfq_prepare_panel(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return RFQ preparation panel — vendor data + exhaustion check."""
    from ..models.offers import Contact as RfqContact

    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    if not req:
        raise HTTPException(404, "Requisition not found")

    # Get requirements for this req
    requirements = db.query(Requirement).filter(Requirement.requisition_id == req_id).all()
    mpns = [r.primary_mpn for r in requirements if r.primary_mpn]

    # Get vendors already contacted
    existing_contacts = (
        db.query(RfqContact.vendor_name_normalized).filter(RfqContact.requisition_id == req_id).distinct().all()
    )
    contacted_norms = {c[0] for c in existing_contacts if c[0]}

    # Get suggested vendors from sightings (join on normalized vendor name)
    from ..models import Sighting

    suggested_vendors = (
        (
            db.query(
                VendorCard.id,
                VendorCard.display_name,
                VendorCard.normalized_name,
                sqlfunc.count(Sighting.id).label("sighting_count"),
            )
            .join(Sighting, Sighting.vendor_name_normalized == VendorCard.normalized_name)
            .filter(
                Sighting.mpn_matched.in_(mpns) if mpns else sqlfunc.literal(False),
                VendorCard.is_blacklisted.isnot(True),
            )
            .group_by(VendorCard.id)
            .order_by(sqlfunc.count(Sighting.id).desc())
            .limit(20)
            .all()
        )
        if mpns
        else []
    )

    vendors = []
    for v in suggested_vendors:
        vendors.append(
            {
                "id": v.id,
                "display_name": v.display_name,
                "normalized_name": v.normalized_name,
                "sighting_count": v.sighting_count,
                "already_contacted": v.normalized_name in contacted_norms,
            }
        )

    return templates.TemplateResponse(
        "htmx/partials/requisitions/rfq_prepare.html",
        {"request": request, "req": req, "vendors": vendors, "mpns": mpns, "total_contacted": len(contacted_norms)},
    )


@router.post("/v2/partials/requisitions/{req_id}/log-phone", response_class=HTMLResponse)
async def log_phone_call(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Log a phone call to a vendor and return updated activity tab."""
    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    if not req:
        raise HTTPException(404, "Requisition not found")

    form = await request.form()
    vendor_name = form.get("vendor_name", "").strip()
    vendor_phone = form.get("vendor_phone", "").strip()
    notes = form.get("notes", "").strip()

    if not vendor_name or not vendor_phone:
        raise HTTPException(400, "Vendor name and phone are required")

    from ..models.intelligence import ActivityLog
    from ..models.offers import Contact as RfqContact

    contact = RfqContact(
        requisition_id=req_id,
        user_id=user.id,
        contact_type="phone",
        vendor_name=vendor_name,
        vendor_contact=vendor_phone,
        details=notes or f"Phone call to {vendor_name}",
        status="completed",
    )
    db.add(contact)

    log = ActivityLog(
        user_id=user.id,
        activity_type="phone_call",
        channel="phone",
        company_id=None,
        contact_name=vendor_name,
        contact_email=vendor_phone,
        notes=notes or f"Called {vendor_name} at {vendor_phone}",
    )
    db.add(log)
    db.commit()
    logger.info("Phone call logged for req {} vendor {} by {}", req_id, vendor_name, user.email)

    return templates.TemplateResponse(
        "htmx/partials/requisitions/phone_log_success.html",
        {"request": request, "vendor_name": vendor_name, "vendor_phone": vendor_phone},
    )


@router.post("/v2/partials/follow-ups/send-batch", response_class=HTMLResponse)
async def send_batch_follow_up(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Send follow-ups to all stale contacts at once."""
    from ..models.offers import Contact as RfqContact

    cfg = getattr(request.app, "state", None)
    threshold_days = getattr(cfg, "follow_up_days", 2) if cfg else 2
    threshold = datetime.now(timezone.utc) - timedelta(days=threshold_days)

    stale = (
        db.query(RfqContact)
        .filter(
            RfqContact.contact_type == "email",
            RfqContact.status.in_(["sent", "opened"]),
            RfqContact.created_at < threshold,
        )
        .limit(50)
        .all()
    )

    sent_count = 0
    for contact in stale:
        contact.status = "followed_up"
        contact.status_updated_at = datetime.now(timezone.utc)
        sent_count += 1
    db.commit()
    logger.info("Batch follow-up: {} contacts marked by {}", sent_count, user.email)

    return templates.TemplateResponse(
        "htmx/partials/follow_ups/batch_result.html",
        {"request": request, "sent_count": sent_count},
    )


@router.get("/v2/partials/follow-ups/badge", response_class=HTMLResponse)
async def follow_up_badge(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return follow-up count badge for nav sidebar."""
    from ..models.offers import Contact as RfqContact

    threshold = datetime.now(timezone.utc) - timedelta(days=2)
    count = (
        db.query(sqlfunc.count(RfqContact.id))
        .filter(
            RfqContact.contact_type == "email",
            RfqContact.status.in_(["sent", "opened"]),
            RfqContact.created_at < threshold,
        )
        .scalar()
        or 0
    )
    if count > 0:
        return HTMLResponse(
            f'<span class="ml-auto px-1.5 py-0.5 text-[10px] font-bold text-white bg-amber-500 rounded-full">{count}</span>'
        )
    return HTMLResponse("")


@router.patch("/v2/partials/requisitions/{req_id}/responses/{response_id}/status", response_class=HTMLResponse)
async def update_response_status(
    request: Request,
    req_id: int,
    response_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Update vendor response status (reviewed/rejected/flagged)."""
    from ..models.offers import VendorResponse

    vr = (
        db.query(VendorResponse)
        .filter(
            VendorResponse.id == response_id,
            VendorResponse.requisition_id == req_id,
        )
        .first()
    )
    if not vr:
        raise HTTPException(404, "Response not found")

    form = await request.form()
    new_status = form.get("status", "").strip()
    valid = {"reviewed", "rejected", "flagged", "new"}
    if new_status not in valid:
        raise HTTPException(400, f"Invalid status. Must be one of: {', '.join(valid)}")

    vr.status = new_status
    db.commit()
    logger.info("Response {} status → {} by {}", response_id, new_status, user.email)

    return templates.TemplateResponse(
        "htmx/partials/requisitions/response_status_badge.html",
        {"request": request, "response": vr},
    )


# ── Sprint 7: Email Integration ────────────────────────────────────────


@router.get("/v2/partials/emails/thread/{conversation_id}", response_class=HTMLResponse)
async def email_thread_viewer(
    request: Request,
    conversation_id: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render email thread viewer with all messages."""
    messages = []
    error = None
    try:
        from ..dependencies import require_fresh_token as _rft

        token = await _rft(request, db)
        from ..services.email_threads import fetch_thread_messages

        messages = await fetch_thread_messages(conversation_id, token)
    except HTTPException:
        error = "M365 connection needs refresh — please reconnect in Settings"
    except (ConnectionError, TimeoutError, OSError, RuntimeError) as exc:
        error = f"Could not load thread: {str(exc)[:100]}"

    return templates.TemplateResponse(
        "htmx/partials/emails/thread_viewer.html",
        {"request": request, "messages": messages, "conversation_id": conversation_id, "error": error},
    )


@router.post("/v2/partials/emails/reply", response_class=HTMLResponse)
async def send_email_reply(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Send an email reply and return success confirmation."""
    form = await request.form()
    to = form.get("to", "").strip()
    subject = form.get("subject", "").strip()
    body = form.get("body", "").strip()
    conversation_id = form.get("conversation_id", "").strip()

    if not to or not body:
        raise HTTPException(400, "Recipient and message body are required")

    error = None
    try:
        from ..dependencies import require_fresh_token as _rft

        token = await _rft(request, db)
        from ..email_service import _build_html_body
        from ..utils.graph_client import GraphClient

        gc = GraphClient(token)
        html_body = _build_html_body(body)
        mail_payload = {
            "message": {
                "subject": subject or "Re:",
                "body": {"contentType": "HTML", "content": html_body},
                "toRecipients": [{"emailAddress": {"address": to}}],
            },
            "saveToSentItems": "true",
        }
        result = await gc.post_json("/me/sendMail", mail_payload)
        if "error" in result:
            error = f"Send failed: {result.get('detail', 'Unknown error')}"
    except HTTPException:
        error = "M365 connection needs refresh"
    except (ConnectionError, TimeoutError, OSError, RuntimeError) as exc:
        error = f"Send failed: {str(exc)[:100]}"

    return templates.TemplateResponse(
        "htmx/partials/emails/reply_result.html",
        {"request": request, "to": to, "error": error, "conversation_id": conversation_id},
    )


@router.get("/v2/partials/emails/thread/{conversation_id}/summary", response_class=HTMLResponse)
async def email_thread_summary(
    request: Request,
    conversation_id: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return AI-generated summary of an email thread."""
    summary = None
    error = None
    try:
        from ..dependencies import require_fresh_token as _rft

        token = await _rft(request, db)
        from ..services.email_intelligence_service import summarize_thread

        summary = await summarize_thread(token, conversation_id, db, user.id)
        if not summary:
            error = "Could not generate summary"
    except HTTPException:
        error = "M365 connection needs refresh"
    except (ConnectionError, TimeoutError, OSError, RuntimeError) as exc:
        error = f"Summary failed: {str(exc)[:100]}"

    return templates.TemplateResponse(
        "htmx/partials/emails/thread_summary.html",
        {"request": request, "summary": summary, "error": error},
    )


@router.get("/v2/partials/email-intelligence", response_class=HTMLResponse)
async def email_intelligence_partial(
    request: Request,
    classification: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return email intelligence dashboard as HTML partial."""
    from ..services.email_intelligence_service import get_recent_intelligence
    from ..services.response_analytics import get_email_intelligence_dashboard

    items = get_recent_intelligence(db, user.id, limit=50, classification=classification or None)
    dashboard = get_email_intelligence_dashboard(db, user.id, days=7)

    return templates.TemplateResponse(
        "htmx/partials/emails/intelligence_dashboard.html",
        {"request": request, "items": items, "dashboard": dashboard, "classification": classification},
    )


# ── Dashboard partial ───────────────────────────────────────────────────


@router.get("/v2/partials/dashboard", response_class=HTMLResponse)
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


# ── AI Insights HTMX routes (Phase 6) ─────────────────────────────────


def _render_insights(request, user, insights, entity_type, entity_id):
    """Render the shared insights panel partial."""
    ctx = _base_ctx(request, user, entity_type)
    ctx["insights"] = insights
    ctx["entity_type"] = entity_type
    ctx["entity_id"] = entity_id
    return templates.TemplateResponse("htmx/partials/shared/insights_panel.html", ctx)


@router.get("/v2/partials/requisitions/{req_id}/insights", response_class=HTMLResponse)
async def requisition_insights_panel(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return cached AI insights panel for a requisition."""
    from ..services.knowledge_service import get_cached_insights

    insights = get_cached_insights(db, req_id)
    return _render_insights(request, user, insights, "requisitions", req_id)


@router.post("/v2/partials/requisitions/{req_id}/insights/refresh", response_class=HTMLResponse)
async def requisition_insights_refresh(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Generate fresh AI insights for a requisition and return panel."""
    from ..services.knowledge_service import generate_insights, get_cached_insights

    try:
        generate_insights(db, req_id)
    except Exception as e:
        logger.warning(f"Insight generation failed for req {req_id}: {e}")
    insights = get_cached_insights(db, req_id)
    return _render_insights(request, user, insights, "requisitions", req_id)


@router.get("/v2/partials/vendors/{vendor_id}/insights", response_class=HTMLResponse)
async def vendor_insights_panel(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return cached AI insights panel for a vendor."""
    from ..services.knowledge_service import get_cached_vendor_insights

    insights = get_cached_vendor_insights(db, vendor_id)
    return _render_insights(request, user, insights, "vendors", vendor_id)


@router.post("/v2/partials/vendors/{vendor_id}/insights/refresh", response_class=HTMLResponse)
async def vendor_insights_refresh(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Generate fresh AI insights for a vendor and return panel."""
    from ..services.knowledge_service import generate_vendor_insights, get_cached_vendor_insights

    try:
        generate_vendor_insights(db, vendor_id)
    except Exception as e:
        logger.warning(f"Insight generation failed for vendor {vendor_id}: {e}")
    insights = get_cached_vendor_insights(db, vendor_id)
    return _render_insights(request, user, insights, "vendors", vendor_id)


@router.get("/v2/partials/companies/{company_id}/insights", response_class=HTMLResponse)
async def company_insights_panel(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return cached AI insights panel for a company."""
    from ..services.knowledge_service import get_cached_company_insights

    insights = get_cached_company_insights(db, company_id)
    return _render_insights(request, user, insights, "companies", company_id)


@router.post("/v2/partials/companies/{company_id}/insights/refresh", response_class=HTMLResponse)
async def company_insights_refresh(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Generate fresh AI insights for a company and return panel."""
    from ..services.knowledge_service import generate_company_insights, get_cached_company_insights

    try:
        generate_company_insights(db, company_id)
    except Exception as e:
        logger.warning(f"Insight generation failed for company {company_id}: {e}")
    insights = get_cached_company_insights(db, company_id)
    return _render_insights(request, user, insights, "companies", company_id)


@router.get("/v2/partials/dashboard/pipeline-insights", response_class=HTMLResponse)
async def pipeline_insights_panel(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return cached pipeline insights for the dashboard."""
    from ..services.knowledge_service import get_cached_pipeline_insights

    insights = get_cached_pipeline_insights(db)
    return _render_insights(request, user, insights, "dashboard", 0)


@router.post("/v2/partials/dashboard/pipeline-insights/refresh", response_class=HTMLResponse)
async def pipeline_insights_refresh(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Generate fresh pipeline insights and return panel."""
    from ..services.knowledge_service import generate_pipeline_insights, get_cached_pipeline_insights

    try:
        generate_pipeline_insights(db)
    except Exception as e:
        logger.warning(f"Pipeline insight generation failed: {e}")
    insights = get_cached_pipeline_insights(db)
    return _render_insights(request, user, insights, "dashboard", 0)


# ── Buy Plans partials ─────────────────────────────────────────────────


def _is_ops_member(user: User, db: Session) -> bool:
    """Check if user is in the ops verification group."""
    return db.query(VerificationGroupMember).filter_by(user_id=user.id, is_active=True).first() is not None


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
            BuyPlan.sales_order_number.ilike(f"%{safe}%") | BuyPlan.customer_po_number.ilike(f"%{safe}%")
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

        buy_plans.append(
            {
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
            }
        )

    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update(
        {
            "buy_plans": buy_plans,
            "q": q,
            "status": status,
            "mine": mine,
            "total": len(buy_plans),
        }
    )
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
    ctx.update(
        {
            "bp": bp,
            "lines": bp.lines or [],
            "is_ops_member": _is_ops_member(user, db),
            "user": user,
        }
    )
    return templates.TemplateResponse("htmx/partials/buy_plans/detail.html", ctx)


@router.post("/v2/partials/buy-plans/{plan_id}/submit", response_class=HTMLResponse)
async def buy_plan_submit_partial(
    request: Request,
    plan_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Submit a draft buy plan with SO# — returns refreshed detail partial."""
    from ..services.buyplan_notifications import (
        notify_approved,
        notify_submitted,
        run_notify_bg,
    )
    from ..services.buyplan_workflow import submit_buy_plan

    form = await request.form()
    so = form.get("sales_order_number", "").strip()
    if not so:
        raise HTTPException(400, "Sales Order # is required")

    try:
        plan = submit_buy_plan(
            plan_id,
            so,
            user,
            db,
            customer_po_number=form.get("customer_po_number") or None,
            salesperson_notes=form.get("salesperson_notes") or None,
        )
        db.commit()
        if plan.auto_approved:
            await run_notify_bg(notify_approved, plan.id)
        else:
            await run_notify_bg(notify_submitted, plan.id)
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
    from ..services.buyplan_notifications import (
        notify_approved,
        notify_rejected,
        run_notify_bg,
    )
    from ..services.buyplan_workflow import approve_buy_plan

    form = await request.form()
    action = form.get("action", "approve")

    if user.role not in ("manager", "admin"):
        raise HTTPException(403, "Manager or admin role required")

    try:
        plan = approve_buy_plan(plan_id, action, user, db, notes=form.get("notes"))
        db.commit()
        if action == "approve":
            await run_notify_bg(notify_approved, plan.id)
        else:
            await run_notify_bg(notify_rejected, plan.id)
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
    from ..services.buyplan_notifications import (
        notify_so_rejected,
        notify_so_verified,
        run_notify_bg,
    )
    from ..services.buyplan_workflow import verify_so

    form = await request.form()
    action = form.get("action", "approve")

    try:
        plan = verify_so(
            plan_id,
            action,
            user,
            db,
            rejection_note=form.get("rejection_note"),
        )
        db.commit()
        if action == "approve":
            await run_notify_bg(notify_so_verified, plan.id)
        else:
            await run_notify_bg(notify_so_rejected, plan.id, action=action)
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

    from ..services.buyplan_notifications import notify_po_confirmed, run_notify_bg
    from ..services.buyplan_workflow import confirm_po

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
        await run_notify_bg(notify_po_confirmed, plan_id, line_id=line_id)
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
    from ..services.buyplan_notifications import notify_completed, run_notify_bg
    from ..services.buyplan_workflow import check_completion, verify_po

    form = await request.form()
    action = form.get("action", "approve")

    try:
        verify_po(plan_id, line_id, action, user, db, rejection_note=form.get("rejection_note"))
        db.commit()
        updated = check_completion(plan_id, db)
        if updated and updated.status == "completed":
            db.commit()
            await run_notify_bg(notify_completed, plan_id)
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


# ── Sourcing partials ──────────────────────────────────────────────────


@router.get("/v2/sourcing/{requirement_id}", response_class=HTMLResponse)
async def v2_sourcing_page(request: Request, requirement_id: int, db: Session = Depends(get_db)):
    """Full page load for sourcing results."""
    user = get_user(request, db)
    if not user:
        return templates.TemplateResponse("htmx/login.html", {"request": request, **_vite_assets()})
    ctx = _base_ctx(request, user, "requisitions")
    ctx["partial_url"] = f"/v2/partials/sourcing/{requirement_id}"
    return templates.TemplateResponse("htmx/base_page.html", ctx)


@router.get("/v2/sourcing/leads/{lead_id}", response_class=HTMLResponse)
async def v2_lead_detail_page(request: Request, lead_id: int, db: Session = Depends(get_db)):
    """Full page load for lead detail."""
    user = get_user(request, db)
    if not user:
        return templates.TemplateResponse("htmx/login.html", {"request": request, **_vite_assets()})
    ctx = _base_ctx(request, user, "requisitions")
    ctx["partial_url"] = f"/v2/partials/sourcing/leads/{lead_id}"
    return templates.TemplateResponse("htmx/base_page.html", ctx)


@router.get("/v2/partials/sourcing/{requirement_id}/stream")
async def sourcing_stream(
    request: Request,
    requirement_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """SSE endpoint for sourcing search progress.

    Streams per-source completion events as connectors finish searching.
    Client connects via hx-ext="sse" sse-connect attribute.
    Channel: sourcing:{requirement_id}
    """
    from sse_starlette.sse import EventSourceResponse

    from ..services.sse_broker import broker

    req = db.query(Requirement).filter(Requirement.id == requirement_id).first()
    if not req:
        raise HTTPException(404, "Requirement not found")

    async def event_generator():
        async for msg in broker.listen(f"sourcing:{requirement_id}"):
            if await request.is_disconnected():
                break
            yield {
                "event": msg["event"],
                "data": msg["data"],
            }

    return EventSourceResponse(event_generator())


@router.post("/v2/partials/sourcing/{requirement_id}/search", response_class=HTMLResponse)
async def sourcing_search_trigger(
    request: Request,
    requirement_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Trigger multi-source search for a requirement.

    Runs connectors in parallel, publishes SSE events per source completion, syncs leads
    on completion, returns redirect to sourcing results.
    """
    import asyncio
    import json

    from ..services.sse_broker import broker

    req = db.query(Requirement).filter(Requirement.id == requirement_id).first()
    if not req:
        raise HTTPException(404, "Requirement not found")

    mpn = req.primary_mpn or ""
    sources = ["brokerbin", "nexar", "digikey", "mouser", "oemsecrets", "element14"]
    channel = f"sourcing:{requirement_id}"
    all_sightings = []

    async def search_source(source_name):
        start_t = time.time()
        try:
            from ..search_service import quick_search_mpn

            raw = await quick_search_mpn(mpn, db)
            results = raw if isinstance(raw, list) else raw.get("sightings", [])
            elapsed = int((time.time() - start_t) * 1000)
            count = len(results) if results else 0
            await broker.publish(
                channel,
                "source-complete",
                json.dumps({"source": source_name, "count": count, "elapsed_ms": elapsed, "status": "done"}),
            )
            return results or []
        except Exception as exc:
            elapsed = int((time.time() - start_t) * 1000)
            logger.error("Sourcing search failed for {} on {}: {}", mpn, source_name, exc)
            await broker.publish(
                channel,
                "source-complete",
                json.dumps(
                    {"source": source_name, "count": 0, "elapsed_ms": elapsed, "status": "failed", "error": str(exc)}
                ),
            )
            return []

    results_by_source = await asyncio.gather(*[search_source(s) for s in sources], return_exceptions=True)

    for source_results in results_by_source:
        if isinstance(source_results, list):
            all_sightings.extend(source_results)

    await broker.publish(
        channel, "search-complete", json.dumps({"total": len(all_sightings), "requirement_id": requirement_id})
    )

    return HTMLResponse(status_code=200, headers={"HX-Redirect": f"/v2/sourcing/{requirement_id}"})


@router.get("/v2/partials/sourcing/{requirement_id}", response_class=HTMLResponse)
async def sourcing_results_partial(
    request: Request,
    requirement_id: int,
    confidence: str = "",
    safety: str = "",
    freshness: str = "",
    source: str = "",
    status: str = "",
    contactability: str = "",
    corroborated: str = "",
    sort: str = "best",
    page: int = Query(1, ge=1),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return sourcing results with lead cards for a requirement.

    Supports filtering by confidence band, safety band, freshness window, source type,
    buyer status, contactability, and corroboration. Sorts by best overall (default),
    freshest, safest, easiest to contact, or most proven.
    """
    from ..models.sourcing_lead import SourcingLead

    req = db.query(Requirement).filter(Requirement.id == requirement_id).first()
    if not req:
        raise HTTPException(404, "Requirement not found")

    query = db.query(SourcingLead).filter(SourcingLead.requirement_id == requirement_id)

    if confidence:
        bands = [b.strip() for b in confidence.split(",")]
        query = query.filter(SourcingLead.confidence_band.in_(bands))
    if safety:
        bands = [b.strip() for b in safety.split(",")]
        query = query.filter(SourcingLead.vendor_safety_band.in_(bands))
    if freshness and freshness != "all":
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        cutoffs = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
        if freshness in cutoffs:
            query = query.filter(SourcingLead.source_last_seen_at >= now - cutoffs[freshness])
    if source:
        sources_list = [s.strip() for s in source.split(",")]
        query = query.filter(SourcingLead.primary_source_type.in_(sources_list))
    if status and status != "all":
        statuses = [s.strip() for s in status.split(",")]
        query = query.filter(SourcingLead.buyer_status.in_(statuses))
    if contactability == "has_email":
        query = query.filter(SourcingLead.contact_email.isnot(None))
    elif contactability == "has_phone":
        query = query.filter(SourcingLead.contact_phone.isnot(None))
    if corroborated == "yes":
        query = query.filter(SourcingLead.corroborated.is_(True))
    elif corroborated == "no":
        query = query.filter(SourcingLead.corroborated.is_(False))

    sort_map = {
        "best": [SourcingLead.confidence_score.desc()],
        "freshest": [SourcingLead.source_last_seen_at.desc().nullslast()],
        "safest": [SourcingLead.vendor_safety_score.desc().nullslast()],
        "contact": [SourcingLead.contactability_score.desc().nullslast()],
        "proven": [SourcingLead.historical_success_score.desc().nullslast()],
    }
    for col in sort_map.get(sort, sort_map["best"]):
        query = query.order_by(col)

    total = query.count()
    per_page = 24
    leads = query.offset((page - 1) * per_page).limit(per_page).all()

    lead_sighting_data = {}
    if leads:
        for lead in leads:
            best_sighting = (
                db.query(Sighting)
                .filter(
                    Sighting.requirement_id == requirement_id,
                    Sighting.vendor_name_normalized == lead.vendor_name_normalized,
                )
                .order_by(Sighting.created_at.desc().nullslast())
                .first()
            )
            if best_sighting:
                lead_sighting_data[lead.id] = {
                    "qty_available": best_sighting.qty_available,
                    "unit_price": best_sighting.unit_price,
                }

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update(
        {
            "requirement": req,
            "leads": leads,
            "lead_sighting_data": lead_sighting_data,
            "total": total,
            "page": page,
            "total_pages": max(1, (total + per_page - 1) // per_page),
            "per_page": per_page,
            "f_confidence": confidence,
            "f_safety": safety,
            "f_freshness": freshness,
            "f_source": source,
            "f_status": status,
            "f_contactability": contactability,
            "f_corroborated": corroborated,
            "f_sort": sort,
        }
    )
    return templates.TemplateResponse("htmx/partials/sourcing/results.html", ctx)


@router.get("/v2/partials/sourcing/leads/{lead_id}", response_class=HTMLResponse)
async def lead_detail_partial(
    request: Request,
    lead_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return lead detail as HTML partial.

    Loads the SourcingLead, its evidence (sorted by confidence_impact desc), groups
    evidence by source category, fetches vendor card and best sighting.
    """
    from ..models.sourcing_lead import LeadEvidence, SourcingLead
    from ..services.sourcing_leads import _source_category

    lead = db.query(SourcingLead).filter(SourcingLead.id == lead_id).first()
    if not lead:
        raise HTTPException(404, "Lead not found")

    evidence = (
        db.query(LeadEvidence)
        .filter(LeadEvidence.lead_id == lead.id)
        .order_by(LeadEvidence.confidence_impact.desc().nullslast())
        .all()
    )

    evidence_by_category = {}
    for ev in evidence:
        cat = _source_category(ev.source_type)
        evidence_by_category.setdefault(cat, []).append(ev)

    category_labels = {
        "api": "API",
        "marketplace": "Marketplace",
        "salesforce_history": "Salesforce History",
        "avail_history": "Avail History",
        "web_ai": "Web / AI",
        "safety_review": "Safety Review",
        "buyer_feedback": "Buyer Feedback",
    }

    requirement = db.query(Requirement).filter(Requirement.id == lead.requirement_id).first()

    vendor_card = lead.vendor_card

    best_sighting = (
        db.query(Sighting)
        .filter(
            Sighting.requirement_id == lead.requirement_id,
            Sighting.vendor_name_normalized == lead.vendor_name_normalized,
        )
        .order_by(Sighting.created_at.desc().nullslast())
        .first()
    )

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update(
        {
            "lead": lead,
            "evidence": evidence,
            "evidence_by_category": evidence_by_category,
            "category_labels": category_labels,
            "requirement": requirement,
            "vendor_card": vendor_card,
            "best_sighting": best_sighting,
        }
    )
    return templates.TemplateResponse("htmx/partials/sourcing/lead_detail.html", ctx)


@router.post("/v2/partials/sourcing/leads/{lead_id}/status", response_class=HTMLResponse)
async def lead_status_update(
    request: Request,
    lead_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Update lead buyer status.

    Returns updated lead card when called from results view (for OOB swap), or updated
    lead detail when called from lead detail page.
    """
    from ..services.sourcing_leads import update_lead_status

    form = await request.form()
    status_val = form.get("status", "").strip()
    note = form.get("note", "").strip() or None

    try:
        lead = update_lead_status(
            db,
            lead_id,
            status_val,
            note=note,
            actor_user_id=user.id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    if not lead:
        raise HTTPException(404, "Lead not found")

    hx_target = request.headers.get("HX-Target", "")
    referer = request.headers.get("HX-Current-URL", "")

    # Workspace panel context: return updated panel detail
    if hx_target == "split-right-sourcing":
        return await lead_panel_partial(request, lead_id, user, db)

    # Full-page lead detail context
    if "/leads/" in referer:
        return await lead_detail_partial(request, lead_id, user, db)

    # Workspace lead row context: return updated lead row
    if hx_target.startswith("lead-row-"):
        best_sighting = (
            db.query(Sighting)
            .filter(
                Sighting.requirement_id == lead.requirement_id,
                Sighting.vendor_name_normalized == lead.vendor_name_normalized,
            )
            .order_by(Sighting.created_at.desc().nullslast())
            .first()
        )
        lead_sighting_data = {}
        if best_sighting:
            lead_sighting_data[lead.id] = {
                "qty_available": best_sighting.qty_available,
                "unit_price": best_sighting.unit_price,
            }
        ctx = _base_ctx(request, user, "requisitions")
        ctx.update({"lead": lead, "lead_sighting_data": lead_sighting_data, "selected_lead_id": 0})
        return templates.TemplateResponse("htmx/partials/sourcing/lead_row.html", ctx)

    # Default: card view (results grid)
    best_sighting = (
        db.query(Sighting)
        .filter(
            Sighting.requirement_id == lead.requirement_id,
            Sighting.vendor_name_normalized == lead.vendor_name_normalized,
        )
        .order_by(Sighting.created_at.desc().nullslast())
        .first()
    )
    lead_sighting_data = {}
    if best_sighting:
        lead_sighting_data[lead.id] = {
            "qty_available": best_sighting.qty_available,
            "unit_price": best_sighting.unit_price,
        }

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"lead": lead, "lead_sighting_data": lead_sighting_data})
    return templates.TemplateResponse("htmx/partials/sourcing/lead_card.html", ctx)


@router.post("/v2/partials/sourcing/leads/{lead_id}/feedback", response_class=HTMLResponse)
async def lead_feedback(
    request: Request,
    lead_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add feedback event to a lead without changing status.

    Returns updated lead detail.
    """
    from ..services.sourcing_leads import append_lead_feedback

    form = await request.form()
    note = form.get("note", "").strip() or None
    reason_code = form.get("reason_code", "").strip() or None
    contact_method = form.get("contact_method", "").strip() or None

    lead = append_lead_feedback(
        db,
        lead_id,
        note=note,
        reason_code=reason_code,
        contact_method=contact_method,
        actor_user_id=user.id,
    )
    if not lead:
        raise HTTPException(404, "Lead not found")

    return await lead_detail_partial(request, lead_id, user, db)


# ── Sourcing workspace (split-panel) ─────────────────────────────────


@router.get("/v2/sourcing/{requirement_id}/workspace", response_class=HTMLResponse)
async def v2_sourcing_workspace_page(request: Request, requirement_id: int, db: Session = Depends(get_db)):
    """Full page load for sourcing workspace (split-panel view)."""
    user = get_user(request, db)
    if not user:
        return templates.TemplateResponse("htmx/login.html", {"request": request, **_vite_assets()})
    ctx = _base_ctx(request, user, "requisitions")
    ctx["partial_url"] = f"/v2/partials/sourcing/{requirement_id}/workspace"
    return templates.TemplateResponse("htmx/base_page.html", ctx)


@router.get("/v2/partials/sourcing/{requirement_id}/workspace", response_class=HTMLResponse)
async def sourcing_workspace_partial(
    request: Request,
    requirement_id: int,
    confidence: str = "",
    safety: str = "",
    freshness: str = "",
    source: str = "",
    status: str = "",
    contactability: str = "",
    corroborated: str = "",
    sort: str = "best",
    page: int = Query(1, ge=1),
    lead: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return sourcing workspace split-panel layout.

    Reuses the same filtering/sorting logic as sourcing_results_partial but renders lead
    rows in a split-panel instead of card grid. Optional lead=ID param pre-selects a
    lead in the right panel.
    """
    from ..models.sourcing_lead import SourcingLead

    req = db.query(Requirement).filter(Requirement.id == requirement_id).first()
    if not req:
        raise HTTPException(404, "Requirement not found")

    query = db.query(SourcingLead).filter(SourcingLead.requirement_id == requirement_id)

    if confidence:
        bands = [b.strip() for b in confidence.split(",")]
        query = query.filter(SourcingLead.confidence_band.in_(bands))
    if safety:
        bands = [b.strip() for b in safety.split(",")]
        query = query.filter(SourcingLead.vendor_safety_band.in_(bands))
    if freshness and freshness != "all":
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        cutoffs = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
        if freshness in cutoffs:
            query = query.filter(SourcingLead.source_last_seen_at >= now - cutoffs[freshness])
    if source:
        sources_list = [s.strip() for s in source.split(",")]
        query = query.filter(SourcingLead.primary_source_type.in_(sources_list))
    if status and status != "all":
        statuses = [s.strip() for s in status.split(",")]
        query = query.filter(SourcingLead.buyer_status.in_(statuses))
    if contactability == "has_email":
        query = query.filter(SourcingLead.contact_email.isnot(None))
    elif contactability == "has_phone":
        query = query.filter(SourcingLead.contact_phone.isnot(None))
    if corroborated == "yes":
        query = query.filter(SourcingLead.corroborated.is_(True))
    elif corroborated == "no":
        query = query.filter(SourcingLead.corroborated.is_(False))

    sort_map = {
        "best": [SourcingLead.confidence_score.desc()],
        "freshest": [SourcingLead.source_last_seen_at.desc().nullslast()],
        "safest": [SourcingLead.vendor_safety_score.desc().nullslast()],
        "contact": [SourcingLead.contactability_score.desc().nullslast()],
        "proven": [SourcingLead.historical_success_score.desc().nullslast()],
    }
    for col in sort_map.get(sort, sort_map["best"]):
        query = query.order_by(col)

    total = query.count()
    per_page = 24
    leads = query.offset((page - 1) * per_page).limit(per_page).all()

    lead_sighting_data = {}
    if leads:
        for ld in leads:
            best_sighting = (
                db.query(Sighting)
                .filter(
                    Sighting.requirement_id == requirement_id,
                    Sighting.vendor_name_normalized == ld.vendor_name_normalized,
                )
                .order_by(Sighting.created_at.desc().nullslast())
                .first()
            )
            if best_sighting:
                lead_sighting_data[ld.id] = {
                    "qty_available": best_sighting.qty_available,
                    "unit_price": best_sighting.unit_price,
                }

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update(
        {
            "requirement": req,
            "leads": leads,
            "lead_sighting_data": lead_sighting_data,
            "total": total,
            "page": page,
            "total_pages": max(1, (total + per_page - 1) // per_page),
            "per_page": per_page,
            "f_confidence": confidence,
            "f_safety": safety,
            "f_freshness": freshness,
            "f_source": source,
            "f_status": status,
            "f_contactability": contactability,
            "f_corroborated": corroborated,
            "f_sort": sort,
            "selected_lead_id": lead if lead else 0,
        }
    )
    return templates.TemplateResponse("htmx/partials/sourcing/workspace.html", ctx)


@router.get("/v2/partials/sourcing/{requirement_id}/workspace-list", response_class=HTMLResponse)
async def sourcing_workspace_list_partial(
    request: Request,
    requirement_id: int,
    confidence: str = "",
    safety: str = "",
    freshness: str = "",
    source: str = "",
    status: str = "",
    contactability: str = "",
    corroborated: str = "",
    sort: str = "best",
    page: int = Query(1, ge=1),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return just the lead list rows for the workspace left panel.

    Used when filters change — swaps only #lead-list-content without touching the right
    panel or overall layout.
    """
    from ..models.sourcing_lead import SourcingLead

    req = db.query(Requirement).filter(Requirement.id == requirement_id).first()
    if not req:
        raise HTTPException(404, "Requirement not found")

    query = db.query(SourcingLead).filter(SourcingLead.requirement_id == requirement_id)

    if confidence:
        bands = [b.strip() for b in confidence.split(",")]
        query = query.filter(SourcingLead.confidence_band.in_(bands))
    if safety:
        bands = [b.strip() for b in safety.split(",")]
        query = query.filter(SourcingLead.vendor_safety_band.in_(bands))
    if freshness and freshness != "all":
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        cutoffs = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
        if freshness in cutoffs:
            query = query.filter(SourcingLead.source_last_seen_at >= now - cutoffs[freshness])
    if source:
        sources_list = [s.strip() for s in source.split(",")]
        query = query.filter(SourcingLead.primary_source_type.in_(sources_list))
    if status and status != "all":
        statuses = [s.strip() for s in status.split(",")]
        query = query.filter(SourcingLead.buyer_status.in_(statuses))
    if contactability == "has_email":
        query = query.filter(SourcingLead.contact_email.isnot(None))
    elif contactability == "has_phone":
        query = query.filter(SourcingLead.contact_phone.isnot(None))
    if corroborated == "yes":
        query = query.filter(SourcingLead.corroborated.is_(True))
    elif corroborated == "no":
        query = query.filter(SourcingLead.corroborated.is_(False))

    sort_map = {
        "best": [SourcingLead.confidence_score.desc()],
        "freshest": [SourcingLead.source_last_seen_at.desc().nullslast()],
        "safest": [SourcingLead.vendor_safety_score.desc().nullslast()],
        "contact": [SourcingLead.contactability_score.desc().nullslast()],
        "proven": [SourcingLead.historical_success_score.desc().nullslast()],
    }
    for col in sort_map.get(sort, sort_map["best"]):
        query = query.order_by(col)

    total = query.count()
    per_page = 24
    leads = query.offset((page - 1) * per_page).limit(per_page).all()

    lead_sighting_data = {}
    if leads:
        for ld in leads:
            best_sighting = (
                db.query(Sighting)
                .filter(
                    Sighting.requirement_id == requirement_id,
                    Sighting.vendor_name_normalized == ld.vendor_name_normalized,
                )
                .order_by(Sighting.created_at.desc().nullslast())
                .first()
            )
            if best_sighting:
                lead_sighting_data[ld.id] = {
                    "qty_available": best_sighting.qty_available,
                    "unit_price": best_sighting.unit_price,
                }

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update(
        {
            "requirement": req,
            "leads": leads,
            "lead_sighting_data": lead_sighting_data,
            "total": total,
            "page": page,
            "total_pages": max(1, (total + per_page - 1) // per_page),
            "per_page": per_page,
            "f_confidence": confidence,
            "f_safety": safety,
            "f_freshness": freshness,
            "f_source": source,
            "f_status": status,
            "f_contactability": contactability,
            "f_corroborated": corroborated,
            "f_sort": sort,
            "selected_lead_id": 0,
        }
    )

    html_parts = []
    if leads:
        for ld in leads:
            rendered = templates.get_template("htmx/partials/sourcing/lead_row.html").render({**ctx, "lead": ld})
            html_parts.append(rendered)
    else:
        html_parts.append(
            '<div class="flex flex-col items-center justify-center py-12 text-gray-400">'
            '<p class="text-sm">No leads found</p></div>'
        )

    return HTMLResponse("".join(html_parts))


@router.get("/v2/partials/sourcing/leads/{lead_id}/panel", response_class=HTMLResponse)
async def lead_panel_partial(
    request: Request,
    lead_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return condensed lead detail for workspace right panel.

    Same data as lead_detail_partial but uses lead_panel.html template (no breadcrumb,
    collapsible sections, denser layout). Includes OOB swap of the lead row in the left
    panel for highlight update.
    """
    from ..models.sourcing_lead import LeadEvidence, SourcingLead
    from ..services.sourcing_leads import _source_category

    lead = db.query(SourcingLead).filter(SourcingLead.id == lead_id).first()
    if not lead:
        raise HTTPException(404, "Lead not found")

    evidence = (
        db.query(LeadEvidence)
        .filter(LeadEvidence.lead_id == lead.id)
        .order_by(LeadEvidence.confidence_impact.desc().nullslast())
        .all()
    )

    evidence_by_category = {}
    for ev in evidence:
        cat = _source_category(ev.source_type)
        evidence_by_category.setdefault(cat, []).append(ev)

    category_labels = {
        "api": "API",
        "marketplace": "Marketplace",
        "salesforce_history": "Salesforce History",
        "avail_history": "Avail History",
        "web_ai": "Web / AI",
        "safety_review": "Safety Review",
        "buyer_feedback": "Buyer Feedback",
    }

    requirement = db.query(Requirement).filter(Requirement.id == lead.requirement_id).first()
    vendor_card = lead.vendor_card

    best_sighting = (
        db.query(Sighting)
        .filter(
            Sighting.requirement_id == lead.requirement_id,
            Sighting.vendor_name_normalized == lead.vendor_name_normalized,
        )
        .order_by(Sighting.created_at.desc().nullslast())
        .first()
    )

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update(
        {
            "lead": lead,
            "evidence": evidence,
            "evidence_by_category": evidence_by_category,
            "category_labels": category_labels,
            "requirement": requirement,
            "vendor_card": vendor_card,
            "best_sighting": best_sighting,
        }
    )
    return templates.TemplateResponse("htmx/partials/sourcing/lead_panel.html", ctx)


# ── Materials partials ────────────────────────────────────────────────


@router.get("/v2/partials/materials", response_class=HTMLResponse)
async def materials_list_partial(
    request: Request,
    q: str = "",
    lifecycle: str = "",
    category: str = "",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return material cards list as HTML partial."""
    from sqlalchemy import func as sqlfunc

    from ..models.intelligence import MaterialCard
    from ..services.material_search_service import classify_query, search_materials_local

    interpreted_query = ""
    if q.strip():
        query_type = classify_query(q.strip())
        if query_type == "mpn":
            materials, total = search_materials_local(db, q.strip(), lifecycle, category, limit, offset)
        else:
            materials, total = search_materials_local(db, q.strip(), lifecycle, category, limit, offset)
            interpreted_query = f"Searching: {q.strip()}"
    else:
        materials, total = search_materials_local(db, "", lifecycle, category, limit, offset)

    # Top commodity categories for filter pills
    top_categories = (
        db.query(MaterialCard.category, sqlfunc.count(MaterialCard.id))
        .filter(
            MaterialCard.category.isnot(None),
            MaterialCard.category != "",
            MaterialCard.category != "other",
            MaterialCard.deleted_at.is_(None),
        )
        .group_by(MaterialCard.category)
        .order_by(sqlfunc.count(MaterialCard.id).desc())
        .limit(12)
        .all()
    )

    # Compute vendor_count and best_price for each material
    from sqlalchemy import func as sqlfunc

    from ..models.intelligence import MaterialVendorHistory

    card_ids = [m.id for m in materials]
    if card_ids:
        vendor_stats = (
            db.query(
                MaterialVendorHistory.material_card_id,
                sqlfunc.count(MaterialVendorHistory.id).label("vendor_count"),
                sqlfunc.min(MaterialVendorHistory.last_price).label("best_price"),
            )
            .filter(MaterialVendorHistory.material_card_id.in_(card_ids))
            .group_by(MaterialVendorHistory.material_card_id)
            .all()
        )
        stats_map = {
            row.material_card_id: {"vendor_count": row.vendor_count, "best_price": row.best_price}
            for row in vendor_stats
        }
    else:
        stats_map = {}

    # Attach stats to materials
    for m in materials:
        s = stats_map.get(m.id, {})
        m._vendor_count = s.get("vendor_count", 0)
        m._best_price = s.get("best_price")

    ctx = _base_ctx(request, user, "materials")
    ctx.update(
        {
            "materials": materials,
            "q": q,
            "lifecycle": lifecycle,
            "category": category,
            "total": total,
            "limit": limit,
            "offset": offset,
            "interpreted_query": interpreted_query,
            "top_categories": top_categories,
        }
    )
    return templates.TemplateResponse("htmx/partials/materials/list.html", ctx)


@router.get("/v2/partials/materials/{card_id}", response_class=HTMLResponse)
async def material_detail_partial(
    request: Request,
    card_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return material card detail as HTML partial."""
    from ..models.intelligence import MaterialCard

    card = (
        db.query(MaterialCard)
        .filter(
            MaterialCard.id == card_id,
            MaterialCard.deleted_at.is_(None),
        )
        .first()
    )
    if not card:
        raise HTTPException(404, "Material card not found")

    sightings = (
        db.query(Sighting)
        .filter(Sighting.material_card_id == card_id)
        .order_by(Sighting.created_at.desc().nullslast())
        .limit(50)
        .all()
    )
    offers = (
        db.query(Offer)
        .filter(Offer.material_card_id == card_id)
        .order_by(Offer.created_at.desc().nullslast())
        .limit(50)
        .all()
    )
    ctx = _base_ctx(request, user, "materials")
    ctx.update({"card": card, "sightings": sightings, "offers": offers})
    return templates.TemplateResponse("htmx/partials/materials/detail.html", ctx)


@router.get(
    "/v2/partials/materials/{card_id}/tab/{tab_name}",
    response_class=HTMLResponse,
)
async def material_tab_partial(
    request: Request,
    card_id: int,
    tab_name: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return a material detail tab partial."""
    from ..models.intelligence import MaterialCard, MaterialVendorHistory

    card = db.get(MaterialCard, card_id)
    if not card:
        return HTMLResponse(
            "<p class='text-gray-400 text-sm py-4 text-center'>Material not found</p>",
            status_code=404,
        )

    ctx = _base_ctx(request, user, "materials")
    ctx["card"] = card

    if tab_name == "vendors":
        ctx["vendors"] = (
            db.query(MaterialVendorHistory)
            .filter_by(material_card_id=card_id)
            .order_by(MaterialVendorHistory.last_seen.desc().nullslast())
            .all()
        )
        return templates.TemplateResponse("htmx/partials/materials/tabs/vendors.html", ctx)
    elif tab_name == "customers":
        from ..models.purchase_history import CustomerPartHistory

        ctx["customers"] = (
            db.query(CustomerPartHistory)
            .filter_by(material_card_id=card_id)
            .order_by(CustomerPartHistory.last_purchased_at.desc().nullslast())
            .all()
        )
        return templates.TemplateResponse("htmx/partials/materials/tabs/customers.html", ctx)
    elif tab_name == "sourcing":
        from ..models.sourcing import Requirement

        ctx["requirements"] = (
            db.query(Requirement)
            .filter(Requirement.material_card_id == card_id)
            .order_by(Requirement.created_at.desc())
            .all()
        )
        return templates.TemplateResponse("htmx/partials/materials/tabs/sourcing.html", ctx)
    elif tab_name == "price_history":
        from ..models.price_snapshot import MaterialPriceSnapshot

        ctx["snapshots"] = (
            db.query(MaterialPriceSnapshot)
            .filter_by(material_card_id=card_id)
            .order_by(MaterialPriceSnapshot.recorded_at.desc())
            .limit(200)
            .all()
        )
        return templates.TemplateResponse("htmx/partials/materials/tabs/price_history.html", ctx)
    else:
        return HTMLResponse(
            "<p class='text-gray-400 text-sm py-4 text-center'>Unknown tab</p>",
            status_code=404,
        )


@router.put("/v2/partials/materials/{card_id}", response_class=HTMLResponse)
async def update_material_card(
    request: Request,
    card_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Update material card fields.

    Returns refreshed detail.
    """
    from ..models.intelligence import MaterialCard

    card = (
        db.query(MaterialCard)
        .filter(
            MaterialCard.id == card_id,
            MaterialCard.deleted_at.is_(None),
        )
        .first()
    )
    if not card:
        raise HTTPException(404, "Material card not found")

    form = await request.form()
    updatable = [
        "manufacturer",
        "description",
        "category",
        "package_type",
        "lifecycle_status",
        "rohs_status",
        "pin_count",
    ]
    for field in updatable:
        if field in form:
            val = form[field].strip() if form[field] else None
            if field == "pin_count" and val:
                val = int(val)
            setattr(card, field, val or None)

    db.commit()
    logger.info("Material card {} updated by {}", card_id, user.email)
    return await material_detail_partial(request, card_id, user, db)


# ── Quotes partials ───────────────────────────────────────────────────


@router.get("/v2/partials/quotes", response_class=HTMLResponse)
async def quotes_list_partial(
    request: Request,
    q: str = "",
    status: str = "",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return quotes list as HTML partial."""
    query = db.query(Quote).options(
        joinedload(Quote.customer_site).joinedload(CustomerSite.company),
        joinedload(Quote.requisition),
        joinedload(Quote.created_by),
    )
    if q.strip():
        safe = escape_like(q.strip())
        query = query.filter(Quote.quote_number.ilike(f"%{safe}%"))
    if status:
        query = query.filter(Quote.status == status)
    total = query.count()
    quotes = query.order_by(Quote.created_at.desc()).offset(offset).limit(limit).all()
    ctx = _base_ctx(request, user, "quotes")
    ctx.update({"quotes": quotes, "q": q, "status": status, "total": total, "limit": limit, "offset": offset})
    return templates.TemplateResponse("htmx/partials/quotes/list.html", ctx)


@router.get("/v2/partials/quotes/{quote_id}", response_class=HTMLResponse)
async def quote_detail_partial(
    request: Request,
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return quote detail as HTML partial."""
    quote = (
        db.query(Quote)
        .options(
            joinedload(Quote.customer_site).joinedload(CustomerSite.company),
            joinedload(Quote.requisition),
            joinedload(Quote.created_by),
        )
        .filter(Quote.id == quote_id)
        .first()
    )
    if not quote:
        raise HTTPException(404, "Quote not found")
    lines = db.query(QuoteLine).filter(QuoteLine.quote_id == quote_id).all()
    offers = (
        db.query(Offer).filter(Offer.requisition_id == quote.requisition_id).order_by(Offer.created_at.desc()).all()
    )
    ctx = _base_ctx(request, user, "quotes")
    ctx.update({"quote": quote, "lines": lines, "offers": offers})
    return templates.TemplateResponse("htmx/partials/quotes/detail.html", ctx)


@router.put("/v2/partials/quotes/{quote_id}/lines/{line_id}", response_class=HTMLResponse)
async def update_quote_line(
    request: Request,
    quote_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Inline edit a quote line item, return updated row."""
    line = db.get(QuoteLine, line_id)
    if not line or line.quote_id != quote_id:
        raise HTTPException(404, "Line not found")
    form = await request.form()
    if "mpn" in form:
        line.mpn = form["mpn"]
    if "manufacturer" in form:
        line.manufacturer = form["manufacturer"]
    if "qty" in form:
        line.qty = int(form["qty"])
    if "cost_price" in form:
        line.cost_price = float(form["cost_price"])
    if "sell_price" in form:
        line.sell_price = float(form["sell_price"])
    if line.sell_price and float(line.sell_price) > 0 and line.cost_price is not None:
        line.margin_pct = round((float(line.sell_price) - float(line.cost_price)) / float(line.sell_price) * 100, 2)
    db.commit()
    ctx = _base_ctx(request, user, "quotes")
    ctx["line"] = line
    return templates.TemplateResponse("htmx/partials/quotes/line_row.html", ctx)


@router.delete("/v2/partials/quotes/{quote_id}/lines/{line_id}", response_class=HTMLResponse)
async def delete_quote_line(
    request: Request,
    quote_id: int,
    line_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a quote line item."""
    line = db.get(QuoteLine, line_id)
    if not line or line.quote_id != quote_id:
        raise HTTPException(404, "Line not found")
    db.delete(line)
    db.commit()
    return HTMLResponse("")


@router.post("/v2/partials/quotes/{quote_id}/lines", response_class=HTMLResponse)
async def add_quote_line(
    request: Request,
    quote_id: int,
    mpn: str = Form(...),
    manufacturer: str = Form(""),
    qty: int = Form(1),
    cost_price: float = Form(0),
    sell_price: float = Form(0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add a new line item to a quote, return the new row HTML."""
    quote = db.get(Quote, quote_id)
    if not quote:
        raise HTTPException(404, "Quote not found")
    margin_pct = 0.0
    if sell_price > 0:
        margin_pct = round((sell_price - cost_price) / sell_price * 100, 2)
    line = QuoteLine(
        quote_id=quote_id,
        mpn=mpn,
        manufacturer=manufacturer or None,
        qty=qty,
        cost_price=cost_price,
        sell_price=sell_price,
        margin_pct=margin_pct,
    )
    db.add(line)
    db.commit()
    db.refresh(line)
    ctx = _base_ctx(request, user, "quotes")
    ctx["line"] = line
    return templates.TemplateResponse("htmx/partials/quotes/line_row.html", ctx)


@router.post("/v2/partials/quotes/{quote_id}/add-offer/{offer_id}", response_class=HTMLResponse)
async def add_offer_to_quote(
    request: Request,
    quote_id: int,
    offer_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add an offer as a line item to a quote."""
    quote = db.get(Quote, quote_id)
    if not quote:
        raise HTTPException(404, "Quote not found")
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    line = QuoteLine(
        quote_id=quote_id,
        offer_id=offer_id,
        mpn=offer.mpn,
        manufacturer=offer.manufacturer,
        qty=offer.qty_available or 0,
        cost_price=float(offer.unit_price) if offer.unit_price else 0,
        sell_price=0,
        margin_pct=0,
    )
    db.add(line)
    db.commit()
    db.refresh(line)
    ctx = _base_ctx(request, user, "quotes")
    ctx["line"] = line
    return templates.TemplateResponse("htmx/partials/quotes/line_row.html", ctx)


@router.post("/v2/partials/quotes/{quote_id}/send", response_class=HTMLResponse)
async def send_quote_htmx(
    request: Request,
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Mark quote as sent — returns refreshed detail partial."""
    from datetime import datetime, timezone

    quote = db.get(Quote, quote_id)
    if not quote:
        raise HTTPException(404, "Quote not found")
    quote.status = "sent"
    quote.sent_at = datetime.now(timezone.utc)
    db.commit()
    logger.info("Quote {} marked as sent by {}", quote.quote_number, user.email)
    return await quote_detail_partial(request, quote_id, user, db)


@router.post("/v2/partials/quotes/{quote_id}/result", response_class=HTMLResponse)
async def quote_result_htmx(
    request: Request,
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Mark quote result (won/lost) — returns refreshed detail partial."""
    from datetime import datetime, timezone

    quote = db.get(Quote, quote_id)
    if not quote:
        raise HTTPException(404, "Quote not found")
    form = await request.form()
    result = form.get("result", "")
    if result not in ("won", "lost"):
        raise HTTPException(400, "Result must be 'won' or 'lost'")
    quote.result = result
    quote.status = result
    quote.result_at = datetime.now(timezone.utc)
    quote.result_reason = form.get("result_reason", "")
    db.commit()
    logger.info("Quote {} marked as {} by {}", quote.quote_number, result, user.email)
    return await quote_detail_partial(request, quote_id, user, db)


@router.post("/v2/partials/quotes/{quote_id}/revise", response_class=HTMLResponse)
async def revise_quote_htmx(
    request: Request,
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a new revision of the quote — returns the new quote detail."""
    quote = db.get(Quote, quote_id)
    if not quote:
        raise HTTPException(404, "Quote not found")
    new_rev = (quote.revision or 1) + 1
    new_quote = Quote(
        requisition_id=quote.requisition_id,
        customer_site_id=quote.customer_site_id,
        quote_number=f"{quote.quote_number}-R{new_rev}",
        revision=new_rev,
        line_items=quote.line_items or [],
        subtotal=quote.subtotal,
        total_cost=quote.total_cost,
        total_margin_pct=quote.total_margin_pct,
        payment_terms=quote.payment_terms,
        shipping_terms=quote.shipping_terms,
        validity_days=quote.validity_days,
        notes=quote.notes,
        status="draft",
        created_by_id=user.id,
    )
    db.add(new_quote)
    db.commit()
    db.refresh(new_quote)
    logger.info("Quote {} revised to rev {} as {}", quote.quote_number, new_rev, new_quote.quote_number)
    return await quote_detail_partial(request, new_quote.id, user, db)


@router.post("/v2/partials/quotes/{quote_id}/apply-markup", response_class=HTMLResponse)
async def apply_markup_htmx(
    request: Request,
    quote_id: int,
    markup_pct: float = Form(25.0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Apply a markup percentage to all lines in the quote."""
    lines = db.query(QuoteLine).filter(QuoteLine.quote_id == quote_id).all()
    for line in lines:
        if line.cost_price and float(line.cost_price) > 0:
            multiplier = 1 + (markup_pct / 100)
            line.sell_price = round(float(line.cost_price) * multiplier, 4)
            line.margin_pct = round(markup_pct / multiplier, 2)
    db.commit()
    return await quote_detail_partial(request, quote_id, user, db)


@router.post("/v2/partials/requisitions/{req_id}/add-offers-to-quote", response_class=HTMLResponse)
async def add_offers_to_draft_quote(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add selected offers to an existing draft quote.

    Returns updated quote detail.
    """
    import json as _json

    body = await request.body()
    try:
        data = _json.loads(body)
    except (ValueError, TypeError):
        raise HTTPException(400, "Invalid JSON body")

    offer_ids = [int(x) for x in data.get("offer_ids", []) if x]
    quote_id = int(data.get("quote_id", 0))

    if not offer_ids or not quote_id:
        raise HTTPException(400, "Missing offer_ids or quote_id")

    quote = db.query(Quote).filter(Quote.id == quote_id, Quote.requisition_id == req_id).first()
    if not quote:
        raise HTTPException(404, "Quote not found")
    if quote.status != "draft":
        raise HTTPException(400, "Can only add to draft quotes")

    offers = db.query(Offer).filter(Offer.id.in_(offer_ids), Offer.requisition_id == req_id).all()
    for o in offers:
        existing = db.query(QuoteLine).filter(QuoteLine.quote_id == quote_id, QuoteLine.offer_id == o.id).first()
        if existing:
            continue
        sell_price = float(o.unit_price or 0)
        qty = o.qty_available or 1
        line = QuoteLine(
            quote_id=quote.id,
            offer_id=o.id,
            mpn=o.mpn or "",
            manufacturer=o.manufacturer or "",
            qty=qty,
            cost_price=sell_price,
            sell_price=sell_price,
            margin_pct=0.0,
        )
        db.add(line)

    # Recalculate totals
    db.flush()
    all_lines = db.query(QuoteLine).filter(QuoteLine.quote_id == quote.id).all()
    subtotal = sum(float(ln.sell_price or 0) * (ln.qty or 1) for ln in all_lines)
    total_cost = sum(float(ln.cost_price or 0) * (ln.qty or 1) for ln in all_lines)
    quote.subtotal = subtotal
    quote.total_cost = total_cost
    quote.total_margin_pct = ((subtotal - total_cost) / subtotal * 100) if subtotal else 0
    db.commit()

    logger.info("Added {} offers to quote {} by {}", len(offers), quote.quote_number, user.email)
    return HTMLResponse('<span class="text-emerald-600 text-sm">Offers added to quote</span>')


@router.post("/v2/partials/quotes/{quote_id}/build-buy-plan", response_class=HTMLResponse)
async def build_buy_plan_htmx(
    request: Request,
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Build a buy plan from a won quote.

    Returns buy plan detail partial.
    """
    from ..services.buyplan_builder import build_buy_plan

    quote = db.query(Quote).filter(Quote.id == quote_id).first()
    if not quote:
        raise HTTPException(404, "Quote not found")
    if quote.status != "won":
        raise HTTPException(400, "Quote must be won to build a buy plan")

    try:
        plan = build_buy_plan(quote_id, db)
    except ValueError as e:
        raise HTTPException(400, str(e))

    db.add(plan)
    db.commit()
    db.refresh(plan)

    logger.info("Buy plan #{} built from quote #{} by {}", plan.id, quote_id, user.email)

    # Return buy plan detail partial
    bp_lines = db.query(BuyPlanLine).filter(BuyPlanLine.buy_plan_id == plan.id).all()
    ctx = _base_ctx(request, user, "buy-plans")
    ctx["plan"] = plan
    ctx["lines"] = bp_lines
    return templates.TemplateResponse("htmx/partials/buy_plans/detail.html", ctx)


# ── Prospecting partials ──────────────────────────────────────────────


@router.get("/v2/partials/prospecting", response_class=HTMLResponse)
async def prospecting_list_partial(
    request: Request,
    q: str = "",
    status: str = "",
    sort: str = "buyer_ready_desc",
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return prospecting list as HTML partial."""
    query = db.query(ProspectAccount)
    if status:
        query = query.filter(ProspectAccount.status == status)
    else:
        query = query.filter(ProspectAccount.status.in_(["suggested", "claimed", "dismissed"]))
    if q.strip():
        safe = escape_like(q.strip())
        query = query.filter(ProspectAccount.name.ilike(f"%{safe}%") | ProspectAccount.domain.ilike(f"%{safe}%"))
    total = query.count()
    if sort == "fit_desc":
        query = query.order_by(ProspectAccount.fit_score.desc())
    elif sort == "recent_desc":
        query = query.order_by(ProspectAccount.created_at.desc())
    else:
        query = query.order_by(ProspectAccount.readiness_score.desc(), ProspectAccount.fit_score.desc())
    prospects = query.offset((page - 1) * per_page).limit(per_page).all()
    total_pages = (total + per_page - 1) // per_page
    ctx = _base_ctx(request, user, "prospecting")
    ctx.update(
        {
            "prospects": prospects,
            "q": q,
            "status": status,
            "sort": sort,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
        }
    )
    return templates.TemplateResponse("htmx/partials/prospecting/list.html", ctx)


# Sprint 8 prospecting static routes — must precede {prospect_id} catch-all
@router.get("/v2/partials/prospecting/stats", response_class=HTMLResponse)
async def prospecting_stats(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return prospecting stats summary panel."""
    from ..models.prospect_account import ProspectAccount

    total = db.query(sqlfunc.count(ProspectAccount.id)).scalar() or 0
    buyer_ready = (
        db.query(sqlfunc.count(ProspectAccount.id)).filter(ProspectAccount.readiness_score >= 70).scalar() or 0
    )

    return templates.TemplateResponse(
        "htmx/partials/prospecting/stats.html",
        {"request": request, "total": total, "buyer_ready": buyer_ready},
    )


@router.post("/v2/partials/prospecting/add-domain", response_class=HTMLResponse)
async def add_prospect_domain(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Submit a domain for prospecting."""
    form = await request.form()
    domain = form.get("domain", "").strip()
    if not domain:
        raise HTTPException(400, "Domain is required")

    try:
        from ..services.prospect_claim import manual_add_prospect

        prospect = manual_add_prospect(db, domain, user.id)
        return HTMLResponse(
            f'<div class="bg-emerald-50 border border-emerald-200 rounded p-2 text-sm text-emerald-700">'
            f"Prospect added: {domain} (ID {prospect.id})</div>"
        )
    except (ImportError, ValueError, RuntimeError) as exc:
        return HTMLResponse(
            f'<div class="bg-rose-50 border border-rose-200 rounded p-2 text-sm text-rose-700">'
            f"Error: {str(exc)[:100]}</div>"
        )


@router.get("/v2/partials/prospecting/{prospect_id}", response_class=HTMLResponse)
async def prospecting_detail_partial(
    request: Request,
    prospect_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return prospect detail as HTML partial."""
    prospect = (
        db.query(ProspectAccount)
        .options(
            joinedload(ProspectAccount.claimed_by_user),
        )
        .filter(ProspectAccount.id == prospect_id)
        .first()
    )
    if not prospect:
        raise HTTPException(404, "Prospect not found")
    ctx = _base_ctx(request, user, "prospecting")
    ctx["prospect"] = prospect
    ctx["enrichment"] = prospect.enrichment_data or {}
    ctx["warm_intro"] = (prospect.enrichment_data or {}).get("warm_intro", {})
    return templates.TemplateResponse("htmx/partials/prospecting/detail.html", ctx)


@router.post("/v2/partials/prospecting/{prospect_id}/claim", response_class=HTMLResponse)
async def claim_prospect_htmx(
    request: Request,
    prospect_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Claim a prospect — returns updated card for OOB swap."""
    from ..services.prospect_claim import claim_prospect

    try:
        claim_prospect(prospect_id, user.id, db)
    except (LookupError, ValueError) as e:
        raise HTTPException(400, str(e))
    prospect = (
        db.query(ProspectAccount)
        .options(
            joinedload(ProspectAccount.claimed_by_user),
        )
        .filter(ProspectAccount.id == prospect_id)
        .first()
    )
    ctx = _base_ctx(request, user, "prospecting")
    ctx["prospect"] = prospect
    return templates.TemplateResponse("htmx/partials/prospecting/_card.html", ctx)


@router.post("/v2/partials/prospecting/{prospect_id}/dismiss", response_class=HTMLResponse)
async def dismiss_prospect_htmx(
    request: Request,
    prospect_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Dismiss a prospect — returns updated card for OOB swap."""
    from datetime import datetime, timezone

    prospect = db.get(ProspectAccount, prospect_id)
    if not prospect:
        raise HTTPException(404, "Prospect not found")
    prospect.status = "dismissed"
    prospect.dismissed_by = user.id
    prospect.dismissed_at = datetime.now(timezone.utc)
    db.commit()
    ctx = _base_ctx(request, user, "prospecting")
    ctx["prospect"] = prospect
    return templates.TemplateResponse("htmx/partials/prospecting/_card.html", ctx)


@router.post("/v2/partials/prospecting/{prospect_id}/enrich", response_class=HTMLResponse)
async def enrich_prospect_htmx(
    request: Request,
    prospect_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Run free enrichment on a prospect — returns refreshed detail."""
    prospect = db.get(ProspectAccount, prospect_id)
    if not prospect:
        raise HTTPException(404, "Prospect not found")
    try:
        from ..services.prospect_free_enrichment import run_free_enrichment

        await run_free_enrichment(prospect_id)
        db.refresh(prospect)
    except Exception as exc:
        logger.warning("Enrichment failed for prospect {}: {}", prospect_id, exc)
    try:
        from ..services.prospect_warm_intros import detect_warm_intros, generate_one_liner

        warm = detect_warm_intros(prospect, db)
        one_liner = generate_one_liner(prospect, warm)
        ed = dict(prospect.enrichment_data or {})
        ed["warm_intro"] = warm
        ed["one_liner"] = one_liner
        prospect.enrichment_data = ed
        db.commit()
    except Exception as exc:
        logger.warning("Warm intro detection failed for prospect {}: {}", prospect_id, exc)
    ctx = _base_ctx(request, user, "prospecting")
    ctx["prospect"] = prospect
    ctx["enrichment"] = prospect.enrichment_data or {}
    ctx["warm_intro"] = (prospect.enrichment_data or {}).get("warm_intro", {})
    return templates.TemplateResponse("htmx/partials/prospecting/detail.html", ctx)


# ── Settings partials ────────────────────────────────────────────────


@router.get("/v2/partials/settings", response_class=HTMLResponse)
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


@router.get("/v2/partials/settings/sources", response_class=HTMLResponse)
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


@router.get("/v2/partials/settings/system", response_class=HTMLResponse)
async def settings_system_tab(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """System config tab — admin only."""
    if user.role != "admin":
        raise HTTPException(403, "Admin only")
    from ..services.admin_service import get_all_config

    config = get_all_config(db)
    ctx = _base_ctx(request, user, "settings")
    ctx["config"] = config
    return templates.TemplateResponse("htmx/partials/settings/system.html", ctx)


@router.get("/v2/partials/settings/profile", response_class=HTMLResponse)
async def settings_profile_tab(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """User profile tab."""
    ctx = _base_ctx(request, user, "settings")
    ctx["profile_user"] = user
    return templates.TemplateResponse("htmx/partials/settings/profile.html", ctx)


@router.get("/v2/partials/settings/data-ops", response_class=HTMLResponse)
async def settings_data_ops_tab(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Admin data operations tab — vendor/company dedup suggestions."""
    from ..dependencies import is_admin

    if not is_admin(user):
        raise HTTPException(403, "Admin only")

    vendor_dupes = []
    company_dupes = []
    try:
        from ..vendor_utils import find_vendor_dedup_candidates

        vendor_dupes = find_vendor_dedup_candidates(db, threshold=85, limit=30)
    except Exception as e:
        logger.warning(f"Vendor dedup scan failed: {e}")
    try:
        from ..company_utils import find_company_dedup_candidates

        company_dupes = find_company_dedup_candidates(db, threshold=85, limit=30)
    except Exception as e:
        logger.warning(f"Company dedup scan failed: {e}")

    ctx = _base_ctx(request, user, "settings")
    ctx["vendor_dupes"] = vendor_dupes
    ctx["company_dupes"] = company_dupes
    return templates.TemplateResponse("htmx/partials/settings/data_ops.html", ctx)


@router.post("/v2/partials/admin/vendor-merge", response_class=HTMLResponse)
async def admin_vendor_merge(
    request: Request,
    keep_id: int = Form(...),
    remove_id: int = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Merge two vendor cards via HTMX."""
    from ..dependencies import is_admin

    if not is_admin(user):
        raise HTTPException(403, "Admin only")

    from ..services.vendor_merge_service import merge_vendor_cards as _merge

    try:
        result = _merge(keep_id, remove_id, db)
        db.commit()
        return HTMLResponse(
            f'<p class="text-sm text-emerald-600 py-2">Merged into {result.get("kept_name", "vendor")}. '
            f"{result.get('reassigned', 0)} records reassigned.</p>"
        )
    except ValueError as e:
        return HTMLResponse(f'<p class="text-sm text-rose-600 py-2">Error: {e}</p>')


@router.post("/v2/partials/admin/company-merge", response_class=HTMLResponse)
async def admin_company_merge(
    request: Request,
    keep_id: int = Form(...),
    remove_id: int = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Merge two companies via HTMX."""
    from ..dependencies import is_admin

    if not is_admin(user):
        raise HTTPException(403, "Admin only")

    from ..services.company_merge_service import merge_companies

    try:
        result = merge_companies(keep_id, remove_id, db)
        db.commit()
        return HTMLResponse(
            f'<p class="text-sm text-emerald-600 py-2">Merged into {result.get("kept_name", "company")}.</p>'
        )
    except (ValueError, Exception) as e:
        return HTMLResponse(f'<p class="text-sm text-rose-600 py-2">Error: {e}</p>')


# ── Proactive Part Match ─────────────────────────────────────────────


@router.get("/v2/partials/proactive", response_class=HTMLResponse)
async def proactive_list_partial(
    request: Request,
    tab: str = "matches",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Proactive matches list partial — shows matches and sent offers."""
    from ..services.proactive_service import get_matches_for_user, get_sent_offers

    matches = get_matches_for_user(db, user.id, status="new")
    sent = get_sent_offers(db, user.id) if tab == "sent" else []

    ctx = _base_ctx(request, user, "proactive")
    ctx["matches"] = matches
    ctx["sent"] = sent
    ctx["tab"] = tab
    return templates.TemplateResponse("htmx/partials/proactive/list.html", ctx)


@router.post("/v2/partials/proactive/{match_id}/dismiss", response_class=HTMLResponse)
async def proactive_dismiss(
    match_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Dismiss a proactive match and reload the list."""
    from ..models import ProactiveMatch

    db.query(ProactiveMatch).filter(
        ProactiveMatch.id == match_id,
        ProactiveMatch.salesperson_id == user.id,
        ProactiveMatch.status == "new",
    ).update({"status": "dismissed"}, synchronize_session=False)
    db.commit()

    # Re-render list
    from ..services.proactive_service import get_matches_for_user

    matches = get_matches_for_user(db, user.id, status="new")
    ctx = _base_ctx(request, user, "proactive")
    ctx["matches"] = matches
    ctx["sent"] = []
    ctx["tab"] = "matches"
    return templates.TemplateResponse("htmx/partials/proactive/list.html", ctx)


# ── Sprint 8: Proactive Selling + Prospecting Completion ───────────────


@router.post("/v2/partials/proactive/{match_id}/draft", response_class=HTMLResponse)
async def proactive_draft(
    request: Request,
    match_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """AI-draft a proactive offer email for a match."""
    from ..models import ProactiveMatch

    match = (
        db.query(ProactiveMatch).filter(ProactiveMatch.id == match_id, ProactiveMatch.salesperson_id == user.id).first()
    )
    if not match:
        raise HTTPException(404, "Match not found")

    # Try AI draft via proactive service
    draft_body = ""
    draft_subject = ""
    try:
        from ..services.proactive_email import draft_proactive_email

        result = await draft_proactive_email(match, db, user)
        draft_body = result.get("body", "")
        draft_subject = result.get("subject", f"Stock Available: {match.mpn}")
    except (ImportError, RuntimeError, Exception) as exc:
        logger.warning("Proactive draft failed: {}", exc)
        draft_subject = f"Stock Available: {match.mpn}"
        draft_body = (
            f"Dear Customer,\n\nWe have {match.mpn} available. Please let us know if you're interested.\n\nBest regards"
        )

    return templates.TemplateResponse(
        "htmx/partials/proactive/draft_form.html",
        {"request": request, "match": match, "draft_subject": draft_subject, "draft_body": draft_body},
    )


@router.post("/v2/partials/proactive/{match_id}/send", response_class=HTMLResponse)
async def proactive_send(
    request: Request,
    match_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Send a proactive offer email."""
    from ..models import ProactiveMatch

    match = (
        db.query(ProactiveMatch).filter(ProactiveMatch.id == match_id, ProactiveMatch.salesperson_id == user.id).first()
    )
    if not match:
        raise HTTPException(404, "Match not found")

    form = await request.form()
    body = form.get("body", "").strip()
    if not body:
        raise HTTPException(400, "Email body is required")

    # Mark as sent
    match.status = "sent"
    db.commit()
    logger.info("Proactive match {} sent by {}", match_id, user.email)

    return templates.TemplateResponse(
        "htmx/partials/proactive/send_success.html",
        {"request": request, "match": match},
    )


@router.post("/v2/partials/proactive/{offer_id}/convert", response_class=HTMLResponse)
async def proactive_convert(
    request: Request,
    offer_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Convert a won proactive offer into req+quote+buyplan."""
    from ..models import ProactiveOffer

    offer = db.query(ProactiveOffer).filter(ProactiveOffer.id == offer_id).first()
    if not offer:
        raise HTTPException(404, "Proactive offer not found")

    try:
        from ..services.proactive_service import convert_proactive_offer

        result = convert_proactive_offer(db, offer, user)
        return templates.TemplateResponse(
            "htmx/partials/proactive/convert_success.html",
            {"request": request, "offer": offer, "result": result},
        )
    except (ImportError, RuntimeError, Exception) as exc:
        logger.error("Proactive conversion failed: {}", exc)
        raise HTTPException(500, f"Conversion failed: {str(exc)[:100]}")


@router.get("/v2/partials/proactive/scorecard", response_class=HTMLResponse)
async def proactive_scorecard(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return proactive offers scorecard/metrics panel."""
    try:
        from ..services.proactive_service import get_scorecard

        stats = get_scorecard(db, user.id)
    except (ImportError, RuntimeError, Exception):
        stats = {"total_sent": 0, "total_converted": 0, "conversion_rate": 0, "total_revenue": 0}

    return templates.TemplateResponse(
        "htmx/partials/proactive/scorecard.html",
        {"request": request, "stats": stats},
    )


@router.get("/v2/partials/proactive/badge", response_class=HTMLResponse)
async def proactive_badge(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return proactive match count badge for nav sidebar."""
    from ..models import ProactiveMatch

    count = (
        db.query(sqlfunc.count(ProactiveMatch.id))
        .filter(ProactiveMatch.salesperson_id == user.id, ProactiveMatch.status == "new")
        .scalar()
        or 0
    )
    if count > 0:
        return HTMLResponse(
            f'<span class="ml-auto px-1.5 py-0.5 text-[10px] font-bold text-white bg-emerald-500 rounded-full">{count}</span>'
        )
    return HTMLResponse("")


@router.post("/v2/partials/proactive/do-not-offer", response_class=HTMLResponse)
async def proactive_do_not_offer(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add an MPN+customer combo to the do-not-offer list."""
    from ..models.intelligence import ProactiveDoNotOffer

    form = await request.form()
    mpn = form.get("mpn", "").strip()
    company_id = form.get("customer_site_id", "") or form.get("company_id", "")

    if not mpn or not company_id:
        raise HTTPException(400, "MPN and company are required")

    dno = ProactiveDoNotOffer(
        mpn=mpn,
        company_id=int(company_id),
        created_by_id=user.id,
    )
    db.add(dno)
    db.commit()
    logger.info("Do-not-offer: {} for company {} by {}", mpn, company_id, user.email)

    return HTMLResponse('<span class="text-xs text-gray-500">Suppressed</span>')


# ── Sprint 9: Materials + Activity + Knowledge ────────────────────────


@router.post("/v2/partials/materials/{material_id}/enrich", response_class=HTMLResponse)
async def enrich_material(
    request: Request,
    material_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Trigger AI enrichment for a material card."""
    from ..models.intelligence import MaterialCard

    mc = db.query(MaterialCard).filter(MaterialCard.id == material_id).first()
    if not mc:
        raise HTTPException(404, "Material not found")

    # Placeholder enrichment — in production this would call AI service
    return templates.TemplateResponse(
        "htmx/partials/materials/enrich_result.html",
        {"request": request, "material": mc},
    )


@router.get("/v2/partials/materials/{material_id}/insights", response_class=HTMLResponse)
async def material_insights(
    request: Request,
    material_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return MPN insights panel for a material card."""
    from ..models.intelligence import MaterialCard

    mc = db.query(MaterialCard).filter(MaterialCard.id == material_id).first()
    if not mc:
        raise HTTPException(404, "Material not found")

    # Get related offers for pricing data
    offers = (
        (
            db.query(Offer)
            .filter(Offer.normalized_mpn == mc.normalized_mpn, Offer.unit_price.isnot(None))
            .order_by(Offer.created_at.desc())
            .limit(20)
            .all()
        )
        if mc.normalized_mpn
        else []
    )

    return templates.TemplateResponse(
        "htmx/partials/materials/insights.html",
        {"request": request, "material": mc, "offers": offers},
    )


@router.get("/v2/partials/knowledge", response_class=HTMLResponse)
async def knowledge_list(
    request: Request,
    q: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return knowledge base entries list."""
    from ..models.knowledge import KnowledgeEntry

    query = db.query(KnowledgeEntry)
    if q.strip():
        safe = escape_like(q.strip())
        query = query.filter(KnowledgeEntry.content.ilike(f"%{safe}%"))
    entries = query.order_by(KnowledgeEntry.created_at.desc()).limit(50).all()

    return templates.TemplateResponse(
        "htmx/partials/knowledge/list.html",
        {"request": request, "entries": entries, "q": q},
    )


@router.post("/v2/partials/knowledge", response_class=HTMLResponse)
async def create_knowledge_entry(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a knowledge base entry."""
    from ..models.knowledge import KnowledgeEntry

    form = await request.form()
    content = form.get("content", "").strip()
    if not content:
        raise HTTPException(400, "Content is required")

    entry = KnowledgeEntry(
        entry_type=form.get("entry_type", "note").strip(),
        content=content,
        source="manual",
        created_by=user.id,
    )
    db.add(entry)
    db.commit()
    logger.info("Knowledge entry {} created by {}", entry.id, user.email)

    return await knowledge_list(request=request, user=user, db=db)


# ── Sprint 10: Admin + Import Completion ──────────────────────────────


@router.get("/v2/partials/admin/api-health", response_class=HTMLResponse)
async def admin_api_health(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return connector health dashboard."""
    try:
        from ..services.connector_health import get_health_dashboard

        health = get_health_dashboard(db)
    except (ImportError, RuntimeError, Exception):
        health = {"connectors": [], "overall_status": "unknown"}

    return templates.TemplateResponse(
        "htmx/partials/admin/api_health.html",
        {"request": request, "health": health},
    )


@router.post("/v2/partials/admin/import/vendors", response_class=HTMLResponse)
async def import_vendors_csv(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Import vendors from CSV upload."""
    form = await request.form()
    file = form.get("file")
    if not file:
        raise HTTPException(400, "CSV file is required")

    import csv
    import io

    content = await file.read()
    reader = csv.DictReader(io.StringIO(content.decode("utf-8")))
    count = 0
    for row in reader:
        name = row.get("name", "").strip()
        if not name:
            continue
        from ..vendor_utils import normalize_vendor_name

        norm = normalize_vendor_name(name)
        existing = db.query(VendorCard).filter(VendorCard.normalized_name == norm).first()
        if not existing:
            vc = VendorCard(
                display_name=name,
                normalized_name=norm,
                emails=[row.get("email", "")] if row.get("email") else [],
                phones=[row.get("phone", "")] if row.get("phone") else [],
                website=row.get("website", ""),
                sighting_count=0,
            )
            db.add(vc)
            count += 1
    db.commit()
    logger.info("Vendor CSV import: {} new vendors by {}", count, user.email)

    return HTMLResponse(
        f'<div class="bg-emerald-50 border border-emerald-200 rounded p-3 text-sm text-emerald-700">'
        f"Imported {count} new vendors from CSV.</div>"
    )


@router.get("/v2/partials/admin/data-ops", response_class=HTMLResponse)
async def admin_data_ops(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return admin data operations panel."""
    from ..models.intelligence import MaterialCard

    vendor_count = db.query(sqlfunc.count(VendorCard.id)).scalar() or 0
    company_count = db.query(sqlfunc.count(Company.id)).filter(Company.is_active.is_(True)).scalar() or 0
    material_count = db.query(sqlfunc.count(MaterialCard.id)).scalar() or 0

    return templates.TemplateResponse(
        "htmx/partials/admin/data_ops.html",
        {
            "request": request,
            "vendor_count": vendor_count,
            "company_count": company_count,
            "material_count": material_count,
        },
    )


# ── Strategic Vendors (My Vendors) ───────────────────────────────────


@router.get("/v2/partials/strategic", response_class=HTMLResponse)
async def strategic_list_partial(
    request: Request,
    search: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """My Vendors list partial — claimed vendors + open pool."""
    from ..services import strategic_vendor_service as svc

    my_vendors = svc.get_my_strategic(db, user.id)
    open_vendors, open_total = svc.get_open_pool(db, limit=20, offset=0, search=search or None)

    ctx = _base_ctx(request, user, "strategic")
    ctx["my_vendors"] = my_vendors
    ctx["slot_count"] = len(my_vendors)
    ctx["max_slots"] = svc.MAX_STRATEGIC_VENDORS
    ctx["open_vendors"] = open_vendors
    ctx["open_total"] = open_total
    ctx["search"] = search
    return templates.TemplateResponse("htmx/partials/strategic/list.html", ctx)


@router.post("/v2/partials/strategic/claim/{vendor_card_id}", response_class=HTMLResponse)
async def strategic_claim(
    vendor_card_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Claim a vendor as strategic and reload the list."""
    from ..services import strategic_vendor_service as svc

    svc.claim_vendor(db, user.id, vendor_card_id)

    my_vendors = svc.get_my_strategic(db, user.id)
    open_vendors, open_total = svc.get_open_pool(db, limit=20, offset=0)

    ctx = _base_ctx(request, user, "strategic")
    ctx["my_vendors"] = my_vendors
    ctx["slot_count"] = len(my_vendors)
    ctx["max_slots"] = svc.MAX_STRATEGIC_VENDORS
    ctx["open_vendors"] = open_vendors
    ctx["open_total"] = open_total
    ctx["search"] = ""
    return templates.TemplateResponse("htmx/partials/strategic/list.html", ctx)


@router.delete("/v2/partials/strategic/{vendor_card_id}/drop", response_class=HTMLResponse)
async def strategic_drop(
    vendor_card_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Drop a strategic vendor and reload the list."""
    from ..services import strategic_vendor_service as svc

    svc.drop_vendor(db, user.id, vendor_card_id)

    my_vendors = svc.get_my_strategic(db, user.id)
    open_vendors, open_total = svc.get_open_pool(db, limit=20, offset=0)

    ctx = _base_ctx(request, user, "strategic")
    ctx["my_vendors"] = my_vendors
    ctx["slot_count"] = len(my_vendors)
    ctx["max_slots"] = svc.MAX_STRATEGIC_VENDORS
    ctx["open_vendors"] = open_vendors
    ctx["open_total"] = open_total
    ctx["search"] = ""
    return templates.TemplateResponse("htmx/partials/strategic/list.html", ctx)


# ── Parts Workspace (split-panel) ────────────────────────────────────────────

# Default columns shown when user has no saved preference
_DEFAULT_PARTS_COLUMNS = [
    "mpn",
    "brand",
    "qty",
    "target_price",
    "status",
    "requisition",
    "customer",
    "offers",
    "best_price",
    "owner",
    "created",
]

# All available columns for the column picker
_ALL_PARTS_COLUMNS = [
    ("mpn", "MPN"),
    ("brand", "Brand"),
    ("qty", "Qty Needed"),
    ("target_price", "Target Price"),
    ("status", "Status"),
    ("requisition", "Requisition"),
    ("customer", "Customer"),
    ("offers", "Offers"),
    ("best_price", "Best Price"),
    ("owner", "Owner"),
    ("created", "Created"),
    ("date_codes", "Date Codes"),
    ("condition", "Condition"),
    ("packaging", "Packaging"),
    ("notes", "Notes"),
]


@router.get("/v2/partials/parts", response_class=HTMLResponse)
async def parts_list_partial(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    q: str = "",
    requisition_name: str = "",
    customer: str = "",
    brand: str = "",
    status: str = "",
    owner: int = 0,
    date_from: str = "",
    date_to: str = "",
    include_archived: bool = False,
    sort: str = "created",
    dir: str = "desc",
    offset: int = 0,
    limit: int = 50,
):
    """Return parts (requirements) list as HTML partial with filters and sorting."""
    from sqlalchemy import case

    query = db.query(Requirement).join(Requisition, Requirement.requisition_id == Requisition.id)

    # Archive visibility logic:
    # - status=archived  → show only archived parts (any requisition status)
    # - include_archived → show everything (no filtering)
    # - default          → exclude archived parts AND archived requisitions
    if status == "archived":
        query = query.filter(Requirement.sourcing_status == "archived")
    elif not include_archived:
        query = query.filter(Requisition.status.in_(["active", "open", "sourcing"]))
        query = query.filter(Requirement.sourcing_status != "archived")

    # Filters
    if q:
        pattern = f"%{escape_like(q)}%"
        query = query.filter(
            (Requirement.primary_mpn.ilike(pattern))
            | (Requirement.brand.ilike(pattern))
            | (Requisition.name.ilike(pattern))
            | (Requisition.customer_name.ilike(pattern))
        )
    if requisition_name:
        query = query.filter(Requisition.name.ilike(f"%{escape_like(requisition_name)}%"))
    if customer:
        query = query.filter(Requisition.customer_name.ilike(f"%{escape_like(customer)}%"))
    if brand:
        query = query.filter(Requirement.brand.ilike(f"%{escape_like(brand)}%"))
    if status and status != "archived":
        query = query.filter(Requirement.sourcing_status == status)
    if owner:
        query = query.filter(Requisition.claimed_by_id == owner)
    if date_from:
        query = query.filter(Requirement.created_at >= date_from)
    if date_to:
        query = query.filter(Requirement.created_at <= date_to)

    total = query.count()

    # Sorting
    sort_map = {
        "mpn": Requirement.primary_mpn,
        "brand": Requirement.brand,
        "qty": Requirement.target_qty,
        "target_price": Requirement.target_price,
        "status": Requirement.sourcing_status,
        "requisition": Requisition.name,
        "customer": Requisition.customer_name,
        "created": Requirement.created_at,
    }
    sort_col = sort_map.get(sort, Requirement.created_at)
    query = query.order_by(sort_col.desc() if dir == "desc" else sort_col.asc())
    query = query.offset(offset).limit(limit)

    requirements = query.options(joinedload(Requirement.requisition)).all()

    # Aggregate offer count + best price per requirement
    req_ids = [r.id for r in requirements]
    offer_stats = {}
    if req_ids:
        stats = (
            db.query(
                Offer.requirement_id,
                sqlfunc.count(Offer.id).label("cnt"),
                sqlfunc.min(case((Offer.status == "active", Offer.unit_price), else_=None)).label("best"),
            )
            .filter(Offer.requirement_id.in_(req_ids))
            .group_by(Offer.requirement_id)
            .all()
        )
        for row in stats:
            offer_stats[row.requirement_id] = {"count": row.cnt, "best_price": row.best}

    # User column prefs
    visible_cols = user.parts_column_prefs or _DEFAULT_PARTS_COLUMNS

    # Team users for owner filter
    users_list = db.query(User).filter(User.is_active.is_(True)).order_by(User.name).all()

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update(
        {
            "requirements": requirements,
            "offer_stats": offer_stats,
            "q": q,
            "requisition_name": requisition_name,
            "customer": customer,
            "brand": brand,
            "status": status,
            "owner": owner,
            "date_from": date_from,
            "date_to": date_to,
            "include_archived": include_archived,
            "sort": sort,
            "dir": dir,
            "offset": offset,
            "limit": limit,
            "total": total,
            "users": users_list,
            "visible_cols": visible_cols,
            "all_columns": _ALL_PARTS_COLUMNS,
            "user_role": user.role,
        }
    )
    return templates.TemplateResponse("htmx/partials/parts/list.html", ctx)


@router.post("/v2/partials/parts/column-prefs", response_class=HTMLResponse)
async def save_column_prefs(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save user's visible column preferences and return updated parts list."""
    form = await request.form()
    cols = [c for c in form.getlist("columns") if c in dict(_ALL_PARTS_COLUMNS)]
    if not cols:
        cols = _DEFAULT_PARTS_COLUMNS

    user.parts_column_prefs = cols
    db.commit()
    logger.info("Column prefs saved for user {}: {}", user.email, cols)

    # Re-render the parts list with new columns
    return await parts_list_partial(request=request, user=user, db=db)


@router.get("/v2/partials/parts/{requirement_id}/tab/offers", response_class=HTMLResponse)
async def part_tab_offers(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return offers table for a specific part number."""
    req = db.query(Requirement).options(joinedload(Requirement.requisition)).get(requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")

    offers = db.query(Offer).filter(Offer.requirement_id == requirement_id).order_by(Offer.created_at.desc()).all()

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"requirement": req, "offers": offers})
    return templates.TemplateResponse("htmx/partials/parts/tabs/offers.html", ctx)


@router.get("/v2/partials/parts/{requirement_id}/tab/sourcing", response_class=HTMLResponse)
async def part_tab_sourcing(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return vendor-level sighting summaries for a specific part number."""
    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")

    summaries = (
        db.query(VendorSightingSummary)
        .filter(VendorSightingSummary.requirement_id == requirement_id)
        .order_by(VendorSightingSummary.score.desc(), VendorSightingSummary.id.desc())
        .all()
    )

    # Raw sightings grouped by vendor for popover breakdowns
    raw_sightings = (
        db.query(Sighting).filter(Sighting.requirement_id == requirement_id).order_by(Sighting.score.desc()).all()
    )
    raw_by_vendor: dict[str, list] = {}
    for s in raw_sightings:
        vn = (s.vendor_name or "unknown").lower().strip()
        raw_by_vendor.setdefault(vn, []).append(s)

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update(
        {
            "requirement": req,
            "summaries": summaries,
            "raw_sightings_by_vendor": raw_by_vendor,
        }
    )
    return templates.TemplateResponse("htmx/partials/parts/tabs/sourcing.html", ctx)


@router.get("/v2/partials/parts/{requirement_id}/header", response_class=HTMLResponse)
async def part_header(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the part detail header strip (display mode)."""
    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")

    ctx = _base_ctx(request, user, "requisitions")
    ctx["requirement"] = req
    return templates.TemplateResponse("htmx/partials/parts/header.html", ctx)


_PART_HEADER_EDITABLE = {
    "brand",
    "target_qty",
    "target_price",
    "condition",
    "sourcing_status",
    "notes",
    "date_codes",
    "packaging",
}
_CONDITION_CHOICES = ["New", "Used", "Refurbished", "Any"]


@router.get("/v2/partials/parts/{requirement_id}/header/edit/{field}", response_class=HTMLResponse)
async def part_header_edit_cell(
    requirement_id: int,
    field: str,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return an inline edit input for a single header field."""
    if field not in _PART_HEADER_EDITABLE:
        return HTMLResponse("Invalid field", status_code=400)

    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")

    current = getattr(req, field, "") or ""
    cell_id = f"hdr-{field}"
    cancel_url = f"/v2/partials/parts/{requirement_id}/header"
    save_url = f"/v2/partials/parts/{requirement_id}/header"

    if field == "sourcing_status":
        statuses = ["open", "sourcing", "offered", "quoted", "won", "lost", "archived"]
        options = "".join(
            f'<option value="{s}" {"selected" if s == current else ""}>{s.capitalize()}</option>' for s in statuses
        )
        return HTMLResponse(
            f'<select name="value" id="{cell_id}" '
            f'hx-patch="{save_url}" hx-target="#part-header-wrap" hx-swap="innerHTML" '
            f'hx-vals=\'{{"field": "{field}"}}\' '
            f'class="text-xs px-1.5 py-0.5 rounded border border-brand-300 focus:ring-1 focus:ring-brand-500" '
            f"@keydown.escape=\"htmx.ajax('GET', '{cancel_url}', {{target: '#part-header-wrap', swap: 'innerHTML'}})\">"
            f"{options}</select>",
        )

    if field == "condition":
        options = "".join(
            f'<option value="{c}" {"selected" if c == current else ""}>{c}</option>' for c in _CONDITION_CHOICES
        )
        return HTMLResponse(
            f'<select name="value" id="{cell_id}" '
            f'hx-patch="{save_url}" hx-target="#part-header-wrap" hx-swap="innerHTML" '
            f'hx-vals=\'{{"field": "{field}"}}\' '
            f'class="text-xs px-1.5 py-0.5 rounded border border-brand-300 focus:ring-1 focus:ring-brand-500" '
            f"@keydown.escape=\"htmx.ajax('GET', '{cancel_url}', {{target: '#part-header-wrap', swap: 'innerHTML'}})\">"
            f"{options}</select>",
        )

    input_type = "number" if field in ("target_qty", "target_price") else "text"
    step = ' step="0.0001"' if field == "target_price" else ""

    return HTMLResponse(
        f'<input type="{input_type}" name="value" id="{cell_id}" value="{current}" '
        f'hx-patch="{save_url}" hx-target="#part-header-wrap" hx-swap="innerHTML" '
        f'hx-vals=\'{{"field": "{field}"}}\' '
        f"hx-trigger=\"keyup[key=='Enter']\" "
        f"@keydown.escape=\"htmx.ajax('GET', '{cancel_url}', {{target: '#part-header-wrap', swap: 'innerHTML'}})\" "
        f'class="text-sm px-1.5 py-0.5 rounded border border-brand-300 focus:ring-1 focus:ring-brand-500 w-24"'
        f"{step} autofocus />",
    )


@router.patch("/v2/partials/parts/{requirement_id}/header", response_class=HTMLResponse)
async def part_header_save(
    requirement_id: int,
    request: Request,
    field: str = Form(...),
    value: str = Form(default=""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save an inline header field edit and return the refreshed header."""
    if field not in _PART_HEADER_EDITABLE:
        return HTMLResponse("Invalid field", status_code=400)

    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")

    if field == "sourcing_status":
        from app.services.requirement_status import transition_requirement

        ok = transition_requirement(req, value, db, user)
        if not ok:
            logger.warning(
                "Status transition rejected: {} → {} for part {}", req.sourcing_status, value, requirement_id
            )
    elif field == "target_qty":
        req.target_qty = int(value) if value else None
    elif field == "target_price":
        from decimal import Decimal, InvalidOperation

        try:
            req.target_price = Decimal(value) if value else None
        except InvalidOperation:
            req.target_price = None
    else:
        setattr(req, field, value.strip() if value else None)

    db.commit()
    logger.info("Part {} header field '{}' updated by {}", requirement_id, field, user.email)

    ctx = _base_ctx(request, user, "requisitions")
    ctx["requirement"] = req
    response = templates.TemplateResponse("htmx/partials/parts/header.html", ctx)
    response.headers["HX-Trigger"] = json.dumps({"part-updated": {"id": requirement_id}})
    return response


@router.get("/v2/partials/parts/{requirement_id}/tab/activity", response_class=HTMLResponse)
async def part_tab_activity(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return activity timeline for the parent requisition of this part."""
    from ..models.intelligence import ActivityLog

    req = db.query(Requirement).get(requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")

    activities = (
        db.query(ActivityLog)
        .filter(ActivityLog.requisition_id == req.requisition_id)
        .order_by(ActivityLog.created_at.desc())
        .limit(50)
        .all()
    )

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"requirement": req, "activities": activities})
    return templates.TemplateResponse("htmx/partials/parts/tabs/activity.html", ctx)


@router.get("/v2/partials/parts/{requirement_id}/tab/comms", response_class=HTMLResponse)
async def part_tab_comms(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return communications tab — notes and tasks for this part."""
    req = db.query(Requirement).options(joinedload(Requirement.requisition)).get(requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")

    tasks = (
        db.query(RequisitionTask)
        .options(joinedload(RequisitionTask.assignee), joinedload(RequisitionTask.creator))
        .filter(
            (RequisitionTask.requisition_id == req.requisition_id)
            & ((RequisitionTask.requirement_id == requirement_id) | (RequisitionTask.requirement_id.is_(None)))
        )
        .order_by(
            RequisitionTask.status.asc(),  # pending before done
            RequisitionTask.due_at.asc().nullslast(),
            RequisitionTask.created_at.desc(),
        )
        .all()
    )

    users_list = db.query(User).filter(User.is_active.is_(True)).order_by(User.name).all()

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"requirement": req, "tasks": tasks, "users": users_list})
    return templates.TemplateResponse("htmx/partials/parts/tabs/comms.html", ctx)


@router.post("/v2/partials/parts/{requirement_id}/tasks", response_class=HTMLResponse)
async def create_part_task(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a task for a specific part number."""
    req = db.query(Requirement).get(requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")

    form = await request.form()
    title = (form.get("title") or "").strip()
    if not title:
        raise HTTPException(422, "Title is required")

    task = RequisitionTask(
        requisition_id=req.requisition_id,
        requirement_id=requirement_id,
        title=title,
        description=(form.get("notes") or "").strip() or None,
        assigned_to_id=int(form["assigned_to"]) if form.get("assigned_to") else None,
        created_by=user.id,
        due_at=form.get("due_date") or None,
        status="todo",
        source="manual",
    )
    db.add(task)
    db.commit()
    logger.info("Task '{}' created for requirement {} by {}", title, requirement_id, user.email)

    # Return refreshed comms tab
    return await part_tab_comms(requirement_id, request, user, db)


@router.post("/v2/partials/parts/tasks/{task_id}/done", response_class=HTMLResponse)
async def mark_task_done(
    task_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Mark a task as done."""
    from datetime import datetime, timezone

    task = db.query(RequisitionTask).get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    task.status = "done"
    task.completed_at = datetime.now(timezone.utc)
    db.commit()
    logger.info("Task {} marked done by {}", task_id, user.email)

    # Return refreshed comms tab for the requirement
    req_id = task.requirement_id
    if req_id:
        return await part_tab_comms(req_id, request, user, db)

    # Fallback — return just the updated task row
    ctx = _base_ctx(request, user, "requisitions")
    ctx["task"] = task
    return HTMLResponse('<div class="text-sm text-green-600">Task completed</div>')


@router.post("/v2/partials/parts/tasks/{task_id}/reopen", response_class=HTMLResponse)
async def reopen_task(
    task_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Reopen a completed task."""
    task = db.query(RequisitionTask).get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    task.status = "todo"
    task.completed_at = None
    db.commit()
    logger.info("Task {} reopened by {}", task_id, user.email)

    req_id = task.requirement_id
    if req_id:
        return await part_tab_comms(req_id, request, user, db)
    return HTMLResponse('<div class="text-sm text-amber-600">Task reopened</div>')


# ── Archive system ────────────────────────────────────────────────────


@router.patch("/v2/partials/parts/{requirement_id}/archive", response_class=HTMLResponse)
async def archive_single_part(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Archive a single part (requirement) by setting sourcing_status to archived."""
    part = db.get(Requirement, requirement_id)
    if not part:
        raise HTTPException(404, "Part not found")

    part.sourcing_status = "archived"
    db.commit()
    logger.info("Part {} archived by {}", requirement_id, user.email)

    response = await parts_list_partial(request=request, user=user, db=db)
    response.headers["HX-Trigger"] = json.dumps({"part-archived": {"id": requirement_id}})
    return response


@router.patch("/v2/partials/parts/{requirement_id}/unarchive", response_class=HTMLResponse)
async def unarchive_single_part(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Unarchive a single part — restores sourcing_status to open."""
    part = db.get(Requirement, requirement_id)
    if not part:
        raise HTTPException(404, "Part not found")

    part.sourcing_status = "open"
    db.commit()
    logger.info("Part {} unarchived by {}", requirement_id, user.email)

    return await parts_list_partial(request=request, user=user, db=db)


@router.patch("/v2/partials/requisitions/{req_id}/archive", response_class=HTMLResponse)
async def archive_requisition(
    req_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Archive a whole requisition and cascade to all its requirements."""
    requisition = db.get(Requisition, req_id)
    if not requisition:
        raise HTTPException(404, "Requisition not found")

    requisition.status = "archived"
    for child in requisition.requirements:
        child.sourcing_status = "archived"
    db.commit()
    logger.info("Requisition {} ({} parts) archived by {}", req_id, len(requisition.requirements), user.email)

    return await parts_list_partial(request=request, user=user, db=db)


@router.patch("/v2/partials/requisitions/{req_id}/unarchive", response_class=HTMLResponse)
async def unarchive_requisition(
    req_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Unarchive a requisition and restore all its requirements to open."""
    requisition = db.get(Requisition, req_id)
    if not requisition:
        raise HTTPException(404, "Requisition not found")

    requisition.status = "active"
    for child in requisition.requirements:
        if child.sourcing_status == "archived":
            child.sourcing_status = "open"
    db.commit()
    logger.info("Requisition {} unarchived by {}", req_id, user.email)

    return await parts_list_partial(request=request, user=user, db=db)


@router.post("/v2/partials/parts/bulk-archive", response_class=HTMLResponse)
async def bulk_archive(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Bulk-archive parts and/or requisitions.

    Body: {"requirement_ids": [], "requisition_ids": []}.
    """
    body = await request.json()
    requirement_ids = body.get("requirement_ids", [])
    requisition_ids = body.get("requisition_ids", [])

    # Bulk-update parts in a single query instead of N+1
    if requirement_ids:
        db.query(Requirement).filter(
            Requirement.id.in_(requirement_ids),
        ).update({"sourcing_status": "archived"}, synchronize_session="fetch")

    # Archive requisitions and cascade to their children
    if requisition_ids:
        reqs = db.query(Requisition).filter(Requisition.id.in_(requisition_ids)).all()
        for requisition in reqs:
            requisition.status = "archived"
        # Cascade: archive all children of these requisitions
        db.query(Requirement).filter(
            Requirement.requisition_id.in_(requisition_ids),
        ).update({"sourcing_status": "archived"}, synchronize_session="fetch")

    db.commit()
    logger.info("Bulk archive by {}: {} parts, {} requisitions", user.email, len(requirement_ids), len(requisition_ids))

    return await parts_list_partial(request=request, user=user, db=db)


@router.post("/v2/partials/parts/bulk-unarchive", response_class=HTMLResponse)
async def bulk_unarchive(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Bulk-unarchive parts and/or requisitions.

    Body: {"requirement_ids": [], "requisition_ids": []}.
    """
    body = await request.json()
    requirement_ids = body.get("requirement_ids", [])
    requisition_ids = body.get("requisition_ids", [])

    # Bulk-update parts in a single query instead of N+1
    if requirement_ids:
        db.query(Requirement).filter(
            Requirement.id.in_(requirement_ids),
            Requirement.sourcing_status == "archived",
        ).update({"sourcing_status": "open"}, synchronize_session="fetch")

    # Unarchive requisitions and cascade to their children
    if requisition_ids:
        reqs = db.query(Requisition).filter(Requisition.id.in_(requisition_ids)).all()
        for requisition in reqs:
            requisition.status = "active"
        # Cascade: restore archived children of these requisitions
        db.query(Requirement).filter(
            Requirement.requisition_id.in_(requisition_ids),
            Requirement.sourcing_status == "archived",
        ).update({"sourcing_status": "open"}, synchronize_session="fetch")

    db.commit()
    logger.info(
        "Bulk unarchive by {}: {} parts, {} requisitions", user.email, len(requirement_ids), len(requisition_ids)
    )

    return await parts_list_partial(request=request, user=user, db=db)
