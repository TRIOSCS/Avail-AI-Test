"""
dashboard.py — Sales Command Center API endpoints

Provides:
- /api/dashboard/needs-attention — stale accounts needing outreach
- /api/dashboard/morning-brief — AI-generated daily summary

Called by: app/static/app.js (loadDashboard)
Depends on: models/crm.py, models/intelligence.py, models/quotes.py
"""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_user

log = logging.getLogger("avail.dashboard")

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/needs-attention")
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

    # Get all active companies owned by this user
    companies = (
        db.query(Company)
        .filter(
            Company.account_owner_id == user.id,
            Company.is_active.is_(True),
        )
        .all()
    )

    if not companies:
        return []

    company_ids = [c.id for c in companies]

    # Batch: latest activity per company (outbound only — email_sent, call_outbound)
    outbound_types = ("email_sent", "call_outbound")
    latest_activity_q = (
        db.query(
            ActivityLog.company_id,
            func.max(ActivityLog.created_at).label("last_at"),
        )
        .filter(
            ActivityLog.company_id.in_(company_ids),
            ActivityLog.activity_type.in_(outbound_types),
        )
        .group_by(ActivityLog.company_id)
        .all()
    )
    last_outreach_map = {row.company_id: row.last_at for row in latest_activity_q}

    # Batch: last channel per company (most recent outbound activity)
    last_channel_q = (
        db.query(ActivityLog)
        .filter(
            ActivityLog.company_id.in_(company_ids),
            ActivityLog.activity_type.in_(outbound_types),
        )
        .order_by(ActivityLog.created_at.desc())
        .all()
    )
    last_channel_map = {}
    for act in last_channel_q:
        if act.company_id not in last_channel_map:
            last_channel_map[act.company_id] = act.channel

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
async def morning_brief(
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
    owned_companies = (
        db.query(Company)
        .filter(
            Company.account_owner_id == user.id,
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

    quotes_awaiting = 0
    if site_ids:
        quotes_awaiting = (
            db.query(func.count(Quote.id))
            .filter(
                Quote.customer_site_id.in_(site_ids),
                Quote.status == "sent",
                Quote.result.is_(None),
            )
            .scalar()
        ) or 0

    # New proactive matches
    new_proactive_matches = (
        db.query(func.count(ProactiveMatch.id))
        .filter(
            ProactiveMatch.salesperson_id == user.id,
            ProactiveMatch.status == "new",
        )
        .scalar()
    ) or 0

    # Won/lost this week
    won_this_week = 0
    lost_this_week = 0
    if site_ids:
        won_this_week = (
            db.query(func.count(Quote.id))
            .filter(
                Quote.customer_site_id.in_(site_ids),
                Quote.result == "won",
                Quote.result_at >= week_ago,
            )
            .scalar()
        ) or 0

        lost_this_week = (
            db.query(func.count(Quote.id))
            .filter(
                Quote.customer_site_id.in_(site_ids),
                Quote.result == "lost",
                Quote.result_at >= week_ago,
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
        result = await claude_structured(
            prompt=prompt,
            schema=schema,
            system="You write concise sales briefings. Be specific about numbers. No fluff.",
            model_tier="fast",
            max_tokens=256,
            timeout=10,
        )
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
