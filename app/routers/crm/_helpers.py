"""Shared helpers for CRM sub-routers."""

from datetime import date, timedelta

from sqlalchemy.orm import Session

from ...models import ChangeLog, Quote

# Late import — re-exported for backward compatibility
from ...services.crm_service import next_quote_number  # noqa: F401

# _build_quote_email_html lives in app/services/quote_send.py (single home, alongside the
# send service). Re-exported here so the preview route and existing imports keep working.
from ...services.quote_send import _build_quote_email_html  # noqa: F401

# Statuses considered for pricing history lookups
_PRICED_STATUSES = ["sent", "won", "lost"]


def _iso(dt) -> str | None:
    """Return a datetime as an ISO string, or None if unset."""
    return dt.isoformat() if dt else None


def _float(v) -> float | None:
    """Return a numeric value as a float, or None if falsy."""
    return float(v) if v else None


def _quote_date_iso(q: Quote) -> str | None:
    """Return the best available date for a quote as an ISO string."""
    return _iso(q.sent_at or q.created_at)


def record_changes(
    db: Session, entity_type: str, entity_id: int, user_id: int, old_dict: dict, new_dict: dict, fields: list[str]
):
    """Record field-level changes to the change_log table."""
    for f in fields:
        old_val = str(old_dict.get(f) or "")
        new_val = str(new_dict.get(f) or "")
        if old_val != new_val:
            db.add(
                ChangeLog(
                    entity_type=entity_type,
                    entity_id=entity_id,
                    user_id=user_id,
                    field_name=f,
                    old_value=old_val,
                    new_value=new_val,
                )
            )


def _preload_last_quoted_prices(db: Session) -> dict[str, dict]:
    """Load recent quotes ONCE and build MPN/card_id to price lookup dict.

    Keys by both MPN string (uppercase) and material_card_id so callers
    can look up by either.  card_id keys are prefixed with ``card:`` to
    avoid collisions with MPN strings.
    """
    quotes = (
        db.query(Quote)
        .filter(Quote.status.in_(_PRICED_STATUSES))
        .order_by(Quote.sent_at.desc().nullslast(), Quote.created_at.desc())
        .limit(100)
        .all()
    )
    result: dict[str, dict] = {}
    for q in quotes:
        date_str = _quote_date_iso(q)
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


def quote_to_dict(q: Quote, db=None) -> dict:
    """Serialize a Quote to API response dict."""
    enriched_items = q.line_items or []
    if db and enriched_items:
        try:
            card_ids = [li.get("material_card_id") for li in enriched_items if li.get("material_card_id")]
            if card_ids:
                from ...models import MaterialCard

                cards = {c.id: c for c in db.query(MaterialCard).filter(MaterialCard.id.in_(card_ids)).all()}
                enriched_items = []
                for li in q.line_items or []:
                    item = dict(li)
                    card = cards.get(li.get("material_card_id"))
                    if card:
                        item.setdefault("description", card.description)
                        item.setdefault("category", card.category)
                    enriched_items.append(item)
        except Exception:
            from loguru import logger

            logger.warning("MaterialCard enrichment failed for quote {}, returning raw items", q.id)
            enriched_items = q.line_items or []
    # Compute expiration fields
    is_expired = False
    days_until_expiry = None
    if q.sent_at and q.validity_days:
        sent_date = q.sent_at.date() if hasattr(q.sent_at, "date") else q.sent_at
        expiry_date = sent_date + timedelta(days=q.validity_days)
        days_until_expiry = (expiry_date - date.today()).days
        is_expired = days_until_expiry < 0

    return {
        "id": q.id,
        "requisition_id": q.requisition_id,
        "customer_site_id": q.customer_site_id,
        "customer_name": (
            f"{q.customer_site.company.name} — {q.customer_site.site_name}"
            if q.customer_site and q.customer_site.company
            else ""
        ),
        "company_domain": (q.customer_site.company.domain if q.customer_site and q.customer_site.company else None),
        "company_name_short": (q.customer_site.company.name if q.customer_site and q.customer_site.company else None),
        "contact_name": q.customer_site.contact_name if q.customer_site else None,
        "contact_email": q.customer_site.contact_email if q.customer_site else None,
        "site_contacts": [
            {
                "id": c.id,
                "full_name": c.full_name,
                "email": c.email,
                "title": c.title,
                "is_primary": c.is_primary,
            }
            for c in (q.customer_site.site_contacts if q.customer_site else [])
        ],
        "quote_number": q.quote_number,
        "revision": q.revision,
        "line_items": enriched_items,
        "subtotal": _float(q.subtotal),
        "total_cost": _float(q.total_cost),
        "total_margin_pct": _float(q.total_margin_pct),
        "payment_terms": q.payment_terms,
        "shipping_terms": q.shipping_terms,
        "validity_days": q.validity_days,
        "notes": q.notes,
        "status": q.status,
        "sent_at": _iso(q.sent_at),
        "result": q.result,
        "result_reason": q.result_reason,
        "result_notes": q.result_notes,
        "result_at": _iso(q.result_at),
        "won_revenue": _float(q.won_revenue),
        "created_by": q.created_by.name if q.created_by else None,
        "created_at": _iso(q.created_at),
        "updated_at": _iso(q.updated_at),
        "is_expired": is_expired,
        "days_until_expiry": days_until_expiry,
    }
