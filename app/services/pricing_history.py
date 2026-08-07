"""pricing_history.py — last-quoted-price lookup, keyed by MPN or MaterialCard id.

A single preload query over recent Quotes (sent / won / lost — a quote only counts as
a real market price once it left draft), used to seed a smart default sell price on
Build-Quote tab / builder-modal lines: the last price we actually sold this part for,
when known, else callers fall back to a cost-based markup.

Also home to the proactive price anchors (2026-08-06 spec, D5): per-part
"last quote from us" / "last won deal" lookups over the structured quote_lines
table (indexed on mpn), carrying price + date + customer + rep, bounded by
proactive_price_lookback_months. INTERNAL ONLY — these can carry another
customer's pricing and must never reach a customer-facing surface.

Called by: app.services.quote_builder_service (Build-Quote tab + builder-modal lines),
    app.routers.crm._helpers (re-exported for the Quote-detail pricing-history panel and
    the Quote list's own pricing-history section),
    app.services.proactive_matching (score anchors) + the proactive digest builder
Depends on: app.models (Quote, QuoteLine, Company, CustomerSite, Requisition, User)
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Company, CustomerSite, Quote, QuoteLine, Requisition, User

# Quote statuses eligible for a pricing-history lookup.
PRICED_STATUSES = ["sent", "won", "lost"]


def _iso(dt: datetime | None) -> str | None:
    """Return a datetime as an ISO string, or None if unset."""
    return dt.isoformat() if dt else None


def quote_date_iso(q: Quote) -> str | None:
    """Return the best available date for a quote as an ISO string."""
    return _iso(q.sent_at or q.created_at)


def _lookback_start() -> datetime:
    """Start of the price-anchor window (shared with the requirement window)."""
    return datetime.now(UTC) - timedelta(days=round(settings.proactive_price_lookback_months * 30.44))


def _anchor_query(db: Session, part: str):
    """Base join for per-part anchors: quote line → quote → customer + rep."""
    return (
        db.query(QuoteLine, Quote, Company, Requisition, User)
        .join(Quote, QuoteLine.quote_id == Quote.id)
        .outerjoin(CustomerSite, CustomerSite.id == Quote.customer_site_id)
        .outerjoin(Company, Company.id == CustomerSite.company_id)
        .outerjoin(Requisition, Requisition.id == Quote.requisition_id)
        .outerjoin(User, User.id == Quote.created_by_id)
        .filter(
            func.upper(QuoteLine.mpn) == part,
            QuoteLine.sell_price.isnot(None),
            Quote.status.in_(PRICED_STATUSES),
            func.coalesce(Quote.sent_at, Quote.created_at) >= _lookback_start(),
        )
    )


def _anchor_dict(row, at: datetime | None) -> dict:
    line, quote, company, requisition, rep = row
    return {
        "price": float(line.sell_price),
        "at": at,
        "company_id": company.id if company else None,
        "company": (company.name if company else None) or (requisition.customer_name if requisition else None),
        "rep": rep.name if rep else None,
        "quote_number": quote.quote_number,
    }


def last_quote_for_part(db: Session, *, part: str) -> dict | None:
    """Most recent quote of this part — any customer, any rep (2026-08-06 D5).

    Returns {price, at, company_id, company, rep, quote_number} or None when the part
    has never been quoted inside the lookback. INTERNAL ONLY — the company may be a
    different customer than the one being offered.
    """
    row = _anchor_query(db, part).order_by(Quote.sent_at.desc().nullslast(), Quote.created_at.desc()).first()
    if not row:
        return None
    quote = row[1]
    return _anchor_dict(row, quote.sent_at or quote.created_at)


def last_win_for_part(db: Session, *, part: str) -> dict | None:
    """Most recent WON deal on this part — any customer, any rep (2026-08-06 D5).

    Shown even when it is the same customer and rep: still the best price
    anchor available. Returns the same shape as last_quote_for_part, or None.
    """
    row = (
        _anchor_query(db, part)
        .filter(Quote.result == "won")
        .order_by(Quote.result_at.desc().nullslast(), Quote.sent_at.desc().nullslast(), Quote.created_at.desc())
        .first()
    )
    if not row:
        return None
    quote = row[1]
    return _anchor_dict(row, quote.result_at or quote.sent_at or quote.created_at)


def preload_last_quoted_prices(db: Session) -> dict[str, dict]:
    """Load recent quotes ONCE and build MPN/card_id to price lookup dict.

    Keys by both MPN string (uppercase) and material_card_id so callers
    can look up by either.  card_id keys are prefixed with ``card:`` to
    avoid collisions with MPN strings.
    """
    quotes = (
        db.query(Quote)
        .filter(Quote.status.in_(PRICED_STATUSES))
        .order_by(Quote.sent_at.desc().nullslast(), Quote.created_at.desc())
        .limit(100)
        .all()
    )
    result: dict[str, dict] = {}
    for q in quotes:
        date_str = quote_date_iso(q)
        for item in q.line_items or []:
            entry = {
                "sell_price": item.get("sell_price"),
                "margin_pct": item.get("margin_pct"),
                "quote_number": q.quote_number,
                "date": date_str,
                "result": q.result,
            }
            mpn_key = (item.get("mpn") or "").upper().strip()
            if mpn_key and mpn_key not in result:
                result[mpn_key] = entry
            card_id = item.get("material_card_id")
            if card_id:
                card_key = f"card:{card_id}"
                if card_key not in result:
                    result[card_key] = entry
    return result
