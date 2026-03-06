"""Dashboard overview endpoints — needs-attention + attention-feed.

Provides the main dashboard views for the Sales Command Center.

Called by: app/static/app.js (loadDashboard)
Depends on: models/crm.py, models/intelligence.py, models/quotes.py, models/sourcing.py
"""

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from loguru import logger
from sqlalchemy import and_, case, func
from sqlalchemy.orm import Session

from ...cache.decorators import cached_endpoint
from ...database import get_db
from ...dependencies import require_user
from ._shared import _ensure_aware

router = APIRouter()


@router.get("/needs-attention")
@cached_endpoint(prefix="needs_attention", ttl_hours=0.5, key_params=["days", "scope"])
def needs_attention(
    days: int = Query(default=30, ge=0, le=366),
    scope: str = Query(default="my", pattern="^(my|team)$"),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    """Return companies that haven't been contacted in `days` days.

    scope=my: only companies where user owns at least one site.
    scope=team: all active companies (team-wide view).

    Sorted by staleness (most stale first). Includes strategic flag,
    open req count, and open quote value for prioritization.
    """
    from ...models.crm import Company, CustomerSite
    from ...models.intelligence import ActivityLog
    from ...models.quotes import Quote
    from ...models.sourcing import Requisition

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    # Get active companies — scope controls which ones
    if scope == "team":
        companies = (
            db.query(Company)
            .filter(Company.is_active.is_(True))
            .all()
        )
    else:
        owned_company_ids = db.query(CustomerSite.company_id).filter(CustomerSite.owner_id == user.id).distinct()
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
        db.query(CustomerSite.id, CustomerSite.company_id).filter(CustomerSite.company_id.in_(company_ids)).all()
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

        results.append(
            {
                "company_id": c.id,
                "company_name": c.name,
                "is_strategic": bool(c.is_strategic),
                "last_outreach_at": last_at.isoformat() if last_at else None,
                "last_channel": last_channel_map.get(c.id),
                "days_since_contact": days_since,
                "open_req_count": open_req_map.get(c.id, 0),
                "open_quote_value": round(open_quote_map.get(c.id, 0), 2),
            }
        )

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
    from ...models.crm import Company, CustomerSite
    from ...models.intelligence import ActivityLog
    from ...models.offers import Offer
    from ...models.quotes import Quote
    from ...models.sourcing import Requisition

    now = datetime.now(timezone.utc)

    def _user_filter(col):  # pragma: no cover
        if scope == "my":
            return col == user.id
        return col.isnot(None)

    items = []
    urgency_order = {"critical": 0, "warning": 1, "info": 2}

    # ── Source 1: Stale accounts (no outreach in N days) ──
    owned_company_ids = db.query(CustomerSite.company_id).filter(CustomerSite.owner_id == user.id).distinct()
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
            if days_since < days:  # pragma: no cover
                continue
        else:
            days_since = 999
        urgency = "critical" if days_since > 60 or co.is_strategic else "warning"
        detail = "Never contacted" if days_since == 999 else f"{days_since}d since last outreach"
        items.append(
            {
                "type": "stale_account",
                "urgency": urgency,
                "title": co.name,
                "detail": detail,
                "link_type": "company",
                "link_id": co.id,
            }
        )

    # ── Source 2: Reqs at risk (deadline passed or low offers) ──
    offer_count_sub = (
        db.query(Offer.requisition_id, func.count(Offer.id).label("cnt")).group_by(Offer.requisition_id).subquery()
    )
    risk_rows = (
        db.query(
            Requisition.id,
            Requisition.name,
            Requisition.customer_name,
            Requisition.deadline,
            Requisition.created_at,
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
                    elif days_left <= 3 and num_offers == 0:  # pragma: no cover
                        urgency = "critical"
                        detail = f"{days_left}d left — no offers"
                    elif days_left <= 3 and num_offers == 1:  # pragma: no cover
                        urgency = "warning"
                        detail = f"{days_left}d left — only 1 offer"
                except (ValueError, TypeError):
                    pass

        if not urgency and num_offers == 0 and age_hours >= 48:
            urgency = "warning"
            detail = f"No offers after {int(age_hours / 24)}d"

        if urgency:
            items.append(
                {
                    "type": "req_at_risk",
                    "urgency": urgency,
                    "title": row.name or f"REQ #{row.id}",
                    "detail": detail,
                    "link_type": "requisition",
                    "link_id": row.id,
                }
            )

    # ── Source 3: Needs quote (has offers, no quote sent) ──
    quoted_req_ids = (
        db.query(Quote.requisition_id).filter(Quote.status.in_(("sent", "won"))).distinct().scalar_subquery()
    )
    needs_quote_rows = (
        db.query(
            Requisition.id,
            Requisition.name,
            Requisition.customer_name,
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
        items.append(
            {
                "type": "needs_quote",
                "urgency": "warning",
                "title": r.customer_name or r.name or f"REQ #{r.id}",
                "detail": f"{r.offer_count} offer{'s' if r.offer_count != 1 else ''} ready — no quote sent",
                "link_type": "requisition",
                "link_id": r.id,
            }
        )

    # ── Source 4: Expiring quotes ──
    expiring_q = (
        db.query(
            Quote.id.label("quote_id"),
            Quote.quote_number,
            Quote.subtotal,
            Quote.sent_at,
            Quote.validity_days,
            Requisition.id.label("req_id"),
            Requisition.customer_name,
        )
        .join(Requisition, Requisition.id == Quote.requisition_id)
        .filter(
            Quote.status == "sent",
            Quote.result.is_(None),
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
            items.append(
                {
                    "type": "expiring_quote",
                    "urgency": urgency,
                    "title": q.customer_name or q.quote_number or f"Quote #{q.quote_id}",
                    "detail": f"{q.quote_number} expiring ({dl_label}){val}",
                    "link_type": "requisition",
                    "link_id": q.req_id,
                }
            )

    # ── Source 5: Buy plans pending ──
    try:
        from ...models.buy_plan import BuyPlanV3

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
            items.append(
                {
                    "type": "buyplan_pending",
                    "urgency": "info",
                    "title": bp_names.get(bp.requisition_id, f"BP #{bp.id}"),
                    "detail": f"Buy plan {bp.status}{rev}",
                    "link_type": "requisition",
                    "link_id": bp.requisition_id or bp.id,
                }
            )
    except ImportError:  # pragma: no cover
        pass

    # Sort by urgency: critical > warning > info, then by type for stability
    items.sort(key=lambda x: (urgency_order.get(x["urgency"], 9), x["type"]))
    return items[:12]
