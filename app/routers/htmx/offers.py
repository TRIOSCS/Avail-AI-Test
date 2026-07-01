"""routers/htmx/offers.py — Offer / RFQ / follow-up partial views (HTMX + Alpine).

Server-rendered HTML partials for the deal-execution surface: AI offer parsing
(parse-email/paste/parse-offer/save), offer CRUD + review/promote/reject/changelog,
quote-from-offers, activity logging, RFQ compose/cleanup/rephrase/send, follow-ups
(list/send/ai-draft/batch/badge), and vendor-response review/reply. Extracted
verbatim from htmx_views.py (same `/v2/partials` paths, same `htmx-views` tag).

Called by: app/main.py (router mount).
Depends on: app.models, app.dependencies, app.database, app.services, ._shared
    (_safe_int/_safe_float), .requisitions (requisition_tab — every offer route
    re-renders the requisition offers tab).
"""

import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session, joinedload, selectinload

from ...constants import (
    RESTRICTED_ROLES,
    AccessKey,
    ActivityType,
    AttributionStatus,
    ContactStatus,
    OfferStatus,
    QuoteStatus,
    UserRole,
    VendorResponseStatus,
)
from ...database import get_db
from ...dependencies import (
    require_access,
    require_requisition_access,
    require_user,
)
from ...models import (
    Offer,
    Quote,
    QuoteLine,
    Requirement,
    Requisition,
    Sighting,
    SiteContact,
    User,
    VendorCard,
)
from ...services.activity_service import log_activity as _log_activity
from ...services.status_machine import require_valid_transition
from ...services.vendor_unavailability import maybe_release_on_offer
from ...template_env import template_response
from .._lookup_helpers import get_requisition_or_404
from ._shared import _base_ctx, _parse_date_safe, _safe_float, _safe_int
from .requisitions import requisition_tab

router = APIRouter(tags=["htmx-views"])


@router.get("/v2/partials/requisitions/{req_id}/parse-email-form", response_class=HTMLResponse)
async def parse_email_form(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the parse-email paste form."""
    req = get_requisition_or_404(db, req_id)
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
    req = get_requisition_or_404(db, req_id)
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
    """Save user-edited parsed offers to the requisition."""
    req = get_requisition_or_404(db, req_id)
    require_requisition_access(db, req_id, user)

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
            status=OfferStatus.ACTIVE,
        )
        db.add(offer)
        # Offer hook: the user reviewed and saved this parse ACTIVE — user-initiated
        # proof of availability, release the vendor's matching active records.
        maybe_release_on_offer(db, req_match_id, offer.vendor_name, user, offer_condition=offer.condition)
        saved_count += 1

    db.commit()

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
    form = await request.form()
    offer_ids_raw = form.getlist("offer_ids")
    try:
        offer_ids = [int(x) for x in offer_ids_raw if x]
    except (ValueError, TypeError):
        raise HTTPException(400, "offer_ids must be integers")

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
    return template_response("htmx/partials/quotes/detail.html", ctx)


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

    require_requisition_access(db, req_id, user)
    offer = db.query(Offer).filter(Offer.id == offer_id, Offer.requisition_id == req_id).first()
    if not offer:
        raise HTTPException(404, "Offer not found")

    old_status = offer.status

    if action == "approve":
        require_valid_transition("offer", offer.status, OfferStatus.APPROVED)
        offer.status = OfferStatus.APPROVED
        offer.approved_by_id = user.id

        offer.approved_at = datetime.now(timezone.utc)
        # Offer hook: user approval of a pending offer is user-initiated proof of
        # availability — release the vendor's matching active unavailability records.
        maybe_release_on_offer(db, offer.requirement_id, offer.vendor_name, user, offer_condition=offer.condition)
    else:
        require_valid_transition("offer", offer.status, OfferStatus.REJECTED)
        offer.status = OfferStatus.REJECTED

    _log_activity(
        db,
        activity_type=ActivityType.OFFER_STATUS_CHANGED,
        requisition_id=offer.requisition_id,
        user_id=user.id,
        vendor_card_id=offer.vendor_card_id,
        description=f"Offer {offer.vendor_name} status: {old_status} → {offer.status}",
        details={
            "offer_id": offer.id,
            "old_status": str(old_status),
            "new_status": str(offer.status),
        },
    )

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
    req = get_requisition_or_404(db, req_id)
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
    """Create a manual offer and return refreshed offers tab."""
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

    from datetime import date as date_type

    from ...services.offer_qualification import (
        apply_qualification,
        normalize_offer_condition,
    )
    from ...utils.normalization import normalize_mpn_key
    from ...vendor_utils import normalize_vendor_name

    offer = Offer(
        requisition_id=req_id,
        vendor_name=vendor_name,
        vendor_name_normalized=normalize_vendor_name(vendor_name),
        mpn=mpn,
        # Canonical dedup key (dash-stripped) so the part-centric offers query
        # matches consistently with create_offer's normalize_mpn_key.
        normalized_mpn=normalize_mpn_key(mpn),
        qty_available=_safe_int(form.get("qty_available")),
        unit_price=_safe_float(form.get("unit_price")),
        lead_time=form.get("lead_time") or None,
        date_code=form.get("date_code") or None,
        condition=normalize_offer_condition(form.get("condition")) or form.get("condition") or None,
        moq=_safe_int(form.get("moq")),
        manufacturer=form.get("manufacturer") or None,
        spq=_safe_int(form.get("spq")),
        packaging=form.get("packaging") or None,
        firmware=form.get("firmware") or None,
        hardware_code=form.get("hardware_code") or None,
        warranty=form.get("warranty") or None,
        country_of_origin=form.get("country_of_origin") or None,
        valid_until=_parse_date_safe(form.get("valid_until"), date_type),
        notes=form.get("notes") or None,
        requirement_id=_safe_int(form.get("requirement_id")),
        source="manual",
        status=OfferStatus.ACTIVE,
        entered_by_id=user.id,
        created_at=datetime.now(timezone.utc),
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
    qual["requests"] = []
    qual["schema"] = 1  # forward-version the qualification blob (spec §3.1)
    offer.qualification = qual if any(qual[k] for k in _qkeys) else None
    apply_qualification(offer)  # non-raising: composes note + sets status
    db.add(offer)
    db.flush()  # offer.id populated; activity row + offer committed together below
    # Offer hook: a manually entered offer is user-initiated proof of availability —
    # release the vendor's matching active unavailability records.
    maybe_release_on_offer(db, offer.requirement_id, offer.vendor_name, user, offer_condition=offer.condition)
    logger.info("Manual offer created: {} on req {} by {}", mpn, req_id, user.email)

    _log_activity(
        db,
        activity_type=ActivityType.OFFER_CREATED,
        requisition_id=offer.requisition_id,
        requirement_id=offer.requirement_id,
        user_id=user.id,
        vendor_card_id=offer.vendor_card_id,
        description=f"Offer added: {offer.vendor_name} — {offer.mpn}",
        details={"offer_id": offer.id, "source": offer.source},
    )
    db.commit()

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
    require_requisition_access(db, req_id, user)
    offer = db.query(Offer).filter(Offer.id == offer_id, Offer.requisition_id == req_id).first()
    if not offer:
        raise HTTPException(404, "Offer not found")

    now = datetime.now(timezone.utc)
    offer.reconfirmed_at = now
    offer.reconfirm_count = (offer.reconfirm_count or 0) + 1
    offer.expires_at = now + timedelta(days=14)
    offer.attribution_status = AttributionStatus.ACTIVE
    offer.is_stale = False
    offer.updated_at = now
    offer.updated_by_id = user.id
    db.commit()
    logger.info("Offer {} reconfirmed (count={}) by {}", offer_id, offer.reconfirm_count, user.email)

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
    """Save edits to an offer and return refreshed offers tab."""
    from ...models.intelligence import ChangeLog

    require_requisition_access(db, req_id, user)
    offer = db.query(Offer).filter(Offer.id == offer_id, Offer.requisition_id == req_id).first()
    if not offer:
        raise HTTPException(404, "Offer not found")

    form = await request.form()
    trackable = [
        "vendor_name",
        "qty_available",
        "unit_price",
        "lead_time",
        "condition",
        "date_code",
        "moq",
        "notes",
        "manufacturer",
        "spq",
        "packaging",
        "firmware",
        "hardware_code",
        "warranty",
        "country_of_origin",
        "valid_until",
    ]
    now = datetime.now(timezone.utc)

    for field in trackable:
        new_val = form.get(field, "").strip()
        old_val = str(getattr(offer, field) or "")
        if new_val != old_val and new_val:
            if field in ("qty_available", "moq", "spq"):
                try:
                    setattr(offer, field, int(new_val))
                except ValueError:
                    continue
            elif field == "unit_price":
                try:
                    setattr(offer, field, float(new_val))
                except ValueError:
                    continue
            elif field == "valid_until":
                from datetime import date as date_type

                try:
                    setattr(offer, field, date_type.fromisoformat(new_val) if new_val else None)
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

    from ...services.offer_qualification import (
        apply_qualification,
        normalize_offer_condition,
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
    submitted_qual = {k: (form.get(k) or None) for k in _qkeys}
    if any(submitted_qual.values()):
        merged = dict(offer.qualification or {})
        merged.update(submitted_qual)
        merged.setdefault("requests", [])
        merged["schema"] = 1  # forward-version the qualification blob (spec §3.1)
        offer.qualification = merged
    cond_raw = form.get("condition", "").strip()
    if cond_raw:
        offer.condition = normalize_offer_condition(cond_raw) or cond_raw

    apply_qualification(offer)  # non-raising: composes note + sets status
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
    require_requisition_access(db, req_id, user)
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
    from ...models.intelligence import ChangeLog

    require_requisition_access(db, req_id, user)
    offer = db.query(Offer).filter(Offer.id == offer_id, Offer.requisition_id == req_id).first()
    if not offer:
        raise HTTPException(404, "Offer not found")
    if offer.status == OfferStatus.SOLD:
        return await requisition_tab(request=request, req_id=req_id, tab="offers", user=user, db=db)

    old_status = offer.status
    require_valid_transition("offer", offer.status, OfferStatus.SOLD)
    offer.status = OfferStatus.SOLD
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

    _log_activity(
        db,
        activity_type=ActivityType.OFFER_STATUS_CHANGED,
        requisition_id=offer.requisition_id,
        user_id=user.id,
        vendor_card_id=offer.vendor_card_id,
        description=f"Offer {offer.vendor_name} status: {old_status} → {offer.status}",
        details={
            "offer_id": offer.id,
            "old_status": str(old_status),
            "new_status": str(offer.status),
        },
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
    """Promote a pending_review offer to active and return refreshed queue."""
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    require_requisition_access(db, offer.requisition_id, user, owner_id=offer.entered_by_id, label="Offer")
    if offer.status != OfferStatus.PENDING_REVIEW:
        raise HTTPException(400, "Only pending_review offers can be promoted")

    old_status = offer.status
    require_valid_transition("offer", offer.status, OfferStatus.ACTIVE)
    offer.status = OfferStatus.ACTIVE
    offer.approved_by_id = user.id
    offer.approved_at = datetime.now(timezone.utc)
    offer.updated_at = datetime.now(timezone.utc)
    offer.updated_by_id = user.id

    # Offer hook: user approval of a pending offer is user-initiated proof of
    # availability — release the vendor's matching active unavailability records.
    maybe_release_on_offer(db, offer.requirement_id, offer.vendor_name, user, offer_condition=offer.condition)

    _log_activity(
        db,
        activity_type=ActivityType.OFFER_STATUS_CHANGED,
        requisition_id=offer.requisition_id,
        user_id=user.id,
        vendor_card_id=offer.vendor_card_id,
        description=f"Offer {offer.vendor_name} status: {old_status} → {offer.status}",
        details={
            "offer_id": offer.id,
            "old_status": str(old_status),
            "new_status": str(offer.status),
        },
    )

    db.commit()
    logger.info("Offer {} promoted by {}", offer_id, user.email)

    return await offer_review_queue(request=request, user=user, db=db)


@router.post("/v2/partials/offers/{offer_id}/reject", response_class=HTMLResponse)
async def reject_offer_htmx(
    request: Request,
    offer_id: int,
    user: User = Depends(require_access(AccessKey.APPROVE_OFFERS)),
    db: Session = Depends(get_db),
):
    """Reject a pending_review offer and return refreshed queue."""
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    require_requisition_access(db, offer.requisition_id, user, owner_id=offer.entered_by_id, label="Offer")
    if offer.status != OfferStatus.PENDING_REVIEW:
        raise HTTPException(400, "Only pending_review offers can be rejected")

    old_status = offer.status
    require_valid_transition("offer", offer.status, OfferStatus.REJECTED)
    offer.status = OfferStatus.REJECTED
    offer.updated_at = datetime.now(timezone.utc)
    offer.updated_by_id = user.id

    _log_activity(
        db,
        activity_type=ActivityType.OFFER_STATUS_CHANGED,
        requisition_id=offer.requisition_id,
        user_id=user.id,
        vendor_card_id=offer.vendor_card_id,
        description=f"Offer {offer.vendor_name} status: {old_status} → {offer.status}",
        details={
            "offer_id": offer.id,
            "old_status": str(old_status),
            "new_status": str(offer.status),
        },
    )

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
    from ...models.intelligence import ChangeLog

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
    return template_response(
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
    from ...models.intelligence import ActivityLog

    get_requisition_or_404(db, req_id)  # validates existence
    require_requisition_access(db, req_id, user)

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
    from ...models.offers import Contact as RfqContact

    req = get_requisition_or_404(db, req_id)

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
            (
                db.query(VendorCard)
                .options(selectinload(VendorCard.vendor_contacts))
                .filter(VendorCard.normalized_name.in_(norm_names))
                .limit(50)
                .all()
            )
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
            v_contacts = v.vendor_contacts[:5]  # Already eagerly loaded
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
    return template_response("htmx/partials/requisitions/rfq_compose.html", ctx)


@router.post("/v2/partials/requisitions/{req_id}/ai-cleanup-email", response_class=HTMLResponse)
async def ai_cleanup_email(
    request: Request,
    req_id: int,
    body: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Clean up user-written email — fix grammar, tone, and formatting."""
    get_requisition_or_404(db, req_id)  # validates existence
    require_requisition_access(db, req_id, user)

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
        logger.error("AI cleanup error for req {}: {}", req_id, exc)
        cleaned = user_text

    # Return a script that replaces the textarea content with the cleaned text
    escaped = cleaned.replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$").replace("</", "<\\/")
    return HTMLResponse(
        f'<script>document.getElementById("rfq-body-textarea").value = `{escaped}`;</script>'
        '<p class="text-xs text-green-600 mt-1">Email cleaned up. Review and edit as needed.</p>'
    )


@router.post("/v2/partials/requisitions/{req_id}/ai-rephrase-email", response_class=HTMLResponse)
async def ai_rephrase_email(
    request: Request,
    req_id: int,
    body: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Rephrase the RFQ email so each send reads uniquely, keeping all parts intact."""
    get_requisition_or_404(db, req_id)  # validates existence
    require_requisition_access(db, req_id, user)

    user_text = body.strip()
    if not user_text:
        return HTMLResponse(
            '<p class="text-xs text-amber-600 mt-1">Write your email first, then click AI Rephrase.</p>'
        )

    from app.services.email_drafting import draft_email

    result = await draft_email("rfq_rephrase", {"body": user_text})
    rephrased = (result or {}).get("body") or user_text

    # Return a script that replaces the textarea content with the rephrased text.
    escaped = rephrased.replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$").replace("</", "<\\/")
    return HTMLResponse(
        f'<script>document.getElementById("rfq-body-textarea").value = `{escaped}`;</script>'
        '<p class="text-xs text-green-600 mt-1">Rephrased. Review and edit as needed.</p>'
    )


@router.post("/v2/partials/requisitions/{req_id}/rfq-send", response_class=HTMLResponse)
async def rfq_send(
    request: Request,
    req_id: int,
    user: User = Depends(require_access(AccessKey.SEND_RFQ)),
    db: Session = Depends(get_db),
):
    """Send RFQs via Graph API, falling back to DB-only in test mode."""

    from ...models.offers import Contact as RfqContact

    req = get_requisition_or_404(db, req_id)
    require_requisition_access(db, req_id, user)

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
            from ...dependencies import require_fresh_token

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
                from ...email_service import send_batch_rfq

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
                        status=ContactStatus.PENDING,
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
                status=ContactStatus.SENT,
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
    return template_response("htmx/partials/requisitions/rfq_results.html", ctx)


@router.get("/v2/partials/follow-ups", response_class=HTMLResponse)
async def follow_ups_list_partial(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Cross-requisition follow-up queue as HTML partial."""
    from ...config import settings as cfg
    from ...models.offers import Contact as RfqContact

    threshold_days = getattr(cfg, "follow_up_days", 2)
    threshold = datetime.now() - __import__("datetime").timedelta(days=threshold_days)

    stale_q = db.query(RfqContact).filter(
        RfqContact.contact_type == "email",
        RfqContact.status.in_(["sent", "opened"]),
        RfqContact.created_at < threshold,
    )
    if getattr(user, "role", None) in (UserRole.SALES, UserRole.TRADER):
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
        ca = c.created_at if c.created_at else now
        days_waiting = (now - ca).days
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
    return template_response("htmx/partials/follow_ups/list.html", ctx)


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

    from ...models.offers import Contact as RfqContact

    contact = db.get(RfqContact, contact_id)
    if not contact:
        raise HTTPException(404, "Contact not found")
    require_requisition_access(db, contact.requisition_id, user, owner_id=contact.user_id, label="Contact")

    form = await request.form()
    body = (form.get("body") or "").strip()

    # DNC hard-block — never email a do-not-contact vendor (checked in all modes,
    # before the TESTING gate), mirroring send_reply_htmx / send_batch_rfq.
    if contact.vendor_contact:
        dnc = (
            db.query(SiteContact)
            .filter(
                sqlfunc.lower(SiteContact.email) == contact.vendor_contact.lower(),
                SiteContact.do_not_contact.is_(True),
            )
            .first()
        )
        if dnc:
            logger.warning(
                "Follow-up skipped — do-not-contact flag set for vendor '{}' ({})",
                contact.vendor_name,
                contact.vendor_contact,
            )
            return HTMLResponse(
                '<div class="rounded bg-rose-50 border border-rose-200 text-rose-700 text-xs px-2 py-1.5">'
                "This vendor is on the do-not-contact list — follow-up not sent.</div>"
            )

    is_testing = os.environ.get("TESTING") == "1"
    email_sent = False

    if not is_testing and contact.vendor_contact:
        # Try to send real follow-up via Graph API
        try:
            from ...dependencies import require_fresh_token

            token = await require_fresh_token(request, db)

            from ...utils.graph_client import GraphClient

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

    if email_sent or is_testing:
        contact.status = ContactStatus.SENT
        contact.status_updated_at = datetime.now(tz.utc)
        db.commit()

    mode = "via Graph API" if email_sent else ("test mode" if is_testing else "FAILED")
    logger.info(
        "Follow-up {} for contact {} (vendor: {}, {}) by {}",
        "sent" if email_sent or is_testing else "FAILED",
        contact_id,
        contact.vendor_name,
        mode,
        user.email,
    )

    ctx = _base_ctx(request, user, "follow-ups")
    ctx["contact_id"] = contact_id
    ctx["vendor_name"] = contact.vendor_name or "Vendor"
    return template_response("htmx/partials/follow_ups/sent_success.html", ctx)


@router.post("/v2/partials/follow-ups/{contact_id}/ai-draft", response_class=HTMLResponse)
async def ai_draft_follow_up(
    request: Request,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Draft a contextual follow-up body and fill the compose textarea."""
    from datetime import timezone as tz

    from ...models.offers import Contact as RfqContact

    contact = db.get(RfqContact, contact_id)
    if not contact:
        raise HTTPException(404, "Contact not found")
    require_requisition_access(db, contact.requisition_id, user, owner_id=contact.user_id, label="Contact")

    days_waiting = (datetime.now(tz.utc) - contact.created_at).days if contact.created_at else None

    from app.services.email_drafting import draft_email

    result = await draft_email(
        "follow_up",
        {
            "vendor_name": contact.vendor_name,
            "parts": contact.parts_included or [],
            "days_waiting": days_waiting,
            "subject": contact.subject,
        },
    )
    drafted = (result or {}).get("body") or ""

    escaped = drafted.replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$").replace("</", "<\\/")
    return HTMLResponse(
        f'<script>document.getElementById("follow-up-body-{contact_id}").value = `{escaped}`;</script>'
        '<p class="text-xs text-green-600 mt-1">Draft ready. Review and edit before sending.</p>'
    )


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
    from ...models.offers import VendorResponse

    require_requisition_access(db, req_id, user)
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
    return template_response("htmx/partials/requisitions/tabs/response_card.html", ctx)


@router.post(
    "/v2/partials/requisitions/{req_id}/responses/{response_id}/ai-draft-reply",
    response_class=HTMLResponse,
)
async def ai_draft_reply(
    request: Request,
    req_id: int,
    response_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Draft an AI reply to a vendor response and render an editable compose block."""
    from ...models.offers import VendorResponse

    require_requisition_access(db, req_id, user)

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

    parsed = vr.parsed_data or {}

    from app.services.email_drafting import draft_email

    result = await draft_email(
        "vendor_reply",
        {
            "classification": vr.classification,
            "vendor_name": vr.vendor_name,
            "mpn": parsed.get("mpn"),
            "qty": parsed.get("qty") or parsed.get("qty_available"),
            "price": parsed.get("price") or parsed.get("unit_price"),
            "lead_time": parsed.get("lead_time"),
            "subject": vr.subject,
        },
    )

    default_subject = vr.subject or "RFQ"
    if not default_subject.lower().startswith("re:"):
        default_subject = f"Re: {default_subject}"

    ctx = _base_ctx(request, user, "requisitions")
    ctx["req_id"] = req_id
    ctx["r"] = vr
    ctx["reply_subject"] = (result or {}).get("subject") or default_subject
    ctx["reply_body"] = (result or {}).get("body") or ""
    ctx["ai_failed"] = result is None
    return template_response("htmx/partials/requisitions/tabs/reply_compose.html", ctx)


@router.post(
    "/v2/partials/requisitions/{req_id}/responses/{response_id}/send-reply",
    response_class=HTMLResponse,
)
async def send_reply_htmx(
    request: Request,
    req_id: int,
    response_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Send a reply to a vendor response (as the signed-in user) and mark it
    reviewed."""

    from ...models.offers import VendorResponse

    require_requisition_access(db, req_id, user)

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
    subject = (form.get("subject") or "").strip() or f"Re: {vr.subject or 'RFQ'}"
    body = (form.get("body") or "").strip()
    if not body:
        raise HTTPException(400, "Reply body is required")

    # DNC hard-block — never email a do-not-contact vendor (checked in all modes).
    if vr.vendor_email:
        from sqlalchemy import func as _sqlfunc

        dnc = (
            db.query(SiteContact)
            .filter(
                _sqlfunc.lower(SiteContact.email) == vr.vendor_email.lower(),
                SiteContact.do_not_contact.is_(True),
            )
            .first()
        )
        if dnc:
            return HTMLResponse(
                '<div class="rounded bg-rose-50 border border-rose-200 text-rose-700 text-xs px-2 py-1.5">'
                "This vendor is on the do-not-contact list — reply not sent.</div>"
            )

    is_testing = os.environ.get("TESTING") == "1"
    email_sent = False

    if not is_testing and vr.vendor_email:
        try:
            from ...dependencies import require_fresh_token

            token = await require_fresh_token(request, db)

            from ...utils.graph_client import GraphClient

            gc = GraphClient(token)
            payload = {
                "message": {
                    "subject": subject,
                    "body": {"contentType": "Text", "content": body},
                    "toRecipients": [{"emailAddress": {"address": vr.vendor_email}}],
                },
                "saveToSentItems": "true",
            }
            await gc.post_json("/me/sendMail", payload)
            email_sent = True
        except Exception as exc:
            logger.warning("Vendor reply send failed for response {}: {}", response_id, exc)

    if email_sent or is_testing:
        vr.status = VendorResponseStatus.REVIEWED
        db.commit()

    logger.info(
        "Vendor reply {} for response {} (vendor: {}) by {}",
        "sent" if email_sent or is_testing else "FAILED",
        response_id,
        vr.vendor_name,
        user.email,
    )

    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    ctx = _base_ctx(request, user, "requisitions")
    ctx["r"] = vr
    ctx["req"] = req
    return template_response("htmx/partials/requisitions/tabs/response_card.html", ctx)


@router.get("/v2/partials/requisitions/{req_id}/rfq-prepare", response_class=HTMLResponse)
async def rfq_prepare_panel(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return RFQ preparation panel — vendor data + exhaustion check."""
    from ...models.offers import Contact as RfqContact

    req = get_requisition_or_404(db, req_id)

    # Get requirements for this req
    requirements = db.query(Requirement).filter(Requirement.requisition_id == req_id).all()
    mpns = [r.primary_mpn for r in requirements if r.primary_mpn]

    # Get vendors already contacted
    existing_contacts = (
        db.query(RfqContact.vendor_name_normalized).filter(RfqContact.requisition_id == req_id).distinct().all()
    )
    contacted_norms = {c[0] for c in existing_contacts if c[0]}

    # Get suggested vendors from sightings (join on normalized vendor name)
    from ...models import Sighting

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

    return template_response(
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
    get_requisition_or_404(db, req_id)  # validates existence
    require_requisition_access(db, req_id, user)

    form = await request.form()
    vendor_name = form.get("vendor_name", "").strip()
    vendor_phone = form.get("vendor_phone", "").strip()
    notes = form.get("notes", "").strip()

    if not vendor_name or not vendor_phone:
        raise HTTPException(400, "Vendor name and phone are required")

    from ...models.offers import Contact as RfqContact
    from ...services.activity_service import log_call_activity

    contact = RfqContact(
        requisition_id=req_id,
        user_id=user.id,
        contact_type="phone",
        vendor_name=vendor_name,
        vendor_contact=vendor_phone,
        details=notes or f"Phone call to {vendor_name}",
        status=ContactStatus.SENT,
    )
    db.add(contact)

    # Route through log_call_activity so the call is matched to a vendor/company,
    # recorded as the canonical CALL_LOGGED type, and bumps last_activity_at.
    # force_meaningful=True: a manually logged call is a deliberate human interaction →
    # always meaningful, regardless of the duration-gate (which targets auto-captured
    # Teams/8x8 calls that carry a real duration).
    log = log_call_activity(
        user_id=user.id,
        direction="outbound",
        phone=vendor_phone,
        duration_seconds=None,
        external_id=None,
        contact_name=vendor_name,
        db=db,
        requisition_id=req_id,
        force_meaningful=True,
    )
    if log is not None:
        log.notes = notes or f"Called {vendor_name} at {vendor_phone}"
    db.commit()
    logger.info("Phone call logged for req {} vendor {} by {}", req_id, vendor_name, user.email)

    return template_response(
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
    from ...models.offers import Contact as RfqContact

    cfg = getattr(request.app, "state", None)
    threshold_days = getattr(cfg, "follow_up_days", 2) if cfg else 2
    threshold = datetime.now(timezone.utc) - timedelta(days=threshold_days)

    q = db.query(RfqContact).filter(
        RfqContact.contact_type == "email",
        RfqContact.status.in_(["sent", "opened"]),
        RfqContact.created_at < threshold,
    )
    # Restricted roles act only on contacts under their own requisitions; buyer/manager/admin
    # stay global. Keep this in lockstep with follow_up_badge so the badge counts what the
    # batch acts on.
    if user.role in RESTRICTED_ROLES:
        q = q.join(Requisition, RfqContact.requisition_id == Requisition.id).filter(Requisition.created_by == user.id)
    stale = q.limit(50).all()

    sent_count = 0
    for contact in stale:
        contact.status = ContactStatus.RESPONDED
        contact.status_updated_at = datetime.now(timezone.utc)
        sent_count += 1
    db.commit()
    logger.info("Batch follow-up: {} contacts marked by {}", sent_count, user.email)

    msg = f"{sent_count} contact{'s' if sent_count != 1 else ''} marked as responded."
    return HTMLResponse(
        f'<div class="text-sm text-green-700 bg-green-50 border border-green-200 rounded-lg px-3 py-2">{msg}</div>'
    )


@router.get("/v2/partials/follow-ups/badge", response_class=HTMLResponse)
async def follow_up_badge(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return follow-up count badge for nav sidebar."""
    from ...models.offers import Contact as RfqContact

    threshold = datetime.now(timezone.utc) - timedelta(days=2)
    q = db.query(sqlfunc.count(RfqContact.id)).filter(
        RfqContact.contact_type == "email",
        RfqContact.status.in_(["sent", "opened"]),
        RfqContact.created_at < threshold,
    )
    # Same per-owner scope as send_batch_follow_up so the badge matches the batch.
    if user.role in RESTRICTED_ROLES:
        q = q.join(Requisition, RfqContact.requisition_id == Requisition.id).filter(Requisition.created_by == user.id)
    count = q.scalar() or 0
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
    from ...models.offers import VendorResponse

    require_requisition_access(db, req_id, user)
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
    action = form.get("status", "").strip()
    # Map accepted action strings to in-vocabulary VendorResponseStatus members so
    # the persisted status always matches enum-based filters/reports.
    status_by_action = {
        VendorResponseStatus.NEW.value: VendorResponseStatus.NEW,
        VendorResponseStatus.REVIEWED.value: VendorResponseStatus.REVIEWED,
        VendorResponseStatus.REJECTED.value: VendorResponseStatus.REJECTED,
        VendorResponseStatus.FLAGGED.value: VendorResponseStatus.FLAGGED,
    }
    new_status = status_by_action.get(action)
    if new_status is None:
        raise HTTPException(
            400,
            f"Invalid status. Must be one of: {', '.join(status_by_action)}",
        )

    vr.status = new_status
    db.commit()
    logger.info("Response {} status → {} by {}", response_id, new_status, user.email)

    return template_response(
        "htmx/partials/requisitions/response_status_badge.html",
        {"request": request, "response": vr},
    )
