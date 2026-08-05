"""routers/htmx/offers/crud.py — Offer parse/CRUD/review/promote partials (HTMX +
Alpine).

Server-rendered HTML partials for the offer lifecycle: AI offer parsing
(parse-email/paste/parse-offer/save), quote-from-offers, offer CRUD (add/edit/
reconfirm/delete/mark-sold), and the review-queue promote/reject/changelog flow.
Split out of the monolithic offers.py (P4.3) along the offer-CRUD seam. All offer
business logic lives in app/services/offer_service.py (W3 "one offer_service",
spec §9) — these handlers only adapt form input and re-render partials.

Called by: app/routers/htmx/offers/__init__.py (router mount).
Depends on: app.models, app.dependencies, app.database, app.services.offer_service,
    .._shared (_safe_int/_safe_float/_parse_date_safe), .._shared_tabs
    (requisition_tab — every offer route re-renders the requisition offers tab),
    app.routers._lookup_helpers.
"""

from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse
from loguru import logger
from pydantic import ValidationError
from sqlalchemy.orm import Session, joinedload

from ....constants import (
    AccessKey,
    OfferCondition,
    OfferStatus,
    QuoteStatus,
)
from ....database import get_db
from ....dependencies import require_access, require_requisition_access, require_user
from ....models import Offer, Quote, QuoteLine, Requirement, User
from ....models.intelligence import ChangeLog
from ....schemas.crm import OfferCreate, OfferUpdate
from ....services import offer_service
from ....services.ai_offer_service import parse_offer_form_rows, save_form_parsed_offers
from ..._lookup_helpers import get_requisition_or_404
from .._shared import _base_ctx, _parse_date_safe, _safe_float, _safe_int

router = APIRouter(tags=["htmx-views"])


@router.get("/v2/partials/requisitions/{req_id}/parse-email-form", response_class=HTMLResponse)
async def parse_email_form(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the parse-email paste form."""
    from . import template_response

    req = get_requisition_or_404(db, req_id)
    require_requisition_access(db, req_id, user)
    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req
    return template_response("htmx/partials/requisitions/tabs/parse_email_form.html", ctx)


@router.get("/v2/partials/requisitions/{req_id}/paste-offer-form", response_class=HTMLResponse)
async def paste_offer_form(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the paste-offer freeform form."""
    from . import template_response

    req = get_requisition_or_404(db, req_id)
    require_requisition_access(db, req_id, user)
    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req
    return template_response("htmx/partials/requisitions/tabs/paste_offer_form.html", ctx)


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
    from . import template_response

    req = get_requisition_or_404(db, req_id)
    require_requisition_access(db, req_id, user)

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

    return template_response("htmx/partials/requisitions/tabs/parsed_email_results.html", ctx)


@router.post("/v2/partials/requisitions/{req_id}/parse-offer", response_class=HTMLResponse)
async def parse_offer_action(
    request: Request,
    req_id: int,
    raw_text: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Parse freeform vendor text and return editable offer cards."""
    from . import template_response

    req = get_requisition_or_404(db, req_id)
    require_requisition_access(db, req_id, user)

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

    return template_response("htmx/partials/requisitions/tabs/parsed_offer_results.html", ctx)


@router.post("/v2/partials/requisitions/{req_id}/save-parsed-offers", response_class=HTMLResponse)
async def save_parsed_offers(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save user-edited parsed offers to the requisition.

    Business logic (form-array parsing, MPN→requirement matching, VendorCard
    lookup/creation, Offer construction) lives in app.services.ai_offer_service
    (parse_offer_form_rows / save_form_parsed_offers) — this route stays HTTP-only
    (P4.2).
    """
    from . import template_response

    req = get_requisition_or_404(db, req_id)
    require_requisition_access(db, req_id, user)

    form = await request.form()
    vendor_name = form.get("vendor_name", "")
    offers_data = parse_offer_form_rows(form, vendor_name)

    if not offers_data:
        return HTMLResponse(
            '<div class="p-4 text-center text-sm text-amber-600 bg-amber-50 rounded-lg border border-amber-200">'
            "No offers to save.</div>"
        )

    saved_count = await save_form_parsed_offers(db, req_id, vendor_name, offers_data, user)

    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req
    ctx["saved_count"] = saved_count
    return template_response("htmx/partials/requisitions/tabs/parse_save_success.html", ctx)


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
    from . import template_response

    form = await request.form()
    offer_ids_raw = form.getlist("offer_ids")
    try:
        offer_ids = [int(x) for x in offer_ids_raw if x]
    except (ValueError, TypeError) as e:
        raise HTTPException(400, "offer_ids must be integers") from e

    if not offer_ids:
        raise HTTPException(400, "No offers selected")

    req = get_requisition_or_404(db, req_id)
    require_requisition_access(db, req_id, user)

    offers = db.query(Offer).filter(Offer.id.in_(offer_ids), Offer.requisition_id == req_id).all()
    if not offers:
        raise HTTPException(404, "No matching offers found")

    # Build line items from offers

    quote_number = f"Q-{req_id}-{db.query(Quote).filter(Quote.requisition_id == req_id).count() + 1}"
    quote = Quote(
        requisition_id=req_id,
        quote_number=quote_number,
        status=QuoteStatus.DRAFT,
        created_by_id=user.id,
        customer_site_id=req.customer_site_id,
    )
    db.add(quote)
    db.flush()

    subtotal = 0.0
    total_cost = 0.0
    line_items = []  # canonical JSON the email/PDF render from (quote_send.py:220)
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
        # Mirror each line into quote.line_items in the shape quote_builder_service
        # emits — otherwise the sent email / PDF render an EMPTY line-item table
        # (they read quote.line_items, not the QuoteLine rows).
        line_items.append(
            {
                "mpn": o.mpn or "",
                "manufacturer": o.manufacturer or "",
                "qty": qty,
                "cost_price": cost_price,
                "sell_price": sell_price,
                "margin_pct": margin_pct,
                "lead_time": o.lead_time,
                "date_code": o.date_code,
                "condition": o.condition,
                "packaging": o.packaging,
                "moq": o.moq,
                "offer_id": o.id,
            }
        )
        subtotal += sell_price * qty
        total_cost += cost_price * qty

    quote.line_items = line_items
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
    return template_response("htmx/partials/quotes/detail.html", ctx)


@router.post("/v2/partials/requisitions/{req_id}/offers/{offer_id}/review", response_class=HTMLResponse)
async def review_offer(
    request: Request,
    req_id: int,
    offer_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Approve or reject a pending_review offer — approve lands on ACTIVE (the ONE
    approve landing status, W3).

    Returns refreshed offers tab.
    """
    from . import requisition_tab

    form = await request.form()
    action = form.get("action", "")

    if action not in ("approve", "reject"):
        raise HTTPException(400, "Invalid action")

    require_requisition_access(db, req_id, user)
    offer = db.query(Offer).filter(Offer.id == offer_id, Offer.requisition_id == req_id).first()
    if not offer:
        raise HTTPException(404, "Offer not found")

    if action == "approve":
        offer_service.approve_offer(db, offer, user)
    else:
        offer_service.reject_offer(db, offer, user)
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
    from . import template_response

    req = get_requisition_or_404(db, req_id)
    require_requisition_access(db, req_id, user)
    requirements = db.query(Requirement).filter(Requirement.requisition_id == req_id).all()
    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req
    ctx["requirements"] = requirements
    return template_response("htmx/partials/requisitions/add_offer_form.html", ctx)


@router.post("/v2/partials/requisitions/{req_id}/add-offer", response_class=HTMLResponse)
async def add_offer(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a manual offer via the canonical offer_service.create_offer pipeline and
    return refreshed offers tab."""
    from . import requisition_tab

    get_requisition_or_404(db, req_id)  # validates existence
    require_requisition_access(db, req_id, user)

    form = await request.form()
    vendor_name = (form.get("vendor_name") or "").strip()
    mpn = (form.get("mpn") or "").strip()
    if not vendor_name or not mpn:
        return HTMLResponse(
            '<div class="p-3 text-sm text-rose-600 bg-rose-50 rounded mb-4">Vendor name and MPN are required.</div>',
            status_code=400,
        )

    _qkeys = (
        "usage",
        "refurbished_by",
        "refurb_process",
        "cert_doc",
        "part_condition",
        "provenance_story",
        "terms",
        "lead_time_reason",
    )
    qual = {k: (form.get(k) or None) for k in _qkeys}
    try:
        payload = OfferCreate(
            mpn=mpn,
            vendor_name=vendor_name,
            requirement_id=_safe_int(form.get("requirement_id")),
            manufacturer=form.get("manufacturer") or None,
            qty_available=_safe_int(form.get("qty_available")),
            unit_price=_safe_float(form.get("unit_price")),
            lead_time=form.get("lead_time") or None,
            date_code=form.get("date_code") or None,
            condition=form.get("condition") or OfferCondition.NEW,
            packaging=form.get("packaging") or None,
            firmware=form.get("firmware") or None,
            hardware_code=form.get("hardware_code") or None,
            moq=_safe_int(form.get("moq")),
            spq=_safe_int(form.get("spq")),
            warranty=form.get("warranty") or None,
            country_of_origin=form.get("country_of_origin") or None,
            valid_until=_parse_date_safe(form.get("valid_until"), date),
            notes=form.get("notes") or None,
            source="manual",
            qualification=qual if any(qual.values()) else None,
        )
    except ValidationError as e:
        # Surface as a 422 (not a 500) so a bad numeric/date is reported, not crashed.
        raise RequestValidationError(e.errors()) from e

    await offer_service.create_offer(db, req_id, payload, user)
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
    from . import requisition_tab

    require_requisition_access(db, req_id, user)
    offer = db.query(Offer).filter(Offer.id == offer_id, Offer.requisition_id == req_id).first()
    if not offer:
        raise HTTPException(404, "Offer not found")

    offer_service.reconfirm_offer(db, offer, user)

    return await requisition_tab(request=request, req_id=req_id, tab="offers", user=user, db=db)


@router.get("/v2/partials/requisitions/{req_id}/offers/{offer_id}/edit-form", response_class=HTMLResponse)
async def edit_offer_form(
    request: Request,
    req_id: int,
    offer_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return inline edit form for an existing offer."""
    from . import template_response

    require_requisition_access(db, req_id, user)
    offer = db.query(Offer).filter(Offer.id == offer_id, Offer.requisition_id == req_id).first()
    if not offer:
        raise HTTPException(404, "Offer not found")
    requirements = db.query(Requirement).filter(Requirement.requisition_id == req_id).all()
    return template_response(
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
    """Save edits to an offer via offer_service.update_offer and return refreshed offers
    tab.

    Only non-empty submitted values are applied (a blank field never clears a stored
    value — the historical semantics of this form).
    """
    from . import requisition_tab

    require_requisition_access(db, req_id, user)
    offer = db.query(Offer).filter(Offer.id == offer_id, Offer.requisition_id == req_id).first()
    if not offer:
        raise HTTPException(404, "Offer not found")

    form = await request.form()
    kwargs: dict = {}
    for field in (
        "vendor_name",
        "lead_time",
        "condition",
        "date_code",
        "notes",
        "manufacturer",
        "packaging",
        "firmware",
        "hardware_code",
        "warranty",
        "country_of_origin",
    ):
        val = (form.get(field) or "").strip()
        if val:
            kwargs[field] = val
    for field in ("qty_available", "moq", "spq"):
        val = (form.get(field) or "").strip()
        if val:
            try:
                kwargs[field] = int(val)
            except ValueError:
                continue
    val = (form.get("unit_price") or "").strip()
    if val:
        try:
            kwargs["unit_price"] = float(val)
        except ValueError:
            pass
    val = (form.get("valid_until") or "").strip()
    if val:
        try:
            kwargs["valid_until"] = date.fromisoformat(val)
        except ValueError:
            pass

    req_id_val = form.get("requirement_id", "")
    if req_id_val:
        kwargs["requirement_id"] = int(req_id_val) if req_id_val.isdigit() else None

    _qkeys = (
        "usage",
        "refurbished_by",
        "refurb_process",
        "cert_doc",
        "part_condition",
        "provenance_story",
        "terms",
        "lead_time_reason",
    )
    submitted_qual = {k: (form.get(k) or None) for k in _qkeys}
    if any(submitted_qual.values()):
        merged = dict(offer.qualification or {})
        merged.update(submitted_qual)
        merged.setdefault("requests", [])
        merged["schema"] = 1  # forward-version the qualification blob (spec §3.1)
        kwargs["qualification"] = merged

    try:
        payload = OfferUpdate(**kwargs)
    except ValidationError as e:
        # Surface as a 422 (not a 500) so a bad constrained value is reported.
        raise RequestValidationError(e.errors()) from e

    offer_service.update_offer(db, offer, payload, user)
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
    from . import requisition_tab

    require_requisition_access(db, req_id, user)
    offer = db.query(Offer).filter(Offer.id == offer_id, Offer.requisition_id == req_id).first()
    if not offer:
        raise HTTPException(404, "Offer not found")
    offer_service.delete_offer(db, offer, user)

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
    from . import requisition_tab

    require_requisition_access(db, req_id, user)
    offer = db.query(Offer).filter(Offer.id == offer_id, Offer.requisition_id == req_id).first()
    if not offer:
        raise HTTPException(404, "Offer not found")
    if offer_service.mark_offer_sold(db, offer, user):
        logger.info("Offer {} marked sold by {}", offer_id, user.email)

    return await requisition_tab(request=request, req_id=req_id, tab="offers", user=user, db=db)


@router.get("/v2/partials/offers/review-queue", response_class=HTMLResponse)
async def offer_review_queue(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the offer review queue page — medium-confidence AI-parsed offers."""
    from . import template_response

    offers = (
        db.query(Offer)
        .filter(Offer.status == OfferStatus.PENDING_REVIEW)
        .order_by(Offer.created_at.desc())
        .limit(100)
        .all()
    )
    return template_response(
        "htmx/partials/offers/review_queue.html",
        {"request": request, "offers": offers, "user": user},
    )


@router.post("/v2/partials/offers/{offer_id}/promote", response_class=HTMLResponse)
async def promote_offer_htmx(
    request: Request,
    offer_id: int,
    user: User = Depends(require_access(AccessKey.APPROVE_OFFERS)),
    db: Session = Depends(get_db),
):
    """Promote a pending_review offer to active and return refreshed queue.

    Promote IS approve — same pending→ACTIVE transition, one implementation
    (offer_service.approve_offer).
    """
    from . import offer_review_queue as _offer_review_queue

    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    require_requisition_access(db, offer.requisition_id, user, owner_id=offer.entered_by_id, label="Offer")

    offer_service.approve_offer(db, offer, user)
    logger.info("Offer {} promoted by {}", offer_id, user.email)

    return await _offer_review_queue(request=request, user=user, db=db)


@router.post("/v2/partials/offers/{offer_id}/reject", response_class=HTMLResponse)
async def reject_offer_htmx(
    request: Request,
    offer_id: int,
    user: User = Depends(require_access(AccessKey.APPROVE_OFFERS)),
    db: Session = Depends(get_db),
):
    """Reject a pending_review offer and return refreshed queue."""
    from . import offer_review_queue as _offer_review_queue

    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    require_requisition_access(db, offer.requisition_id, user, owner_id=offer.entered_by_id, label="Offer")

    offer_service.reject_offer(db, offer, user)
    logger.info("Offer {} rejected by {}", offer_id, user.email)

    return await _offer_review_queue(request=request, user=user, db=db)


@router.get("/v2/partials/offers/{offer_id}/changelog", response_class=HTMLResponse)
async def offer_changelog(
    request: Request,
    offer_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render change history for an offer."""
    from . import template_response

    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    require_requisition_access(db, offer.requisition_id, user, owner_id=offer.entered_by_id, label="Offer")
    rows = (
        db.query(ChangeLog)
        .filter(ChangeLog.entity_type == "offer", ChangeLog.entity_id == offer_id)
        .options(joinedload(ChangeLog.user))
        .order_by(ChangeLog.created_at.desc())
        .limit(50)
        .all()
    )
    return template_response(
        "htmx/partials/offers/changelog.html",
        {"request": request, "offer": offer, "changes": rows},
    )
