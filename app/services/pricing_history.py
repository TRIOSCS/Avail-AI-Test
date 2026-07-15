"""pricing_history.py — last-quoted-price lookup, keyed by MPN or MaterialCard id.

A single preload query over recent Quotes (sent / won / lost — a quote only counts as
a real market price once it left draft), used to seed a smart default sell price on
Build-Quote tab / builder-modal lines: the last price we actually sold this part for,
when known, else callers fall back to a cost-based markup.

Called by: app.services.quote_builder_service (Build-Quote tab + builder-modal lines),
    app.routers.crm._helpers (re-exported for the Quote-detail pricing-history panel and
    the Quote list's own pricing-history section)
Depends on: app.models (Quote)
"""

from datetime import datetime

from sqlalchemy.orm import Session

from ..models import Quote

# Quote statuses eligible for a pricing-history lookup.
PRICED_STATUSES = ["sent", "won", "lost"]


def _iso(dt: datetime | None) -> str | None:
    """Return a datetime as an ISO string, or None if unset."""
    return dt.isoformat() if dt else None


def quote_date_iso(q: Quote) -> str | None:
    """Return the best available date for a quote as an ISO string."""
    return _iso(q.sent_at or q.created_at)


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
