"""
proactive_service.py — Background matching engine and proactive offer logic.

Scans newly-logged offers against archived requisitions (closed 30+ days).
Generates ProactiveMatch records for salespeople to review and send to customers.

Called by: scheduler.py (background scan), routers/proactive.py (endpoints)
Depends on: models, config, utils/graph_client
"""

import logging
import secrets
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    BuyPlan,
    CustomerSite,
    Offer,
    ProactiveMatch,
    ProactiveOffer,
    ProactiveThrottle,
    Quote,
    Requirement,
    Requisition,
    SiteContact,
    User,
)

log = logging.getLogger("avail.proactive")

_last_proactive_scan = datetime.min.replace(tzinfo=timezone.utc)


# ── Background Matching ──────────────────────────────────────────────────


def scan_new_offers_for_matches(db: Session) -> dict:
    """Scan recently logged offers for matches against archived requirements.

    Called by scheduler every tick. Returns {scanned, matches_created}.
    """
    global _last_proactive_scan
    now = datetime.now(timezone.utc)
    archive_cutoff = now - timedelta(days=settings.proactive_archive_age_days)
    throttle_cutoff = now - timedelta(days=settings.proactive_throttle_days)

    # Find offers created since last scan
    new_offers = (
        db.query(Offer)
        .filter(
            Offer.created_at > _last_proactive_scan,
            Offer.mpn.isnot(None),
        )
        .all()
    )

    _last_proactive_scan = now

    if not new_offers:
        return {"scanned": 0, "matches_created": 0}

    matches_created = 0
    for offer in new_offers:
        offer_mpn = (offer.mpn or "").strip().upper()
        if not offer_mpn or len(offer_mpn) < 3:
            continue

        # Find archived requirements with matching MPN
        candidates = (
            db.query(Requirement, Requisition)
            .join(Requisition, Requirement.requisition_id == Requisition.id)
            .filter(
                sqlfunc.upper(sqlfunc.btrim(Requirement.primary_mpn)) == offer_mpn,
                Requisition.status.in_(["archived", "won", "lost"]),
                Requisition.customer_site_id.isnot(None),
                Requisition.id != offer.requisition_id,  # Don't self-match
                Requisition.created_at < archive_cutoff,
            )
            .all()
        )

        for req_item, requisition in candidates:
            site_id = requisition.customer_site_id
            sales_id = requisition.created_by

            # Check throttle
            throttled = (
                db.query(ProactiveThrottle)
                .filter(
                    ProactiveThrottle.mpn == offer_mpn,
                    ProactiveThrottle.customer_site_id == site_id,
                    ProactiveThrottle.last_offered_at > throttle_cutoff,
                )
                .first()
            )
            if throttled:
                continue

            # Check dedup
            existing = (
                db.query(ProactiveMatch)
                .filter(
                    ProactiveMatch.offer_id == offer.id,
                    ProactiveMatch.requirement_id == req_item.id,
                )
                .first()
            )
            if existing:
                continue

            db.add(
                ProactiveMatch(
                    offer_id=offer.id,
                    requirement_id=req_item.id,
                    requisition_id=requisition.id,
                    customer_site_id=site_id,
                    salesperson_id=sales_id,
                    mpn=offer_mpn,
                )
            )
            matches_created += 1

    if matches_created:
        try:
            db.commit()
        except Exception as e:
            log.error(f"Failed to commit proactive matches: {e}")
            db.rollback()
            return {"scanned": len(new_offers), "matches_created": 0}

    return {"scanned": len(new_offers), "matches_created": matches_created}


# ── Match Retrieval ──────────────────────────────────────────────────────


def get_matches_for_user(db: Session, user_id: int, status: str = "new") -> list[dict]:
    """Get proactive matches grouped by customer site for a salesperson."""
    query = db.query(ProactiveMatch).filter(
        ProactiveMatch.salesperson_id == user_id,
    )
    if status:
        query = query.filter(ProactiveMatch.status == status)
    query = query.order_by(ProactiveMatch.created_at.desc())
    matches = query.all()

    # Group by customer site
    groups: dict[int, dict] = {}
    for m in matches:
        site_id = m.customer_site_id
        if site_id not in groups:
            site = m.customer_site
            company_name = ""
            if site and site.company:
                company_name = site.company.name
            groups[site_id] = {
                "customer_site_id": site_id,
                "company_name": company_name,
                "site_name": site.site_name if site else "",
                "matches": [],
            }
        offer = m.offer
        groups[site_id]["matches"].append(
            {
                "id": m.id,
                "mpn": m.mpn,
                "offer_id": m.offer_id,
                "vendor_name": offer.vendor_name if offer else "",
                "qty_available": offer.qty_available if offer else 0,
                "unit_price": float(offer.unit_price)
                if offer and offer.unit_price
                else None,
                "condition": offer.condition if offer else "",
                "lead_time": offer.lead_time if offer else "",
                "manufacturer": offer.manufacturer if offer else "",
                "offer_created_at": offer.created_at.isoformat()
                if offer and offer.created_at
                else None,
                "original_req_name": m.requisition.name if m.requisition else "",
                "status": m.status,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
        )

    return list(groups.values())


def get_match_count(db: Session, user_id: int) -> int:
    """Count new (unseen) matches for a salesperson. Used for nav badge."""
    return (
        db.query(ProactiveMatch)
        .filter(
            ProactiveMatch.salesperson_id == user_id,
            ProactiveMatch.status == "new",
        )
        .count()
    )


# ── Send Proactive Offer ──────────────────────────────────────────────────


async def send_proactive_offer(
    db: Session,
    user: User,
    token: str,
    match_ids: list[int],
    contact_ids: list[int],
    sell_prices: dict,
    subject: str | None = None,
    notes: str | None = None,
) -> dict:
    """Send a proactive offer email to a customer. Returns the created ProactiveOffer dict."""
    # Load and validate matches
    matches = (
        db.query(ProactiveMatch)
        .filter(
            ProactiveMatch.id.in_(match_ids),
            ProactiveMatch.salesperson_id == user.id,
        )
        .all()
    )
    if not matches:
        raise ValueError("No valid matches found")

    site_id = matches[0].customer_site_id
    site = db.get(CustomerSite, site_id)
    company = site.company if site else None
    company_name = company.name if company else "Customer"

    # Load contacts
    contacts = (
        db.query(SiteContact)
        .filter(
            SiteContact.id.in_(contact_ids),
            SiteContact.customer_site_id == site_id,
        )
        .all()
    )
    if not contacts:
        raise ValueError("No valid contacts selected")

    recipient_emails = [c.email for c in contacts if c.email]
    if not recipient_emails:
        raise ValueError("Selected contacts have no email addresses")

    # Build line items
    line_items = []
    total_sell = Decimal("0")
    total_cost = Decimal("0")
    for m in matches:
        offer = m.offer
        if not offer:
            continue
        cost = float(offer.unit_price) if offer.unit_price else 0
        sell = sell_prices.get(str(m.id), cost * 1.3)
        qty = offer.qty_available or 0
        line_total_sell = Decimal(str(sell)) * qty
        line_total_cost = Decimal(str(cost)) * qty
        total_sell += line_total_sell
        total_cost += line_total_cost
        line_items.append(
            {
                "match_id": m.id,
                "offer_id": offer.id,
                "mpn": m.mpn,
                "vendor_name": offer.vendor_name,
                "manufacturer": offer.manufacturer,
                "qty": qty,
                "unit_price": cost,
                "sell_price": float(sell),
                "condition": offer.condition,
                "lead_time": offer.lead_time,
            }
        )

    # Build email HTML
    if not subject:
        subject = f"Parts Available — {company_name}"

    rows_html = ""
    for item in line_items:
        rows_html += f"""<tr>
            <td style="padding:6px 10px;border:1px solid #e5e7eb">{item["mpn"]}</td>
            <td style="padding:6px 10px;border:1px solid #e5e7eb">{item.get("manufacturer", "")}</td>
            <td style="padding:6px 10px;border:1px solid #e5e7eb;text-align:right">{item["qty"]:,}</td>
            <td style="padding:6px 10px;border:1px solid #e5e7eb;text-align:right">${item["sell_price"]:.4f}</td>
            <td style="padding:6px 10px;border:1px solid #e5e7eb">{item.get("condition", "")}</td>
            <td style="padding:6px 10px;border:1px solid #e5e7eb">{item.get("lead_time", "")}</td>
        </tr>"""

    contact_names = ", ".join(c.full_name for c in contacts if c.full_name)
    greeting = (
        f"Hi {contacts[0].full_name},"
        if len(contacts) == 1 and contacts[0].full_name
        else f"Hi {contact_names},"
        if contact_names
        else "Hello,"
    )

    notes_html = f'<p style="margin-top:12px">{notes}</p>' if notes else ""
    salesperson_name = user.name or user.email.split("@")[0]

    html_body = f"""
    <div style="font-family:Arial,sans-serif;max-width:700px">
        <p>{greeting}</p>
        <p>We have the following parts available that may be of interest based on your previous requirements:</p>
        <table style="border-collapse:collapse;width:100%;margin:16px 0">
            <thead><tr style="background:#f3f4f6">
                <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:left">Part Number</th>
                <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:left">Manufacturer</th>
                <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:right">Qty Available</th>
                <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:right">Unit Price</th>
                <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:left">Condition</th>
                <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:left">Lead Time</th>
            </tr></thead>
            <tbody>{rows_html}</tbody>
        </table>
        {notes_html}
        <p>Please reply to this email if you'd like to place an order or need more details on any of these items.</p>
        <p>Best regards,<br>{salesperson_name}<br>Trio Supply Chain Solutions</p>
    </div>
    """

    # Create ProactiveOffer record first to get ID for subject tag
    po = ProactiveOffer(
        customer_site_id=site_id,
        salesperson_id=user.id,
        line_items=line_items,
        recipient_contact_ids=[c.id for c in contacts],
        recipient_emails=recipient_emails,
        subject=subject,
        email_body_html=html_body,
        total_sell=total_sell,
        total_cost=total_cost,
    )
    db.add(po)
    db.flush()

    tagged_subject = f"[AVAIL-PROACTIVE-{po.id}] {subject}"

    # Send email via Graph API
    try:
        from ..utils.graph_client import GraphClient

        gc = GraphClient(token)
        to_recipients = [{"emailAddress": {"address": e}} for e in recipient_emails]
        await gc.post_json(
            "/me/sendMail",
            {
                "message": {
                    "subject": tagged_subject,
                    "body": {"contentType": "HTML", "content": html_body},
                    "toRecipients": to_recipients,
                },
                "saveToSentItems": "true",
            },
        )
        log.info(f"Proactive offer #{po.id} sent to {', '.join(recipient_emails)}")
    except Exception as e:
        log.error(f"Failed to send proactive offer email: {e}")

    # Update match statuses
    for m in matches:
        m.status = "sent"

    # Upsert throttle entries
    now = datetime.now(timezone.utc)
    for m in matches:
        existing_throttle = (
            db.query(ProactiveThrottle)
            .filter(
                ProactiveThrottle.mpn == m.mpn,
                ProactiveThrottle.customer_site_id == site_id,
            )
            .first()
        )
        if existing_throttle:
            existing_throttle.last_offered_at = now
            existing_throttle.proactive_offer_id = po.id
        else:
            db.add(
                ProactiveThrottle(
                    mpn=m.mpn,
                    customer_site_id=site_id,
                    last_offered_at=now,
                    proactive_offer_id=po.id,
                )
            )

    db.commit()
    return _proactive_offer_to_dict(po)


# ── Conversion to Win ──────────────────────────────────────────────────────


def convert_proactive_to_win(db: Session, proactive_offer_id: int, user: User) -> dict:
    """Convert a proactive offer to a won requisition + quote + buy plan."""
    from ..routers.crm import next_quote_number

    po = db.get(ProactiveOffer, proactive_offer_id)
    if not po:
        raise ValueError("Proactive offer not found")
    if po.salesperson_id != user.id:
        raise ValueError("Not your proactive offer")
    if po.status == "converted":
        raise ValueError("Already converted")

    site = db.get(CustomerSite, po.customer_site_id)
    company_name = site.company.name if site and site.company else "Customer"
    date_str = datetime.now(timezone.utc).strftime("%b %Y")

    # Create requisition
    req = Requisition(
        name=f"Proactive — {company_name} — {date_str}",
        customer_site_id=po.customer_site_id,
        status="won",
        created_by=user.id,
    )
    db.add(req)
    db.flush()

    # Create requirements and build quote line items
    quote_line_items = []
    offer_ids = []
    for item in po.line_items or []:
        # Create a requirement
        from ..models import Requirement

        requirement = Requirement(
            requisition_id=req.id,
            primary_mpn=item["mpn"],
            target_qty=item.get("qty", 0),
        )
        db.add(requirement)
        db.flush()

        # Clone the offer
        orig_offer = db.get(Offer, item.get("offer_id"))
        new_offer = Offer(
            requisition_id=req.id,
            requirement_id=requirement.id,
            vendor_name=item.get("vendor_name", ""),
            mpn=item["mpn"],
            manufacturer=item.get("manufacturer", ""),
            qty_available=item.get("qty", 0),
            unit_price=item.get("unit_price"),
            lead_time=item.get("lead_time"),
            condition=item.get("condition"),
            entered_by_id=orig_offer.entered_by_id if orig_offer else user.id,
            source="proactive",
            status="active",
        )
        if orig_offer and orig_offer.vendor_card_id:
            new_offer.vendor_card_id = orig_offer.vendor_card_id
        db.add(new_offer)
        db.flush()
        offer_ids.append(new_offer.id)

        cost = float(item.get("unit_price") or 0)
        sell = float(item.get("sell_price") or cost)
        qty = item.get("qty", 0)
        margin = round((sell - cost) / sell * 100, 2) if sell > 0 else 0
        quote_line_items.append(
            {
                "mpn": item["mpn"],
                "manufacturer": item.get("manufacturer", ""),
                "qty": qty,
                "cost_price": cost,
                "sell_price": sell,
                "margin_pct": margin,
                "lead_time": item.get("lead_time"),
                "condition": item.get("condition"),
                "offer_id": new_offer.id,
            }
        )

    # Create quote
    total_sell = sum(li["qty"] * li["sell_price"] for li in quote_line_items)
    total_cost_val = sum(li["qty"] * li["cost_price"] for li in quote_line_items)
    margin_pct = (
        round((total_sell - total_cost_val) / total_sell * 100, 2)
        if total_sell > 0
        else 0
    )

    quote = Quote(
        requisition_id=req.id,
        customer_site_id=po.customer_site_id,
        quote_number=next_quote_number(db),
        line_items=quote_line_items,
        subtotal=total_sell,
        total_cost=total_cost_val,
        total_margin_pct=margin_pct,
        payment_terms=site.payment_terms if site else None,
        shipping_terms=site.shipping_terms if site else None,
        created_by_id=user.id,
        status="won",
        result="won",
        result_at=datetime.now(timezone.utc),
        won_revenue=total_sell,
    )
    db.add(quote)
    db.flush()

    # Create buy plan
    bp_line_items = []
    for item in po.line_items or []:
        orig_offer = db.get(Offer, item.get("offer_id"))
        bp_line_items.append(
            {
                "offer_id": item.get("offer_id"),
                "mpn": item["mpn"],
                "vendor_name": item.get("vendor_name", ""),
                "manufacturer": item.get("manufacturer", ""),
                "qty": item.get("qty", 0),
                "cost_price": float(item.get("unit_price") or 0),
                "lead_time": item.get("lead_time"),
                "condition": item.get("condition"),
                "entered_by_id": orig_offer.entered_by_id if orig_offer else None,
                "po_number": None,
                "po_sent_at": None,
                "po_recipient": None,
                "po_verified": False,
            }
        )

    buy_plan = BuyPlan(
        requisition_id=req.id,
        quote_id=quote.id,
        status="pending_approval",
        line_items=bp_line_items,
        submitted_by_id=user.id,
        approval_token=secrets.token_urlsafe(32),
    )
    db.add(buy_plan)

    # Update proactive offer status
    po.status = "converted"
    po.converted_requisition_id = req.id
    po.converted_quote_id = quote.id
    po.converted_at = datetime.now(timezone.utc)

    # Update matches
    for item in po.line_items or []:
        match_id = item.get("match_id")
        if match_id:
            match = db.get(ProactiveMatch, match_id)
            if match:
                match.status = "converted"

    db.commit()

    return {
        "requisition_id": req.id,
        "quote_id": quote.id,
        "buy_plan_id": buy_plan.id,
        "proactive_offer_id": po.id,
    }


# ── Scorecard ──────────────────────────────────────────────────────────────


def get_scorecard(db: Session, salesperson_id: int | None = None) -> dict:
    """Get proactive offer scorecard metrics."""
    query = db.query(ProactiveOffer)
    if salesperson_id:
        query = query.filter(ProactiveOffer.salesperson_id == salesperson_id)

    all_offers = query.all()
    sent = len(all_offers)
    converted = sum(1 for o in all_offers if o.status == "converted")
    sum(float(o.total_sell or 0) for o in all_offers)
    converted_revenue = sum(
        float(o.total_sell or 0) for o in all_offers if o.status == "converted"
    )
    converted_cost = sum(
        float(o.total_cost or 0) for o in all_offers if o.status == "converted"
    )
    gross_profit = converted_revenue - converted_cost
    pending_revenue = sum(
        float(o.total_sell or 0) for o in all_offers if o.status == "sent"
    )
    quoted = sum(1 for o in all_offers if o.converted_quote_id is not None)
    converted_quote_ids = [o.converted_quote_id for o in all_offers if o.converted_quote_id]
    po_count = 0
    if converted_quote_ids:
        po_count = db.query(BuyPlan).filter(
            BuyPlan.quote_id.in_(converted_quote_ids),
            BuyPlan.status.in_(["approved", "po_entered"]),
        ).count()

    result = {
        "total_sent": sent,
        "total_converted": converted,
        "total_quoted": quoted,
        "total_po": po_count,
        "conversion_rate": round(converted / sent * 100, 1) if sent > 0 else 0,
        "anticipated_revenue": round(pending_revenue, 2),
        "converted_revenue": round(converted_revenue, 2),
        "gross_profit": round(gross_profit, 2),
    }

    # Per-salesperson breakdown (for admin view)
    if not salesperson_id:
        sales_ids = {o.salesperson_id for o in all_offers}
        salespeople = (
            db.query(User).filter(User.id.in_(sales_ids)).all() if sales_ids else []
        )
        sales_map = {u.id: u.name or u.email.split("@")[0] for u in salespeople}
        breakdown = []
        for sid in sales_ids:
            user_offers = [o for o in all_offers if o.salesperson_id == sid]
            u_sent = len(user_offers)
            u_conv = sum(1 for o in user_offers if o.status == "converted")
            u_rev = sum(
                float(o.total_sell or 0) for o in user_offers if o.status == "converted"
            )
            u_cost = sum(
                float(o.total_cost or 0) for o in user_offers if o.status == "converted"
            )
            u_pending = sum(
                float(o.total_sell or 0) for o in user_offers if o.status == "sent"
            )
            u_quoted = sum(1 for o in user_offers if o.converted_quote_id is not None)
            u_quote_ids = [o.converted_quote_id for o in user_offers if o.converted_quote_id]
            u_po = 0
            if u_quote_ids:
                u_po = db.query(BuyPlan).filter(
                    BuyPlan.quote_id.in_(u_quote_ids),
                    BuyPlan.status.in_(["approved", "po_entered"]),
                ).count()
            breakdown.append(
                {
                    "salesperson_id": sid,
                    "salesperson_name": sales_map.get(sid, "Unknown"),
                    "sent": u_sent,
                    "converted": u_conv,
                    "quoted": u_quoted,
                    "po": u_po,
                    "conversion_rate": round(u_conv / u_sent * 100, 1)
                    if u_sent > 0
                    else 0,
                    "anticipated_revenue": round(u_pending, 2),
                    "revenue": round(u_rev, 2),
                    "gross_profit": round(u_rev - u_cost, 2),
                }
            )
        breakdown.sort(key=lambda x: x["converted"], reverse=True)
        result["breakdown"] = breakdown

    return result


# ── Sent Offers List ──────────────────────────────────────────────────────


def get_sent_offers(db: Session, user_id: int) -> list[dict]:
    """Get sent proactive offers for a salesperson."""
    offers = (
        db.query(ProactiveOffer)
        .filter(
            ProactiveOffer.salesperson_id == user_id,
        )
        .order_by(ProactiveOffer.sent_at.desc())
        .all()
    )
    return [_proactive_offer_to_dict(o) for o in offers]


def _proactive_offer_to_dict(po: ProactiveOffer) -> dict:
    site = po.customer_site
    company_name = site.company.name if site and site.company else ""
    return {
        "id": po.id,
        "customer_site_id": po.customer_site_id,
        "company_name": company_name,
        "site_name": site.site_name if site else "",
        "line_items": po.line_items or [],
        "recipient_emails": po.recipient_emails or [],
        "subject": po.subject,
        "status": po.status,
        "sent_at": po.sent_at.isoformat() if po.sent_at else None,
        "total_sell": float(po.total_sell) if po.total_sell else 0,
        "total_cost": float(po.total_cost) if po.total_cost else 0,
        "converted_requisition_id": po.converted_requisition_id,
        "converted_at": po.converted_at.isoformat() if po.converted_at else None,
    }
