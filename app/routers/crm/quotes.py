"""
routers/crm/quotes.py — Quote endpoints (create, update, send, result, revise, reopen)
and pricing history.

Extracted from routers/crm.py.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from sqlalchemy.orm import Session, joinedload

from ...database import get_db
from ...dependencies import require_user
from ...models import ActivityLog, CustomerSite, Offer, Quote, Requisition, User
from ...schemas.crm import QuoteCreate, QuoteReopen, QuoteResult, QuoteSendOverride, QuoteUpdate
from ...schemas.responses import QuoteDetailResponse
from ._helpers import (
    _PRICED_STATUSES,
    _build_quote_email_html,
    _preload_last_quoted_prices,
    _quote_date_iso,
    next_quote_number,
    quote_to_dict,
)

router = APIRouter()


# ── Quotes ───────────────────────────────────────────────────────────────


@router.get(
    "/api/requisitions/{req_id}/quote", response_model=QuoteDetailResponse | None, response_model_exclude_none=True
)
async def get_quote(req_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    from ...dependencies import get_req_for_user

    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    quote = (
        db.query(Quote)
        .options(
            joinedload(Quote.customer_site).joinedload(CustomerSite.company),
            joinedload(Quote.customer_site).joinedload(CustomerSite.site_contacts),
            joinedload(Quote.created_by),
        )
        .filter(Quote.requisition_id == req_id)
        .order_by(Quote.revision.desc())
        .first()
    )
    if not quote:
        return None
    return quote_to_dict(quote, db)


@router.get("/api/quotes/recent-terms")
async def recent_quote_terms(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Return payment/shipping terms from the 5 most recent quotes for copy-from-quote UX."""
    quotes = (
        db.query(Quote)
        .options(joinedload(Quote.customer_site).joinedload(CustomerSite.company))
        .filter(Quote.created_by_id == user.id, Quote.payment_terms.isnot(None))
        .order_by(Quote.created_at.desc())
        .limit(5)
        .all()
    )
    results = []
    for q in quotes:
        company_name = ""
        if q.customer_site and q.customer_site.company:
            company_name = q.customer_site.company.name or ""
        results.append(
            {
                "quote_number": q.quote_number,
                "customer_name": company_name,
                "payment_terms": q.payment_terms or "",
                "shipping_terms": q.shipping_terms or "",
                "validity_days": q.validity_days or 7,
                "notes": q.notes or "",
            }
        )
    return results


@router.get("/api/requisitions/{req_id}/quotes")
async def list_quotes(req_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """List all quotes (including revisions) for a requisition."""
    from ...dependencies import get_req_for_user

    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    quotes = (
        db.query(Quote)
        .options(
            joinedload(Quote.customer_site).joinedload(CustomerSite.company),
            joinedload(Quote.customer_site).joinedload(CustomerSite.site_contacts),
            joinedload(Quote.created_by),
        )
        .filter(Quote.requisition_id == req_id)
        .order_by(Quote.revision.desc())
        .all()
    )
    return [quote_to_dict(q, db) for q in quotes]


@router.post("/api/requisitions/{req_id}/quote")
async def create_quote(
    req_id: int,
    payload: QuoteCreate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from ...dependencies import get_req_for_user

    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    if not req.customer_site_id:
        raise HTTPException(400, "Requisition must be linked to a customer site before quoting")
    offer_ids = payload.offer_ids
    line_items = [li.model_dump() for li in payload.line_items] if payload.line_items else []
    if offer_ids and not line_items:
        offers = db.query(Offer).options(joinedload(Offer.requirement)).filter(Offer.id.in_(offer_ids)).all()
        quoted_prices = _preload_last_quoted_prices(db)
        line_items = []
        for o in offers:
            target = None
            last_q_price = None
            if o.requirement:
                target = float(o.requirement.target_price) if o.requirement.target_price else None
                lq = (
                    quoted_prices.get(f"card:{o.material_card_id}") if o.material_card_id else None
                ) or quoted_prices.get((o.mpn or "").upper().strip())
                last_q_price = lq.get("sell_price") if lq else None
            cost = float(o.unit_price) if o.unit_price else 0
            line_items.append(
                {
                    "mpn": o.mpn,
                    "manufacturer": o.manufacturer,
                    "qty": o.qty_available or 0,
                    "cost_price": cost,
                    "sell_price": cost,
                    "margin_pct": 0,
                    "lead_time": o.lead_time,
                    "condition": o.condition,
                    "date_code": o.date_code,
                    "firmware": o.firmware,
                    "hardware_code": o.hardware_code,
                    "packaging": o.packaging,
                    "offer_id": o.id,
                    "material_card_id": o.material_card_id,
                    "target_price": target,
                    "last_quoted_price": last_q_price,
                }
            )
    # Resolve material_card_id for line items that don't have one.
    # Use a savepoint so that failures (e.g. unique constraint violations)
    # don't poison the outer transaction.
    from ...search_service import resolve_material_card

    for li in line_items:
        if not li.get("material_card_id") and li.get("mpn"):
            try:
                nested = db.begin_nested()
                card = resolve_material_card(li["mpn"], db)
                if card:
                    li["material_card_id"] = card.id
                nested.commit()
            except Exception:
                nested.rollback()
                logger.warning(
                    "Failed to resolve material card for MPN=%s during quote creation for req=%d",
                    li.get("mpn"),
                    req_id,
                )

    site = db.get(CustomerSite, req.customer_site_id)
    total_sell = sum((item.get("qty") or 0) * (item.get("sell_price") or 0) for item in line_items)
    total_cost = sum((item.get("qty") or 0) * (item.get("cost_price") or 0) for item in line_items)
    from sqlalchemy.exc import IntegrityError

    old_status = req.status
    for attempt in range(3):
        quote = Quote(
            requisition_id=req_id,
            customer_site_id=req.customer_site_id,
            quote_number=next_quote_number(db),
            line_items=line_items,
            subtotal=total_sell,
            total_cost=total_cost,
            total_margin_pct=(round((total_sell - total_cost) / total_sell * 100, 2) if total_sell > 0 else 0),
            payment_terms=site.payment_terms if site else None,
            shipping_terms=site.shipping_terms if site else None,
            created_by_id=user.id,
        )
        db.add(quote)
        if req.status in ("active", "sourcing", "offers"):
            from ...services.requisition_state import transition as req_transition

            try:
                req_transition(req, "quoting", user, db)
            except ValueError:
                pass  # already in quoting or later state
        try:
            db.commit()
            break
        except IntegrityError:
            db.rollback()
            req.status = old_status
            if attempt == 2:
                raise
    # Write structured QuoteLine rows (parallel to JSON for backward compat)
    from ...models import QuoteLine

    for li in line_items:
        ql = QuoteLine(
            quote_id=quote.id,
            material_card_id=li.get("material_card_id"),
            offer_id=li.get("offer_id"),
            mpn=li.get("mpn", ""),
            manufacturer=li.get("manufacturer"),
            qty=li.get("qty"),
            cost_price=li.get("cost_price"),
            sell_price=li.get("sell_price"),
            margin_pct=li.get("margin_pct"),
            currency=li.get("currency", "USD"),
        )
        db.add(ql)
    db.commit()

    # Auto-advance per-part sourcing status to "quoted"
    try:
        from app.services.requirement_status import on_quote_built

        req_item_ids = [li.get("offer_id") for li in line_items if li.get("offer_id")]
        if req_item_ids:
            from ...models import Offer as OfferModel

            offers_used = db.query(OfferModel).filter(OfferModel.id.in_(req_item_ids)).all()
            requirement_ids = list({o.requirement_id for o in offers_used if o.requirement_id})
            if requirement_ids:
                on_quote_built(requirement_ids, db, actor=user)
                db.commit()
    except Exception as e:
        logger.warning("Requirement status update (on_quote_built) failed: {}", e)

    # Auto-capture quote facts into Knowledge Ledger
    try:
        from app.services.knowledge_service import capture_quote_fact

        capture_quote_fact(db, quote=quote, user_id=user.id)
    except Exception as e:
        logger.warning("Knowledge auto-capture (quote) failed: {}", e)

    result = quote_to_dict(quote, db)
    result["req_status"] = req.status
    result["status_changed"] = req.status != old_status
    return result


@router.put("/api/quotes/{quote_id}")
async def update_quote(
    quote_id: int,
    payload: QuoteUpdate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from ...dependencies import get_quote_for_user

    quote = get_quote_for_user(db, user, quote_id)
    if not quote:
        raise HTTPException(404, "Quote not found")
    if quote.status not in ("draft", None):
        raise HTTPException(400, "Only draft quotes can be edited")
    updates = payload.model_dump(exclude_unset=True)
    if "line_items" in updates:
        quote.line_items = updates.pop("line_items")
        total_sell = sum((item.get("qty") or 0) * (item.get("sell_price") or 0) for item in (quote.line_items or []))
        total_cost = sum((item.get("qty") or 0) * (item.get("cost_price") or 0) for item in (quote.line_items or []))
        quote.subtotal = total_sell
        quote.total_cost = total_cost
        quote.total_margin_pct = round((total_sell - total_cost) / total_sell * 100, 2) if total_sell > 0 else 0
    for field, value in updates.items():
        setattr(quote, field, value)
    db.commit()
    # Eager-load relations for serialization
    quote = (
        db.query(Quote)
        .options(
            joinedload(Quote.customer_site).joinedload(CustomerSite.company),
            joinedload(Quote.customer_site).joinedload(CustomerSite.site_contacts),
            joinedload(Quote.created_by),
        )
        .filter(Quote.id == quote.id)
        .first()
    )
    return quote_to_dict(quote, db)


@router.delete("/api/quotes/{quote_id}")
async def delete_quote(
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a draft quote. Sent/won/lost quotes cannot be deleted."""
    from ...dependencies import get_quote_for_user

    quote = get_quote_for_user(db, user, quote_id)
    if not quote:
        raise HTTPException(404, "Quote not found")
    if quote.status != "draft":
        raise HTTPException(400, "Only draft quotes can be deleted")
    db.delete(quote)
    db.commit()
    return {"ok": True}


@router.post("/api/quotes/{quote_id}/preview")
async def preview_quote_email(
    quote_id: int,
    body: QuoteSendOverride | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the HTML email preview without sending."""
    from ...dependencies import get_quote_for_user

    quote = get_quote_for_user(db, user, quote_id)
    if not quote:
        raise HTTPException(404, "Quote not found")
    override_name = ((body.to_name if body else None) or "").strip()
    site = db.get(CustomerSite, quote.customer_site_id)
    to_name = override_name or (site.contact_name if site else "") or ""
    company_name = site.company.name if site and site.company else ""
    html = _build_quote_email_html(quote, to_name, company_name, user)
    return {"html": html}


@router.post("/api/quotes/{quote_id}/send")
async def send_quote(
    quote_id: int,
    request: Request,
    body: QuoteSendOverride | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from ...dependencies import require_fresh_token
    from ...dependencies import get_quote_for_user

    quote = get_quote_for_user(db, user, quote_id)
    if not quote:
        raise HTTPException(404, "Quote not found")

    # Allow caller to override recipient email/name
    override_email = ((body.to_email if body else None) or "").strip()
    override_name = ((body.to_name if body else None) or "").strip()

    site = db.get(CustomerSite, quote.customer_site_id)
    to_email = override_email or (site.contact_email if site else None)
    if not to_email:
        raise HTTPException(400, "No recipient email — select a contact or enter one manually")
    if "@" not in to_email:
        raise HTTPException(400, f"Invalid email address: {to_email}")

    to_name = override_name or (site.contact_name if site else "") or ""
    company_name = site.company.name if site and site.company else ""

    # Build the HTML quote email
    html = _build_quote_email_html(quote, to_name, company_name, user)

    subject = f"Quote {quote.quote_number} — Trio Supply Chain Solutions"

    # Send via Graph API
    token = await require_fresh_token(request, db)
    from app.utils.graph_client import GraphClient

    gc = GraphClient(token)
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html},
            "toRecipients": [{"emailAddress": {"address": to_email, "name": to_name}}],
        },
        "saveToSentItems": "true",
    }
    result = await gc.post_json("/me/sendMail", payload)
    if "error" in result:
        raise HTTPException(502, f"Failed to send quote email: {result.get('detail', '')}")

    quote.status = "sent"
    quote.sent_at = datetime.now(timezone.utc)
    req = db.get(Requisition, quote.requisition_id)
    old_status = req.status if req else None
    if req and req.status not in ("won", "lost", "archived"):
        req.status = "quoted"
    db.commit()
    return {
        "ok": True,
        "status": "sent",
        "sent_to": to_email,
        "req_status": req.status if req else None,
        "status_changed": req and req.status != old_status,
    }


@router.post("/api/quotes/{quote_id}/result")
async def quote_result(
    quote_id: int,
    payload: QuoteResult,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from ...dependencies import get_quote_for_user

    quote = get_quote_for_user(db, user, quote_id)
    if not quote:
        raise HTTPException(404, "Quote not found")
    quote.result = payload.result
    quote.result_reason = payload.reason
    quote.result_notes = payload.notes
    quote.result_at = datetime.now(timezone.utc)
    quote.status = payload.result
    if payload.result == "won":
        quote.won_revenue = quote.subtotal
    req = db.get(Requisition, quote.requisition_id)
    if req:
        req.status = payload.result

    # CPH hook: record purchase history when quote is won
    if payload.result == "won":
        _record_quote_won_history(db, req, quote)

    # Notify requisition creator about quote outcome
    notify_user_id = req.created_by if req else None
    if notify_user_id and payload.result in ("won", "lost"):
        customer = req.customer_name or (req.name if req else "") or ""
        if payload.result == "won":
            subj = f"Quote won: {customer} — ${quote.subtotal or 0:,.0f}"
        else:
            subj = f"Quote lost: {customer} — {payload.reason or 'no reason'}"
        db.add(
            ActivityLog(
                user_id=notify_user_id,
                activity_type=f"quote_{payload.result}",
                channel="system",
                requisition_id=req.id if req else None,
                quote_id=quote.id,
                contact_name=customer,
                subject=subj,
            )
        )

    db.commit()
    return {
        "ok": True,
        "status": payload.result,
        "req_status": req.status if req else None,
        "status_changed": True,
    }


@router.post("/api/quotes/{quote_id}/revise")
async def revise_quote(quote_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    from ...dependencies import get_quote_for_user

    old = get_quote_for_user(db, user, quote_id)
    if not old:
        raise HTTPException(404, "Quote not found")
    old.status = "revised"
    old_number = old.quote_number
    old.quote_number = f"{old_number}-R{old.revision}"
    new_quote = Quote(
        requisition_id=old.requisition_id,
        customer_site_id=old.customer_site_id,
        quote_number=old_number,
        revision=old.revision + 1,
        line_items=old.line_items,
        payment_terms=old.payment_terms,
        shipping_terms=old.shipping_terms,
        validity_days=old.validity_days,
        notes=old.notes,
        created_by_id=user.id,
    )
    db.add(new_quote)
    db.commit()
    return quote_to_dict(new_quote, db)


@router.post("/api/quotes/{quote_id}/reopen")
async def reopen_quote(
    quote_id: int,
    payload: QuoteReopen,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from ...dependencies import get_quote_for_user

    quote = get_quote_for_user(db, user, quote_id)
    if not quote:
        raise HTTPException(404, "Quote not found")
    req = db.get(Requisition, quote.requisition_id)
    if req:
        req.status = "reopened"
    if payload.revise:
        quote.status = "revised"
        old_number = quote.quote_number
        quote.quote_number = f"{old_number}-R{quote.revision}"
        new_quote = Quote(
            requisition_id=quote.requisition_id,
            customer_site_id=quote.customer_site_id,
            quote_number=old_number,
            revision=quote.revision + 1,
            line_items=quote.line_items,
            payment_terms=quote.payment_terms,
            shipping_terms=quote.shipping_terms,
            validity_days=quote.validity_days,
            notes=quote.notes,
            created_by_id=user.id,
        )
        db.add(new_quote)
        db.commit()
        return quote_to_dict(new_quote, db)
    else:
        quote.status = "sent"
        quote.result = None
        quote.result_reason = None
        quote.result_notes = None
        quote.result_at = None
        db.commit()
        return quote_to_dict(quote, db)


# ── Pricing History ──────────────────────────────────────────────────────


@router.get("/api/pricing-history/{mpn}")
async def pricing_history(mpn: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    from ...models import MaterialCard
    from ...utils.normalization import normalize_mpn_key

    # Resolve MPN to material_card_id for fast FK lookup
    norm_key = normalize_mpn_key(mpn)
    card = db.query(MaterialCard).filter(MaterialCard.normalized_mpn == norm_key).first() if norm_key else None

    mpn_upper = mpn.upper().strip()
    quotes = db.query(Quote).options(joinedload(Quote.customer_site).joinedload(CustomerSite.company)).filter(
        Quote.status.in_(_PRICED_STATUSES)
    )
    if user.role == "sales":
        quotes = quotes.join(Requisition, Quote.requisition_id == Requisition.id).filter(Requisition.created_by == user.id)
    quotes = quotes.order_by(Quote.sent_at.desc().nullslast(), Quote.created_at.desc()).limit(500).all()
    history = []
    card_id = card.id if card else None
    for q in quotes:
        for item in q.line_items or []:
            matched_by_card = card_id and item.get("material_card_id") == card_id
            matched_by_mpn = (item.get("mpn") or "").upper().strip() == mpn_upper
            if not (matched_by_card or matched_by_mpn):
                continue
            site_name = ""
            if q.customer_site:
                site_name = (
                    f"{q.customer_site.company.name} — {q.customer_site.site_name}"
                    if q.customer_site.company
                    else q.customer_site.site_name
                )
            history.append(
                {
                    "date": _quote_date_iso(q),
                    "qty": item.get("qty"),
                    "cost_price": item.get("cost_price"),
                    "sell_price": item.get("sell_price"),
                    "margin_pct": item.get("margin_pct"),
                    "customer": site_name,
                    "result": q.result,
                    "quote_number": q.quote_number,
                }
            )
            break
    prices = [h["sell_price"] for h in history if h.get("sell_price")]
    margins = [h["margin_pct"] for h in history if h.get("margin_pct")]
    return {
        "mpn": mpn,
        "material_card_id": card_id,
        "history": history[:50],
        "avg_price": round(sum(prices) / len(prices), 4) if prices else None,
        "avg_margin": round(sum(margins) / len(margins), 2) if margins else None,
        "price_range": [min(prices), max(prices)] if prices else None,
    }


def _record_quote_won_history(db: Session, req: Requisition | None, quote: Quote) -> None:
    """Feed customer_part_history from quote line items when quote is won directly.

    Errors are logged but never block the quote result flow.
    """
    if not req or not req.customer_site_id:
        return
    try:
        from ...services.purchase_history_service import upsert_purchase

        site = db.get(CustomerSite, req.customer_site_id)
        if not site or not site.company_id:  # pragma: no cover
            return
        company_id = site.company_id

        for li in quote.line_items or []:
            card_id = li.get("material_card_id")
            if not card_id:
                continue
            upsert_purchase(
                db,
                company_id=company_id,
                material_card_id=card_id,
                source="avail_quote_won",
                unit_price=li.get("sell_price"),
                quantity=li.get("qty"),
                source_ref=f"quote:{quote.id}",
            )
    except Exception as e:
        logger.error(
            "Quote won purchase history recording failed for quote_id=%d quote_number=%s: %s",
            quote.id,
            quote.quote_number,
            e,
            exc_info=True,
        )
