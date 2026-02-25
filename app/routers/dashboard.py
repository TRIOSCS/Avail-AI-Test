"""
dashboard.py — Sales Command Center API endpoints

Provides:
- /api/dashboard/needs-attention — stale accounts needing outreach
- /api/dashboard/morning-brief — AI-generated daily summary

Called by: app/static/app.js (loadDashboard)
Depends on: models/crm.py, models/intelligence.py, models/quotes.py
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, case, func
from sqlalchemy.orm import Session

from ..cache.decorators import cached_endpoint
from ..database import get_db
from ..dependencies import require_user

log = logging.getLogger("avail.dashboard")

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/needs-attention")
@cached_endpoint(prefix="needs_attention", ttl_hours=0.5, key_params=["days"])
def needs_attention(
    days: int = Query(default=7, ge=0, le=90),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    """Return user's owned companies that haven't been contacted in `days` days.

    Sorted by staleness (most stale first). Includes strategic flag,
    open req count, and open quote value for prioritization.
    """
    from ..models.crm import Company, CustomerSite
    from ..models.intelligence import ActivityLog
    from ..models.offers import Offer
    from ..models.quotes import Quote
    from ..models.sourcing import Requisition

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    # Get all active companies where user owns at least one site
    owned_company_ids = (
        db.query(CustomerSite.company_id)
        .filter(CustomerSite.owner_id == user.id)
        .distinct()
        .subquery()
    )
    companies = (
        db.query(Company)
        .filter(
            Company.id.in_(owned_company_ids),
            Company.is_active.is_(True),
        )
        .all()
    )

    if not companies:
        return []

    company_ids = [c.id for c in companies]

    # Batch: latest activity + channel per company (outbound only)
    # Uses subquery for max(created_at) then joins back to get the channel
    outbound_types = ("email_sent", "call_outbound")
    latest_sub = (
        db.query(
            ActivityLog.company_id,
            func.max(ActivityLog.created_at).label("max_at"),
        )
        .filter(
            ActivityLog.company_id.in_(company_ids),
            ActivityLog.activity_type.in_(outbound_types),
        )
        .group_by(ActivityLog.company_id)
        .subquery()
    )
    latest_rows = (
        db.query(
            latest_sub.c.company_id,
            latest_sub.c.max_at.label("last_at"),
            ActivityLog.channel,
        )
        .join(
            ActivityLog,
            and_(
                ActivityLog.company_id == latest_sub.c.company_id,
                ActivityLog.created_at == latest_sub.c.max_at,
            ),
        )
        .filter(ActivityLog.activity_type.in_(outbound_types))
        .all()
    )
    last_outreach_map = {}
    last_channel_map = {}
    for row in latest_rows:
        if row.company_id not in last_outreach_map:
            last_outreach_map[row.company_id] = row.last_at
            last_channel_map[row.company_id] = row.channel

    # Batch: open req count per company (via customer_site)
    site_ids_q = (
        db.query(CustomerSite.id, CustomerSite.company_id)
        .filter(CustomerSite.company_id.in_(company_ids))
        .all()
    )
    site_to_company = {s.id: s.company_id for s in site_ids_q}
    site_ids = list(site_to_company.keys())

    open_req_map = {}
    if site_ids:
        open_reqs_q = (
            db.query(
                Requisition.customer_site_id,
                func.count(Requisition.id).label("cnt"),
            )
            .filter(
                Requisition.customer_site_id.in_(site_ids),
                Requisition.status.in_(("open", "active")),
            )
            .group_by(Requisition.customer_site_id)
            .all()
        )
        for row in open_reqs_q:
            cid = site_to_company.get(row.customer_site_id)
            if cid:
                open_req_map[cid] = open_req_map.get(cid, 0) + row.cnt

    # Batch: open quote value per company (quotes sent but no result)
    open_quote_map = {}
    if site_ids:
        open_quotes_q = (
            db.query(
                Quote.customer_site_id,
                func.sum(Quote.subtotal).label("total_value"),
            )
            .filter(
                Quote.customer_site_id.in_(site_ids),
                Quote.status == "sent",
                Quote.result.is_(None),
            )
            .group_by(Quote.customer_site_id)
            .all()
        )
        for row in open_quotes_q:
            cid = site_to_company.get(row.customer_site_id)
            if cid:
                val = float(row.total_value) if row.total_value else 0
                open_quote_map[cid] = open_quote_map.get(cid, 0) + val

    # Build results — filter to stale companies
    results = []
    for c in companies:
        last_at = last_outreach_map.get(c.id)

        # Compute days since contact (timezone-safe)
        if last_at:
            if last_at.tzinfo is None:
                last_at_aware = last_at.replace(tzinfo=timezone.utc)
            else:
                last_at_aware = last_at
            days_since = (now - last_at_aware).days
        else:
            days_since = 9999  # Never contacted

        # Filter: include if stale (no contact in `days` days)
        if last_at and last_at_aware >= cutoff:
            continue

        results.append({
            "company_id": c.id,
            "company_name": c.name,
            "is_strategic": bool(c.is_strategic),
            "last_outreach_at": last_at.isoformat() if last_at else None,
            "last_channel": last_channel_map.get(c.id),
            "days_since_contact": days_since,
            "open_req_count": open_req_map.get(c.id, 0),
            "open_quote_value": round(open_quote_map.get(c.id, 0), 2),
        })

    # Sort by staleness (most stale first)
    results.sort(key=lambda x: -x["days_since_contact"])

    return results


@router.get("/morning-brief")
@cached_endpoint(prefix="morning_brief", ttl_hours=1, key_params=[])
def morning_brief(
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    """AI-generated morning brief for a sales user.

    Gathers portfolio stats, calls Claude Haiku for a 2-sentence summary,
    and returns text + stats fallback. Cached per user for 4 hours.
    """
    from ..models.crm import Company, CustomerSite
    from ..models.intelligence import ActivityLog, ProactiveMatch
    from ..models.quotes import Quote
    from ..models.sourcing import Requisition

    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)

    # ── Gather stats ──

    # Stale accounts (no outbound activity in 7 days)
    owned_company_ids_sub = (
        db.query(CustomerSite.company_id)
        .filter(CustomerSite.owner_id == user.id)
        .distinct()
        .subquery()
    )
    owned_companies = (
        db.query(Company)
        .filter(
            Company.id.in_(owned_company_ids_sub),
            Company.is_active.is_(True),
        )
        .all()
    )
    company_ids = [c.id for c in owned_companies]

    stale_count = 0
    if company_ids:
        outbound_types = ("email_sent", "call_outbound")
        recent_activity = (
            db.query(ActivityLog.company_id)
            .filter(
                ActivityLog.company_id.in_(company_ids),
                ActivityLog.activity_type.in_(outbound_types),
                ActivityLog.created_at >= week_ago,
            )
            .distinct()
            .all()
        )
        recently_contacted = {r.company_id for r in recent_activity}
        stale_count = len(company_ids) - len(recently_contacted)

    # Quotes awaiting response
    site_ids = []
    if company_ids:
        sites = (
            db.query(CustomerSite.id)
            .filter(CustomerSite.company_id.in_(company_ids))
            .all()
        )
        site_ids = [s.id for s in sites]

    # Quote stats: awaiting + won/lost this week (single query with case())
    quotes_awaiting = 0
    won_this_week = 0
    lost_this_week = 0
    if site_ids:
        quote_stats = db.query(
            func.count(case(
                (and_(Quote.status == "sent", Quote.result.is_(None)), Quote.id),
            )).label("awaiting"),
            func.count(case(
                (and_(Quote.result == "won", Quote.result_at >= week_ago), Quote.id),
            )).label("won"),
            func.count(case(
                (and_(Quote.result == "lost", Quote.result_at >= week_ago), Quote.id),
            )).label("lost"),
        ).filter(Quote.customer_site_id.in_(site_ids)).first()
        quotes_awaiting = quote_stats.awaiting or 0
        won_this_week = quote_stats.won or 0
        lost_this_week = quote_stats.lost or 0

    # New proactive matches
    new_proactive_matches = (
        db.query(func.count(ProactiveMatch.id))
        .filter(
            ProactiveMatch.salesperson_id == user.id,
            ProactiveMatch.status == "new",
        )
        .scalar()
    ) or 0

    stats = {
        "stale_accounts": stale_count,
        "quotes_awaiting": quotes_awaiting,
        "new_proactive_matches": new_proactive_matches,
        "won_this_week": won_this_week,
        "lost_this_week": lost_this_week,
    }

    # ── Generate AI brief ──
    brief_text = None
    try:
        from ..utils.claude_client import claude_structured

        prompt = (
            f"You are a sales assistant for an electronic component distributor. "
            f"Write a 2-sentence morning brief for {user.name}. Be direct and actionable.\n\n"
            f"Stats:\n"
            f"- {stale_count} accounts with no outreach in 7+ days\n"
            f"- {quotes_awaiting} quotes sent but awaiting customer response\n"
            f"- {new_proactive_matches} new proactive match opportunities\n"
            f"- {won_this_week} won this week, {lost_this_week} lost this week\n"
            f"- {len(owned_companies)} total accounts owned\n\n"
            f"Return JSON: {{\"text\": \"your brief here\"}}"
        )
        schema = {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }
        result = asyncio.run(claude_structured(
            prompt=prompt,
            schema=schema,
            system="You write concise sales briefings. Be specific about numbers. No fluff.",
            model_tier="fast",
            max_tokens=256,
            timeout=10,
        ))
        if result and result.get("text"):
            brief_text = result["text"]
    except Exception as e:
        log.warning("Morning brief AI generation failed: %s", e)

    return {
        "text": brief_text,
        "generated_at": now.isoformat(),
        "stats": stats,
    }


@router.get("/hot-offers")
def hot_offers(
    days: int = Query(default=7, ge=1, le=90),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    """Return recent vendor offers received within `days` window.

    Shows vendor name, MPN, unit price, age, and links to the requisition.
    Sorted by most recent first.
    """
    from ..models.offers import Offer
    from ..models.sourcing import Requisition

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    offers = (
        db.query(Offer)
        .join(Requisition, Offer.requisition_id == Requisition.id)
        .filter(
            Offer.created_at >= cutoff,
            Offer.status == "active",
        )
        .order_by(Offer.created_at.desc())
        .limit(15)
        .all()
    )

    results = []
    for o in offers:
        age_hours = (now - o.created_at).total_seconds() / 3600 if o.created_at else 0
        if age_hours < 1:
            age_label = "just now"
        elif age_hours < 24:
            age_label = f"{int(age_hours)}h ago"
        else:
            age_label = f"{int(age_hours / 24)}d ago"

        results.append({
            "id": o.id,
            "vendor_name": o.vendor_name,
            "mpn": o.mpn,
            "unit_price": float(o.unit_price) if o.unit_price else None,
            "currency": o.currency or "USD",
            "requisition_id": o.requisition_id,
            "source": o.source,
            "age_label": age_label,
            "created_at": o.created_at.isoformat() if o.created_at else None,
        })

    return results


@router.get("/buyer-brief")
@cached_endpoint(prefix="buyer_brief", ttl_hours=0.25, key_params=["days", "scope"])
def buyer_brief(
    days: int = Query(default=7, ge=1, le=90),
    scope: str = Query(default="my", pattern="^(my|team)$"),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    """Buyer Command Center data: KPIs, priority tiles, follow-up tiles.

    scope='my' filters to current user's work; scope='team' shows all.
    """
    from ..models.offers import Contact, Offer
    from ..models.quotes import BuyPlan, Quote
    from ..models.sourcing import Requisition

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # ── Scope filter helper ──
    def _user_filter(col):
        """Return filter clause: restrict to user if scope='my'."""
        if scope == "my":
            return col == user.id
        return col.isnot(None)  # team: all non-null

    # ── KPI 1: Sourcing Ratio (reqs with ≥1 offer / total reqs this month) ──
    total_reqs_q = (
        db.query(func.count(Requisition.id))
        .filter(Requisition.created_at >= month_start, _user_filter(Requisition.created_by))
        .scalar()
    ) or 0

    sourced_reqs_q = 0
    if total_reqs_q:
        sourced_reqs_q = (
            db.query(func.count(Requisition.id.distinct()))
            .join(Offer, Offer.requisition_id == Requisition.id)
            .filter(
                Requisition.created_at >= month_start,
                _user_filter(Requisition.created_by),
            )
            .scalar()
        ) or 0

    sourcing_ratio = round(sourced_reqs_q / total_reqs_q * 100) if total_reqs_q else 0

    # ── KPI 2: Offer→Quote Rate (offers converted / total offers) — single query ──
    offer_stats = db.query(
        func.count(Offer.id).label("total"),
        func.count(case(
            (Offer.attribution_status == "converted", Offer.id),
        )).label("quoted"),
    ).filter(Offer.created_at >= month_start, _user_filter(Offer.entered_by_id)).first()
    total_offers_month = offer_stats.total or 0
    quoted_offers_month = offer_stats.quoted or 0
    offer_quote_rate = round(quoted_offers_month / total_offers_month * 100) if total_offers_month else 0

    # ── KPI 3: Quote→Win Rate — single query ──
    quote_stats = db.query(
        func.count(case((Quote.result == "won", Quote.id))).label("won"),
        func.count(case((Quote.result == "lost", Quote.id))).label("lost"),
    ).filter(Quote.result_at >= month_start, _user_filter(Quote.created_by_id)).first()
    won_quotes = quote_stats.won or 0
    lost_quotes = quote_stats.lost or 0
    quote_win_rate = round(won_quotes / (won_quotes + lost_quotes) * 100) if (won_quotes + lost_quotes) else 0

    # ── KPI 4: Buy Plan→PO Rate — single query ──
    bp_stats = db.query(
        func.count(BuyPlan.id).label("total"),
        func.count(case(
            (BuyPlan.status.in_(("approved", "po_entered", "po_confirmed", "complete")), BuyPlan.id),
        )).label("approved"),
        func.count(case(
            (BuyPlan.status.in_(("po_confirmed", "complete")), BuyPlan.id),
        )).label("confirmed"),
    ).filter(BuyPlan.submitted_at >= month_start, _user_filter(BuyPlan.submitted_by_id)).first()
    total_bps = bp_stats.total or 0
    approved_bps = bp_stats.approved or 0
    po_confirmed_bps = bp_stats.confirmed or 0
    buyplan_po_rate = round(po_confirmed_bps / total_bps * 100) if total_bps else 0

    # ── Tile 1: New Requirements (active reqs in window, check for offers) ──
    new_reqs_q = (
        db.query(Requisition)
        .filter(
            Requisition.created_at >= cutoff,
            Requisition.status.in_(("open", "active")),
            _user_filter(Requisition.created_by),
        )
        .order_by(Requisition.created_at.desc())
        .limit(15)
        .all()
    )
    new_req_ids = [r.id for r in new_reqs_q]
    reqs_with_offers = set()
    if new_req_ids:
        rows = (
            db.query(Offer.requisition_id)
            .filter(Offer.requisition_id.in_(new_req_ids))
            .distinct()
            .all()
        )
        reqs_with_offers = {r.requisition_id for r in rows}

    new_requirements = []
    for r in new_reqs_q:
        age_hours = (now - _ensure_aware(r.created_at)).total_seconds() / 3600 if r.created_at else 0
        new_requirements.append({
            "id": r.id,
            "name": r.name,
            "customer_name": r.customer_name,
            "has_offers": r.id in reqs_with_offers,
            "deadline": r.deadline,
            "age_label": _age_label(age_hours),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })

    # ── Tile 2: Offers to Review (needs_review status) ──
    review_offers = (
        db.query(Offer)
        .filter(Offer.status == "needs_review")
        .order_by(Offer.created_at.desc())
        .limit(15)
        .all()
    )
    offers_to_review = []
    for o in review_offers:
        age_hours = (now - _ensure_aware(o.created_at)).total_seconds() / 3600 if o.created_at else 0
        offers_to_review.append({
            "id": o.id,
            "requisition_id": o.requisition_id,
            "vendor_name": o.vendor_name,
            "mpn": o.mpn,
            "unit_price": float(o.unit_price) if o.unit_price else None,
            "source": o.source,
            "age_label": _age_label(age_hours),
        })

    # ── Tile 3: Awaiting Vendor Response (contacts sent, no response) ──
    awaiting_q = (
        db.query(Contact)
        .filter(
            Contact.status == "sent",
            Contact.created_at >= cutoff,
            _user_filter(Contact.user_id),
        )
        .order_by(Contact.created_at.asc())
        .limit(15)
        .all()
    )
    awaiting_vendor = []
    for c in awaiting_q:
        wait_hours = (now - _ensure_aware(c.created_at)).total_seconds() / 3600 if c.created_at else 0
        awaiting_vendor.append({
            "id": c.id,
            "requisition_id": c.requisition_id,
            "vendor_name": c.vendor_name,
            "contact_type": c.contact_type,
            "wait_label": _age_label(wait_hours),
            "created_at": c.created_at.isoformat() if c.created_at else None,
        })

    # ── Tile 4: Quotes Due Soon (approaching deadlines) ──
    deadline_reqs = (
        db.query(Requisition)
        .filter(
            Requisition.deadline.isnot(None),
            Requisition.status.in_(("open", "active", "sourcing")),
            _user_filter(Requisition.created_by),
        )
        .all()
    )
    quotes_due_soon = []
    for r in deadline_reqs:
        dl = r.deadline
        if dl == "ASAP":
            urgency = "critical"
            days_left = 0
        else:
            try:
                from datetime import date as date_type

                dl_date = datetime.fromisoformat(dl).date() if "T" in dl else date_type.fromisoformat(dl)
                days_left = (dl_date - now.date()).days
                if days_left <= 0:
                    urgency = "critical"
                elif days_left <= 3:
                    urgency = "warning"
                else:
                    urgency = "normal"
            except (ValueError, TypeError):
                continue
        quotes_due_soon.append({
            "id": r.id,
            "name": r.name,
            "deadline": dl,
            "days_left": days_left,
            "urgency": urgency,
        })
    quotes_due_soon.sort(key=lambda x: x["days_left"])
    quotes_due_soon = quotes_due_soon[:15]

    # ── Tile 5: Pipeline Summary ──
    pipeline_active = (
        db.query(func.count(Requisition.id))
        .filter(Requisition.status.in_(("open", "active", "sourcing")), _user_filter(Requisition.created_by))
        .scalar()
    ) or 0
    pipeline_quoted = (
        db.query(func.count(Quote.id))
        .filter(Quote.status == "sent", Quote.result.is_(None), _user_filter(Quote.created_by_id))
        .scalar()
    ) or 0
    pipeline_won = won_quotes
    pipeline_buyplans = approved_bps

    # ── Tile 6: Top Vendors (by offer volume this month) ──
    top_vendors_q = (
        db.query(
            Offer.vendor_name,
            func.count(Offer.id).label("offer_count"),
        )
        .filter(
            Offer.created_at >= month_start,
            Offer.status.in_(("active", "approved")),
        )
        .group_by(Offer.vendor_name)
        .order_by(func.count(Offer.id).desc())
        .limit(10)
        .all()
    )
    top_vendors = [
        {"vendor_name": row.vendor_name, "offer_count": row.offer_count}
        for row in top_vendors_q
    ]

    return {
        "kpis": {
            "sourcing_ratio": sourcing_ratio,
            "total_reqs": total_reqs_q,
            "sourced_reqs": sourced_reqs_q,
            "offer_quote_rate": offer_quote_rate,
            "total_offers": total_offers_month,
            "quoted_offers": quoted_offers_month,
            "quote_win_rate": quote_win_rate,
            "won": won_quotes,
            "lost": lost_quotes,
            "buyplan_po_rate": buyplan_po_rate,
            "total_buyplans": total_bps,
            "confirmed_pos": po_confirmed_bps,
        },
        "new_requirements": new_requirements,
        "offers_to_review": offers_to_review,
        "awaiting_vendor": awaiting_vendor,
        "quotes_due_soon": quotes_due_soon,
        "top_vendors": top_vendors,
        "pipeline": {
            "active_reqs": pipeline_active,
            "quotes_out": pipeline_quoted,
            "won_this_month": pipeline_won,
            "buyplans_approved": pipeline_buyplans,
        },
    }


def _ensure_aware(dt):
    """Ensure a datetime is timezone-aware (SQLite strips tzinfo)."""
    if dt and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _age_label(hours: float) -> str:
    """Convert hours-old to human-readable label."""
    if hours < 1:
        return "just now"
    if hours < 24:
        return f"{int(hours)}h ago"
    return f"{int(hours / 24)}d ago"
