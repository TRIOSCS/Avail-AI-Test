"""Shared helpers for CRM sub-routers."""

from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ...models import ChangeLog, Quote, User

# Late import — re-exported for backward compatibility
from ...services.crm_service import next_quote_number  # noqa: F401

# Statuses considered for pricing history lookups
_PRICED_STATUSES = ["sent", "won", "lost"]


def _quote_date_iso(q: Quote) -> str | None:
    """Return the best available date for a quote as an ISO string."""
    dt = q.sent_at or q.created_at
    return dt.isoformat() if dt else None


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


def get_last_quoted_price(mpn: str, db: Session) -> dict | None:
    """Find most recent sell price for an MPN across all quotes."""
    from ...models import MaterialCard
    from ...utils.normalization import normalize_mpn_key

    quotes = (
        db.query(Quote)
        .filter(Quote.status.in_(_PRICED_STATUSES))
        .order_by(Quote.sent_at.desc().nullslast(), Quote.created_at.desc())
        .limit(100)
        .all()
    )
    norm_key = normalize_mpn_key(mpn)
    card = db.query(MaterialCard).filter(MaterialCard.normalized_mpn == norm_key).first() if norm_key else None
    card_id = card.id if card else None
    mpn_upper = mpn.upper().strip()
    for q in quotes:
        for item in q.line_items or []:
            matched_by_card = card_id and item.get("material_card_id") == card_id
            matched_by_mpn = (item.get("mpn") or "").upper().strip() == mpn_upper
            if matched_by_card or matched_by_mpn:
                return {
                    "sell_price": item.get("sell_price"),
                    "margin_pct": item.get("margin_pct"),
                    "quote_number": q.quote_number,
                    "date": _quote_date_iso(q),
                    "result": q.result,
                }
    return None


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

            logger.warning("MaterialCard enrichment failed for quote %s, returning raw items", q.id)
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
        "subtotal": float(q.subtotal) if q.subtotal else None,
        "total_cost": float(q.total_cost) if q.total_cost else None,
        "total_margin_pct": float(q.total_margin_pct) if q.total_margin_pct else None,
        "payment_terms": q.payment_terms,
        "shipping_terms": q.shipping_terms,
        "validity_days": q.validity_days,
        "notes": q.notes,
        "status": q.status,
        "sent_at": q.sent_at.isoformat() if q.sent_at else None,
        "result": q.result,
        "result_reason": q.result_reason,
        "result_notes": q.result_notes,
        "result_at": q.result_at.isoformat() if q.result_at else None,
        "won_revenue": float(q.won_revenue) if q.won_revenue else None,
        "created_by": q.created_by.name if q.created_by else None,
        "created_at": q.created_at.isoformat() if q.created_at else None,
        "updated_at": q.updated_at.isoformat() if q.updated_at else None,
        "is_expired": is_expired,
        "days_until_expiry": days_until_expiry,
    }


def _build_quote_email_html(quote: Quote, to_name: str, company_name: str, user: User) -> str:
    """Build a professional HTML quote email with Trio branding."""
    import html as _html
    from datetime import timedelta

    _esc = _html.escape

    BLUE = "#127fbf"
    NAVY = "#4a6fa5"
    DARK = "#282c30"

    validity = quote.validity_days or 7
    now_ts = quote.sent_at or datetime.now(timezone.utc)
    expires = now_ts + timedelta(days=validity)
    expires_str = expires.strftime("%B %d, %Y")
    date_str = now_ts.strftime("%B %d, %Y")

    import re as _re

    def _fmt_price(v):
        if not v:
            return "—"
        v = float(v)
        return f"${v:,.2f}" if v % 1 >= 0.005 else f"${v:,.0f}"

    def _fmt_lead(s):
        if not s:
            return "—"
        s = s.strip()
        if _re.match(r"^\d+$", s):
            return s + " days"
        if _re.match(r"^\d+\s*-\s*\d+$", s):
            return _re.sub(r"\s*-\s*", "-", s) + " days"
        if _re.search(r"days?|wks?|weeks?", s, _re.I):
            return s
        return s + " days"

    # Build line items rows
    rows = ""
    row_bg = ["#ffffff", "#f8fafc"]
    for idx, item in enumerate(quote.line_items or []):
        sell = item.get("sell_price", 0)
        price = _fmt_price(sell)
        qty = f"{item.get('qty', 0):,}" if item.get("qty") else "—"
        ext_val = (sell or 0) * (item.get("qty") or 0)
        ext = f"${ext_val:,.2f}" if ext_val else "—"
        cond = item.get("condition") or "—"
        dc = item.get("date_code") or "—"
        pkg = item.get("packaging") or "—"
        lead = _fmt_lead(item.get("lead_time") or "")
        bg = row_bg[idx % 2]
        td = f'style="padding:10px 14px;border-bottom:1px solid #e8ecf0;background:{bg}"'
        rows += f"""<tr>
            <td {td}><strong>{_esc(item.get("mpn", ""))}</strong></td>
            <td {td}>{_esc(item.get("manufacturer", "") or "—")}</td>
            <td {td} style="padding:10px 14px;border-bottom:1px solid #e8ecf0;background:{bg};text-align:center">{qty}</td>
            <td {td}>{_esc(cond)}</td>
            <td {td}>{_esc(dc)}</td>
            <td {td}>{_esc(pkg)}</td>
            <td {td} style="padding:10px 14px;border-bottom:1px solid #e8ecf0;background:{bg};text-align:right;font-family:Consolas,monospace">{price}</td>
            <td {td} style="padding:10px 14px;border-bottom:1px solid #e8ecf0;background:{bg};text-align:right">{_esc(lead)}</td>
            <td {td} style="padding:10px 14px;border-bottom:1px solid #e8ecf0;background:{bg};text-align:right;font-weight:700;font-family:Consolas,monospace">{ext}</td>
        </tr>"""

    total = f"${float(quote.subtotal or 0):,.2f}"

    # Terms table
    terms_rows = ""
    if quote.payment_terms:
        terms_rows += f'<tr><td style="padding:6px 0;color:#8e8f92;width:120px">Payment</td><td style="padding:6px 0;font-weight:600">{_esc(quote.payment_terms)}</td></tr>'
    if quote.shipping_terms:
        terms_rows += f'<tr><td style="padding:6px 0;color:#8e8f92">Shipping</td><td style="padding:6px 0;font-weight:600">{_esc(quote.shipping_terms)}</td></tr>'
    terms_rows += '<tr><td style="padding:6px 0;color:#8e8f92">Currency</td><td style="padding:6px 0;font-weight:600">USD</td></tr>'
    terms_rows += f'<tr><td style="padding:6px 0;color:#8e8f92">Valid Until</td><td style="padding:6px 0;font-weight:600">{expires_str}</td></tr>'

    greeting = f"Dear {_esc(to_name)}," if to_name else "Dear Valued Customer,"
    notes_block = (
        f'<div style="margin-top:16px;padding:12px 16px;background:#F3F5F7;border-left:3px solid {BLUE};border-radius:4px;font-size:13px;color:#444">{_esc(quote.notes)}</div>'
        if quote.notes
        else ""
    )
    signature = user.email_signature or user.name or "Trio Supply Chain Solutions"
    sig_html = _esc(signature).replace("\n", "<br>")

    th_base = f"padding:10px 14px;border-bottom:2px solid {BLUE};font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:{DARK};background:#F3F5F7"

    return f"""<html><body style="margin:0;padding:0;background:#F3F5F7;font-family:Lato,Calibri,Arial,Helvetica,sans-serif;color:#020202;-webkit-font-smoothing:antialiased">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#F3F5F7;padding:32px 0">
<tr><td align="center">
<table width="684" cellpadding="0" cellspacing="0" style="border-radius:8px;overflow:hidden;box-shadow:0 2px 16px rgba(0,0,0,0.07)">
<tr><td style="background:{NAVY};padding:2px">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:6px;overflow:hidden">

<!-- Header: Logo + Company -->
<tr><td style="padding:32px 40px 24px">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
        <td style="vertical-align:middle"><img src="https://www.trioscs.com/wp-content/uploads/2022/02/TRIO_CV_400.png" alt="Trio Supply Chain Solutions" style="height:64px;display:inline-block"></td>
    </tr></table>
</td></tr>

<!-- Divider -->
<tr><td style="padding:0 40px"><div style="height:1px;background:#e8ecf0"></div></td></tr>

<!-- Quote title block -->
<tr><td style="padding:24px 40px 20px">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
        <td style="vertical-align:top">
            <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:{BLUE};margin-bottom:6px">Quotation</div>
            <div style="font-size:20px;font-weight:700;color:{DARK};line-height:1.2">{quote.quote_number}</div>
            <div style="font-size:12px;color:#8e8f92;margin-top:4px">Rev {quote.revision} &nbsp;&middot;&nbsp; {date_str}</div>
        </td>
        <td style="text-align:right;vertical-align:top">
            <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:{BLUE};margin-bottom:6px">Prepared For</div>
            <div style="font-size:14px;font-weight:600;color:{DARK}">{_esc(company_name or "")}</div>
            {f'<div style="font-size:12px;color:#8e8f92;margin-top:2px">{_esc(to_name)}</div>' if to_name else ""}
        </td>
    </tr></table>
</td></tr>

<!-- Body -->
<tr><td style="padding:0 40px 32px">
    <p style="margin:0 0 4px;font-size:15px;color:{DARK}">{greeting}</p>
    <p style="margin:0 0 28px;font-size:13px;color:#8e8f92;line-height:1.6">Thank you for your interest. Please find our quotation detailed below.</p>

    <!-- Line Items Table -->
    <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:12px;margin-bottom:4px;border:1px solid #e8ecf0;border-radius:6px">
        <thead>
            <tr>
                <th style="{th_base};text-align:left;border-top-left-radius:6px">Part Number</th>
                <th style="{th_base};text-align:left">Mfr</th>
                <th style="{th_base};text-align:center">Qty</th>
                <th style="{th_base};text-align:left">Cond</th>
                <th style="{th_base};text-align:left">Date Code</th>
                <th style="{th_base};text-align:left">Pkg</th>
                <th style="{th_base};text-align:right">Unit Price</th>
                <th style="{th_base};text-align:right">Lead Time</th>
                <th style="{th_base};text-align:right;border-top-right-radius:6px">Ext. Price</th>
            </tr>
        </thead>
        <tbody>{rows}</tbody>
    </table>

    <!-- Total -->
    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:28px">
        <tr>
            <td style="padding:16px 14px;text-align:right;font-size:13px;color:#8e8f92">Subtotal</td>
            <td style="padding:16px 14px;text-align:right;font-size:20px;font-weight:700;color:{NAVY};font-family:Consolas,monospace;width:140px;border-bottom:3px solid {BLUE}">{total}</td>
        </tr>
    </table>

    <!-- Terms -->
    <table cellpadding="0" cellspacing="0" style="font-size:13px;margin-bottom:16px;background:#FBFBFC;border-radius:6px;border:1px solid #e8ecf0;width:100%">
        <tr><td colspan="2" style="padding:12px 16px 6px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;color:{NAVY}">Terms</td></tr>
        {terms_rows.replace("padding:6px 0", "padding:6px 16px")}
        <tr><td colspan="2" style="height:8px"></td></tr>
    </table>

    {notes_block}

    <!-- Signature -->
    <div style="margin-top:32px;padding-top:20px;border-top:1px solid #e8ecf0">
        <p style="margin:0;font-size:13px;color:#555;line-height:1.6">{sig_html}</p>
    </div>
</td></tr>

<!-- Terms & Conditions -->
<tr><td style="padding:0 40px 28px">
    <details style="font-size:10px;color:#aaa">
    <summary style="font-size:10px;font-weight:700;color:#8e8f92;text-transform:uppercase;letter-spacing:0.5px;cursor:pointer;padding:8px 0">Terms &amp; Conditions</summary>
    <ol style="margin:8px 0 0;padding-left:16px;font-size:10px;color:#aaa;line-height:1.8">
        <li>This quotation is valid for the period stated above. Prices are subject to change after expiration.</li>
        <li>All prices are in USD unless otherwise stated. Sales tax is not included and will be applied where applicable.</li>
        <li>Payment terms are as stated above. Past-due invoices are subject to a 1.5% monthly finance charge.</li>
        <li>Delivery dates are estimated and subject to availability at time of order confirmation.</li>
        <li>All sales are subject to Trio Supply Chain Solutions' standard terms and conditions of sale.</li>
        <li>Cancellation or rescheduling of confirmed orders may be subject to restocking and/or cancellation fees.</li>
        <li>Warranty: Parts are warranted against defects for 90 days from date of shipment. Warranty does not cover misuse, modification, or improper installation.</li>
        <li>Trio Supply Chain Solutions shall not be liable for any indirect, incidental, or consequential damages arising from the sale or use of products.</li>
        <li>Export compliance: Buyer is responsible for compliance with all applicable export control laws and regulations.</li>
    </ol>
    </details>
</td></tr>

<!-- Footer -->
<tr><td style="background:{DARK};padding:16px 40px;border-top:2px solid {NAVY}">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
        <td style="font-size:11px;color:#8899aa;font-weight:600">Trio Supply Chain Solutions</td>
        <td style="text-align:right;font-size:11px"><a href="https://trioscs.com" style="color:{BLUE};text-decoration:none;font-weight:600">trioscs.com</a></td>
    </tr></table>
</td></tr>

</table>
</td></tr>
</table>
</td></tr></table>
</body></html>"""
