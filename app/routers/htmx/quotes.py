"""routers/htmx/quotes.py — Quote partials / quote-line management (HTMX + Alpine).

Server-rendered HTML partials for the quote surface: preview/delete/reopen/edit
metadata, recent-terms + pricing-history lookups, the quote detail panel, quote-line
CRUD, add-offer-to-quote, send/result/revise/apply-markup, add-offers-to-draft, and
build-buy-plan-from-quote. Extracted verbatim from htmx_views.py (same `/v2/partials`
paths, same `htmx-views` tag).

Called by: app/main.py (router mount).
Depends on: app.models, app.dependencies, app.database, app.services, ._shared
"""

import html as html_mod
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.orm import Session, joinedload

from ...constants import (
    QuoteStatus,
)
from ...database import get_db
from ...dependencies import (
    get_quote_for_user,
    require_user,
)
from ...models import (
    BuyPlanLine,
    CustomerSite,
    Offer,
    Quote,
    QuoteLine,
    User,
)
from ...services.buyplan_naming import summarize_top_flag
from ...services.quote_send import (
    QuoteSendDNCBlocked,
    QuoteSendError,
    send_quote_email,
)
from ...services.status_machine import require_valid_transition
from ...template_env import template_response
from ._shared import _base_ctx, _is_ops_member

router = APIRouter(tags=["htmx-views"])


# ── Sprint 5: Quote Workflow Completion ────────────────────────────────


@router.post("/v2/partials/quotes/{quote_id}/preview", response_class=HTMLResponse)
async def preview_quote(
    request: Request,
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render quote email preview before sending."""
    quote = get_quote_for_user(db, user, quote_id, options=[joinedload(Quote.quote_lines)])

    return template_response(
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
    """Delete a draft quote and redirect to the requisitions page."""
    quote = get_quote_for_user(db, user, quote_id)
    if quote.status != QuoteStatus.DRAFT:
        raise HTTPException(400, "Only draft quotes can be deleted")

    db.delete(quote)
    db.commit()
    logger.info("Quote {} deleted by {}", quote_id, user.email)

    return HTMLResponse(status_code=200, headers={"HX-Redirect": "/v2/requisitions"})


@router.post("/v2/partials/quotes/{quote_id}/reopen", response_class=HTMLResponse)
async def reopen_quote(
    request: Request,
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Reopen a sent/closed quote back to draft."""
    quote = get_quote_for_user(db, user, quote_id)
    if quote.status not in (QuoteStatus.SENT, QuoteStatus.WON, QuoteStatus.LOST):
        raise HTTPException(400, "Only sent/won/lost quotes can be reopened")

    require_valid_transition("quote", quote.status, QuoteStatus.DRAFT)
    quote.status = QuoteStatus.DRAFT
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
    payment_opts = [f'<option value="{html_mod.escape(t[0])}">' for t in payment_terms if t[0]]
    shipping_opts = [f'<option value="{html_mod.escape(t[0])}">' for t in shipping_terms if t[0]]
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
    from ...utils.normalization import normalize_mpn_key

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

    return template_response(
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
    quote = get_quote_for_user(db, user, quote_id)

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


# ── Quotes partials ───────────────────────────────────────────────────


@router.get("/v2/partials/quotes/{quote_id}", response_class=HTMLResponse)
async def quote_detail_partial(
    request: Request,
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return quote detail as HTML partial."""
    quote = get_quote_for_user(
        db,
        user,
        quote_id,
        options=[
            joinedload(Quote.customer_site).joinedload(CustomerSite.company),
            joinedload(Quote.requisition),
            joinedload(Quote.created_by),
        ],
    )
    lines = db.query(QuoteLine).filter(QuoteLine.quote_id == quote_id).all()
    offers = (
        db.query(Offer).filter(Offer.requisition_id == quote.requisition_id).order_by(Offer.created_at.desc()).all()
    )
    ctx = _base_ctx(request, user, "quotes")
    ctx.update({"quote": quote, "lines": lines, "offers": offers})
    return template_response("htmx/partials/quotes/detail.html", ctx)


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
    # Scope the parent quote through ownership (raises 404 for SALES accessing other users' quotes).
    get_quote_for_user(db, user, line.quote_id)
    form = await request.form()
    if "mpn" in form:
        line.mpn = form["mpn"]
    if "manufacturer" in form:
        line.manufacturer = form["manufacturer"]
    if "qty" in form:
        try:
            line.qty = int(form["qty"])
        except (ValueError, TypeError):
            raise HTTPException(400, "qty must be an integer")
    if "cost_price" in form:
        try:
            line.cost_price = float(form["cost_price"])
        except (ValueError, TypeError):
            raise HTTPException(400, "cost_price must be a number")
    if "sell_price" in form:
        try:
            line.sell_price = float(form["sell_price"])
        except (ValueError, TypeError):
            raise HTTPException(400, "sell_price must be a number")
    if line.sell_price and float(line.sell_price) > 0 and line.cost_price is not None:
        line.margin_pct = round((float(line.sell_price) - float(line.cost_price)) / float(line.sell_price) * 100, 2)
    db.commit()
    ctx = _base_ctx(request, user, "quotes")
    ctx["line"] = line
    return template_response("htmx/partials/quotes/line_row.html", ctx)


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
    # Scope the parent quote through ownership (raises 404 for SALES accessing other users' quotes).
    get_quote_for_user(db, user, line.quote_id)
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
    # Ownership/existence check (raises 404 if the quote isn't visible to the user).
    get_quote_for_user(db, user, quote_id)
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
    return template_response("htmx/partials/quotes/line_row.html", ctx)


@router.post("/v2/partials/quotes/{quote_id}/add-offer/{offer_id}", response_class=HTMLResponse)
async def add_offer_to_quote(
    request: Request,
    quote_id: int,
    offer_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add an offer as a line item to a quote."""
    quote = get_quote_for_user(db, user, quote_id)
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    if offer.requisition_id is not None and offer.requisition_id != quote.requisition_id:
        raise HTTPException(
            status_code=403,
            detail={"error": "offer does not belong to this quote's requisition"},
        )
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
    return template_response("htmx/partials/quotes/line_row.html", ctx)


@router.post("/v2/partials/quotes/{quote_id}/send", response_class=HTMLResponse)
async def send_quote_htmx(
    request: Request,
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Send the quote to the customer (real email) — returns refreshed detail partial.

    Delegates to the canonical quote-send service so this button actually emails the
    customer (captures Graph ids, writes an outbound ActivityLog, hard-blocks DNC). In
    TESTING the service skips the real Graph call but still marks the quote sent.
    """
    quote = get_quote_for_user(db, user, quote_id)
    testing = os.environ.get("TESTING") == "1"
    # Only acquire a real M365 token outside TESTING — the service skips the Graph send in
    # TESTING, and require_fresh_token (called directly, not via Depends) would 401 in tests.
    token = ""
    if not testing:
        from ...dependencies import require_fresh_token

        token = await require_fresh_token(request, db)
    try:
        await send_quote_email(db, quote, user, token=token, testing=testing)
    except QuoteSendDNCBlocked:
        return HTMLResponse(
            '<div class="rounded bg-rose-50 border border-rose-200 text-rose-700 text-xs px-2 py-1.5">'
            "This recipient is do-not-contact — quote not sent.</div>"
        )
    except QuoteSendError as exc:
        return HTMLResponse(
            '<div class="rounded bg-rose-50 border border-rose-200 text-rose-700 text-xs px-2 py-1.5">'
            f"{html_mod.escape(exc.detail)}</div>"
        )
    return await quote_detail_partial(request, quote_id, user, db)


@router.post("/v2/partials/quotes/{quote_id}/result", response_class=HTMLResponse)
async def quote_result_htmx(
    request: Request,
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Mark quote result (won/lost) — returns refreshed detail partial."""

    quote = get_quote_for_user(db, user, quote_id)
    form = await request.form()
    result = form.get("result", "")
    if result not in ("won", "lost"):
        raise HTTPException(400, "Result must be 'won' or 'lost'")
    quote.result = result
    require_valid_transition("quote", quote.status, result)
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
    quote = get_quote_for_user(db, user, quote_id)
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
        status=QuoteStatus.DRAFT,
        created_by_id=user.id,
        # Carry revenue attribution forward so a revision of a proactive-sourced
        # quote stays attributed to proactive selling (Wave 6).
        source=quote.source,
    )
    db.add(new_quote)
    db.flush()  # need new_quote.id for the cloned lines

    # Clone the parent's QuoteLine rows — quote_detail_partial, the send email, the
    # PDF, and Build-Buy-Plan all read QuoteLine (not line_items JSON). Without this
    # the revision showed an empty line table and couldn't build a buy plan (OQ-04,
    # the inverse of the create-from-offers OQ-01 gap).
    for src in db.query(QuoteLine).filter(QuoteLine.quote_id == quote.id).all():
        db.add(
            QuoteLine(
                quote_id=new_quote.id,
                material_card_id=src.material_card_id,
                offer_id=src.offer_id,
                mpn=src.mpn,
                description=src.description,
                manufacturer=src.manufacturer,
                qty=src.qty,
                cost_price=src.cost_price,
                sell_price=src.sell_price,
                margin_pct=src.margin_pct,
                currency=src.currency,
            )
        )
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
    # Scope ownership check before mutating any lines (raises 404 for SALES on other users' quotes).
    get_quote_for_user(db, user, quote_id)
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

    try:
        offer_ids = [int(x) for x in data.get("offer_ids", []) if x]
        quote_id = int(data.get("quote_id", 0))
    except (ValueError, TypeError):
        raise HTTPException(400, "offer_ids must be integers and quote_id must be an integer")

    if not offer_ids or not quote_id:
        raise HTTPException(400, "Missing offer_ids or quote_id")

    quote = get_quote_for_user(db, user, quote_id)
    if quote.requisition_id != req_id:
        raise HTTPException(404, "Quote not found")
    if quote.status != QuoteStatus.DRAFT:
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
    from ...services.buyplan_builder import build_buy_plan

    quote = get_quote_for_user(db, user, quote_id)
    if quote.status != QuoteStatus.WON:
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
    ctx["bp"] = plan
    ctx["lines"] = bp_lines
    ctx["user"] = user
    ctx["is_ops_member"] = _is_ops_member(user, db)
    ctx["top_flag"] = summarize_top_flag(plan.ai_flags)
    return template_response("htmx/partials/buy_plans/detail.html", ctx)
