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
from datetime import datetime
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
    RequisitionTask,
    Requisition,
    Sighting,
    SiteContact,
    SourcingLead,
    User,
    VendorCard,
    VerificationGroupMember,
)
from ..models.buy_plan import BuyPlanLineStatus, BuyPlanStatus, SOVerificationStatus
from ..models.prospect_account import ProspectAccount
from ..models.enrichment import ProspectContact
from ..models.vendors import VendorContact
from ..scoring import classify_lead, confidence_color, explain_lead, score_unified
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
    """Return Vite asset URLs for templates. Keys: js_file, css_files."""
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
        current_view = "dashboard"

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
            .filter(
                VendorCard.display_name.ilike(f"%{safe}%")
                | VendorCard.normalized_name.ilike(f"%{safe}%")
            )
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
        query = query.filter(
            Requisition.name.ilike(f"%{safe}%")
            | Requisition.customer_name.ilike(f"%{safe}%")
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
    ctx.update({
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
    })
    return templates.TemplateResponse("htmx/partials/requisitions/list.html", ctx)


@router.get("/v2/partials/requisitions/create-form", response_class=HTMLResponse)
async def requisition_create_form(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the create requisition modal form."""
    ctx = _base_ctx(request, user, "requisitions")
    return templates.TemplateResponse("partials/requisitions/create_modal.html", ctx)


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
    response = templates.TemplateResponse("partials/requisitions/req_row.html", ctx)
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
    return templates.TemplateResponse("partials/requisitions/tabs/req_row.html", ctx)


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
        requirements = (
            db.query(Requirement)
            .filter(Requirement.requisition_id == req_id)
            .all()
        )
        for r in requirements:
            r.sighting_count = len(r.sightings) if r.sightings else 0
        ctx["requirements"] = requirements
        return templates.TemplateResponse("partials/requisitions/tabs/parts.html", ctx)

    elif tab == "offers":
        offers = (
            db.query(Offer)
            .filter(Offer.requisition_id == req_id)
            .order_by(Offer.created_at.desc().nullslast())
            .all()
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
        return templates.TemplateResponse("partials/requisitions/tabs/offers.html", ctx)

    elif tab == "quotes":
        quotes = (
            db.query(Quote)
            .filter(Quote.requisition_id == req_id)
            .order_by(Quote.created_at.desc().nullslast())
            .all()
        )
        ctx["quotes"] = quotes
        return templates.TemplateResponse("partials/requisitions/tabs/quotes.html", ctx)

    elif tab == "buy_plans":
        buy_plans = (
            db.query(BuyPlan)
            .options(joinedload(BuyPlan.lines))
            .filter(BuyPlan.requisition_id == req_id)
            .order_by(BuyPlan.created_at.desc().nullslast())
            .all()
        )
        ctx["buy_plans"] = buy_plans
        return templates.TemplateResponse("partials/requisitions/tabs/buy_plans.html", ctx)

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
        return templates.TemplateResponse("partials/requisitions/tabs/tasks.html", ctx)

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
        return templates.TemplateResponse("partials/requisitions/tabs/responses.html", ctx)

    else:  # activity
        from ..models.offers import Contact as RfqContact
        from ..models.intelligence import ActivityLog

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
        return templates.TemplateResponse("partials/requisitions/tabs/activity.html", ctx)


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
    return templates.TemplateResponse("partials/requisitions/tabs/parse_email_form.html", ctx)


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
    return templates.TemplateResponse("partials/requisitions/tabs/paste_offer_form.html", ctx)


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

    return templates.TemplateResponse(
        "partials/requisitions/tabs/parsed_email_results.html", ctx
    )


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

    return templates.TemplateResponse(
        "partials/requisitions/tabs/parsed_offer_results.html", ctx
    )


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
    return templates.TemplateResponse(
        "partials/requisitions/tabs/parse_save_success.html", ctx
    )


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
        request=request, q="", status="", owner=0, urgency="",
        date_from="", date_to="", sort="created_at", dir="desc",
        limit=50, offset=0, user=user, db=db,
    )


@router.post("/v2/partials/requisitions/{req_id}/create-quote", response_class=HTMLResponse)
async def create_quote_from_offers(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a new quote from selected offer IDs. Returns quote detail partial."""
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
    import itertools

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
    """Approve or reject an offer. Returns refreshed offers tab."""
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
            db.query(VendorCard)
            .filter(VendorCard.normalized_name.in_(norm_names))
            .limit(50)
            .all()
        ) if norm_names else []
        # Check which vendors already have RFQs sent
        sent_vendor_names = set()
        existing_contacts = (
            db.query(RfqContact)
            .filter(RfqContact.requisition_id == req_id)
            .all()
        )
        for c in existing_contacts:
            if c.vendor_name_normalized:
                sent_vendor_names.add(c.vendor_name_normalized)

        for v in vendor_rows:
            # Get contacts for this vendor
            v_contacts = (
                db.query(VendorContact)
                .filter(VendorContact.vendor_card_id == v.id)
                .limit(5)
                .all()
            )
            vendors.append({
                "id": v.id,
                "display_name": v.display_name,
                "normalized_name": v.normalized_name,
                "domain": v.domain,
                "contacts": v_contacts,
                "already_asked": v.normalized_name in sent_vendor_names,
                "emails": [c.email for c in v_contacts if c.email],
            })

    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req
    ctx["parts"] = parts
    ctx["vendors"] = vendors
    return templates.TemplateResponse("partials/requisitions/rfq_compose.html", ctx)


@router.post("/v2/partials/requisitions/{req_id}/ai-draft-rfq", response_class=HTMLResponse)
async def ai_draft_rfq(
    request: Request,
    req_id: int,
    vendor_names: str = Form(""),
    parts_summary: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Generate AI-personalized RFQ email body for the first selected vendor."""
    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    if not req:
        raise HTTPException(404, "Requisition not found")

    # Take the first vendor name from the form
    vendor_name = vendor_names.strip() if vendor_names else "Vendor"
    parts = [p.strip() for p in parts_summary.split(",") if p.strip()]

    ctx = _base_ctx(request, user, "requisitions")
    ctx["vendor_name"] = vendor_name

    try:
        from app.routers.ai import _build_vendor_history
        from app.services.ai_service import draft_rfq

        vendor_history = _build_vendor_history(vendor_name, db)
        draft = await draft_rfq(
            vendor_name=vendor_name,
            parts=parts,
            vendor_history=vendor_history,
            user_name=user.name or "",
        )
        ctx["draft_body"] = draft or ""
    except Exception as exc:
        logger.error(f"AI draft RFQ error for req {req_id}: {exc}")
        ctx["draft_body"] = ""

    return templates.TemplateResponse(
        "partials/requisitions/rfq_draft_result.html", ctx
    )


@router.post("/v2/partials/requisitions/{req_id}/rfq-send", response_class=HTMLResponse)
async def rfq_send(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Send RFQs via Graph API, falling back to DB-only in test mode."""
    from ..models.offers import Contact as RfqContact
    from datetime import datetime, timezone
    import os

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
            vendor_groups.append({
                "vendor_name": name,
                "vendor_email": email,
                "parts": parts_text,
                "subject": subject,
                "body": body or f"Dear {name},\n\nWe are looking for the following parts: {parts_text}\n\nPlease provide your best pricing and availability.\n\nThank you.",
            })

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
    return templates.TemplateResponse("partials/requisitions/rfq_results.html", ctx)


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
        follow_ups.append({
            "contact_id": c.id,
            "requisition_id": c.requisition_id,
            "requisition_name": req_names.get(c.requisition_id, "Unknown"),
            "vendor_name": c.vendor_name,
            "vendor_email": c.vendor_contact,
            "parts": c.parts_included or [],
            "status": c.status,
            "days_waiting": days_waiting,
        })

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
    """Send a follow-up email for a stale contact. Returns success card."""
    from ..models.offers import Contact as RfqContact
    import os

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
            follow_up_body = body or f"Dear {contact.vendor_name},\n\nI'm following up on our previous inquiry. Please let us know if you have availability.\n\nThank you."
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
    logger.info("Follow-up sent for contact {} (vendor: {}, {}) by {}", contact_id, contact.vendor_name, mode, user.email)

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
    """Mark a vendor response as reviewed or rejected. Returns updated card."""
    from ..models.offers import VendorResponse

    vr = db.query(VendorResponse).filter(
        VendorResponse.id == response_id,
        VendorResponse.requisition_id == req_id,
    ).first()
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
    return templates.TemplateResponse("partials/requisitions/tabs/response_card.html", ctx)


@router.post("/v2/partials/requisitions/{req_id}/poll-inbox", response_class=HTMLResponse)
async def poll_inbox_htmx(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Poll inbox for vendor responses (test mode: no-op, returns refreshed responses tab)."""
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
    """Delete a requirement from a requisition. Returns empty response for hx-swap='delete'."""
    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    if not req:
        raise HTTPException(404, "Requisition not found")
    item = db.query(Requirement).filter(
        Requirement.id == item_id, Requirement.requisition_id == req_id
    ).first()
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
    """Update a requirement inline. Returns the updated row HTML."""
    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    if not req:
        raise HTTPException(404, "Requisition not found")
    item = db.query(Requirement).filter(
        Requirement.id == item_id, Requirement.requisition_id == req_id
    ).first()
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
    return templates.TemplateResponse("partials/requisitions/tabs/req_row.html", ctx)


# ── Search partials ─────────────────────────────────────────────────────


@router.get("/v2/partials/search", response_class=HTMLResponse)
async def search_form_partial(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the search form partial."""
    ctx = _base_ctx(request, user, "search")
    return templates.TemplateResponse("partials/search/form.html", ctx)


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
    source_errors: list[str] = []

    try:
        # Use the search service to find parts across all connectors
        from ..search_service import quick_search_mpn

        raw_results = await quick_search_mpn(search_mpn, db)
        if isinstance(raw_results, list):
            results = raw_results
        else:
            results = raw_results.get("sightings", [])
            source_errors = raw_results.get("source_errors", [])
    except Exception as exc:
        logger.error("Search failed for {}: {}", search_mpn, exc)
        error = f"Search error: {exc}"

    elapsed = time.time() - start

    # Enrich each result with confidence, lead quality, and reason summary
    # using scoring functions that already exist in scoring.py.
    for r in results:
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

    ctx = _base_ctx(request, user, "search")
    ctx.update({
        "results": results,
        "mpn": search_mpn,
        "elapsed_seconds": elapsed,
        "error": error,
        "source_errors": source_errors,
    })
    return templates.TemplateResponse("partials/search/results.html", ctx)


@router.get("/v2/partials/search/lead-detail", response_class=HTMLResponse)
async def search_lead_detail(
    request: Request,
    idx: int = Query(0, ge=0),
    mpn: str = Query(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the lead detail drawer content for a single search result.

    Re-runs the search (cached in search_service) and returns the enriched
    result at the given index.
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
        mc = db.query(MaterialCard.id).filter(
            MaterialCard.normalized_mpn == mpn_clean
        ).first()
        if mc:
            material_card_id = mc.id

    ctx = _base_ctx(request, user, "search")
    ctx.update({
        "lead": r,
        "mpn": mpn.strip(),
        "idx": idx,
        "safety_band": safety_band,
        "safety_score": safety_score,
        "safety_summary": safety_summary,
        "safety_flags": safety_flags,
        "safety_available": safety_available,
        "material_card_id": material_card_id,
    })
    return templates.TemplateResponse("partials/search/lead_detail.html", ctx)


# ── Vendor partials ─────────────────────────────────────────────────────


@router.get("/v2/partials/vendors", response_class=HTMLResponse)
async def vendors_list_partial(
    request: Request,
    q: str = "",
    hide_blacklisted: bool = True,
    sort: str = "sighting_count",
    dir: str = "desc",
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return vendor list as HTML partial with blacklisted toggle and sorting."""
    query = db.query(VendorCard)

    if hide_blacklisted:
        query = query.filter(VendorCard.is_blacklisted.is_(False))

    if q.strip():
        safe = escape_like(q.strip())
        query = query.filter(
            VendorCard.display_name.ilike(f"%{safe}%")
            | VendorCard.domain.ilike(f"%{safe}%")
        )

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
    ctx.update({
        "vendors": vendors,
        "q": q,
        "hide_blacklisted": hide_blacklisted,
        "sort": sort,
        "dir": dir,
        "total": total,
        "limit": limit,
        "offset": offset,
    })
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
    ctx.update({
        "vendor": vendor,
        "contacts": contacts,
        "recent_sightings": recent_sightings,
        "safety_band": safety_band,
        "safety_summary": safety_summary,
        "safety_flags": safety_flags,
        "safety_score": None,
        "safety_available": False,
    })
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

    valid_tabs = {"overview", "contacts", "find_contacts", "emails", "analytics", "offers"}
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
        ctx.update({
            "recent_sightings": recent_sightings,
            "contacts": contacts,
            "safety_band": safety_band,
            "safety_summary": safety_summary,
            "safety_flags": safety_flags,
            "safety_score": safety_score,
            "safety_available": safety_available,
        })
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
        return templates.TemplateResponse("partials/vendors/tabs/contacts.html", ctx)

    elif tab == "find_contacts":
        prospects = (
            db.query(ProspectContact)
            .filter(ProspectContact.vendor_card_id == vendor_id)
            .order_by(ProspectContact.created_at.desc())
            .limit(50)
            .all()
        )
        ctx["prospects"] = prospects
        return templates.TemplateResponse(
            "htmx/partials/vendors/find_contacts_tab.html", ctx
        )

    elif tab == "emails":
        from ..models.offers import Contact as RfqContact, VendorResponse

        norm = (vendor.normalized_name or "").lower().strip()
        contacts = (
            db.query(RfqContact)
            .filter(RfqContact.vendor_name_normalized == norm)
            .order_by(RfqContact.created_at.desc())
            .limit(100)
            .all()
        ) if norm else []
        responses = (
            db.query(VendorResponse)
            .filter(sqlfunc.lower(VendorResponse.vendor_name) == norm)
            .order_by(VendorResponse.received_at.desc().nullslast())
            .limit(100)
            .all()
        ) if norm else []
        ctx = _base_ctx(request, user, "vendors")
        ctx.update({"vendor": vendor, "contacts": contacts, "responses": responses})
        return templates.TemplateResponse("htmx/partials/vendors/emails_tab.html", ctx)

    elif tab == "analytics":
        html = f"""<div class="space-y-6">
          <div class="grid grid-cols-2 md:grid-cols-3 gap-4">
            <div class="bg-white rounded-lg border border-gray-200 p-4 text-center">
              <p class="text-2xl font-bold text-brand-500">{'{:.0f}%'.format((vendor.overall_win_rate or 0) * 100)}</p>
              <p class="text-xs text-gray-500 mt-1">Win Rate</p>
            </div>
            <div class="bg-white rounded-lg border border-gray-200 p-4 text-center">
              <p class="text-2xl font-bold text-brand-500">{'{:.0f}%'.format((vendor.response_rate or 0) * 100)}</p>
              <p class="text-xs text-gray-500 mt-1">Response Rate</p>
            </div>
            <div class="bg-white rounded-lg border border-gray-200 p-4 text-center">
              <p class="text-2xl font-bold text-brand-500">{'{:.0f}'.format(vendor.vendor_score or 0)}</p>
              <p class="text-xs text-gray-500 mt-1">Vendor Score</p>
            </div>
            <div class="bg-white rounded-lg border border-gray-200 p-4 text-center">
              <p class="text-2xl font-bold text-gray-900">{vendor.sighting_count or 0}</p>
              <p class="text-xs text-gray-500 mt-1">Sightings</p>
            </div>
            <div class="bg-white rounded-lg border border-gray-200 p-4 text-center">
              <p class="text-2xl font-bold text-gray-900">{'{:.0f}'.format(vendor.avg_response_hours or 0)}</p>
              <p class="text-xs text-gray-500 mt-1">Avg Response Hours</p>
            </div>
            <div class="bg-white rounded-lg border border-gray-200 p-4 text-center">
              <p class="text-2xl font-bold text-gray-900">{'{:.0f}'.format(vendor.engagement_score or 0)}</p>
              <p class="text-xs text-gray-500 mt-1">Engagement Score</p>
            </div>
          </div>
          <p class="text-sm text-gray-500 text-center">Analytics data builds as you interact with this vendor.</p>
        </div>"""
        return HTMLResponse(html)

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
            date_str = o.created_at.strftime('%b %d, %Y') if o.created_at else _DASH
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
                <tbody class="divide-y divide-gray-200">{''.join(rows)}</tbody>
              </table>
            </div>"""
        else:
            html = '<div class="p-8 text-center"><p class="text-sm text-gray-500">No offers from this vendor yet.</p></div>'
        return HTMLResponse(html)


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
        web_results = await enrich_contacts_websearch(
            vendor.display_name, vendor.domain, keywords, limit=10
        )

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
    return templates.TemplateResponse(
        "htmx/partials/vendors/find_contacts_results.html", ctx
    )


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
    return templates.TemplateResponse(
        "htmx/partials/vendors/prospect_card.html", ctx
    )


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
        existing = (
            db.query(VendorContact)
            .filter_by(vendor_card_id=vendor_id, email=pc.email)
            .first()
        )

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
    return templates.TemplateResponse(
        "htmx/partials/vendors/prospect_card.html", ctx
    )


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
    ctx.update({
        "company": company,
        "sites": sites,
        "open_req_count": open_req_count,
        "user": user,
    })
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
        return templates.TemplateResponse("partials/companies/tabs/sites_tab.html", ctx)

    elif tab == "contacts":
        # Get all SiteContact records across all sites for this company
        site_ids = [s.id for s in db.query(CustomerSite.id).filter(
            CustomerSite.company_id == company_id
        ).all()]
        contacts = []
        if site_ids:
            contacts = (
                db.query(SiteContact)
                .filter(SiteContact.customer_site_id.in_(site_ids), SiteContact.is_active.is_(True))
                .order_by(SiteContact.is_primary.desc(), SiteContact.full_name)
                .all()
            )
        # Build a table with site name
        site_map = {s.id: s for s in db.query(CustomerSite).filter(
            CustomerSite.company_id == company_id
        ).all()}
        rows = []
        for c in contacts:
            site = site_map.get(c.customer_site_id)
            site_name = site.site_name if site else _DASH
            phone_html = f'<a href="tel:{c.phone}" class="text-brand-500 hover:text-brand-600">{c.phone}</a>' if c.phone else f'<span class="text-gray-500">{_DASH}</span>'
            primary_badge = ' <span class="px-1 py-0.5 text-[9px] font-medium rounded bg-emerald-50 text-emerald-700">Primary</span>' if c.is_primary else ''
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
                phone_html = f'<a href="tel:{s.contact_phone}" class="text-brand-500">{s.contact_phone}</a>' if s.contact_phone else f'<span class="text-gray-500">{_DASH}</span>'
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
                <tbody class="divide-y divide-gray-200">{''.join(rows)}</tbody>
              </table>
            </div>"""
        else:
            html = '<div class="p-8 text-center"><p class="text-sm text-gray-500">No contacts found. Add contacts via the Sites tab.</p></div>'
        return HTMLResponse(html)

    elif tab == "requisitions":
        from sqlalchemy import or_
        reqs = (
            db.query(Requisition)
            .filter(or_(
                Requisition.company_id == company.id,
                sqlfunc.lower(sqlfunc.trim(Requisition.customer_name)) == company.name.lower().strip(),
            ))
            .order_by(Requisition.created_at.desc().nullslast())
            .limit(50)
            .all()
        )
        rows = []
        for r in reqs:
            date_str = r.created_at.strftime('%b %d, %Y') if r.created_at else '\u2014'
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
                <tbody class="divide-y divide-gray-200">{''.join(rows)}</tbody>
              </table>
            </div>"""
        else:
            html = '<div class="p-8 text-center"><p class="text-sm text-gray-500">No requisitions for this company.</p></div>'
        return HTMLResponse(html)

    else:  # activity
        from sqlalchemy import or_ as or_clause
        from ..models.offers import Contact as RfqContact
        from ..models.intelligence import ActivityLog

        # Find all requisition IDs linked to this company (via FK or name match)
        req_ids = [r.id for r in db.query(Requisition.id).filter(or_clause(
            Requisition.company_id == company.id,
            sqlfunc.lower(sqlfunc.trim(Requisition.customer_name)) == company.name.lower().strip(),
        )).all()]

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
        site_ids = [s.id for s in db.query(CustomerSite.id).filter(
            CustomerSite.company_id == company_id
        ).all()]
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
        ctx.update({
            "company": company,
            "contacts": contacts,
            "quotes": quotes,
            "activities": activities,
            "req_map": req_map,
        })
        return templates.TemplateResponse("partials/companies/tabs/activity_tab.html", ctx)


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
        return HTMLResponse(
            '<div class="p-2 text-xs text-rose-600">Site name is required.</div>'
        )

    # Enforce one-owner-per-site rule: each user can only own one site
    parsed_owner_id = int(owner_id) if owner_id else None
    if parsed_owner_id:
        existing = db.query(CustomerSite).filter(
            CustomerSite.owner_id == parsed_owner_id,
        ).first()
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
    return templates.TemplateResponse("partials/companies/tabs/site_card.html", ctx)


@router.delete("/v2/partials/companies/{company_id}/sites/{site_id}", response_class=HTMLResponse)
async def delete_site(
    request: Request,
    company_id: int,
    site_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Soft-delete a site (set is_active=False)."""
    site = db.query(CustomerSite).filter(
        CustomerSite.id == site_id, CustomerSite.company_id == company_id
    ).first()
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
    site = db.query(CustomerSite).filter(
        CustomerSite.id == site_id, CustomerSite.company_id == company_id
    ).first()
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
    return templates.TemplateResponse("partials/companies/tabs/site_contacts.html", ctx)


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
    site = db.query(CustomerSite).filter(
        CustomerSite.id == site_id, CustomerSite.company_id == company_id
    ).first()
    if not site:
        raise HTTPException(404, "Site not found")

    if not full_name.strip():
        return HTMLResponse(
            '<div class="p-2 text-xs text-rose-600">Name is required.</div>'
        )

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
    return templates.TemplateResponse("partials/companies/tabs/site_contacts.html", ctx)


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
    contact = db.query(SiteContact).filter(
        SiteContact.id == contact_id, SiteContact.customer_site_id == site_id
    ).first()
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
    contact = db.query(SiteContact).filter(
        SiteContact.id == contact_id, SiteContact.customer_site_id == site_id
    ).first()
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
    return templates.TemplateResponse("partials/companies/tabs/site_contacts.html", ctx)


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


# ── Sourcing partials ──────────────────────────────────────────────────


@router.get("/v2/sourcing/{requirement_id}", response_class=HTMLResponse)
async def v2_sourcing_page(
    request: Request, requirement_id: int, db: Session = Depends(get_db)
):
    """Full page load for sourcing results."""
    user = get_user(request, db)
    if not user:
        return templates.TemplateResponse("htmx/login.html", {"request": request})
    ctx = _base_ctx(request, user, "requisitions")
    ctx["partial_url"] = f"/v2/partials/sourcing/{requirement_id}"
    return templates.TemplateResponse("htmx/base_page.html", ctx)


@router.get("/v2/sourcing/leads/{lead_id}", response_class=HTMLResponse)
async def v2_lead_detail_page(
    request: Request, lead_id: int, db: Session = Depends(get_db)
):
    """Full page load for lead detail."""
    user = get_user(request, db)
    if not user:
        return templates.TemplateResponse("htmx/login.html", {"request": request})
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

    Runs connectors in parallel, publishes SSE events per source completion,
    syncs leads on completion, returns redirect to sourcing results.
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
            await broker.publish(channel, "source-complete", json.dumps({
                "source": source_name, "count": count,
                "elapsed_ms": elapsed, "status": "done"
            }))
            return results or []
        except Exception as exc:
            elapsed = int((time.time() - start_t) * 1000)
            logger.error("Sourcing search failed for {} on {}: {}", mpn, source_name, exc)
            await broker.publish(channel, "source-complete", json.dumps({
                "source": source_name, "count": 0,
                "elapsed_ms": elapsed, "status": "failed",
                "error": str(exc)
            }))
            return []

    results_by_source = await asyncio.gather(
        *[search_source(s) for s in sources],
        return_exceptions=True
    )

    for source_results in results_by_source:
        if isinstance(source_results, list):
            all_sightings.extend(source_results)

    await broker.publish(channel, "search-complete", json.dumps({
        "total": len(all_sightings),
        "requirement_id": requirement_id
    }))

    return HTMLResponse(
        status_code=200,
        headers={"HX-Redirect": f"/v2/sourcing/{requirement_id}"}
    )


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

    Supports filtering by confidence band, safety band, freshness window,
    source type, buyer status, contactability, and corroboration. Sorts by
    best overall (default), freshest, safest, easiest to contact, or most proven.
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
    ctx.update({
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
    })
    return templates.TemplateResponse("partials/sourcing/results.html", ctx)


@router.get("/v2/partials/sourcing/leads/{lead_id}", response_class=HTMLResponse)
async def lead_detail_partial(
    request: Request,
    lead_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return lead detail as HTML partial.

    Loads the SourcingLead, its evidence (sorted by confidence_impact desc),
    groups evidence by source category, fetches vendor card and best sighting.
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

    requirement = db.query(Requirement).filter(
        Requirement.id == lead.requirement_id
    ).first()

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
    ctx.update({
        "lead": lead,
        "evidence": evidence,
        "evidence_by_category": evidence_by_category,
        "category_labels": category_labels,
        "requirement": requirement,
        "vendor_card": vendor_card,
        "best_sighting": best_sighting,
    })
    return templates.TemplateResponse("partials/sourcing/lead_detail.html", ctx)


@router.post("/v2/partials/sourcing/leads/{lead_id}/status", response_class=HTMLResponse)
async def lead_status_update(
    request: Request,
    lead_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Update lead buyer status.

    Returns updated lead card when called from results view (for OOB swap),
    or updated lead detail when called from lead detail page.
    """
    from ..models.sourcing_lead import SourcingLead
    from ..services.sourcing_leads import update_lead_status

    form = await request.form()
    status_val = form.get("status", "").strip()
    note = form.get("note", "").strip() or None

    try:
        lead = update_lead_status(
            db, lead_id, status_val,
            note=note,
            actor_user_id=user.id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    if not lead:
        raise HTTPException(404, "Lead not found")

    referer = request.headers.get("HX-Current-URL", "")
    if "/leads/" in referer:
        return await lead_detail_partial(request, lead_id, user, db)

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
    return templates.TemplateResponse("partials/sourcing/lead_card.html", ctx)


@router.post("/v2/partials/sourcing/leads/{lead_id}/feedback", response_class=HTMLResponse)
async def lead_feedback(
    request: Request,
    lead_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add feedback event to a lead without changing status. Returns updated lead detail."""
    from ..services.sourcing_leads import append_lead_feedback

    form = await request.form()
    note = form.get("note", "").strip() or None
    reason_code = form.get("reason_code", "").strip() or None
    contact_method = form.get("contact_method", "").strip() or None

    lead = append_lead_feedback(
        db, lead_id,
        note=note,
        reason_code=reason_code,
        contact_method=contact_method,
        actor_user_id=user.id,
    )
    if not lead:
        raise HTTPException(404, "Lead not found")

    return await lead_detail_partial(request, lead_id, user, db)


# ── Materials partials ────────────────────────────────────────────────


@router.get("/v2/partials/materials", response_class=HTMLResponse)
async def materials_list_partial(
    request: Request,
    q: str = "",
    lifecycle: str = "",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return material cards list as HTML partial."""
    from ..models.intelligence import MaterialCard

    query = db.query(MaterialCard).filter(MaterialCard.deleted_at.is_(None))
    if q.strip():
        safe = escape_like(q.strip())
        query = query.filter(
            MaterialCard.normalized_mpn.ilike(f"%{safe.lower()}%")
            | MaterialCard.display_mpn.ilike(f"%{safe}%")
        )
    if lifecycle:
        query = query.filter(MaterialCard.lifecycle_status == lifecycle)
    total = query.count()
    materials = (
        query.order_by(MaterialCard.search_count.desc().nullslast(), MaterialCard.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    ctx = _base_ctx(request, user, "materials")
    ctx.update({
        "materials": materials,
        "q": q,
        "lifecycle": lifecycle,
        "total": total,
        "limit": limit,
        "offset": offset,
    })
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

    card = db.query(MaterialCard).filter(
        MaterialCard.id == card_id,
        MaterialCard.deleted_at.is_(None),
    ).first()
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


@router.put("/v2/partials/materials/{card_id}", response_class=HTMLResponse)
async def update_material_card(
    request: Request,
    card_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Update material card fields. Returns refreshed detail."""
    from ..models.intelligence import MaterialCard

    card = db.query(MaterialCard).filter(
        MaterialCard.id == card_id,
        MaterialCard.deleted_at.is_(None),
    ).first()
    if not card:
        raise HTTPException(404, "Material card not found")

    form = await request.form()
    updatable = [
        "manufacturer", "description", "category", "package_type",
        "lifecycle_status", "rohs_status", "pin_count",
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
    quote = db.query(Quote).options(
        joinedload(Quote.customer_site).joinedload(CustomerSite.company),
        joinedload(Quote.requisition),
        joinedload(Quote.created_by),
    ).filter(Quote.id == quote_id).first()
    if not quote:
        raise HTTPException(404, "Quote not found")
    lines = db.query(QuoteLine).filter(QuoteLine.quote_id == quote_id).all()
    offers = db.query(Offer).filter(
        Offer.requisition_id == quote.requisition_id
    ).order_by(Offer.created_at.desc()).all()
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
        line.margin_pct = round(
            (float(line.sell_price) - float(line.cost_price)) / float(line.sell_price) * 100, 2
        )
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
    """Add selected offers to an existing draft quote. Returns updated quote detail."""
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
        existing = db.query(QuoteLine).filter(
            QuoteLine.quote_id == quote_id, QuoteLine.offer_id == o.id
        ).first()
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
    subtotal = sum(float(l.sell_price or 0) * (l.qty or 1) for l in all_lines)
    total_cost = sum(float(l.cost_price or 0) * (l.qty or 1) for l in all_lines)
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
    """Build a buy plan from a won quote. Returns buy plan detail partial."""
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
        query = query.filter(
            ProspectAccount.name.ilike(f"%{safe}%")
            | ProspectAccount.domain.ilike(f"%{safe}%")
        )
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
    ctx.update({
        "prospects": prospects, "q": q, "status": status, "sort": sort,
        "page": page, "per_page": per_page, "total": total, "total_pages": total_pages,
    })
    return templates.TemplateResponse("htmx/partials/prospecting/list.html", ctx)


@router.get("/v2/partials/prospecting/{prospect_id}", response_class=HTMLResponse)
async def prospecting_detail_partial(
    request: Request,
    prospect_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return prospect detail as HTML partial."""
    prospect = db.query(ProspectAccount).options(
        joinedload(ProspectAccount.claimed_by_user),
    ).filter(ProspectAccount.id == prospect_id).first()
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
    prospect = db.query(ProspectAccount).options(
        joinedload(ProspectAccount.claimed_by_user),
    ).filter(ProspectAccount.id == prospect_id).first()
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
