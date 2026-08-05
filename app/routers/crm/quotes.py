"""routers/crm/quotes.py — Quote endpoints (update, delete, preview, preflight, send).

Extracted from routers/crm.py.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session, joinedload

from ...constants import QuoteStatus
from ...database import get_db
from ...dependencies import require_user
from ...models import CustomerSite, Quote, User
from ...schemas.crm import QuoteSendOverride, QuoteUpdate
from ._helpers import (
    _build_quote_email_html,
    quote_to_dict,
)

router = APIRouter()


def _quote_load_options():
    """Eager-load options for serializing a Quote (customer site, company, site
    contacts, creator)."""
    return (
        joinedload(Quote.customer_site).joinedload(CustomerSite.company),
        # selectinload (not joinedload) for the to-many site_contacts leg: a joined
        # collection multiplies Quote rows by contacts (cartesian blow-up) in list views.
        joinedload(Quote.customer_site).selectinload(CustomerSite.site_contacts),
        joinedload(Quote.created_by),
    )


def _compute_quote_totals(line_items: list[dict]) -> tuple[float, float, float]:
    """Compute (subtotal, total_cost, total_margin_pct) from quote line items."""
    total_sell = sum((item.get("qty") or 0) * (item.get("sell_price") or 0) for item in line_items)
    total_cost = sum((item.get("qty") or 0) * (item.get("cost_price") or 0) for item in line_items)
    margin_pct = round((total_sell - total_cost) / total_sell * 100, 2) if total_sell > 0 else 0
    return total_sell, total_cost, margin_pct


# ── Quotes ───────────────────────────────────────────────────────────────


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
    if quote.status not in (QuoteStatus.DRAFT.value, None):
        raise HTTPException(400, "Only draft quotes can be edited")
    updates = payload.model_dump(exclude_unset=True)
    if "line_items" in updates:
        quote.line_items = updates.pop("line_items")
        quote.subtotal, quote.total_cost, quote.total_margin_pct = _compute_quote_totals(quote.line_items or [])
    for field, value in updates.items():
        setattr(quote, field, value)
    db.commit()
    # Eager-load relations for serialization
    quote = db.query(Quote).options(*_quote_load_options()).filter(Quote.id == quote.id).first()
    return quote_to_dict(quote, db)


@router.delete("/api/quotes/{quote_id}")
async def delete_quote(
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a draft quote.

    Sent/won/lost quotes cannot be deleted.
    """
    from ...dependencies import get_quote_for_user

    quote = get_quote_for_user(db, user, quote_id)
    if not quote:
        raise HTTPException(404, "Quote not found")
    if quote.status != QuoteStatus.DRAFT.value:
        raise HTTPException(400, "Only draft quotes can be deleted")
    from ...models.buy_plan import BuyPlan

    buy_plans = db.query(BuyPlan).filter(BuyPlan.quote_id == quote.id).count()
    if buy_plans > 0:
        raise HTTPException(400, f"Cannot delete quote with {buy_plans} linked buy plans.")
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


@router.get("/api/quotes/{quote_id}/preflight")
async def quote_preflight_check(
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Advisory pre-send checks (DNC recipient / non-US country-of-origin / MPN drift).

    Read-only; returns the warnings the send UI surfaces. Advisory only — it never
    blocks the send (the salesperson decides). See app/services/quote_preflight.py.
    """
    from ...dependencies import get_quote_for_user
    from ...services.quote_preflight import quote_preflight

    quote = get_quote_for_user(db, user, quote_id)
    if not quote:
        raise HTTPException(404, "Quote not found")
    warnings = quote_preflight(db, quote)
    return {"warnings": [w.to_dict() for w in warnings], "count": len(warnings)}


@router.post("/api/quotes/{quote_id}/send")
async def send_quote(
    quote_id: int,
    request: Request,
    body: QuoteSendOverride | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from ...dependencies import get_quote_for_user, require_fresh_token
    from ...services.quote_send import QuoteSendDNCBlocked, QuoteSendError, send_quote_email

    quote = get_quote_for_user(db, user, quote_id)
    if not quote:
        raise HTTPException(404, "Quote not found")

    # Allow caller to override recipient email/name
    override_email = (body.to_email if body else None) or None
    override_name = (body.to_name if body else None) or None

    token = await require_fresh_token(request, db)
    try:
        result = await send_quote_email(
            db,
            quote,
            user,
            token=token,
            override_email=override_email,
            override_name=override_name,
        )
    except QuoteSendDNCBlocked as e:
        raise HTTPException(409, "Recipient is on the do-not-contact list — clear DNC to send.") from e
    except QuoteSendError as exc:
        # No/invalid recipient is a 400 (caller can fix the address); a Graph send failure
        # is a 502 (upstream). Match the legacy status codes.
        detail = exc.detail
        status_code = 502 if detail.startswith("Failed to send quote email") else 400
        raise HTTPException(status_code, detail) from exc

    return {
        "ok": True,
        "status": result.status,
        "sent_to": result.sent_to,
        "req_status": result.req_status,
        "status_changed": result.status_changed,
    }
