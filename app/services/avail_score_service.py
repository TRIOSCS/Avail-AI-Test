"""Avail Score Service — Behavior + Outcome scoring for buyer and sales leaderboards.

Computes 10 metrics per role (5 behaviors, 5 outcomes) each scored 0–10
for a total of 0–100.  Designed to reward healthy micro-behaviors AND results.

Buyer behaviors:  Speed to Source, Multi-Source, Vendor Follow-Up, Pipeline Hygiene, Stock Lists
Buyer outcomes:   Sourcing Ratio, Offer→Quote, Win Rate, BP Completion, Vendor Diversity

Sales behaviors:  Account Coverage, Outreach Consistency, Quote Follow-Up, Proactive Selling, New Biz
Sales outcomes:   Win Rate, Revenue, Quote Volume, Proactive Conversion, Strategic Wins

Bonus: 1st place $500, 2nd $250 — must meet minimum score thresholds.

Called by: scheduler.py (daily), routers/performance.py (on-demand)
Depends on: models (Requisition, Contact, Offer, Quote, BuyPlan, ActivityLog, etc.)
"""

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, func as sqlfunc
from sqlalchemy.orm import Session

from ..models import (
    ActivityLog,
    BuyPlan,
    Company,
    Contact,
    CustomerSite,
    Offer,
    ProactiveOffer,
    Quote,
    Requisition,
    SiteContact,
    StockListHash,
    User,
)
from ..models.performance import AvailScoreSnapshot

log = logging.getLogger("avail.avail_score")

# ── Bonus thresholds ─────────────────────────────────────────────────
BONUS_1ST = 500.0
BONUS_2ND = 250.0
QUALIFY_1ST = 60  # minimum score to win 1st
QUALIFY_2ND = 50  # minimum score to win 2nd
MIN_REQS_BUYER = 10  # minimum reqs to qualify as buyer
MIN_ACTIVITIES_SALES = 20  # minimum outbound activities to qualify as sales


# ── Tier scoring helper ──────────────────────────────────────────────
def _tier(value, thresholds):
    """Score 0–10 based on threshold tiers.

    thresholds: list of (threshold, score) pairs, checked in order.
    First threshold that value meets gets that score.
    """
    for threshold, score in thresholds:
        if value >= threshold:
            return score
    return 0


# ══════════════════════════════════════════════════════════════════════
#  BUYER AVAIL SCORE
# ══════════════════════════════════════════════════════════════════════

def compute_buyer_avail_score(db: Session, user_id: int, month: date) -> dict:
    """Compute all 10 buyer metrics for a given month.

    Returns dict with b1–b5, o1–o5 scores, labels, raw values, and totals.
    """
    month_start = month.replace(day=1)
    if month_start.month == 12:
        month_end = month_start.replace(year=month_start.year + 1, month=1)
    else:
        month_end = month_start.replace(month=month_start.month + 1)

    start_dt = datetime(month_start.year, month_start.month, month_start.day, tzinfo=timezone.utc)
    end_dt = datetime(month_end.year, month_end.month, month_end.day, tzinfo=timezone.utc)

    # ── Fetch user's reqs for the month ──
    # Include reqs the user created, sent RFQs on, or logged offers for
    created_ids = {
        rid for (rid,) in db.query(Requisition.id).filter(
            Requisition.created_by == user_id,
            Requisition.created_at >= start_dt,
            Requisition.created_at < end_dt,
        ).all()
    }
    rfq_req_ids = {
        rid for (rid,) in db.query(Contact.requisition_id.distinct()).filter(
            Contact.user_id == user_id,
            Contact.created_at >= start_dt,
            Contact.created_at < end_dt,
        ).all()
    }
    offer_req_ids = {
        rid for (rid,) in db.query(Offer.requisition_id.distinct()).filter(
            Offer.entered_by_id == user_id,
            Offer.created_at >= start_dt,
            Offer.created_at < end_dt,
        ).all() if rid is not None
    }
    all_req_ids = created_ids | rfq_req_ids | offer_req_ids
    user_reqs = db.query(Requisition).filter(Requisition.id.in_(all_req_ids)).all() if all_req_ids else []
    req_ids = [r.id for r in user_reqs]
    total_reqs = len(req_ids)

    # Pre-load offer IDs in quotes and buy plans (same pattern as existing leaderboard)
    quoted_offer_ids = set()
    for (items,) in (
        db.query(Quote.line_items)
        .filter(Quote.status.in_(["sent", "won", "lost"]))
        .all()
    ):
        for item in items or []:
            oid = item.get("offer_id")
            if oid:
                quoted_offer_ids.add(oid)

    po_confirmed_offer_ids = set()
    bp_offer_ids = set()
    for bp_status, items in db.query(BuyPlan.status, BuyPlan.line_items).all():
        for item in items or []:
            oid = item.get("offer_id")
            if oid:
                bp_offer_ids.add(oid)
                if bp_status in ("po_confirmed", "complete"):
                    po_confirmed_offer_ids.add(oid)

    # User's offers this month
    user_offers = (
        db.query(Offer)
        .filter(
            Offer.entered_by_id == user_id,
            Offer.created_at >= start_dt,
            Offer.created_at < end_dt,
        )
        .all()
    )
    user_offer_ids = {o.id for o in user_offers}

    # ── B1: Speed to Source ──
    # Avg hours from req created → first RFQ sent
    b1_score, b1_raw = _buyer_b1_speed_to_source(db, req_ids, user_reqs)

    # ── B2: Multi-Source Discipline ──
    # Avg distinct vendors contacted per req
    b2_score, b2_raw = _buyer_b2_multi_source(db, req_ids, user_id)

    # ── B3: Vendor Follow-Up ──
    # % of stale RFQs that got a 2nd contact
    b3_score, b3_raw = _buyer_b3_vendor_followup(db, req_ids, user_id, start_dt, end_dt)

    # ── B4: Pipeline Hygiene ──
    # % of reqs with offers within 5 days
    b4_score, b4_raw = _buyer_b4_pipeline_hygiene(db, req_ids, user_reqs)

    # ── B5: Stock List Processing ──
    b5_count = (
        db.query(sqlfunc.count(StockListHash.id))
        .filter(
            StockListHash.user_id == user_id,
            StockListHash.first_seen_at >= start_dt,
            StockListHash.first_seen_at < end_dt,
        )
        .scalar()
    ) or 0
    b5_score = _tier(b5_count, [(10, 10), (8, 8), (5, 6), (3, 4), (1, 2)])
    b5_raw = f"{b5_count} lists"

    behavior_total = b1_score + b2_score + b3_score + b4_score + b5_score

    # ── O1: Sourcing Ratio ──
    reqs_with_offers = 0
    if req_ids:
        reqs_with_offers = (
            db.query(sqlfunc.count(Requisition.id.distinct()))
            .join(Offer, Offer.requisition_id == Requisition.id)
            .filter(Requisition.id.in_(req_ids))
            .scalar()
        ) or 0
    sourcing_pct = round(reqs_with_offers / total_reqs * 100) if total_reqs else 0
    o1_score = _tier(sourcing_pct, [(90, 10), (80, 8), (70, 6), (60, 4), (1, 2)])
    o1_raw = f"{sourcing_pct}% ({reqs_with_offers}/{total_reqs})"

    # ── O2: Offer→Quote Rate ──
    offers_in_quotes = sum(1 for oid in user_offer_ids if oid in quoted_offer_ids)
    total_user_offers = len(user_offers)
    oq_pct = round(offers_in_quotes / total_user_offers * 100) if total_user_offers else 0
    o2_score = _tier(oq_pct, [(60, 10), (50, 8), (40, 6), (30, 4), (1, 2)])
    o2_raw = f"{oq_pct}% ({offers_in_quotes}/{total_user_offers})"

    # ── O3: Win Rate ──
    won = (
        db.query(sqlfunc.count(Quote.id))
        .filter(Quote.created_by_id == user_id, Quote.result == "won", Quote.result_at >= start_dt, Quote.result_at < end_dt)
        .scalar()
    ) or 0
    lost = (
        db.query(sqlfunc.count(Quote.id))
        .filter(Quote.created_by_id == user_id, Quote.result == "lost", Quote.result_at >= start_dt, Quote.result_at < end_dt)
        .scalar()
    ) or 0
    win_pct = round(won / (won + lost) * 100) if (won + lost) else 0
    o3_score = _tier(win_pct, [(60, 10), (50, 8), (40, 6), (30, 4), (1, 2)])
    o3_raw = f"{win_pct}% ({won}W/{lost}L)"

    # ── O4: Buy Plan Completion ──
    user_bp_offer_ids = user_offer_ids & bp_offer_ids
    user_po_offer_ids = user_offer_ids & po_confirmed_offer_ids
    bp_total = len(user_bp_offer_ids)
    bp_confirmed = len(user_po_offer_ids)
    bp_pct = round(bp_confirmed / bp_total * 100) if bp_total else 0
    o4_score = _tier(bp_pct, [(80, 10), (60, 8), (40, 6), (20, 4), (1, 2)])
    o4_raw = f"{bp_pct}% ({bp_confirmed}/{bp_total})"

    # ── O5: Vendor Diversity ──
    vendor_count = (
        db.query(sqlfunc.count(Offer.vendor_card_id.distinct()))
        .filter(
            Offer.entered_by_id == user_id,
            Offer.created_at >= start_dt,
            Offer.created_at < end_dt,
            Offer.vendor_card_id.isnot(None),
        )
        .scalar()
    ) or 0
    o5_score = _tier(vendor_count, [(15, 10), (12, 8), (8, 6), (5, 4), (1, 2)])
    o5_raw = f"{vendor_count} vendors"

    outcome_total = o1_score + o2_score + o3_score + o4_score + o5_score
    total_score = behavior_total + outcome_total

    return {
        "role_type": "buyer",
        "qualified": total_reqs >= MIN_REQS_BUYER,
        "b1_score": b1_score, "b1_label": "Speed to Source", "b1_raw": b1_raw,
        "b2_score": b2_score, "b2_label": "Multi-Source", "b2_raw": b2_raw,
        "b3_score": b3_score, "b3_label": "Vendor Follow-Up", "b3_raw": b3_raw,
        "b4_score": b4_score, "b4_label": "Pipeline Hygiene", "b4_raw": b4_raw,
        "b5_score": b5_score, "b5_label": "Stock Lists", "b5_raw": b5_raw,
        "behavior_total": behavior_total,
        "o1_score": o1_score, "o1_label": "Sourcing Ratio", "o1_raw": o1_raw,
        "o2_score": o2_score, "o2_label": "Offer→Quote", "o2_raw": o2_raw,
        "o3_score": o3_score, "o3_label": "Win Rate", "o3_raw": o3_raw,
        "o4_score": o4_score, "o4_label": "BP Completion", "o4_raw": o4_raw,
        "o5_score": o5_score, "o5_label": "Vendor Diversity", "o5_raw": o5_raw,
        "outcome_total": outcome_total,
        "total_score": total_score,
    }


def _buyer_b1_speed_to_source(db, req_ids, user_reqs):
    """B1: Avg hours from req created → first RFQ sent."""
    if not req_ids:
        return 0, "no reqs"

    # First contact per req
    first_contacts = (
        db.query(
            Contact.requisition_id,
            sqlfunc.min(Contact.created_at).label("first_at"),
        )
        .filter(Contact.requisition_id.in_(req_ids))
        .group_by(Contact.requisition_id)
        .all()
    )
    fc_map = {r: fa for r, fa in first_contacts}

    total_hours = 0
    counted = 0
    for req in user_reqs:
        first_at = fc_map.get(req.id)
        if first_at and req.created_at:
            req_created = req.created_at
            if req_created.tzinfo is None:
                req_created = req_created.replace(tzinfo=timezone.utc)
            if first_at.tzinfo is None:
                first_at = first_at.replace(tzinfo=timezone.utc)
            hours = (first_at - req_created).total_seconds() / 3600
            if hours >= 0:
                total_hours += hours
                counted += 1

    if counted == 0:
        return 0, "no RFQs sent"

    avg_hours = total_hours / counted
    # Lower is better
    score = _tier(1, [])  # default 0
    if avg_hours < 4:
        score = 10
    elif avg_hours < 8:
        score = 8
    elif avg_hours < 24:
        score = 6
    elif avg_hours < 48:
        score = 4
    elif avg_hours < 72:
        score = 2

    return score, f"{avg_hours:.1f}h avg"


def _buyer_b2_multi_source(db, req_ids, user_id):
    """B2: Avg distinct vendors contacted per req."""
    if not req_ids:
        return 0, "no reqs"

    vendor_counts = (
        db.query(
            Contact.requisition_id,
            sqlfunc.count(Contact.vendor_name_normalized.distinct()).label("cnt"),
        )
        .filter(
            Contact.requisition_id.in_(req_ids),
            Contact.user_id == user_id,
        )
        .group_by(Contact.requisition_id)
        .all()
    )

    if not vendor_counts:
        return 0, "0 vendors/req"

    avg_vendors = sum(vc.cnt for vc in vendor_counts) / len(vendor_counts)
    score = _tier(avg_vendors, [(4, 10), (3, 8), (2, 6), (1.5, 4), (1, 2)])
    return score, f"{avg_vendors:.1f} vendors/req"


def _buyer_b3_vendor_followup(db, req_ids, user_id, start_dt, end_dt):
    """B3: % of stale RFQs (>48h no reply) that got a follow-up contact."""
    if not req_ids:
        return 0, "no reqs"

    cutoff_48h = end_dt - timedelta(hours=48)

    # Stale: status=sent, created >48h ago, in this month's reqs
    stale_contacts = (
        db.query(Contact)
        .filter(
            Contact.requisition_id.in_(req_ids),
            Contact.user_id == user_id,
            Contact.status == "sent",
            Contact.created_at >= start_dt,
            Contact.created_at <= cutoff_48h,
        )
        .all()
    )

    if not stale_contacts:
        return 10, "no stale RFQs"  # all got responses = perfect

    # Check which stale contacts have a follow-up (same req + same vendor, later date)
    followed_up = 0
    for sc in stale_contacts:
        followup = (
            db.query(Contact.id)
            .filter(
                Contact.requisition_id == sc.requisition_id,
                Contact.vendor_name_normalized == sc.vendor_name_normalized,
                Contact.user_id == user_id,
                Contact.created_at > sc.created_at,
            )
            .first()
        )
        if followup:
            followed_up += 1

    pct = round(followed_up / len(stale_contacts) * 100)
    score = _tier(pct, [(80, 10), (60, 8), (40, 6), (20, 4), (1, 2)])
    return score, f"{pct}% ({followed_up}/{len(stale_contacts)})"


def _buyer_b4_pipeline_hygiene(db, req_ids, user_reqs):
    """B4: % of reqs that got at least one offer within 5 days of creation."""
    if not req_ids:
        return 0, "no reqs"

    progressed = 0
    for req in user_reqs:
        if not req.created_at:
            continue
        req_created = req.created_at
        if req_created.tzinfo is None:
            req_created = req_created.replace(tzinfo=timezone.utc)
        deadline = req_created + timedelta(days=5)

        has_offer = (
            db.query(Offer.id)
            .filter(
                Offer.requisition_id == req.id,
                Offer.created_at <= deadline,
            )
            .first()
        )
        if has_offer:
            progressed += 1

    pct = round(progressed / len(user_reqs) * 100) if user_reqs else 0
    score = _tier(pct, [(90, 10), (80, 8), (70, 6), (60, 4), (1, 2)])
    return score, f"{pct}% ({progressed}/{len(user_reqs)})"


# ══════════════════════════════════════════════════════════════════════
#  SALES AVAIL SCORE
# ══════════════════════════════════════════════════════════════════════

def compute_sales_avail_score(db: Session, user_id: int, month: date) -> dict:
    """Compute all 10 sales metrics for a given month."""
    month_start = month.replace(day=1)
    if month_start.month == 12:
        month_end = month_start.replace(year=month_start.year + 1, month=1)
    else:
        month_end = month_start.replace(month=month_start.month + 1)

    start_dt = datetime(month_start.year, month_start.month, month_start.day, tzinfo=timezone.utc)
    end_dt = datetime(month_end.year, month_end.month, month_end.day, tzinfo=timezone.utc)
    outbound_types = ("email_sent", "call_outbound")

    # ── B1: Account Coverage ──
    # % of owned accounts with outbound activity this month
    owned_company_ids = [
        r[0] for r in
        db.query(CustomerSite.company_id)
        .filter(CustomerSite.owner_id == user_id)
        .distinct()
        .all()
    ]
    total_owned = len(owned_company_ids)

    contacted_ids = set()
    if owned_company_ids:
        contacted_ids = {
            r[0] for r in
            db.query(ActivityLog.company_id)
            .filter(
                ActivityLog.user_id == user_id,
                ActivityLog.activity_type.in_(outbound_types),
                ActivityLog.company_id.in_(owned_company_ids),
                ActivityLog.created_at >= start_dt,
                ActivityLog.created_at < end_dt,
            )
            .distinct()
            .all()
        }
    coverage_pct = round(len(contacted_ids) / total_owned * 100) if total_owned else 0
    b1_score = _tier(coverage_pct, [(90, 10), (80, 8), (70, 6), (50, 4), (1, 2)])
    b1_raw = f"{coverage_pct}% ({len(contacted_ids)}/{total_owned})"

    # ── B2: Outreach Consistency ──
    # Distinct days this month with ≥1 outbound activity
    active_days = (
        db.query(sqlfunc.count(sqlfunc.distinct(sqlfunc.date(ActivityLog.created_at))))
        .filter(
            ActivityLog.user_id == user_id,
            ActivityLog.activity_type.in_(outbound_types),
            ActivityLog.created_at >= start_dt,
            ActivityLog.created_at < end_dt,
        )
        .scalar()
    ) or 0
    b2_score = _tier(active_days, [(18, 10), (15, 8), (12, 6), (8, 4), (1, 2)])
    b2_raw = f"{active_days} days"

    # ── B3: Quote Follow-Up ──
    # % of sent quotes that got a follow-up activity within 5 days
    b3_score, b3_raw = _sales_b3_quote_followup(db, user_id, start_dt, end_dt)

    # ── B4: Proactive Selling ──
    proactive_sent = (
        db.query(sqlfunc.count(ProactiveOffer.id))
        .filter(
            ProactiveOffer.salesperson_id == user_id,
            ProactiveOffer.sent_at >= start_dt,
            ProactiveOffer.sent_at < end_dt,
        )
        .scalar()
    ) or 0
    b4_score = _tier(proactive_sent, [(10, 10), (7, 8), (5, 6), (3, 4), (1, 2)])
    b4_raw = f"{proactive_sent} sent"

    # ── B5: New Business Dev ──
    new_accounts = (
        db.query(sqlfunc.count(Company.id))
        .filter(
            Company.account_owner_id == user_id,
            Company.created_at >= start_dt,
            Company.created_at < end_dt,
        )
        .scalar()
    ) or 0
    new_contacts = (
        db.query(sqlfunc.count(SiteContact.id))
        .join(CustomerSite, CustomerSite.id == SiteContact.customer_site_id)
        .filter(
            CustomerSite.owner_id == user_id,
            SiteContact.created_at >= start_dt,
            SiteContact.created_at < end_dt,
        )
        .scalar()
    ) or 0
    new_biz = new_accounts + new_contacts
    b5_score = _tier(new_biz, [(5, 10), (4, 8), (3, 6), (2, 4), (1, 2)])
    b5_raw = f"{new_accounts} accts + {new_contacts} contacts"

    behavior_total = b1_score + b2_score + b3_score + b4_score + b5_score

    # ── O1: Win Rate ──
    won = (
        db.query(sqlfunc.count(Quote.id))
        .filter(Quote.created_by_id == user_id, Quote.result == "won", Quote.result_at >= start_dt, Quote.result_at < end_dt)
        .scalar()
    ) or 0
    lost = (
        db.query(sqlfunc.count(Quote.id))
        .filter(Quote.created_by_id == user_id, Quote.result == "lost", Quote.result_at >= start_dt, Quote.result_at < end_dt)
        .scalar()
    ) or 0
    win_pct = round(won / (won + lost) * 100) if (won + lost) else 0
    o1_score = _tier(win_pct, [(60, 10), (50, 8), (40, 6), (30, 4), (1, 2)])
    o1_raw = f"{win_pct}% ({won}W/{lost}L)"

    # ── O2: Revenue (normalized to team median) ──
    # We score absolute revenue here; normalization happens at ranking time
    revenue = (
        db.query(sqlfunc.coalesce(sqlfunc.sum(Quote.won_revenue), 0))
        .filter(Quote.created_by_id == user_id, Quote.result == "won", Quote.result_at >= start_dt, Quote.result_at < end_dt)
        .scalar()
    ) or 0
    revenue = float(revenue)
    # Threshold-based: $50K+/mo = 10, $30K = 8, $15K = 6, $5K = 4, >0 = 2
    o2_score = _tier(revenue, [(50000, 10), (30000, 8), (15000, 6), (5000, 4), (1, 2)])
    o2_raw = f"${revenue:,.0f}"

    # ── O3: Quote Volume ──
    quotes_sent = (
        db.query(sqlfunc.count(Quote.id))
        .filter(
            Quote.created_by_id == user_id,
            Quote.sent_at >= start_dt,
            Quote.sent_at < end_dt,
        )
        .scalar()
    ) or 0
    o3_score = _tier(quotes_sent, [(15, 10), (12, 8), (8, 6), (5, 4), (1, 2)])
    o3_raw = f"{quotes_sent} quotes"

    # ── O4: Proactive Conversion ──
    proactive_converted = (
        db.query(sqlfunc.count(ProactiveOffer.id))
        .filter(
            ProactiveOffer.salesperson_id == user_id,
            ProactiveOffer.status == "converted",
            ProactiveOffer.converted_at >= start_dt,
            ProactiveOffer.converted_at < end_dt,
        )
        .scalar()
    ) or 0
    proactive_conv_pct = round(proactive_converted / proactive_sent * 100) if proactive_sent else 0
    o4_score = _tier(proactive_conv_pct, [(40, 10), (30, 8), (20, 6), (10, 4), (1, 2)])
    o4_raw = f"{proactive_conv_pct}% ({proactive_converted}/{proactive_sent})"

    # ── O5: Strategic Account Wins ──
    # Wins on quotes linked to strategic-flagged companies
    strategic_wins = 0
    if won > 0:
        strategic_wins = (
            db.query(sqlfunc.count(Quote.id))
            .join(CustomerSite, Quote.customer_site_id == CustomerSite.id)
            .join(Company, CustomerSite.company_id == Company.id)
            .filter(
                Quote.created_by_id == user_id,
                Quote.result == "won",
                Quote.result_at >= start_dt,
                Quote.result_at < end_dt,
                Company.is_strategic.is_(True),
            )
            .scalar()
        ) or 0
    o5_score = _tier(strategic_wins, [(5, 10), (4, 8), (3, 6), (2, 4), (1, 2)])
    o5_raw = f"{strategic_wins} strategic wins"

    outcome_total = o1_score + o2_score + o3_score + o4_score + o5_score
    total_score = behavior_total + outcome_total

    # Activity count for qualification
    total_activities = (
        db.query(sqlfunc.count(ActivityLog.id))
        .filter(
            ActivityLog.user_id == user_id,
            ActivityLog.activity_type.in_(outbound_types),
            ActivityLog.created_at >= start_dt,
            ActivityLog.created_at < end_dt,
        )
        .scalar()
    ) or 0

    return {
        "role_type": "sales",
        "qualified": total_activities >= MIN_ACTIVITIES_SALES,
        "b1_score": b1_score, "b1_label": "Account Coverage", "b1_raw": b1_raw,
        "b2_score": b2_score, "b2_label": "Outreach Consistency", "b2_raw": b2_raw,
        "b3_score": b3_score, "b3_label": "Quote Follow-Up", "b3_raw": b3_raw,
        "b4_score": b4_score, "b4_label": "Proactive Selling", "b4_raw": b4_raw,
        "b5_score": b5_score, "b5_label": "New Business", "b5_raw": b5_raw,
        "behavior_total": behavior_total,
        "o1_score": o1_score, "o1_label": "Win Rate", "o1_raw": o1_raw,
        "o2_score": o2_score, "o2_label": "Revenue", "o2_raw": o2_raw,
        "o3_score": o3_score, "o3_label": "Quote Volume", "o3_raw": o3_raw,
        "o4_score": o4_score, "o4_label": "Proactive Conversion", "o4_raw": o4_raw,
        "o5_score": o5_score, "o5_label": "Strategic Wins", "o5_raw": o5_raw,
        "outcome_total": outcome_total,
        "total_score": total_score,
    }


def _sales_b3_quote_followup(db, user_id, start_dt, end_dt):
    """B3: % of sent quotes followed up within 5 days."""
    sent_quotes = (
        db.query(Quote)
        .filter(
            Quote.created_by_id == user_id,
            Quote.sent_at >= start_dt,
            Quote.sent_at < end_dt,
            Quote.status.in_(["sent", "won", "lost"]),
        )
        .all()
    )

    if not sent_quotes:
        return 10, "no quotes sent"  # nothing to follow up on

    followed_up = 0
    for q in sent_quotes:
        if not q.sent_at:
            continue
        sent_at = q.sent_at
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        followup_deadline = sent_at + timedelta(days=5)

        # Look for outbound activity on the same company after quote was sent
        has_followup = (
            db.query(ActivityLog.id)
            .filter(
                ActivityLog.user_id == user_id,
                ActivityLog.activity_type.in_(("email_sent", "call_outbound")),
                ActivityLog.created_at > sent_at,
                ActivityLog.created_at <= followup_deadline,
            )
            .filter(
                # Match by company through the quote's customer_site
                ActivityLog.company_id.in_(
                    db.query(CustomerSite.company_id)
                    .filter(CustomerSite.id == q.customer_site_id)
                )
            )
            .first()
        )
        if has_followup:
            followed_up += 1

    pct = round(followed_up / len(sent_quotes) * 100)
    score = _tier(pct, [(80, 10), (60, 8), (40, 6), (20, 4), (1, 2)])
    return score, f"{pct}% ({followed_up}/{len(sent_quotes)})"


# ══════════════════════════════════════════════════════════════════════
#  BATCH COMPUTE + RANK + BONUS
# ══════════════════════════════════════════════════════════════════════

def compute_all_avail_scores(db: Session, month: date | None = None) -> dict:
    """Compute Avail Scores for all buyers and salespeople, rank, assign bonuses.

    Returns summary dict with counts.
    """
    month = (month or date.today()).replace(day=1)

    # Exclude system/bot users (e.g. AvailAI Agent)
    _human = [User.is_active.is_(True), ~User.email.like("%@availai.local")]

    # Everyone except pure sales gets buyer scores (buyers, traders, managers, admins)
    buyers = db.query(User).filter(User.role.in_(["buyer", "trader", "manager", "admin"]), *_human).all()
    sales = db.query(User).filter(User.role == "sales", *_human).all()
    # Traders, managers, admins also get sales scores
    multi_role = db.query(User).filter(User.role.in_(["trader", "manager", "admin"]), *_human).all()

    buyer_results = []
    for user in buyers:
        try:
            result = compute_buyer_avail_score(db, user.id, month)
            result["user_id"] = user.id
            result["user_name"] = user.name
            buyer_results.append(result)
        except Exception as e:
            log.error("Avail score error for buyer %s: %s", user.id, e)

    sales_results = []
    for user in sales + multi_role:
        try:
            result = compute_sales_avail_score(db, user.id, month)
            result["user_id"] = user.id
            result["user_name"] = user.name
            sales_results.append(result)
        except Exception as e:
            log.error("Avail score error for sales %s: %s", user.id, e)

    # Rank and assign bonuses
    _rank_and_bonus(buyer_results)
    _rank_and_bonus(sales_results)

    # Persist snapshots
    saved = 0
    for results in [buyer_results, sales_results]:
        for r in results:
            saved += _upsert_snapshot(db, r, month)

    db.commit()
    log.info("Avail scores computed: %d buyers, %d sales, %d saved", len(buyer_results), len(sales_results), saved)
    return {"buyers": len(buyer_results), "sales": len(sales_results), "saved": saved}


def _rank_and_bonus(results):
    """Sort by total_score desc, assign rank and bonus amounts.

    Ties broken by higher behavior_total (rewards process over luck).
    """
    results.sort(key=lambda r: (r["total_score"], r["behavior_total"]), reverse=True)

    for i, r in enumerate(results):
        r["rank"] = i + 1

    # Bonus assignment
    for r in results:
        r["bonus_amount"] = 0.0

    qualified = [r for r in results if r.get("qualified")]
    if len(qualified) >= 1 and qualified[0]["total_score"] >= QUALIFY_1ST:
        qualified[0]["bonus_amount"] = BONUS_1ST
    if len(qualified) >= 2 and qualified[1]["total_score"] >= QUALIFY_2ND:
        qualified[1]["bonus_amount"] = BONUS_2ND


def _upsert_snapshot(db: Session, result: dict, month: date) -> int:
    """Upsert an AvailScoreSnapshot row. Returns 1 if saved."""
    existing = (
        db.query(AvailScoreSnapshot)
        .filter(
            AvailScoreSnapshot.user_id == result["user_id"],
            AvailScoreSnapshot.month == month,
            AvailScoreSnapshot.role_type == result["role_type"],
        )
        .first()
    )

    snap = existing or AvailScoreSnapshot(
        user_id=result["user_id"],
        month=month,
        role_type=result["role_type"],
    )
    if not existing:
        db.add(snap)

    for key in ["b1_score", "b1_label", "b1_raw", "b2_score", "b2_label", "b2_raw",
                "b3_score", "b3_label", "b3_raw", "b4_score", "b4_label", "b4_raw",
                "b5_score", "b5_label", "b5_raw", "behavior_total",
                "o1_score", "o1_label", "o1_raw", "o2_score", "o2_label", "o2_raw",
                "o3_score", "o3_label", "o3_raw", "o4_score", "o4_label", "o4_raw",
                "o5_score", "o5_label", "o5_raw", "outcome_total",
                "total_score", "rank", "qualified", "bonus_amount"]:
        setattr(snap, key, result.get(key))

    return 1


# ══════════════════════════════════════════════════════════════════════
#  API QUERY
# ══════════════════════════════════════════════════════════════════════

def get_avail_scores(db: Session, role_type: str, month: date) -> list[dict]:
    """Return ranked Avail Scores for a role type and month."""
    month_start = month.replace(day=1)
    rows = (
        db.query(AvailScoreSnapshot, User.name)
        .join(User, User.id == AvailScoreSnapshot.user_id)
        .filter(
            AvailScoreSnapshot.month == month_start,
            AvailScoreSnapshot.role_type == role_type,
        )
        .order_by(AvailScoreSnapshot.rank)
        .all()
    )

    results = []
    for snap, user_name in rows:
        entry = {
            "user_id": snap.user_id,
            "user_name": user_name,
            "rank": snap.rank,
            "total_score": snap.total_score,
            "behavior_total": snap.behavior_total,
            "outcome_total": snap.outcome_total,
            "qualified": snap.qualified,
            "bonus_amount": snap.bonus_amount,
            "updated_at": snap.updated_at.isoformat() if snap.updated_at else None,
        }
        # Flat metric keys for frontend expandable detail
        for prefix in ("b", "o"):
            for i in range(1, 6):
                entry[f"{prefix}{i}_score"] = getattr(snap, f"{prefix}{i}_score") or 0
                entry[f"{prefix}{i}_label"] = getattr(snap, f"{prefix}{i}_label") or ""
                entry[f"{prefix}{i}_raw"] = getattr(snap, f"{prefix}{i}_raw") or ""
        results.append(entry)

    return results
