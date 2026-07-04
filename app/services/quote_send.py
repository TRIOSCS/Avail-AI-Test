"""quote_send.py — Canonical quote-send service: the single place that emails a Quote to
the customer and records the result.

Both send routes call send_quote_email():
  - app/routers/htmx_views.py::send_quote_htmx  (POST /v2/partials/quotes/{id}/send)
  - app/routers/crm/quotes.py::send_quote        (POST /api/quotes/{id}/send)

It resolves the recipient, hard-blocks Do-Not-Contact recipients, builds the branded HTML
(via _build_quote_email_html, whose single home is now this module), sends through Microsoft
Graph (/me/sendMail), captures the sent message's Graph ids for reply threading, advances the
quote + requisition status, and writes an OUTBOUND email ActivityLog. In TESTING mode the
real Graph POST and Sent-Items lookup are skipped but the quote is still marked sent.

Depends on: app.utils.graph_client.GraphClient, app.email_service._find_sent_message,
app.services.status_machine.require_valid_transition, app.services.activity_service.
log_email_activity, app.models (Quote, Requisition, CustomerSite, SiteContact).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..constants import QuoteStatus, RequisitionStatus
from ..models import CustomerSite, Quote, Requisition, SiteContact, User
from ..utils.timezones import DEFAULT_DISPLAY_TZ, format_localdate
from .status_machine import require_valid_transition


@dataclass(frozen=True)
class SendQuoteResult:
    """Outcome of a successful quote send — mapped to each route's response shape."""

    sent_to: str
    status: str
    req_status: str | None
    status_changed: bool
    graph_message_id: str | None


class QuoteSendDNCBlocked(Exception):
    """Raised when the resolved recipient is on the do-not-contact list."""

    def __init__(self, recipient: str):
        self.recipient = recipient
        super().__init__(f"Recipient {recipient} is on the do-not-contact list")


class QuoteSendError(Exception):
    """Raised for a recoverable send failure (no/invalid recipient or Graph error)."""

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


def _recipient_is_dnc(db: Session, site: CustomerSite | None, recipient: str) -> bool:
    """True if the site itself is DNC or any SiteContact for that site with a matching
    (case-insensitive) email is flagged do_not_contact.

    Mirrors the vendor-reply idiom in htmx_views.send_reply_htmx.
    """
    if site is not None and site.do_not_contact:
        return True
    contact = (
        db.query(SiteContact)
        .filter(
            SiteContact.customer_site_id == (site.id if site else None),
            func.lower(SiteContact.email) == recipient.lower(),
            SiteContact.do_not_contact.is_(True),
        )
        .first()
    )
    return contact is not None


async def send_quote_email(
    db: Session,
    quote: Quote,
    user: User,
    *,
    token: str,
    override_email: str | None = None,
    override_name: str | None = None,
    testing: bool = False,
) -> SendQuoteResult:
    """Email ``quote`` to the customer and record the send.

    Resolves the recipient (override else the site contact), hard-blocks DNC recipients,
    sends via Graph (skipped under ``testing``), captures the Graph message ids, advances
    the quote→SENT and requisition→QUOTED, writes an outbound ActivityLog, and commits.

    Raises QuoteSendDNCBlocked (recipient on DNC) or QuoteSendError (no/invalid recipient,
    Graph error) — neither mutates the quote status.
    """
    site = db.get(CustomerSite, quote.customer_site_id) if quote.customer_site_id else None

    # 1. Resolve + validate recipient (same messages as the legacy crm route).
    to_email = (override_email or "").strip() or (site.contact_email if site else None)
    if not to_email:
        raise QuoteSendError("No recipient email — select a contact or enter one manually")
    if "@" not in to_email:
        raise QuoteSendError(f"Invalid email address: {to_email}")

    to_name = (override_name or "").strip() or (site.contact_name if site else "") or ""
    company_name = site.company.name if site and site.company else ""

    # 2. DNC hard-block — customer quotes go to customers, but DNC still applies.
    if _recipient_is_dnc(db, site, to_email):
        raise QuoteSendDNCBlocked(to_email)

    # 3. Build the branded HTML body.
    html = _build_quote_email_html(quote, to_name, company_name, user)
    subject = f"Quote {quote.quote_number} — Trio Supply Chain Solutions"

    # 4. Send via Graph + capture the sent-message ids (skipped in TESTING).
    graph_message_id = None
    graph_conversation_id = None
    if not testing:
        from ..utils.graph_client import GraphClient

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
            raise QuoteSendError(f"Failed to send quote email: {result.get('detail', '')}")

        from ..email_service import _find_sent_message

        msg = await _find_sent_message(gc, subject, to_email)
        if msg:
            graph_message_id = msg["id"]
            graph_conversation_id = msg.get("conversationId")
            quote.graph_message_id = graph_message_id
            quote.graph_conversation_id = graph_conversation_id

    # 5. Advance quote + EVERY contributing requisition's status.
    require_valid_transition("quote", quote.status, QuoteStatus.SENT)
    quote.status = QuoteStatus.SENT
    quote.sent_at = datetime.now(timezone.utc)

    from .activity_service import log_email_activity
    from .quote_requisitions import requisition_ids_for_quote

    primary_req = db.get(Requisition, quote.requisition_id)
    primary_old_status = primary_req.status if primary_req else None

    # A combined quote spans multiple requisitions — advance each to QUOTED (unless already
    # WON/LOST) and write one OUTBOUND ActivityLog per requisition, so none is left behind
    # and each requisition's timeline records the send. `or [quote.requisition_id]` keeps
    # the primary covered even for a (pathological) quote with no join row. The response's
    # req_status/status_changed still reflect the PRIMARY only (unchanged shape).
    for rid in requisition_ids_for_quote(db, quote.id) or [quote.requisition_id]:
        r = db.get(Requisition, rid)
        if r and r.status not in (RequisitionStatus.WON, RequisitionStatus.LOST):
            r.status = RequisitionStatus.QUOTED
        # log_email_activity dedupes by external_id, so passing the SAME graph_message_id
        # for every contributing req would silently drop all but the first (a combined
        # quote's send would log activity on the primary req only). Keep the PRIMARY on the
        # raw graph id — the sent-folder reconcile (email_jobs) matches the ActivityLog by
        # that exact id and would create a duplicate otherwise — and give each SECONDARY req
        # a per-req-suffixed id so every contributing req records its own send.
        if graph_message_id and rid != quote.requisition_id:
            activity_external_id: str | None = f"{graph_message_id}:req{rid}"
        else:
            activity_external_id = graph_message_id
        log_email_activity(
            user_id=user.id,
            direction="sent",
            email_addr=to_email,
            subject=f"Quote {quote.quote_number} sent",
            external_id=activity_external_id,
            contact_name=to_name or None,
            db=db,
            requisition_id=rid,
            occurred_at=quote.sent_at,
        )

    # 7. Commit and return.
    db.commit()
    logger.info("Quote {} sent to {} by {}", quote.quote_number, to_email, user.email)
    return SendQuoteResult(
        sent_to=to_email,
        status="sent",
        req_status=primary_req.status if primary_req else None,
        status_changed=bool(primary_req and primary_req.status != primary_old_status),
        graph_message_id=graph_message_id,
    )


def _build_quote_email_html(quote: Quote, to_name: str, company_name: str, user: User) -> str:
    """Build a professional HTML quote email with Trio branding."""
    import html as _html

    _esc = _html.escape

    BLUE = "#127fbf"
    NAVY = "#4a6fa5"
    DARK = "#282c30"

    validity = quote.validity_days or 7
    now_ts = quote.sent_at or datetime.now(timezone.utc)
    expires = now_ts + timedelta(days=validity)
    expires_str = format_localdate(expires, "%B %d, %Y", tz=DEFAULT_DISPLAY_TZ)
    date_str = format_localdate(now_ts, "%B %d, %Y", tz=DEFAULT_DISPLAY_TZ)

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
