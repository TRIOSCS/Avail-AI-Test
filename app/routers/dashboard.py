"""
dashboard.py — Sales Command Center API endpoints

Provides:
- /api/dashboard/needs-attention — stale accounts needing outreach
- /api/dashboard/morning-brief — AI-generated daily summary

Called by: app/static/app.js (loadDashboard)
Depends on: models/crm.py, models/intelligence.py, models/quotes.py
"""

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from loguru import logger
from sqlalchemy import and_, case, func
from sqlalchemy.orm import Session

from ..cache.decorators import cached_endpoint
from ..database import get_db
from ..dependencies import require_user

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/needs-attention")
@cached_endpoint(prefix="needs_attention", ttl_hours=0.5, key_params=["days"])
def needs_attention(
    days: int = Query(default=30, ge=0, le=366),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    """Return user's owned companies that haven't been contacted in `days` days.

    Sorted by staleness (most stale first). Includes strategic flag,
    open req count, and open quote value for prioritization.
    """
    from ..models.crm import Company, CustomerSite
    from ..models.intelligence import ActivityLog
    from ..models.quotes import Quote
    from ..models.sourcing import Requisition

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    # Get all active companies where user owns at least one site
    owned_company_ids = (
        db.query(CustomerSite.company_id)
        .filter(CustomerSite.owner_id == user.id)
        .distinct()
    )
    companies = (
        db.query(Company)
        .filter(
            Company.id.in_(owned_company_ids.scalar_subquery()),
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


@router.get("/attention-feed")
@cached_endpoint(prefix="attention_feed", ttl_hours=0.25, key_params=["days", "scope"])
def attention_feed(
    days: int = Query(default=30, ge=1, le=366),
    scope: str = Query(default="my", pattern="^(my|team)$"),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    """Unified prioritized attention list for the Command Center.

    Merges: stale accounts, reqs at risk, quotes due soon, needs quote,
    expiring quotes, buy plans pending. Sorted by urgency, capped at 12.

    Called by: app.js (loadDashboard / loadBuyerDashboard)
    Depends on: models/crm.py, models/sourcing.py, models/quotes.py, models/offers.py
    """
    from ..models.crm import Company, CustomerSite
    from ..models.intelligence import ActivityLog
    from ..models.offers import Offer
    from ..models.quotes import Quote
    from ..models.sourcing import Requisition

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    def _user_filter(col):
        if scope == "my":
            return col == user.id
        return col.isnot(None)

    items = []
    urgency_order = {"critical": 0, "warning": 1, "info": 2}

    # ── Source 1: Stale accounts (no outreach in N days) ──
    owned_company_ids = (
        db.query(CustomerSite.company_id)
        .filter(CustomerSite.owner_id == user.id)
        .distinct()
    )
    last_outreach = (
        db.query(
            ActivityLog.company_id,
            func.max(ActivityLog.created_at).label("last_at"),
        )
        .filter(
            ActivityLog.activity_type.in_(("email_sent", "call_logged", "meeting_logged")),
        )
        .group_by(ActivityLog.company_id)
        .subquery()
    )
    stale_companies = (
        db.query(Company.id, Company.name, Company.is_strategic, last_outreach.c.last_at)
        .outerjoin(last_outreach, Company.id == last_outreach.c.company_id)
        .filter(
            Company.id.in_(owned_company_ids.scalar_subquery()),
            Company.is_active.is_(True),
        )
        .all()
    )
    for co in stale_companies:
        if co.last_at:
            last_dt = _ensure_aware(co.last_at)
            days_since = (now - last_dt).days
            if days_since < days:
                continue
        else:
            days_since = 999
        urgency = "critical" if days_since > 60 or co.is_strategic else "warning"
        detail = "Never contacted" if days_since == 999 else f"{days_since}d since last outreach"
        items.append({
            "type": "stale_account", "urgency": urgency,
            "title": co.name, "detail": detail,
            "link_type": "company", "link_id": co.id,
        })

    # ── Source 2: Reqs at risk (deadline passed or low offers) ──
    offer_count_sub = (
        db.query(Offer.requisition_id, func.count(Offer.id).label("cnt"))
        .group_by(Offer.requisition_id)
        .subquery()
    )
    risk_rows = (
        db.query(
            Requisition.id, Requisition.name, Requisition.customer_name,
            Requisition.deadline, Requisition.created_at,
            func.coalesce(offer_count_sub.c.cnt, 0).label("num_offers"),
        )
        .outerjoin(offer_count_sub, Requisition.id == offer_count_sub.c.requisition_id)
        .filter(
            Requisition.status.in_(("open", "active", "sourcing")),
            _user_filter(Requisition.created_by),
        )
        .all()
    )
    for row in risk_rows:
        num_offers = row.num_offers
        age_hours = (now - _ensure_aware(row.created_at)).total_seconds() / 3600 if row.created_at else 0
        urgency = None
        detail = None

        if row.deadline:
            dl = row.deadline
            if dl == "ASAP" and num_offers == 0:
                urgency = "critical"
                detail = "ASAP deadline — no offers"
            elif dl != "ASAP":
                try:
                    from datetime import date as date_type
                    dl_date = datetime.fromisoformat(dl).date() if "T" in dl else date_type.fromisoformat(dl)
                    days_left = (dl_date - now.date()).days
                    if days_left <= 0 and num_offers == 0:
                        urgency = "critical"
                        detail = f"{abs(days_left)}d overdue — no offers"
                    elif days_left <= 3 and num_offers == 0:
                        urgency = "critical"
                        detail = f"{days_left}d left — no offers"
                    elif days_left <= 3 and num_offers == 1:
                        urgency = "warning"
                        detail = f"{days_left}d left — only 1 offer"
                except (ValueError, TypeError):
                    pass

        if not urgency and num_offers == 0 and age_hours >= 48:
            urgency = "warning"
            detail = f"No offers after {int(age_hours / 24)}d"

        if urgency:
            items.append({
                "type": "req_at_risk", "urgency": urgency,
                "title": row.name or f"REQ #{row.id}",
                "detail": detail,
                "link_type": "requisition", "link_id": row.id,
            })

    # ── Source 3: Needs quote (has offers, no quote sent) ──
    quoted_req_ids = (
        db.query(Quote.requisition_id)
        .filter(Quote.status.in_(("sent", "won")))
        .distinct()
        .scalar_subquery()
    )
    needs_quote_rows = (
        db.query(
            Requisition.id, Requisition.name, Requisition.customer_name,
            func.count(Offer.id).label("offer_count"),
        )
        .join(Offer, Offer.requisition_id == Requisition.id)
        .filter(
            Requisition.status.in_(("open", "active", "sourcing")),
            Offer.status.in_(("active", "approved")),
            Requisition.id.notin_(quoted_req_ids),
            _user_filter(Requisition.created_by),
        )
        .group_by(Requisition.id)
        .limit(15)
        .all()
    )
    for r in needs_quote_rows:
        items.append({
            "type": "needs_quote", "urgency": "warning",
            "title": r.customer_name or r.name or f"REQ #{r.id}",
            "detail": f"{r.offer_count} offer{'s' if r.offer_count != 1 else ''} ready — no quote sent",
            "link_type": "requisition", "link_id": r.id,
        })

    # ── Source 4: Expiring quotes ──
    expiring_q = (
        db.query(
            Quote.id.label("quote_id"), Quote.quote_number, Quote.subtotal,
            Quote.sent_at, Quote.validity_days,
            Requisition.id.label("req_id"), Requisition.customer_name,
        )
        .join(Requisition, Requisition.id == Quote.requisition_id)
        .filter(
            Quote.status == "sent", Quote.result.is_(None),
            Quote.sent_at.isnot(None),
            _user_filter(Quote.created_by_id),
        )
        .limit(15)
        .all()
    )
    for q in expiring_q:
        sent = _ensure_aware(q.sent_at) if q.sent_at else now
        validity = q.validity_days or 7
        expires_at = sent + timedelta(days=validity)
        days_left = (expires_at - now).days
        if days_left <= 7:
            urgency = "critical" if days_left <= 0 else "warning" if days_left <= 2 else "info"
            dl_label = f"{abs(days_left)}d expired" if days_left <= 0 else f"{days_left}d left"
            val = f" — ${int(q.subtotal):,}" if q.subtotal else ""
            items.append({
                "type": "expiring_quote", "urgency": urgency,
                "title": q.customer_name or q.quote_number or f"Quote #{q.quote_id}",
                "detail": f"{q.quote_number} expiring ({dl_label}){val}",
                "link_type": "requisition", "link_id": q.req_id,
            })

    # ── Source 5: Buy plans pending ──
    try:
        from ..models.buy_plan import BuyPlanV3
        pending_bps = (
            db.query(BuyPlanV3.id, BuyPlanV3.total_revenue, BuyPlanV3.status, BuyPlanV3.requisition_id)
            .filter(
                BuyPlanV3.status.in_(("draft", "pending")),
                _user_filter(BuyPlanV3.submitted_by_id),
            )
            .limit(10)
            .all()
        )
        bp_req_ids = [bp.requisition_id for bp in pending_bps if bp.requisition_id]
        bp_names = {}
        if bp_req_ids:
            for r in db.query(Requisition.id, Requisition.customer_name).filter(Requisition.id.in_(bp_req_ids)).all():
                bp_names[r.id] = r.customer_name
        for bp in pending_bps:
            rev = f" — ${int(float(bp.total_revenue)):,}" if bp.total_revenue else ""
            items.append({
                "type": "buyplan_pending", "urgency": "info",
                "title": bp_names.get(bp.requisition_id, f"BP #{bp.id}"),
                "detail": f"Buy plan {bp.status}{rev}",
                "link_type": "requisition", "link_id": bp.requisition_id or bp.id,
            })
    except ImportError:
        pass

    # Sort by urgency: critical > warning > info, then by type for stability
    items.sort(key=lambda x: (urgency_order.get(x["urgency"], 9), x["type"]))
    return items[:12]


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

    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)

    # ── Gather stats ──

    # Stale accounts (no outbound activity in 7 days)
    owned_company_ids_sub = (
        db.query(CustomerSite.company_id)
        .filter(CustomerSite.owner_id == user.id)
        .distinct()
    )
    owned_companies = (
        db.query(Company)
        .filter(
            Company.id.in_(owned_company_ids_sub.scalar_subquery()),
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
        logger.warning("Morning brief AI generation failed: %s", e)

    return {
        "text": brief_text,
        "generated_at": now.isoformat(),
        "stats": stats,
    }


@router.get("/hot-offers")
def hot_offers(
    days: int = Query(default=30, ge=1, le=366),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    """Return recent vendor offers received within `days` window.

    Shows vendor name, MPN, unit price, age, and links to the requisition.
    Sorted by most recent first. Cached for 15 min.
    """
    from ..models.offers import Offer

    @cached_endpoint(prefix="hot_offers", ttl_hours=0.25, key_params=["days"])
    def _fetch(days, db):
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days)

        # Query only needed columns, skip unnecessary JOIN since we just
        # need requisition_id (already on the Offer row)
        offers = (
            db.query(
                Offer.id, Offer.vendor_name, Offer.mpn, Offer.unit_price,
                Offer.currency, Offer.requisition_id, Offer.source, Offer.created_at,
            )
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
            ca = _ensure_aware(o.created_at)
            age_hours = (now - ca).total_seconds() / 3600 if ca else 0
            results.append({
                "id": o.id,
                "vendor_name": o.vendor_name,
                "mpn": o.mpn,
                "unit_price": float(o.unit_price) if o.unit_price else None,
                "currency": o.currency or "USD",
                "requisition_id": o.requisition_id,
                "source": o.source,
                "age_label": _age_label(age_hours),
                "created_at": o.created_at.isoformat() if o.created_at else None,
            })
        return results

    return _fetch(days=days, db=db)


@router.get("/buyer-brief")
@cached_endpoint(prefix="buyer_brief", ttl_hours=0.25, key_params=["days", "scope"])
def buyer_brief(
    days: int = Query(default=30, ge=1, le=366),
    scope: str = Query(default="my", pattern="^(my|team)$"),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    """Buyer Command Center data: KPIs, priority tiles, follow-up tiles.

    scope='my' filters to current user's work; scope='team' shows all.
    """
    from ..models.offers import Offer
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

    # ── Tile 3a: Revenue & Profit (from buy plans) ──
    from ..models.buy_plan import BuyPlanV3

    bp_financials = db.query(
        func.coalesce(func.sum(BuyPlanV3.total_revenue), 0).label("revenue"),
        func.coalesce(func.sum(BuyPlanV3.total_cost), 0).label("cost"),
        func.count(BuyPlanV3.id).label("plan_count"),
    ).filter(
        BuyPlanV3.status.in_(("active", "completed")),
        BuyPlanV3.created_at >= cutoff,
        _user_filter(BuyPlanV3.submitted_by_id),
    ).first()

    bp_revenue = float(bp_financials.revenue or 0)
    bp_cost = float(bp_financials.cost or 0)
    bp_gross_profit = bp_revenue - bp_cost
    bp_margin_pct = round((bp_gross_profit / bp_revenue) * 100, 1) if bp_revenue > 0 else 0

    # Pipeline (pending/draft buy plans not yet completed)
    bp_pipeline = db.query(
        func.coalesce(func.sum(BuyPlanV3.total_revenue), 0).label("revenue"),
        func.coalesce(func.sum(BuyPlanV3.total_cost), 0).label("cost"),
        func.count(BuyPlanV3.id).label("plan_count"),
    ).filter(
        BuyPlanV3.status.in_(("draft", "pending", "active")),
        BuyPlanV3.created_at >= cutoff,
        _user_filter(BuyPlanV3.submitted_by_id),
    ).first()

    pipeline_revenue = float(bp_pipeline.revenue or 0)
    pipeline_profit = pipeline_revenue - float(bp_pipeline.cost or 0)

    # Recent buy plans for drill-down
    recent_bps = (
        db.query(
            BuyPlanV3.id, BuyPlanV3.total_revenue, BuyPlanV3.total_cost,
            BuyPlanV3.total_margin_pct, BuyPlanV3.status,
            BuyPlanV3.requisition_id, BuyPlanV3.created_at,
        )
        .filter(
            BuyPlanV3.status.in_(("active", "completed", "pending")),
            BuyPlanV3.created_at >= cutoff,
            _user_filter(BuyPlanV3.submitted_by_id),
        )
        .order_by(BuyPlanV3.created_at.desc())
        .limit(15)
        .all()
    )
    # Resolve customer names via requisition
    from ..models.sourcing import Requisition as Req2
    req_ids = [bp.requisition_id for bp in recent_bps if bp.requisition_id]
    req_names = {}
    if req_ids:
        for r in db.query(Req2.id, Req2.customer_name).filter(Req2.id.in_(req_ids)).all():
            req_names[r.id] = r.customer_name

    revenue_profit = {
        "est_revenue": bp_revenue,
        "est_cost": bp_cost,
        "est_gross_profit": bp_gross_profit,
        "margin_pct": bp_margin_pct,
        "plan_count": bp_financials.plan_count or 0,
        "pipeline_revenue": pipeline_revenue,
        "pipeline_profit": pipeline_profit,
        "pipeline_count": bp_pipeline.plan_count or 0,
        "recent_plans": [
            {
                "id": bp.id,
                "requisition_id": bp.requisition_id,
                "customer_name": req_names.get(bp.requisition_id, ""),
                "revenue": float(bp.total_revenue) if bp.total_revenue else 0,
                "cost": float(bp.total_cost) if bp.total_cost else 0,
                "margin_pct": float(bp.total_margin_pct) if bp.total_margin_pct else 0,
                "status": bp.status,
            }
            for bp in recent_bps
        ],
    }

    # ── Tile 3b: Requisitions at Risk ──
    # Single query: join reqs with offer counts, only load reqs that could be at risk
    # (created >48h ago with ≤1 offer, or have a deadline)
    offer_count_sub = (
        db.query(
            Offer.requisition_id,
            func.count(Offer.id).label("offer_cnt"),
        )
        .group_by(Offer.requisition_id)
        .subquery()
    )
    at_risk_rows = (
        db.query(
            Requisition.id,
            Requisition.name,
            Requisition.customer_name,
            Requisition.deadline,
            Requisition.created_at,
            func.coalesce(offer_count_sub.c.offer_cnt, 0).label("num_offers"),
        )
        .outerjoin(offer_count_sub, Requisition.id == offer_count_sub.c.requisition_id)
        .filter(
            Requisition.status.in_(("open", "active", "sourcing")),
            _user_filter(Requisition.created_by),
        )
        .all()
    )

    reqs_at_risk = []
    for row in at_risk_rows:
        num_offers = row.num_offers
        age_hours = (now - _ensure_aware(row.created_at)).total_seconds() / 3600 if row.created_at else 0
        risk_reasons = []
        urgency = "normal"

        if row.deadline:
            dl = row.deadline
            if dl == "ASAP":
                if num_offers == 0:
                    risk_reasons.append("ASAP — no offers")
                    urgency = "critical"
            else:
                try:
                    from datetime import date as date_type
                    dl_date = datetime.fromisoformat(dl).date() if "T" in dl else date_type.fromisoformat(dl)
                    days_left = (dl_date - now.date()).days
                    if days_left <= 0 and num_offers == 0:
                        risk_reasons.append(f"{abs(days_left)}d overdue — no offers")
                        urgency = "critical"
                    elif days_left <= 3 and num_offers == 0:
                        risk_reasons.append(f"{days_left}d left — no offers")
                        urgency = "critical"
                    elif days_left <= 7 and num_offers == 0:
                        risk_reasons.append(f"{days_left}d left — no offers")
                        urgency = "warning"
                    elif days_left <= 3 and num_offers == 1:
                        risk_reasons.append(f"{days_left}d left — only 1 offer")
                        urgency = "warning"
                except (ValueError, TypeError):
                    pass

        if not risk_reasons and num_offers == 0 and age_hours >= 48:
            risk_reasons.append(f"no offers after {int(age_hours / 24)}d")
            urgency = "warning"

        if not risk_reasons and num_offers == 1 and age_hours >= 72:
            risk_reasons.append(f"only 1 offer after {int(age_hours / 24)}d")
            urgency = "normal"

        if risk_reasons:
            reqs_at_risk.append({
                "id": row.id,
                "name": row.name,
                "customer_name": row.customer_name,
                "num_offers": num_offers,
                "risk": risk_reasons[0],
                "urgency": urgency,
            })

    urgency_order = {"critical": 0, "warning": 1, "normal": 2}
    reqs_at_risk.sort(key=lambda x: urgency_order.get(x["urgency"], 3))
    reqs_at_risk = reqs_at_risk[:15]

    # ── Tile 4: Quotes Due Soon (approaching deadlines) ──
    # Only fetch columns needed, limit to 50 to avoid loading entire table
    deadline_rows = (
        db.query(Requisition.id, Requisition.name, Requisition.deadline)
        .filter(
            Requisition.deadline.isnot(None),
            Requisition.status.in_(("open", "active", "sourcing")),
            _user_filter(Requisition.created_by),
        )
        .limit(200)
        .all()
    )
    quotes_due_soon = []
    for row in deadline_rows:
        dl = row.deadline
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
            "id": row.id,
            "name": row.name,
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

    # ── Tile 6a: Needs Response (have offers but no quote sent yet) ──
    # Reqs with ≥1 active offer but no sent/won quote
    from ..models.quotes import Quote as Quote2
    quoted_req_ids = (
        db.query(Quote2.requisition_id)
        .filter(Quote2.status.in_(("sent", "won")))
        .distinct()
        .subquery()
    )
    needs_response_q = (
        db.query(
            Requisition.id, Requisition.name, Requisition.customer_name,
            Requisition.created_at,
            func.count(Offer.id).label("offer_count"),
        )
        .join(Offer, Offer.requisition_id == Requisition.id)
        .filter(
            Requisition.status.in_(("open", "active", "sourcing")),
            Offer.status.in_(("active", "approved")),
            Requisition.id.notin_(quoted_req_ids),
            _user_filter(Requisition.created_by),
        )
        .group_by(Requisition.id)
        .order_by(func.count(Offer.id).desc())
        .limit(15)
        .all()
    )
    needs_response = [
        {
            "id": r.id, "name": r.name, "customer_name": r.customer_name,
            "offer_count": r.offer_count,
            "age_label": _age_label(
                (now - _ensure_aware(r.created_at)).total_seconds() / 3600
            ) if r.created_at else None,
        }
        for r in needs_response_q
    ]

    # ── Tile 6b: Expiring Quotes (sent but no response, going stale) ──
    expiring_quotes_q = (
        db.query(
            Quote.id.label("quote_id"), Quote.quote_number, Quote.subtotal,
            Quote.sent_at, Quote.validity_days,
            Requisition.id.label("req_id"), Requisition.name, Requisition.customer_name,
        )
        .join(Requisition, Requisition.id == Quote.requisition_id)
        .filter(
            Quote.status == "sent",
            Quote.result.is_(None),
            Quote.sent_at.isnot(None),
            _user_filter(Quote.created_by_id),
        )
        .order_by(Quote.sent_at.asc())
        .limit(15)
        .all()
    )
    expiring_quotes = []
    for q in expiring_quotes_q:
        sent = _ensure_aware(q.sent_at) if q.sent_at else now
        validity = q.validity_days or 7
        expires_at = sent + timedelta(days=validity)
        days_left = (expires_at - now).days
        if days_left <= 7:  # Only show quotes expiring within 7 days
            expiring_quotes.append({
                "quote_id": q.quote_id, "quote_number": q.quote_number,
                "requisition_id": q.req_id, "name": q.name,
                "customer_name": q.customer_name,
                "value": float(q.subtotal) if q.subtotal else None,
                "days_left": days_left,
            })
    expiring_quotes.sort(key=lambda x: x["days_left"])

    # ── Tile 6c: Buy Plans Pending (awaiting approval or PO) ──
    from ..models.buy_plan import BuyPlanV3 as BP2
    pending_bps_q = (
        db.query(
            BP2.id, BP2.total_revenue, BP2.total_cost, BP2.total_margin_pct,
            BP2.status, BP2.so_status, BP2.requisition_id, BP2.created_at,
        )
        .filter(
            BP2.status.in_(("draft", "pending", "active")),
            _user_filter(BP2.submitted_by_id),
        )
        .order_by(BP2.created_at.asc())
        .limit(15)
        .all()
    )
    pending_req_ids = [bp.requisition_id for bp in pending_bps_q if bp.requisition_id]
    pending_req_names = {}
    if pending_req_ids:
        for r in db.query(Requisition.id, Requisition.customer_name).filter(Requisition.id.in_(pending_req_ids)).all():
            pending_req_names[r.id] = r.customer_name

    buyplans_pending = [
        {
            "id": bp.id, "requisition_id": bp.requisition_id,
            "customer_name": pending_req_names.get(bp.requisition_id, ""),
            "revenue": float(bp.total_revenue) if bp.total_revenue else 0,
            "margin_pct": float(bp.total_margin_pct) if bp.total_margin_pct else 0,
            "status": bp.status, "so_status": bp.so_status,
            "age_label": _age_label(
                (now - _ensure_aware(bp.created_at)).total_seconds() / 3600
            ) if bp.created_at else None,
        }
        for bp in pending_bps_q
    ]

    # ── Tile 7: Completed Deals (all-time — never filtered by days window) ──
    # Summary stats: total wins, losses, values across all time
    completed_stats = db.query(
        func.count(case((Quote.result == "won", Quote.id))).label("won_count"),
        func.count(case((Quote.result == "lost", Quote.id))).label("lost_count"),
        func.coalesce(func.sum(case((Quote.result == "won", Quote.subtotal))), 0).label("won_value"),
        func.coalesce(func.sum(case((Quote.result == "lost", Quote.subtotal))), 0).label("lost_value"),
    ).filter(
        Quote.result.in_(("won", "lost")),
        _user_filter(Quote.created_by_id),
    ).first()

    all_won = completed_stats.won_count or 0
    all_lost = completed_stats.lost_count or 0
    all_win_rate = round(all_won / (all_won + all_lost) * 100) if (all_won + all_lost) else 0

    # Recent 15 won deals
    recent_won_q = (
        db.query(
            Requisition.id, Requisition.name, Requisition.customer_name,
            Quote.subtotal, Quote.result_at,
        )
        .join(Quote, Quote.requisition_id == Requisition.id)
        .filter(Quote.result == "won", _user_filter(Quote.created_by_id))
        .order_by(Quote.result_at.desc())
        .limit(15)
        .all()
    )
    recent_wins = [
        {
            "id": r.id, "name": r.name, "customer_name": r.customer_name,
            "value": float(r.subtotal) if r.subtotal else None,
            "closed_at": r.result_at.isoformat() if r.result_at else None,
            "age_label": _age_label(
                (now - _ensure_aware(r.result_at)).total_seconds() / 3600
            ) if r.result_at else None,
        }
        for r in recent_won_q
    ]

    # Recent 15 lost deals
    recent_lost_q = (
        db.query(
            Requisition.id, Requisition.name, Requisition.customer_name,
            Quote.subtotal, Quote.result_at,
        )
        .join(Quote, Quote.requisition_id == Requisition.id)
        .filter(Quote.result == "lost", _user_filter(Quote.created_by_id))
        .order_by(Quote.result_at.desc())
        .limit(15)
        .all()
    )
    recent_losses = [
        {
            "id": r.id, "name": r.name, "customer_name": r.customer_name,
            "value": float(r.subtotal) if r.subtotal else None,
            "closed_at": r.result_at.isoformat() if r.result_at else None,
            "age_label": _age_label(
                (now - _ensure_aware(r.result_at)).total_seconds() / 3600
            ) if r.result_at else None,
        }
        for r in recent_lost_q
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
        "revenue_profit": revenue_profit,
        "reqs_at_risk": reqs_at_risk,
        "quotes_due_soon": quotes_due_soon,
        "needs_response": needs_response,
        "expiring_quotes": expiring_quotes,
        "buyplans_pending": buyplans_pending,
        "completed_deals": {
            "won_count": all_won,
            "lost_count": all_lost,
            "won_value": float(completed_stats.won_value or 0),
            "lost_value": float(completed_stats.lost_value or 0),
            "win_rate": all_win_rate,
            "recent_wins": recent_wins,
            "recent_losses": recent_losses,
        },
        "pipeline": {
            "active_reqs": pipeline_active,
            "quotes_out": pipeline_quoted,
            "won_this_month": pipeline_won,
            "lost_this_month": lost_quotes,
            "buyplans_approved": pipeline_buyplans,
        },
    }


@router.get("/team-leaderboard")
@cached_endpoint(prefix="team_leaderboard", ttl_hours=0.5, key_params=["role"])
def team_leaderboard(
    role: str = Query(default="buyer", pattern="^(buyer|sales)$"),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    """Combined leaderboard: Avail Score + Multiplier Points + Bonus status.

    Returns one ranked list per role with both scoring systems merged.
    Called by: app.js (team scope leaderboard tab)
    """
    from datetime import date

    from ..models.auth import User
    from ..models.performance import AvailScoreSnapshot, MultiplierScoreSnapshot

    current_month = date.today().replace(day=1)

    # Fetch Avail Scores for this role+month
    avail_rows = (
        db.query(AvailScoreSnapshot, User.name)
        .join(User, User.id == AvailScoreSnapshot.user_id)
        .filter(
            AvailScoreSnapshot.month == current_month,
            AvailScoreSnapshot.role_type == role,
        )
        .all()
    )
    avail_map = {}
    for snap, uname in avail_rows:
        avail_map[snap.user_id] = {
            "user_name": uname,
            "avail_score": snap.total_score or 0,
            "behavior_total": snap.behavior_total or 0,
            "outcome_total": snap.outcome_total or 0,
            "avail_rank": snap.rank,
            "avail_qualified": snap.qualified,
            "avail_bonus": snap.bonus_amount or 0,
            "avail_updated_at": snap.updated_at.isoformat() if snap.updated_at else None,
            # Include full metric breakdown
            **{f"b{i}_score": getattr(snap, f"b{i}_score", 0) or 0 for i in range(1, 6)},
            **{f"b{i}_label": getattr(snap, f"b{i}_label", "") or "" for i in range(1, 6)},
            **{f"b{i}_raw": getattr(snap, f"b{i}_raw", "") or "" for i in range(1, 6)},
            **{f"o{i}_score": getattr(snap, f"o{i}_score", 0) or 0 for i in range(1, 6)},
            **{f"o{i}_label": getattr(snap, f"o{i}_label", "") or "" for i in range(1, 6)},
            **{f"o{i}_raw": getattr(snap, f"o{i}_raw", "") or "" for i in range(1, 6)},
        }

    # Fetch Multiplier Scores for this role+month
    mult_rows = (
        db.query(MultiplierScoreSnapshot, User.name)
        .join(User, User.id == MultiplierScoreSnapshot.user_id)
        .filter(
            MultiplierScoreSnapshot.month == current_month,
            MultiplierScoreSnapshot.role_type == role,
        )
        .all()
    )
    mult_map = {}
    for snap, uname in mult_rows:
        entry = {
            "user_name": uname,
            "total_points": snap.total_points or 0,
            "offer_points": snap.offer_points or 0,
            "bonus_points": snap.bonus_points or 0,
            "mult_rank": snap.rank,
            "mult_qualified": snap.qualified,
            "mult_bonus": snap.bonus_amount or 0,
            "mult_updated_at": snap.updated_at.isoformat() if snap.updated_at else None,
        }
        # Role-specific breakdown
        if role == "buyer":
            entry["breakdown"] = {
                "offers_total": snap.offers_total or 0,
                "offers_base": snap.offers_base_count or 0,
                "offers_quoted": snap.offers_quoted_count or 0,
                "offers_bp": snap.offers_bp_count or 0,
                "offers_po": snap.offers_po_count or 0,
                "pts_base": snap.offers_base_pts or 0,
                "pts_quoted": snap.offers_quoted_pts or 0,
                "pts_bp": snap.offers_bp_pts or 0,
                "pts_po": snap.offers_po_pts or 0,
                "rfqs_sent": snap.rfqs_sent_count or 0,
                "pts_rfqs": snap.rfqs_sent_pts or 0,
                "stock_lists": snap.stock_lists_count or 0,
                "pts_stock": snap.stock_lists_pts or 0,
            }
        else:
            entry["breakdown"] = {
                "quotes_sent": snap.quotes_sent_count or 0,
                "quotes_won": snap.quotes_won_count or 0,
                "pts_quote_sent": snap.quotes_sent_pts or 0,
                "pts_quote_won": snap.quotes_won_pts or 0,
                "proactive_sent": snap.proactive_sent_count or 0,
                "proactive_converted": snap.proactive_converted_count or 0,
                "pts_proactive_sent": snap.proactive_sent_pts or 0,
                "pts_proactive_converted": snap.proactive_converted_pts or 0,
                "new_accounts": snap.new_accounts_count or 0,
                "pts_accounts": snap.new_accounts_pts or 0,
            }
        mult_map[snap.user_id] = entry

    # Merge all user IDs from both systems
    all_uids = set(avail_map.keys()) | set(mult_map.keys())

    # Fetch user roles for trader badge display
    role_map = {}
    if all_uids:
        role_rows = db.query(User.id, User.role).filter(User.id.in_(all_uids)).all()
        role_map = {r.id: r.role for r in role_rows}

    entries = []
    for uid in all_uids:
        av = avail_map.get(uid, {})
        mu = mult_map.get(uid, {})
        name = av.get("user_name") or mu.get("user_name", f"User #{uid}")
        entries.append({
            "user_id": uid,
            "user_name": name,
            "user_role": role_map.get(uid, "buyer"),
            # Avail Score data
            "avail_score": av.get("avail_score", 0),
            "behavior_total": av.get("behavior_total", 0),
            "outcome_total": av.get("outcome_total", 0),
            "avail_rank": av.get("avail_rank"),
            "avail_qualified": av.get("avail_qualified", False),
            "avail_bonus": av.get("avail_bonus", 0),
            # Multiplier data
            "total_points": mu.get("total_points", 0),
            "offer_points": mu.get("offer_points", 0),
            "bonus_points": mu.get("bonus_points", 0),
            "mult_rank": mu.get("mult_rank"),
            "mult_qualified": mu.get("mult_qualified", False),
            "mult_bonus": mu.get("mult_bonus", 0),
            "breakdown": mu.get("breakdown", {}),
            # Full Avail metric breakdown for expandable rows
            **{k: av.get(k, 0) for k in av if k.startswith(("b", "o")) and "_" in k},
            "updated_at": mu.get("mult_updated_at") or av.get("avail_updated_at"),
        })

    # Sort by total_points desc, tiebreak by avail_score
    entries.sort(key=lambda e: (e["total_points"], e["avail_score"]), reverse=True)
    for i, e in enumerate(entries):
        e["rank"] = i + 1

    return {
        "month": current_month.isoformat(),
        "role": role,
        "entries": entries,
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


# ── Reactivation Signals ─────────────────────────────────────────────


@router.get("/reactivation-signals")
def reactivation_signals(
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return active reactivation signals for the sales dashboard."""
    from ..models import ReactivationSignal

    signals = (
        db.query(ReactivationSignal)
        .filter(ReactivationSignal.dismissed_at.is_(None))
        .order_by(ReactivationSignal.created_at.desc())
        .limit(50)
        .all()
    )
    return [
        {
            "id": s.id,
            "company_id": s.company_id,
            "material_card_id": s.material_card_id,
            "signal_type": s.signal_type,
            "reason": s.reason,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in signals
    ]


# ── Unified Leaderboard ────────────────────────────────────────────


@router.get("/unified-leaderboard")
@cached_endpoint(prefix="unified_lb", ttl_hours=0.25, key_params=["month"])
def unified_leaderboard(
    month: str = Query(None, description="YYYY-MM format"),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    """Return unified cross-role leaderboard with category breakdowns and AI blurbs."""
    from ..services.unified_score_service import get_unified_leaderboard

    if month:
        try:
            m = datetime.strptime(month, "%Y-%m").date()
        except ValueError:
            from fastapi import HTTPException
            raise HTTPException(400, "Invalid month format — use YYYY-MM")
    else:
        m = datetime.now(timezone.utc).date().replace(day=1)

    return get_unified_leaderboard(db, m)


@router.get("/scoring-info")
def scoring_info(user=Depends(require_user)):
    """Return static scoring system explanation for the info pill tooltip."""
    from ..services.unified_score_service import get_scoring_info

    return get_scoring_info()
