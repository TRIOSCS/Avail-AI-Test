"""
routers/htmx/requisitions.py — HTMX partials for requisition management.

Handles all /partials/requisitions/ routes: listing, creating, detail view,
tab loading, bulk actions, offer review, RFQ compose/send, and requirement CRUD.

Called by: main.py (via router mount from _helpers)
Depends on: models, dependencies, database, rfq_compose_service, scoring
"""

from datetime import datetime

from fastapi import Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.orm import Session, joinedload, selectinload

from ...database import get_db
from ...dependencies import require_user
from ...models import (
    BuyPlan,
    Offer,
    Quote,
    QuoteLine,
    Requirement,
    Requisition,
    RequisitionTask,
    Sighting,
    User,
)
from ._helpers import _base_ctx, escape_like, router, templates

_DASH = "\u2014"


@router.get("/partials/requisitions", response_class=HTMLResponse)
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


@router.get("/partials/requisitions/create-form", response_class=HTMLResponse)
async def requisition_create_form(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the create requisition modal form."""
    ctx = _base_ctx(request, user, "requisitions")
    return templates.TemplateResponse("partials/requisitions/create_modal.html", ctx)


@router.get("/partials/requisitions/{req_id}", response_class=HTMLResponse)
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


@router.post("/partials/requisitions/create", response_class=HTMLResponse)
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


@router.post("/partials/requisitions/{req_id}/requirements", response_class=HTMLResponse)
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


@router.get("/partials/requisitions/{req_id}/tab/{tab}", response_class=HTMLResponse)
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
            .options(selectinload(Requirement.sightings))
            .filter(Requirement.requisition_id == req_id)
            .all()
        )
        for r in requirements:
            r.sighting_count = len(r.sightings) if r.sightings else 0
        ctx["requirements"] = requirements
        return templates.TemplateResponse("partials/requisitions/tabs/parts.html", ctx)

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
        return templates.TemplateResponse("partials/requisitions/tabs/offers.html", ctx)

    elif tab == "quotes":
        quotes = (
            db.query(Quote).filter(Quote.requisition_id == req_id).order_by(Quote.created_at.desc().nullslast()).all()
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
        from ...models.offers import VendorResponse

        responses = (
            db.query(VendorResponse)
            .filter(VendorResponse.requisition_id == req_id)
            .order_by(VendorResponse.received_at.desc().nullslast())
            .all()
        )
        ctx["responses"] = responses
        return templates.TemplateResponse("partials/requisitions/tabs/responses.html", ctx)

    else:  # activity
        from ...models.intelligence import ActivityLog
        from ...models.offers import Contact as RfqContact

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


@router.post("/partials/requisitions/bulk/{action}", response_class=HTMLResponse)
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
            try:
                owner_id_int = int(owner_id)
            except (ValueError, TypeError):
                raise HTTPException(400, "Invalid owner_id — must be a number")
            for r in reqs:
                r.created_by = owner_id_int

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


@router.post("/partials/requisitions/{req_id}/create-quote", response_class=HTMLResponse)
async def create_quote_from_offers(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a new quote from selected offer IDs. Returns quote detail partial."""
    form = await request.form()
    offer_ids_raw = form.getlist("offer_ids")
    try:
        offer_ids = [int(x) for x in offer_ids_raw if x]
    except (ValueError, TypeError):
        raise HTTPException(400, "Invalid offer ID — all values must be numbers")

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


@router.post("/partials/requisitions/{req_id}/offers/{offer_id}/review", response_class=HTMLResponse)
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


@router.post("/partials/requisitions/{req_id}/log-activity", response_class=HTMLResponse)
async def log_activity(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Log a manual activity (note/call/email) for a requisition."""
    from ...models.intelligence import ActivityLog

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


@router.get("/partials/requisitions/{req_id}/rfq-compose", response_class=HTMLResponse)
async def rfq_compose(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the RFQ compose form for a requisition."""

    from ...services.rfq_compose_service import build_rfq_vendor_list

    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    if not req:
        raise HTTPException(404, "Requisition not found")

    parts = db.query(Requirement).filter(Requirement.requisition_id == req_id).all()
    vendors = build_rfq_vendor_list(db, req_id)

    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req
    ctx["parts"] = parts
    ctx["vendors"] = vendors
    return templates.TemplateResponse("partials/requisitions/rfq_compose.html", ctx)


@router.post("/partials/requisitions/{req_id}/rfq-send", response_class=HTMLResponse)
async def rfq_send(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Send RFQs — creates Contact records for each selected vendor."""
    from ...services.rfq_compose_service import create_rfq_contacts

    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    if not req:
        raise HTTPException(404, "Requisition not found")

    form = await request.form()
    vendor_names = form.getlist("vendor_names")
    vendor_emails = form.getlist("vendor_emails")
    subject = form.get("subject", f"RFQ - {req.name}")
    parts_text = form.get("parts_summary", "")

    if not vendor_names:
        raise HTTPException(400, "No vendors selected")

    sent = create_rfq_contacts(db, req_id, user.id, vendor_names, vendor_emails, subject, parts_text)
    db.commit()

    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req
    ctx["sent_results"] = sent
    ctx["total_sent"] = len(sent)
    return templates.TemplateResponse("partials/requisitions/rfq_results.html", ctx)


@router.delete("/partials/requisitions/{req_id}/requirements/{item_id}", response_class=HTMLResponse)
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
    item = db.query(Requirement).filter(Requirement.id == item_id, Requirement.requisition_id == req_id).first()
    if not item:
        raise HTTPException(404, "Requirement not found")
    db.delete(item)
    db.commit()
    return HTMLResponse("")


@router.put("/partials/requisitions/{req_id}/requirements/{item_id}", response_class=HTMLResponse)
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
    return templates.TemplateResponse("partials/requisitions/tabs/req_row.html", ctx)
