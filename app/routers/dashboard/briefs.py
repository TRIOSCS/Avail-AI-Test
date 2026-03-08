"""Dashboard brief endpoints — morning-brief, hot-offers, buyer-brief.

Provides AI-generated briefs, hot offer feeds, and buyer command center data.

Called by: app/static/app.js (loadDashboard, loadBuyerDashboard)
Depends on: models/crm.py, models/intelligence.py, models/quotes.py, models/offers.py, models/buy_plan.py
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
from ._shared import _age_label, _ensure_aware

router = APIRouter()


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
    from ...models.crm import Company, CustomerSite
    from ...models.intelligence import ActivityLog, ProactiveMatch
    from ...models.quotes import Quote

    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)

    # ── Gather stats ──

    # Stale accounts (no outbound activity in 7 days)
    owned_company_ids_sub = db.query(CustomerSite.company_id).filter(CustomerSite.owner_id == user.id).distinct()
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
        sites = db.query(CustomerSite.id).filter(CustomerSite.company_id.in_(company_ids)).all()
        site_ids = [s.id for s in sites]

    # Quote stats: awaiting + won/lost this week (single query with case())
    quotes_awaiting = 0
    won_this_week = 0
    lost_this_week = 0
    if site_ids:
        quote_stats = (
            db.query(
                func.count(
                    case(
                        (and_(Quote.status == "sent", Quote.result.is_(None)), Quote.id),
                    )
                ).label("awaiting"),
                func.count(
                    case(
                        (and_(Quote.result == "won", Quote.result_at >= week_ago), Quote.id),
                    )
                ).label("won"),
                func.count(
                    case(
                        (and_(Quote.result == "lost", Quote.result_at >= week_ago), Quote.id),
                    )
                ).label("lost"),
            )
            .filter(Quote.customer_site_id.in_(site_ids))
            .first()
        )
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
        from ...utils.claude_client import claude_structured

        prompt = (
            f"You are a sales assistant for an electronic component distributor. "
            f"Write a 2-sentence morning brief for {user.name}. Be direct and actionable.\n\n"
            f"Stats:\n"
            f"- {stale_count} accounts with no outreach in 7+ days\n"
            f"- {quotes_awaiting} quotes sent but awaiting customer response\n"
            f"- {new_proactive_matches} new proactive match opportunities\n"
            f"- {won_this_week} won this week, {lost_this_week} lost this week\n"
            f"- {len(owned_companies)} total accounts owned\n\n"
            f'Return JSON: {{"text": "your brief here"}}'
        )
        schema = {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }
        result = asyncio.run(
            claude_structured(
                prompt=prompt,
                schema=schema,
                system="You write concise sales briefings. Be specific about numbers. No fluff.",
                model_tier="fast",
                max_tokens=256,
                timeout=10,
            )
        )
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
    from ...models.offers import Offer

    @cached_endpoint(prefix="hot_offers", ttl_hours=0.25, key_params=["days"])
    def _fetch(days, db):
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days)

        # Query only needed columns, skip unnecessary JOIN since we just
        # need requisition_id (already on the Offer row)
        offers = (
            db.query(
                Offer.id,
                Offer.vendor_name,
                Offer.mpn,
                Offer.unit_price,
                Offer.currency,
                Offer.requisition_id,
                Offer.source,
                Offer.created_at,
            )
            .filter(
                Offer.created_at >= cutoff,
                Offer.status == "active",
            )
            .order_by(Offer.created_at.desc())
            .limit(15)
            .all()
        )

        # Deduplicate: max 2 offers per requisition
        seen_reqs = {}
        deduped = []
        for o in offers:
            req_id = o.requisition_id
            seen_reqs[req_id] = seen_reqs.get(req_id, 0) + 1
            if seen_reqs[req_id] <= 2:
                deduped.append(o)
        offers = deduped

        results = []
        for o in offers:
            ca = _ensure_aware(o.created_at)
            age_hours = (now - ca).total_seconds() / 3600 if ca else 0
            results.append(
                {
                    "id": o.id,
                    "vendor_name": o.vendor_name,
                    "mpn": o.mpn,
                    "unit_price": float(o.unit_price) if o.unit_price else None,
                    "currency": o.currency or "USD",
                    "requisition_id": o.requisition_id,
                    "source": o.source,
                    "age_label": _age_label(age_hours),
                    "created_at": o.created_at.isoformat() if o.created_at else None,
                }
            )
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

    Always returns personal KPIs + team_kpis (team-wide rates).
    scope param kept for backward compat but primary KPIs are always user-scoped.
    """
    from ...models.offers import Offer
    from ...models.quotes import BuyPlan, Quote
    from ...models.sourcing import Requisition

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # ── KPI calculation helper (reused for personal + team) ──
    def _compute_kpis(filter_fn):
        """Compute 4 buyer KPIs using the given column filter function."""
        total_reqs = (
            db.query(func.count(Requisition.id))
            .filter(Requisition.created_at >= month_start, filter_fn(Requisition.created_by))
            .scalar()
        ) or 0

        sourced_reqs = 0
        if total_reqs:
            sourced_reqs = (
                db.query(func.count(Requisition.id.distinct()))
                .join(Offer, Offer.requisition_id == Requisition.id)
                .filter(
                    Requisition.created_at >= month_start,
                    filter_fn(Requisition.created_by),
                )
                .scalar()
            ) or 0

        s_ratio = round(sourced_reqs / total_reqs * 100) if total_reqs else 0

        o_stats = (
            db.query(
                func.count(Offer.id).label("total"),
                func.count(
                    case((Offer.attribution_status == "converted", Offer.id))
                ).label("quoted"),
            )
            .filter(Offer.created_at >= month_start, filter_fn(Offer.entered_by_id))
            .first()
        )
        total_offers = o_stats.total or 0
        quoted_offers = o_stats.quoted or 0
        oq_rate = round(quoted_offers / total_offers * 100) if total_offers else 0

        q_stats = (
            db.query(
                func.count(case((Quote.result == "won", Quote.id))).label("won"),
                func.count(case((Quote.result == "lost", Quote.id))).label("lost"),
            )
            .filter(Quote.result_at >= month_start, filter_fn(Quote.created_by_id))
            .first()
        )
        won = q_stats.won or 0
        lost = q_stats.lost or 0
        qw_rate = round(won / (won + lost) * 100) if (won + lost) else 0

        bp = (
            db.query(
                func.count(BuyPlan.id).label("total"),
                func.count(
                    case(
                        (BuyPlan.status.in_(("approved", "po_entered", "po_confirmed", "complete")), BuyPlan.id),
                    )
                ).label("approved"),
                func.count(
                    case(
                        (BuyPlan.status.in_(("po_confirmed", "complete")), BuyPlan.id),
                    )
                ).label("confirmed"),
            )
            .filter(BuyPlan.submitted_at >= month_start, filter_fn(BuyPlan.submitted_by_id))
            .first()
        )
        total_bp = bp.total or 0
        confirmed_bp = bp.confirmed or 0
        bp_rate = round(confirmed_bp / total_bp * 100) if total_bp else 0

        return {
            "sourcing_ratio": s_ratio,
            "total_reqs": total_reqs,
            "sourced_reqs": sourced_reqs,
            "offer_quote_rate": oq_rate,
            "total_offers": total_offers,
            "quoted_offers": quoted_offers,
            "quote_win_rate": qw_rate,
            "won": won,
            "lost": lost,
            "buyplan_po_rate": bp_rate,
            "total_buyplans": total_bp,
            "confirmed_pos": confirmed_bp,
        }

    # Personal KPIs (always user-scoped)
    personal = _compute_kpis(lambda col: col == user.id)
    sourcing_ratio = personal["sourcing_ratio"]
    total_reqs_q = personal["total_reqs"]
    sourced_reqs_q = personal["sourced_reqs"]
    offer_quote_rate = personal["offer_quote_rate"]
    total_offers_month = personal["total_offers"]
    quoted_offers_month = personal["quoted_offers"]
    quote_win_rate = personal["quote_win_rate"]
    won_quotes = personal["won"]
    lost_quotes = personal["lost"]
    buyplan_po_rate = personal["buyplan_po_rate"]
    total_bps = personal["total_buyplans"]
    approved_bps = 0  # not used downstream separately
    po_confirmed_bps = personal["confirmed_pos"]

    # Team-wide KPIs (all users)
    team = _compute_kpis(lambda col: col.isnot(None))
    team_kpis = {
        "sourcing_ratio": team["sourcing_ratio"],
        "offer_quote_rate": team["offer_quote_rate"],
        "quote_win_rate": team["quote_win_rate"],
        "buyplan_po_rate": team["buyplan_po_rate"],
    }

    # ── Scope filter for tiles (always user-scoped for primary view) ──
    def _user_filter(col):
        """Restrict to current user for tile data."""
        return col == user.id

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
        rows = db.query(Offer.requisition_id).filter(Offer.requisition_id.in_(new_req_ids)).distinct().all()
        reqs_with_offers = {r.requisition_id for r in rows}

    new_requirements = []
    for r in new_reqs_q:
        age_hours = (now - _ensure_aware(r.created_at)).total_seconds() / 3600 if r.created_at else 0
        new_requirements.append(
            {
                "id": r.id,
                "name": r.name,
                "customer_name": r.customer_name,
                "has_offers": r.id in reqs_with_offers,
                "deadline": r.deadline,
                "age_label": _age_label(age_hours),
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
        )

    # ── Tile 2: Offers to Review (needs_review status) ──
    review_offers = (
        db.query(Offer).filter(Offer.status == "needs_review").order_by(Offer.created_at.desc()).limit(15).all()
    )
    offers_to_review = []
    for o in review_offers:
        age_hours = (now - _ensure_aware(o.created_at)).total_seconds() / 3600 if o.created_at else 0
        offers_to_review.append(
            {
                "id": o.id,
                "requisition_id": o.requisition_id,
                "vendor_name": o.vendor_name,
                "mpn": o.mpn,
                "unit_price": float(o.unit_price) if o.unit_price else None,
                "source": o.source,
                "age_label": _age_label(age_hours),
            }
        )

    # ── Tile 3a: Revenue & Profit (from buy plans) ──
    from ...models.buy_plan import BuyPlanV3

    # Cap per-plan revenue at $10M to exclude obvious test/junk data
    BP_REVENUE_CAP = 10_000_000

    bp_financials = (
        db.query(
            func.coalesce(func.sum(BuyPlanV3.total_revenue), 0).label("revenue"),
            func.coalesce(func.sum(BuyPlanV3.total_cost), 0).label("cost"),
            func.count(BuyPlanV3.id).label("plan_count"),
        )
        .filter(
            BuyPlanV3.status.in_(("active", "completed")),
            BuyPlanV3.created_at >= cutoff,
            _user_filter(BuyPlanV3.submitted_by_id),
            BuyPlanV3.total_revenue <= BP_REVENUE_CAP,
        )
        .first()
    )

    bp_revenue = float(bp_financials.revenue or 0)
    bp_cost = float(bp_financials.cost or 0)
    bp_gross_profit = bp_revenue - bp_cost
    bp_margin_pct = round((bp_gross_profit / bp_revenue) * 100, 1) if bp_revenue > 0 else 0

    # Pipeline (pending/draft buy plans not yet completed)
    bp_pipeline = (
        db.query(
            func.coalesce(func.sum(BuyPlanV3.total_revenue), 0).label("revenue"),
            func.coalesce(func.sum(BuyPlanV3.total_cost), 0).label("cost"),
            func.count(BuyPlanV3.id).label("plan_count"),
        )
        .filter(
            BuyPlanV3.status.in_(("draft", "pending", "active")),
            BuyPlanV3.created_at >= cutoff,
            _user_filter(BuyPlanV3.submitted_by_id),
            BuyPlanV3.total_revenue <= BP_REVENUE_CAP,
        )
        .first()
    )

    pipeline_revenue = float(bp_pipeline.revenue or 0)
    pipeline_profit = pipeline_revenue - float(bp_pipeline.cost or 0)

    # Recent buy plans for drill-down
    recent_bps = (
        db.query(
            BuyPlanV3.id,
            BuyPlanV3.total_revenue,
            BuyPlanV3.total_cost,
            BuyPlanV3.total_margin_pct,
            BuyPlanV3.status,
            BuyPlanV3.requisition_id,
            BuyPlanV3.created_at,
        )
        .filter(
            BuyPlanV3.status.in_(("active", "completed", "pending")),
            BuyPlanV3.created_at >= cutoff,
            _user_filter(BuyPlanV3.submitted_by_id),
            BuyPlanV3.total_revenue <= BP_REVENUE_CAP,
        )
        .order_by(BuyPlanV3.created_at.desc())
        .limit(15)
        .all()
    )
    # Resolve customer names via requisition
    from ...models.sourcing import Requisition as Req2

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
                    elif days_left <= 7 and num_offers == 0:  # pragma: no cover
                        risk_reasons.append(f"{days_left}d left — no offers")
                        urgency = "warning"
                    elif days_left <= 3 and num_offers == 1:  # pragma: no cover
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
            reqs_at_risk.append(
                {
                    "id": row.id,
                    "name": row.name,
                    "customer_name": row.customer_name,
                    "num_offers": num_offers,
                    "risk": risk_reasons[0],
                    "urgency": urgency,
                }
            )

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
        quotes_due_soon.append(
            {
                "id": row.id,
                "name": row.name,
                "deadline": dl,
                "days_left": days_left,
                "urgency": urgency,
            }
        )
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
    from ...models.quotes import Quote as Quote2

    quoted_req_ids = db.query(Quote2.requisition_id).filter(Quote2.status.in_(("sent", "won"))).distinct().subquery()
    needs_response_q = (
        db.query(
            Requisition.id,
            Requisition.name,
            Requisition.customer_name,
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
            "id": r.id,
            "name": r.name,
            "customer_name": r.customer_name,
            "offer_count": r.offer_count,
            "age_label": _age_label((now - _ensure_aware(r.created_at)).total_seconds() / 3600)
            if r.created_at
            else None,
        }
        for r in needs_response_q
    ]

    # ── Tile 6b: Expiring Quotes (sent but no response, going stale) ──
    expiring_quotes_q = (
        db.query(
            Quote.id.label("quote_id"),
            Quote.quote_number,
            Quote.subtotal,
            Quote.sent_at,
            Quote.validity_days,
            Requisition.id.label("req_id"),
            Requisition.name,
            Requisition.customer_name,
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
    for q in expiring_quotes_q:  # pragma: no cover
        sent = _ensure_aware(q.sent_at) if q.sent_at else now
        validity = q.validity_days or 7
        expires_at = sent + timedelta(days=validity)
        days_left = (expires_at - now).days
        if days_left <= 7:  # Only show quotes expiring within 7 days
            expiring_quotes.append(
                {
                    "quote_id": q.quote_id,
                    "quote_number": q.quote_number,
                    "requisition_id": q.req_id,
                    "name": q.name,
                    "customer_name": q.customer_name,
                    "value": float(q.subtotal) if q.subtotal else None,
                    "days_left": days_left,
                }
            )
    expiring_quotes.sort(key=lambda x: x["days_left"])

    # ── Tile 6c: Buy Plans Pending (awaiting approval or PO) ──
    from ...models.buy_plan import BuyPlanV3 as BP2

    pending_bps_q = (
        db.query(
            BP2.id,
            BP2.total_revenue,
            BP2.total_cost,
            BP2.total_margin_pct,
            BP2.status,
            BP2.so_status,
            BP2.requisition_id,
            BP2.created_at,
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
            "id": bp.id,
            "requisition_id": bp.requisition_id,
            "customer_name": pending_req_names.get(bp.requisition_id, ""),
            "revenue": float(bp.total_revenue) if bp.total_revenue else 0,
            "margin_pct": float(bp.total_margin_pct) if bp.total_margin_pct else 0,
            "status": bp.status,
            "so_status": bp.so_status,
            "age_label": _age_label((now - _ensure_aware(bp.created_at)).total_seconds() / 3600)
            if bp.created_at
            else None,
        }
        for bp in pending_bps_q
    ]

    # ── Tile 7: Completed Deals (all-time — never filtered by days window) ──
    # Summary stats: total wins, losses, values across all time
    completed_stats = (
        db.query(
            func.count(case((Quote.result == "won", Quote.id))).label("won_count"),
            func.count(case((Quote.result == "lost", Quote.id))).label("lost_count"),
            func.coalesce(func.sum(case((Quote.result == "won", Quote.subtotal))), 0).label("won_value"),
            func.coalesce(func.sum(case((Quote.result == "lost", Quote.subtotal))), 0).label("lost_value"),
        )
        .filter(
            Quote.result.in_(("won", "lost")),
            _user_filter(Quote.created_by_id),
        )
        .first()
    )

    all_won = completed_stats.won_count or 0
    all_lost = completed_stats.lost_count or 0
    all_win_rate = round(all_won / (all_won + all_lost) * 100) if (all_won + all_lost) else 0

    # Recent 15 won deals
    recent_won_q = (
        db.query(
            Requisition.id,
            Requisition.name,
            Requisition.customer_name,
            Quote.subtotal,
            Quote.result_at,
        )
        .join(Quote, Quote.requisition_id == Requisition.id)
        .filter(Quote.result == "won", _user_filter(Quote.created_by_id))
        .order_by(Quote.result_at.desc())
        .limit(15)
        .all()
    )
    recent_wins = [
        {
            "id": r.id,
            "name": r.name,
            "customer_name": r.customer_name,
            "value": float(r.subtotal) if r.subtotal else None,
            "closed_at": r.result_at.isoformat() if r.result_at else None,
            "age_label": _age_label((now - _ensure_aware(r.result_at)).total_seconds() / 3600) if r.result_at else None,
        }
        for r in recent_won_q
    ]

    # Recent 15 lost deals
    recent_lost_q = (
        db.query(
            Requisition.id,
            Requisition.name,
            Requisition.customer_name,
            Quote.subtotal,
            Quote.result_at,
        )
        .join(Quote, Quote.requisition_id == Requisition.id)
        .filter(Quote.result == "lost", _user_filter(Quote.created_by_id))
        .order_by(Quote.result_at.desc())
        .limit(15)
        .all()
    )
    recent_losses = [
        {
            "id": r.id,
            "name": r.name,
            "customer_name": r.customer_name,
            "value": float(r.subtotal) if r.subtotal else None,
            "closed_at": r.result_at.isoformat() if r.result_at else None,
            "age_label": _age_label((now - _ensure_aware(r.result_at)).total_seconds() / 3600) if r.result_at else None,
        }
        for r in recent_lost_q
    ]

    return {
        "kpis": personal,
        "team_kpis": team_kpis,
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


@router.get("/briefing")
@cached_endpoint(prefix="daily_briefing", ttl_hours=1, key_params=[])
def daily_briefing(
    role: str = Query("buyer", regex="^(buyer|sales)$"),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    """Role-aware morning briefing — replaces the daily Excel handoff.

    Pure data aggregation, no AI calls. Cached per user for 1 hour.
    """
    from app.services.dashboard_briefing import generate_briefing

    return generate_briefing(db=db, user_id=user.id, role=role)
