"""proactive_service.py — Proactive match retrieval, prepare/send flow, scorecard.

Read/present side of the proactive surface (the matching engine itself lives in
proactive_matching.py, seeded from windowed requirement history + hotlists).
Match rows carry the part-level supply rollup — available qty summed and low
cost MIN'd across live offers in the offer window — plus the demand signals
(requirement_count, last asked) the 2026-08-06 rework stores on each match.

Called by: routers/htmx/proactive.py (endpoints), services/proactive_digest
Depends on: models, config, utils/graph_client, services/proactive_matching
"""

import html
from datetime import UTC, datetime
from decimal import Decimal

from loguru import logger
from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session, joinedload

from ..constants import (
    ActivityType,
    OfferStatus,
    ProactiveMatchStatus,
    ProactiveOfferStatus,
    QuoteStatus,
    RequisitionStatus,
)
from ..models import (
    BuyPlan,
    Company,
    CustomerSite,
    Offer,
    ProactiveDoNotOffer,
    ProactiveMatch,
    ProactiveOffer,
    ProactiveThrottle,
    Quote,
    Requirement,
    Requisition,
    SiteContact,
    User,
)
from ..vendor_utils import normalize_vendor_name
from .activity_service import log_activity

# ── Match Retrieval ──────────────────────────────────────────────────────


def get_matches_for_user(
    db: Session,
    user_id: int,
    status: str = "new",
    *,
    admin_all: bool = False,
) -> dict:
    """Get proactive matches grouped by customer site, with stats.

    Returns {"groups": [...], "stats": {...}}.
    """
    query = db.query(ProactiveMatch)
    if not admin_all:
        query = query.filter(ProactiveMatch.salesperson_id == user_id)
    if status:
        query = query.filter(ProactiveMatch.status == status)

    query = query.options(
        joinedload(ProactiveMatch.offer).joinedload(Offer.vendor_card),
        joinedload(ProactiveMatch.offer).joinedload(Offer.entered_by),
        joinedload(ProactiveMatch.requisition),
        joinedload(ProactiveMatch.customer_site).joinedload(CustomerSite.company),
        # PERF-2: requirement is read (m.requirement.target_qty) inside the per-match
        # loop below — without this joinedload it was one lazy SELECT per match.
        joinedload(ProactiveMatch.requirement),
    ).order_by(ProactiveMatch.match_score.desc(), ProactiveMatch.created_at.desc())
    matches = query.all()

    # Build the do-not-offer set for filtering. PERF-2: scope the query to the companies
    # actually present in this match set — the ProactiveDoNotOffer table only grows and a
    # company absent from the matches can never suppress one, so loading the whole table
    # every call was wasted work. (customer_site is joinedload-ed above, so resolving the
    # company_id here adds no query.)
    match_company_ids = {(m.company_id or (m.customer_site.company_id if m.customer_site else None)) for m in matches}
    match_company_ids.discard(None)
    dno_set: set = set()
    if match_company_ids:
        dno_rows = (
            db.query(ProactiveDoNotOffer.mpn, ProactiveDoNotOffer.company_id)
            .filter(ProactiveDoNotOffer.company_id.in_(match_company_ids))
            .all()
        )
        dno_set = {(r.mpn, r.company_id) for r in dno_rows}

    # Group by customer site
    groups: dict[int, dict] = {}
    all_scores = []
    all_margins = []
    high_margin_count = 0

    # Part-level supply rollup — ONE batch query for every distinct part in the
    # match set, so query count stays independent of match count (PERF guard).
    from .proactive_matching import compute_offer_rollups

    rollups = compute_offer_rollups(db, parts={(m.mpn or "").strip().upper() for m in matches if m.mpn})

    # Rep attribution for the manager's See-All view.
    rep_names: dict[int, str] = {}
    if admin_all:
        rep_ids = {m.salesperson_id for m in matches if m.salesperson_id}
        if rep_ids:
            rep_names = dict(db.execute(select(User.id, User.name).where(User.id.in_(rep_ids))).all())

    for m in matches:
        # Skip matches suppressed by do-not-offer
        site = m.customer_site
        company_id = m.company_id or (site.company_id if site else None)
        mpn_upper = (m.mpn or "").strip().upper()
        if company_id and (mpn_upper, company_id) in dno_set:
            continue
        rollup = rollups.get(mpn_upper) or {"offers": [], "offer_count": 0, "available_qty": 0, "low_cost": None}

        site_id = m.customer_site_id
        if site_id not in groups:
            company_name = ""
            if site and site.company:
                company_name = site.company.name
            groups[site_id] = {
                "customer_site_id": site_id,
                "company_id": company_id,
                "company_name": company_name,
                "site_name": site.site_name if site else "",
                "salesperson_name": rep_names.get(m.salesperson_id, "") if admin_all else "",
                "matches": [],
            }
        offer = m.offer
        score = m.match_score or 0
        margin = m.margin_pct
        all_scores.append(score)
        if margin is not None:
            all_margins.append(margin)
            if margin > 30:
                high_margin_count += 1

        # Vendor card info
        vc = offer.vendor_card if offer else None
        # Buyer info
        entered_by = offer.entered_by if offer else None

        groups[site_id]["matches"].append(
            {
                "id": m.id,
                "mpn": m.mpn,
                "offer_id": m.offer_id,
                "vendor_name": offer.vendor_name if offer else "",
                "qty_available": offer.qty_available if offer else 0,
                "target_qty": m.requirement.target_qty if m.requirement else 0,
                "unit_price": float(offer.unit_price) if offer and offer.unit_price else None,
                "condition": offer.condition if offer else "",
                "warranty": offer.warranty if offer else "",
                "lead_time": offer.lead_time if offer else "",
                "country_of_origin": offer.country_of_origin if offer else "",
                "manufacturer": offer.manufacturer if offer else "",
                "offer_created_at": offer.created_at.isoformat() if offer and offer.created_at else None,
                "original_req_name": m.requisition.name if m.requisition else "",
                "status": m.status,
                "created_at": m.created_at.isoformat() if m.created_at else None,
                # Part-level supply rollup (2026-08-06 rework — the actionable line)
                "available_qty": rollup["available_qty"],
                "low_cost": rollup["low_cost"],
                "offer_count": rollup["offer_count"],
                # Equivalence-pooled spellings (2026-08-08): AI guesses are
                # color-coded in the UI so a human double-checks them.
                "variants": rollup.get("variants", {}),
                "has_ai_variants": rollup.get("has_ai_variants", False),
                # Demand signals
                "requirement_count": m.requirement_count or 0,
                "last_asked_at": m.last_asked_at,
                "last_asked_qty": m.last_asked_qty,
                "match_source": m.match_source or "requirement",
                # CPH-enriched fields
                "match_score": score,
                "margin_pct": margin,
                "our_cost": m.our_cost,
                "customer_purchase_count": m.customer_purchase_count or 0,
                "customer_last_price": float(m.customer_last_price) if m.customer_last_price else None,
                "customer_last_purchased_at": m.customer_last_purchased_at.isoformat()
                if m.customer_last_purchased_at
                else None,
                # Vendor card fields
                "vendor_score": round(vc.vendor_score, 1) if vc and vc.vendor_score else None,
                "engagement_score": round(vc.engagement_score, 1) if vc and vc.engagement_score else None,
                "ghost_rate": round(vc.ghost_rate * 100, 1) if vc and vc.ghost_rate else None,
                "overall_win_rate": round(vc.overall_win_rate * 100, 1) if vc and vc.overall_win_rate else None,
                "vendor_total_wins": vc.total_wins if vc else 0,
                # Buyer info
                "entered_by_name": (entered_by.name or entered_by.email.split("@")[0]) if entered_by else "",
                "entered_by_id": offer.entered_by_id if offer else None,
            }
        )

    # Sort matches within each group by match_score descending
    for g in groups.values():
        g["matches"].sort(key=lambda m: m.get("match_score", 0), reverse=True)

    # Sort groups by total opportunity (sum of margins) descending
    groups_list = sorted(
        groups.values(),
        key=lambda g: sum(m.get("margin_pct") or 0 for m in g["matches"]),
        reverse=True,
    )

    total = sum(len(g["matches"]) for g in groups_list)
    return {
        "groups": groups_list,
        "stats": {
            "total": total,
            "avg_score": round(sum(all_scores) / total, 1) if total else 0,
            "avg_margin": round(sum(all_margins) / len(all_margins), 1) if all_margins else None,
            "high_margin_count": high_margin_count,
        },
    }


def get_match_count(db: Session, user_id: int) -> int:
    """Count new (unseen) matches for a salesperson.

    Used for nav badge.
    """
    return (
        db.query(ProactiveMatch)
        .filter(
            ProactiveMatch.salesperson_id == user_id,
            ProactiveMatch.status == ProactiveMatchStatus.NEW,
        )
        .count()
    )


# QC 2026-08-08: no trailing colon — invalidate_prefix appends ":*", so a
# trailing colon here produced "intel:proactive_picks::*" which matched
# nothing and left the 24h picks cache never invalidated.
PICKS_CACHE_PREFIX = "proactive_picks"


def get_top_picks(db: Session, user_id: int, *, limit: int = 10) -> list[dict]:
    """Top-ranked NEW matches for the picks strip, cached 24h per user.

    Deterministic — the composite match_score already carries repeat demand, quote-vs-
    cost spread, recent wins, and ask age (2026-08-06 rework). The cache is busted by
    the scan/rematch paths via PICKS_CACHE_PREFIX.
    """
    from ..cache.intel_cache import get_cached, set_cached
    from .proactive_matching import compute_offer_rollups

    cache_key = f"{PICKS_CACHE_PREFIX}:{user_id}"
    cached = get_cached(cache_key)
    if isinstance(cached, dict) and isinstance(cached.get("picks"), list):
        return list(cached["picks"])

    matches = db.scalars(
        select(ProactiveMatch)
        .where(
            ProactiveMatch.salesperson_id == user_id,
            ProactiveMatch.status == ProactiveMatchStatus.NEW,
        )
        .order_by(ProactiveMatch.match_score.desc(), ProactiveMatch.created_at.desc())
        .limit(limit)
    ).all()
    company_ids = {m.company_id for m in matches if m.company_id}
    company_names: dict[int, str] = {}
    if company_ids:
        company_names = dict(db.execute(select(Company.id, Company.name).where(Company.id.in_(company_ids))).all())

    rollups = compute_offer_rollups(db, parts={(m.mpn or "").strip().upper() for m in matches if m.mpn})
    picks = []
    for m in matches:
        rollup = rollups.get((m.mpn or "").strip().upper()) or {"offer_count": 0}
        if not rollup["offer_count"]:
            continue  # supply evaporated — not actionable, not a pick
        picks.append(
            {
                "match_id": m.id,
                "mpn": m.mpn,
                "company_name": company_names.get(m.company_id) or ("Trio Back Order" if not m.company_id else ""),
                "score": m.match_score or 0,
                "available_qty": rollup["available_qty"],
                "low_cost": rollup["low_cost"],
                "requirement_count": m.requirement_count or 0,
                "last_asked_display": (
                    f"{m.last_asked_at.month}/{m.last_asked_at.day}/{m.last_asked_at.year}" if m.last_asked_at else ""
                ),
            }
        )
    set_cached(cache_key, {"picks": picks}, ttl_days=1)
    return picks


# ── Send Proactive Offer ──────────────────────────────────────────────────


def _build_line_items(matches: list, sell_prices: dict) -> tuple[list, Decimal, Decimal]:
    """Line items + totals for a customer offer email.

    Shared by the prepare-page send and the Process draft builder. Missing sell prices
    default to cost x 1.3.
    """
    line_items = []
    total_sell = Decimal("0")
    total_cost = Decimal("0")
    for m in matches:
        offer = m.offer
        if not offer:
            continue
        cost = float(offer.unit_price) if offer.unit_price else 0
        # Never email a customer a $0.00 unit price (QC 2026-08-13, supersedes the
        # earlier "intentional 0 honored" note): use the seeded sell price if given,
        # else fall back to cost x 1.3, then skip the line if the resolved price is
        # non-positive (missing basis OR a 0/negative seed). The match stays NEW and
        # resurfaces — a proactive customer offer must carry a real price.
        sell = sell_prices.get(str(m.id))
        if sell is None and cost:
            sell = cost * 1.3
        if not sell or float(sell) <= 0:
            logger.warning("Proactive: skipping match {} ({}) — no positive sell price", m.id, m.mpn)
            continue
        target = m.requirement.target_qty if m.requirement else 0
        avail = offer.qty_available or 0
        qty = min(avail, target) if target > 0 else avail
        total_sell += Decimal(str(sell)) * qty
        total_cost += Decimal(str(cost)) * qty
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
    return line_items, total_sell, total_cost


def _template_email_html(
    salesperson_name: str, contacts: list, line_items: list, notes: str | None, intro_html: str | None = None
) -> str:
    """Customer-offer email body.

    The parts table + closing + signature ALWAYS render. ``intro_html`` (an
    AI-drafted or user-edited opening the rep reviewed) replaces the default
    greeting + boilerplate intro when supplied — so a customer never receives an
    offer whose parts table was dropped (QC 2026-08-10 P0-1). Callers must pass
    intro_html already sanitized/wrapped.
    """
    rows_html = ""
    for item in line_items:
        rows_html += f"""<tr>
            <td style="padding:6px 10px;border:1px solid #e5e7eb">{html.escape(str(item["mpn"]))}</td>
            <td style="padding:6px 10px;border:1px solid #e5e7eb">{html.escape(str(item.get("manufacturer", "")))}</td>
            <td style="padding:6px 10px;border:1px solid #e5e7eb;text-align:right">{item["qty"]:,}</td>
            <td style="padding:6px 10px;border:1px solid #e5e7eb;text-align:right">${item["sell_price"]:.4f}</td>
            <td style="padding:6px 10px;border:1px solid #e5e7eb">{html.escape(str(item.get("condition", "")))}</td>
            <td style="padding:6px 10px;border:1px solid #e5e7eb">{html.escape(str(item.get("lead_time", "")))}</td>
        </tr>"""

    contact_names = ", ".join(c.full_name for c in contacts if c.full_name)
    if len(contacts) == 1 and contacts[0].full_name:
        greeting = f"Hi {html.escape(str(contacts[0].full_name))},"
    elif contact_names:
        greeting = f"Hi {html.escape(str(contact_names))},"
    else:
        greeting = "Hello,"

    notes_html = f'<p style="margin-top:12px">{html.escape(str(notes))}</p>' if notes else ""

    if intro_html:
        intro = intro_html
    else:
        intro = (
            f"<p>{greeting}</p>"
            "<p>We have the following parts available that may be of interest based on "
            "your previous requirements:</p>"
        )

    return f"""
    <div style="font-family:Arial,sans-serif;max-width:700px">
        {intro}
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
        <p>Best regards,<br>{html.escape(str(salesperson_name))}<br>Trio Supply Chain Solutions</p>
    </div>
    """


async def send_proactive_offer(
    db: Session,
    user: User,
    token: str,
    match_ids: list[int],
    contact_ids: list[int],
    sell_prices: dict,
    subject: str | None = None,
    notes: str | None = None,
    email_html: str | None = None,
    allow_all: bool = False,
) -> dict:
    """Send a proactive offer email to a customer.

    ``allow_all`` lets a manager/admin send on a rep's matches; the offer is
    always attributed to the matches' salesperson, the mail goes out from the
    ACTOR's mailbox. Returns the created ProactiveOffer dict.
    """
    # Load and validate matches
    query = db.query(ProactiveMatch).filter(ProactiveMatch.id.in_(match_ids))
    if not allow_all:
        query = query.filter(ProactiveMatch.salesperson_id == user.id)
    matches = query.all()
    if not matches:
        raise ValueError("No valid matches found")

    # Confidentiality guard (QC 2026-08-13): every selected match must belong to
    # ONE customer site. A mixed match_ids selection would build one email body
    # from all matches, then send it (and file the throttle) under matches[0]'s
    # site — leaking one customer's parts/prices to another.
    if len({m.customer_site_id for m in matches}) > 1:
        raise ValueError("All selected matches must be for a single customer site")

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
    line_items, total_sell, total_cost = _build_line_items(matches, sell_prices)

    # Build email HTML — signed by the relationship owner (the matches' rep),
    # even when a manager is the one clicking send.
    owner_id = matches[0].salesperson_id or user.id
    owner = user if owner_id == user.id else (db.get(User, owner_id) or user)
    if not subject:
        subject = f"Parts Available — {company_name}"

    # QC 2026-08-10 P0-1: the parts table ALWAYS renders. An AI-drafted / edited
    # body becomes the intro; it no longer replaces (and drops) the whole email.
    salesperson_name = owner.name or owner.email.split("@")[0]
    html_body = _template_email_html(salesperson_name, contacts, line_items, notes, intro_html=email_html)

    # Create ProactiveOffer record first to get ID for subject tag
    po = ProactiveOffer(
        customer_site_id=site_id,
        salesperson_id=owner_id,
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

    # QC 2026-08-10 P0-1: no internal tracking tag in the customer subject
    # (nothing parsed it back; it just leaked "[AVAIL-PROACTIVE-123]" to buyers).
    tagged_subject = subject

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
        logger.info(f"Proactive offer #{po.id} sent to {', '.join(recipient_emails)}")
        # Update match statuses only on successful send
        for m in matches:
            m.status = ProactiveMatchStatus.SENT
        send_succeeded = True
    except Exception as e:
        logger.error(f"Failed to send proactive offer email: {e}")
        for m in matches:
            m.status = ProactiveMatchStatus.FAILED
        # The row was created with the model's 'sent'/sent_at defaults so the
        # subject tag could carry its id — make it honest (QC 2026-08-08): a
        # rejected send is FAILED, never counted or shown as delivered.
        po.status = ProactiveOfferStatus.FAILED
        po.sent_at = None
        send_succeeded = False

    # Upsert throttle entries only if the email was actually sent
    if send_succeeded:
        now = datetime.now(UTC)
        # Batch-load existing throttles for these MPNs (one query instead of N).
        mpns = {m.mpn for m in matches}
        throttle_by_mpn = {
            t.mpn: t
            for t in db.query(ProactiveThrottle).filter(
                ProactiveThrottle.mpn.in_(mpns),
                ProactiveThrottle.customer_site_id == site_id,
            )
        }
        for m in matches:
            existing_throttle = throttle_by_mpn.get(m.mpn)
            if existing_throttle:
                existing_throttle.last_offered_at = now
                existing_throttle.proactive_offer_id = po.id
            else:
                new_throttle = ProactiveThrottle(
                    mpn=m.mpn,
                    customer_site_id=site_id,
                    last_offered_at=now,
                    proactive_offer_id=po.id,
                )
                db.add(new_throttle)
                # Mirror autoflush: a later match with the same MPN updates this row.
                throttle_by_mpn[m.mpn] = new_throttle

        # A full prepare-page send supersedes any staged Process draft that
        # references the same matches — drop it so nothing double-sends.
        sent_match_ids = {m.id for m in matches}
        for draft in db.scalars(
            select(ProactiveOffer).where(
                ProactiveOffer.customer_site_id == site_id,
                ProactiveOffer.status == ProactiveOfferStatus.DRAFT,
            )
        ).all():
            draft_match_ids = {li.get("match_id") for li in (draft.line_items or [])}
            if draft_match_ids & sent_match_ids:
                db.delete(draft)

    db.commit()
    return _proactive_offer_to_dict(po)


# ── Process flow: per-customer draft offers (check → Process → review → send) ──


def _draft_queued_match_ids(db: Session) -> set[int]:
    """Match ids already referenced by a staged DRAFT offer (double-queue guard)."""
    queued: set[int] = set()
    for draft in db.scalars(select(ProactiveOffer).where(ProactiveOffer.status == ProactiveOfferStatus.DRAFT)).all():
        queued |= {li.get("match_id") for li in (draft.line_items or []) if li.get("match_id")}
    return queued


def process_matches(
    db: Session,
    user: User,
    *,
    send_ids: list[int],
    ignore_ids: list[int],
    allow_all: bool = False,
) -> dict:
    """The Matches tab's Process action: dismiss the ignores, stage the sends.

    Ignored matches are dismissed immediately (a future rematch can resurface the part).
    Send-marked matches become ONE DRAFT ProactiveOffer per customer — default contact,
    sell prices seeded from the last quote on the part (else cost x 1.3), template email
    body — staged for review. Nothing is emailed here. Caller commits.
    """
    ignored = 0
    if ignore_ids:
        conditions = [
            ProactiveMatch.id.in_(ignore_ids),
            ProactiveMatch.status == ProactiveMatchStatus.NEW,
        ]
        if not allow_all:
            conditions.append(ProactiveMatch.salesperson_id == user.id)
        ignored = db.execute(
            update(ProactiveMatch)
            .where(*conditions)
            .values(status=ProactiveMatchStatus.DISMISSED, dismiss_reason="processed_ignore")
            .execution_options(synchronize_session=False)
        ).rowcount
    staged = build_draft_offers(db, user, send_ids, allow_all=allow_all) if send_ids else {}
    return {"ignored": ignored, **staged}


def build_draft_offers(db: Session, user: User, match_ids: list[int], *, allow_all: bool = False) -> dict:
    """Stage one DRAFT ProactiveOffer per customer from the given matches."""
    from .pricing_history import last_quote_for_part

    stmt = select(ProactiveMatch).where(
        ProactiveMatch.id.in_(match_ids),
        ProactiveMatch.status == ProactiveMatchStatus.NEW,
    )
    if not allow_all:
        stmt = stmt.where(ProactiveMatch.salesperson_id == user.id)
    matches = list(
        db.scalars(stmt.options(joinedload(ProactiveMatch.offer), joinedload(ProactiveMatch.requirement))).unique()
    )

    queued = _draft_queued_match_ids(db)
    skipped_queued = len([m for m in matches if m.id in queued])
    matches = [m for m in matches if m.id not in queued]

    by_site: dict[int, list] = {}
    skipped_backorder = 0
    for m in matches:
        if m.customer_site_id:
            by_site.setdefault(m.customer_site_id, []).append(m)
        else:
            skipped_backorder += 1  # no customer account — nowhere to email

    drafts_created = 0
    needs_contact = 0
    for site_id, site_matches in by_site.items():
        site = db.get(CustomerSite, site_id)
        company_name = site.company.name if site and site.company else "Customer"
        contact = db.scalars(
            select(SiteContact)
            .where(
                SiteContact.customer_site_id == site_id,
                SiteContact.email.isnot(None),
                SiteContact.email != "",
            )
            .order_by(SiteContact.id)
        ).first()
        if not contact:
            needs_contact += 1

        sell_prices: dict[str, float] = {}
        for m in site_matches:
            anchor = last_quote_for_part(db, part=(m.mpn or "").strip().upper())
            if anchor and anchor["price"]:
                sell_prices[str(m.id)] = float(anchor["price"])

        line_items, total_sell, total_cost = _build_line_items(site_matches, sell_prices)
        if not line_items:
            continue

        owner_id = site_matches[0].salesperson_id or user.id
        owner = user if owner_id == user.id else (db.get(User, owner_id) or user)
        owner_name = owner.name or owner.email.split("@")[0]
        contacts = [contact] if contact else []

        db.add(
            ProactiveOffer(
                customer_site_id=site_id,
                salesperson_id=owner_id,
                status=ProactiveOfferStatus.DRAFT,
                sent_at=None,
                line_items=line_items,
                recipient_contact_ids=[c.id for c in contacts],
                recipient_emails=[c.email for c in contacts],
                subject=f"Parts Available — {company_name}",
                email_body_html=_template_email_html(owner_name, contacts, line_items, None),
                total_sell=total_sell,
                total_cost=total_cost,
            )
        )
        drafts_created += 1

    return {
        "drafts_created": drafts_created,
        "needs_contact": needs_contact,
        "skipped_backorder": skipped_backorder,
        "skipped_already_queued": skipped_queued,
    }


def list_draft_offers(db: Session, user: User, *, allow_all: bool = False) -> list[dict]:
    """Staged per-customer drafts for the review strip, newest first."""
    stmt = select(ProactiveOffer).where(ProactiveOffer.status == ProactiveOfferStatus.DRAFT)
    if not allow_all:
        stmt = stmt.where(ProactiveOffer.salesperson_id == user.id)
    drafts = (
        db.scalars(
            stmt.options(joinedload(ProactiveOffer.customer_site).joinedload(CustomerSite.company)).order_by(
                ProactiveOffer.id.desc()
            )
        )
        .unique()
        .all()
    )
    rep_names = dict(db.execute(select(User.id, User.name)).all())
    out = []
    for po in drafts:
        site = po.customer_site
        out.append(
            {
                "id": po.id,
                "customer_site_id": po.customer_site_id,
                "company_name": site.company.name if site and site.company else "Customer",
                "salesperson_name": rep_names.get(po.salesperson_id) or "—",
                "salesperson_id": po.salesperson_id,
                "recipient_emails": po.recipient_emails or [],
                "subject": po.subject,
                "body_html": po.email_body_html,
                "line_count": len(po.line_items or []),
                "match_ids": [li.get("match_id") for li in (po.line_items or []) if li.get("match_id")],
                "total_sell": float(po.total_sell) if po.total_sell else 0.0,
            }
        )
    return out


async def send_draft_offer(db: Session, user: User, token: str, po_id: int, *, allow_all: bool = False) -> dict:
    """Send one staged draft to its customer with the ACTOR's mailbox.

    Marks the offer SENT, flips its matches to SENT, and writes the same throttle
    entries as the prepare-page send. Raises ValueError on wrong status / no recipients
    / no permission.
    """
    from ..utils.graph_client import GraphAPIError, GraphClient

    po = db.get(ProactiveOffer, po_id)
    if not po or po.status != ProactiveOfferStatus.DRAFT:
        raise ValueError("Draft offer not found")
    if not allow_all and po.salesperson_id != user.id:
        raise ValueError("Not your draft")
    recipient_emails = po.recipient_emails or []
    if not recipient_emails:
        raise ValueError("No contact on file — open Prepare to pick or add one")

    # Atomic claim (QC 2026-08-14): flip DRAFT→SENT in ONE conditional UPDATE BEFORE the
    # Graph send. A concurrent send_draft_offer — or the digest-draft "supersede" sweep —
    # can then no longer touch this offer while it is in flight: only the caller whose
    # UPDATE matches status=DRAFT wins (rowcount 1); the rest see 0 rows and bail, so the
    # customer is never double-emailed and no StaleDataError races the delete. No row
    # lock is held across the await; a Graph rejection reverts the claim to DRAFT.
    now = datetime.now(UTC)
    claimed = db.execute(
        update(ProactiveOffer)
        .where(ProactiveOffer.id == po_id, ProactiveOffer.status == ProactiveOfferStatus.DRAFT)
        .values(status=ProactiveOfferStatus.SENT, sent_at=now)
    ).rowcount
    db.commit()
    if not claimed:
        raise ValueError("This draft was already sent (or is being sent).")
    db.refresh(po)

    # QC 2026-08-10 P0-1: no internal tracking tag in the customer subject.
    tagged_subject = po.subject
    gc = GraphClient(token)
    try:
        await gc.post_json(
            "/me/sendMail",
            {
                "message": {
                    "subject": tagged_subject,
                    "body": {"contentType": "HTML", "content": po.email_body_html},
                    "toRecipients": [{"emailAddress": {"address": e}} for e in recipient_emails],
                },
                "saveToSentItems": "true",
            },
        )
    except GraphAPIError as exc:
        # Revert the claim so the draft can be retried with one click.
        po.status = ProactiveOfferStatus.DRAFT
        po.sent_at = None
        db.commit()
        logger.error("Proactive draft #{} send rejected by Graph: {}", po.id, exc)
        raise ValueError("Send failed — Microsoft rejected the message; the draft is untouched, try again") from exc
    # po is already SENT (claimed above); continue to flip matches + write throttles.

    match_ids = [li.get("match_id") for li in (po.line_items or []) if li.get("match_id")]
    matches = list(db.scalars(select(ProactiveMatch).where(ProactiveMatch.id.in_(match_ids)))) if match_ids else []
    throttle_by_mpn = {
        t.mpn: t
        for t in db.scalars(
            select(ProactiveThrottle).where(
                ProactiveThrottle.mpn.in_({m.mpn for m in matches} or {""}),
                ProactiveThrottle.customer_site_id == po.customer_site_id,
            )
        )
    }
    for m in matches:
        m.status = ProactiveMatchStatus.SENT
        existing_throttle = throttle_by_mpn.get(m.mpn)
        if existing_throttle:
            existing_throttle.last_offered_at = now
            existing_throttle.proactive_offer_id = po.id
        else:
            new_throttle = ProactiveThrottle(
                mpn=m.mpn,
                customer_site_id=po.customer_site_id,
                last_offered_at=now,
                proactive_offer_id=po.id,
            )
            db.add(new_throttle)
            throttle_by_mpn[m.mpn] = new_throttle

    logger.info("Proactive draft #{} sent to {} by {}", po.id, ", ".join(recipient_emails), user.email)
    db.commit()
    return _proactive_offer_to_dict(po)


def discard_draft_offer(db: Session, user: User, po_id: int, *, allow_all: bool = False) -> None:
    """Delete a staged draft; its matches stay NEW (nothing was sent).

    Caller commits.
    """
    po = db.get(ProactiveOffer, po_id)
    if not po or po.status != ProactiveOfferStatus.DRAFT:
        raise ValueError("Draft offer not found")
    if not allow_all and po.salesperson_id != user.id:
        raise ValueError("Not your draft")
    db.delete(po)


# ── Conversion to Win ──────────────────────────────────────────────────────


def convert_proactive_to_win(db: Session, proactive_offer_id: int, user: User) -> dict:
    """Convert a proactive offer to a won requisition + quote + buy plan."""
    from .crm_service import next_quote_number

    po = db.get(ProactiveOffer, proactive_offer_id)
    if not po:
        raise ValueError("Proactive offer not found")
    if po.salesperson_id and po.salesperson_id != user.id:
        raise ValueError("Not your proactive offer")
    if po.status == ProactiveOfferStatus.CONVERTED:
        raise ValueError("Already converted")

    site = db.get(CustomerSite, po.customer_site_id)
    company_name = site.company.name if site and site.company else "Customer"
    date_str = datetime.now(UTC).strftime("%b %Y")

    # Create requisition
    req = Requisition(
        name=f"Proactive — {company_name} — {date_str}",
        customer_site_id=po.customer_site_id,
        status=RequisitionStatus.WON,
        created_by=user.id,
    )
    db.add(req)
    db.flush()

    # Create requirements and build quote line items
    quote_line_items = []
    offer_ids = []
    for item in po.line_items or []:
        # Create a requirement
        from ..utils.normalization import normalize_mpn, normalize_mpn_key, normalize_quantity

        norm_mpn = normalize_mpn(item["mpn"]) or item["mpn"]
        requirement = Requirement(
            requisition_id=req.id,
            primary_mpn=norm_mpn,
            normalized_mpn=normalize_mpn_key(norm_mpn),
            target_qty=normalize_quantity(item.get("qty")) or item.get("qty", 0),
        )
        db.add(requirement)
        db.flush()

        # Clone the offer
        orig_offer = db.get(Offer, item.get("offer_id"))
        new_offer = Offer(
            requisition_id=req.id,
            requirement_id=requirement.id,
            vendor_name=item.get("vendor_name", ""),
            vendor_name_normalized=normalize_vendor_name(item.get("vendor_name", "")),
            mpn=item["mpn"],
            manufacturer=item.get("manufacturer", ""),
            qty_available=item.get("qty", 0),
            unit_price=item.get("unit_price"),
            lead_time=item.get("lead_time"),
            condition=item.get("condition"),
            entered_by_id=orig_offer.entered_by_id if orig_offer else user.id,
            source="proactive",
            status=OfferStatus.ACTIVE,
        )
        if orig_offer and orig_offer.vendor_card_id:
            new_offer.vendor_card_id = orig_offer.vendor_card_id
        db.add(new_offer)
        db.flush()
        offer_ids.append(new_offer.id)

        log_activity(
            db,
            activity_type=ActivityType.OFFER_CREATED,
            requisition_id=new_offer.requisition_id,
            requirement_id=new_offer.requirement_id,
            user_id=user.id,
            vendor_card_id=new_offer.vendor_card_id,
            description=f"Offer added: {new_offer.vendor_name} — {new_offer.mpn}",
            details={"offer_id": new_offer.id, "source": new_offer.source},
        )

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
    margin_pct = round((total_sell - total_cost_val) / total_sell * 100, 2) if total_sell > 0 else 0

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
        source="proactive",
        status=QuoteStatus.WON,
        result="won",
        result_at=datetime.now(UTC),
        won_revenue=total_sell,
    )
    db.add(quote)
    db.flush()

    # Create buy plan with relational lines
    from app.models.buy_plan import BuyPlanLine, BuyPlanStatus

    buy_plan = BuyPlan(
        requisition_id=req.id,
        quote_id=quote.id,
        status=BuyPlanStatus.PENDING.value,
        submitted_by_id=user.id,
        submitted_at=datetime.now(UTC),
    )
    db.add(buy_plan)
    db.flush()

    for item in po.line_items or []:
        line = BuyPlanLine(
            buy_plan_id=buy_plan.id,
            offer_id=item.get("offer_id"),
            quantity=item.get("qty", 0),
            unit_cost=float(item.get("unit_price") or 0),
            unit_sell=float(item.get("sell_price") or 0),
        )
        db.add(line)

    # Update proactive offer status
    po.status = ProactiveOfferStatus.CONVERTED
    po.converted_requisition_id = req.id
    po.converted_quote_id = quote.id
    po.converted_at = datetime.now(UTC)

    # Update matches
    for item in po.line_items or []:
        match_id = item.get("match_id")
        if match_id:
            match = db.get(ProactiveMatch, match_id)
            if match:
                match.status = ProactiveMatchStatus.CONVERTED

    db.commit()

    return {
        "requisition_id": req.id,
        "quote_id": quote.id,
        "buy_plan_id": buy_plan.id,
        "proactive_offer_id": po.id,
    }


# ── Scorecard ──────────────────────────────────────────────────────────────


def get_scorecard(db: Session, salesperson_id: int | None = None) -> dict:
    """Get proactive offer scorecard metrics using SQL aggregation."""
    CAP = 500_000

    # Capped value expression: values > cap become 0 (treated as bad data)
    def _capped(col):
        return case((col > CAP, 0), else_=col)

    # Capped sum of a column, restricted to rows in a given status.
    def _summed_when(status, col):
        return func.coalesce(
            func.sum(case((ProactiveOffer.status == status, _capped(func.coalesce(col, 0))))),
            0,
        )

    # Base filter — staged drafts aren't outreach yet and FAILED sends never
    # reached the customer; neither may inflate the sent counts.
    base_filter = [ProactiveOffer.status.notin_([ProactiveOfferStatus.DRAFT, ProactiveOfferStatus.FAILED])]
    if salesperson_id:
        base_filter.append(ProactiveOffer.salesperson_id == salesperson_id)

    # Main aggregation query
    row = (
        db.query(
            func.count(ProactiveOffer.id).label("sent"),
            func.count(case((ProactiveOffer.status == ProactiveOfferStatus.CONVERTED, 1))).label("converted"),
            _summed_when(ProactiveOfferStatus.CONVERTED, ProactiveOffer.total_sell).label("converted_revenue"),
            _summed_when(ProactiveOfferStatus.CONVERTED, ProactiveOffer.total_cost).label("converted_cost"),
            _summed_when(ProactiveOfferStatus.SENT, ProactiveOffer.total_sell).label("pending_revenue"),
            func.count(ProactiveOffer.converted_quote_id).label("quoted"),
        )
        .filter(*base_filter)
        .one()
    )

    sent = row.sent or 0
    converted = row.converted or 0
    converted_revenue = float(row.converted_revenue)
    converted_cost = float(row.converted_cost)
    gross_profit = converted_revenue - converted_cost
    pending_revenue = float(row.pending_revenue)
    quoted = row.quoted or 0

    # PO count — separate query joining BuyPlan via converted_quote_id
    po_subq = (
        db.query(ProactiveOffer.converted_quote_id)
        .filter(ProactiveOffer.converted_quote_id.isnot(None), *base_filter)
        .subquery()
    )
    po_count = (
        db.query(func.count(BuyPlan.id))
        .filter(
            BuyPlan.quote_id.in_(db.query(po_subq.c.converted_quote_id)),
            BuyPlan.status.in_(["active", "completed"]),
        )
        .scalar()
    ) or 0

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

    # Per-salesperson breakdown (admin view) — only when not filtered
    if not salesperson_id:
        breakdown_rows = (
            db.query(
                ProactiveOffer.salesperson_id,
                func.count(ProactiveOffer.id).label("sent"),
                func.count(case((ProactiveOffer.status == ProactiveOfferStatus.CONVERTED, 1))).label("converted"),
                _summed_when(ProactiveOfferStatus.CONVERTED, ProactiveOffer.total_sell).label("rev"),
                _summed_when(ProactiveOfferStatus.CONVERTED, ProactiveOffer.total_cost).label("cost"),
                _summed_when(ProactiveOfferStatus.SENT, ProactiveOffer.total_sell).label("pending"),
                func.count(ProactiveOffer.converted_quote_id).label("quoted"),
            )
            # Same base_filter as the top-line query (QC 2026-08-13): without it the
            # per-rep 'sent' counts included staged DRAFTs and FAILED sends, so the
            # breakdown didn't reconcile with the headline total.
            .filter(*base_filter)
            .group_by(ProactiveOffer.salesperson_id)
            .all()
        )

        # Fetch user names
        sp_ids = [r.salesperson_id for r in breakdown_rows]
        salespeople = db.query(User).filter(User.id.in_(sp_ids)).all() if sp_ids else []
        sales_map = {u.id: u.name or u.email.split("@")[0] for u in salespeople}

        # Batch quoted counts per salesperson (avoids N+1)
        quoted_by_sp = dict(
            db.query(ProactiveOffer.salesperson_id, func.count(ProactiveOffer.id))
            .filter(ProactiveOffer.converted_quote_id.isnot(None))
            .group_by(ProactiveOffer.salesperson_id)
            .all()
        )

        # Batch PO counts per salesperson (avoids N+1)
        po_by_sp = dict(
            db.query(ProactiveOffer.salesperson_id, func.count(BuyPlan.id))
            .join(BuyPlan, BuyPlan.quote_id == ProactiveOffer.converted_quote_id)
            .filter(BuyPlan.status.in_(["active", "completed"]))
            .group_by(ProactiveOffer.salesperson_id)
            .all()
        )

        breakdown = []
        for r in breakdown_rows:
            sid = r.salesperson_id
            u_sent = r.sent or 0
            u_conv = r.converted or 0
            u_rev = float(r.rev)
            u_cost = float(r.cost)
            u_pending = float(r.pending)
            u_quoted = quoted_by_sp.get(sid, 0)
            u_po = po_by_sp.get(sid, 0)

            breakdown.append(
                {
                    "salesperson_id": sid,
                    "salesperson_name": sales_map.get(sid, "Unknown"),
                    "sent": u_sent,
                    "converted": u_conv,
                    "quoted": u_quoted,
                    "po": u_po,
                    "conversion_rate": round(u_conv / u_sent * 100, 1) if u_sent > 0 else 0,
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
    """Get sent proactive offers for a salesperson, grouped by customer."""
    offers = (
        db.query(ProactiveOffer)
        .filter(
            ProactiveOffer.salesperson_id == user_id,
            ProactiveOffer.status != ProactiveOfferStatus.DRAFT,  # staged, not sent
        )
        .options(joinedload(ProactiveOffer.customer_site).joinedload(CustomerSite.company))
        .order_by(ProactiveOffer.sent_at.desc())
        .all()
    )
    # Group by company
    groups: dict[int, dict] = {}
    for o in offers:
        site = o.customer_site
        company_name = site.company.name if site and site.company else "Unknown"
        company_id = site.company_id if site else 0
        if company_id not in groups:
            groups[company_id] = {
                "company_name": company_name,
                "site_name": site.site_name if site else "",
                "company_id": company_id,
                "offers": [],
            }
        groups[company_id]["offers"].append(_proactive_offer_to_dict(o))
    return list(groups.values())


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
