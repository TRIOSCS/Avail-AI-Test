"""services/reporting_service.py — CRM reporting aggregations.

Three public functions: coverage_report, pipeline_report, outcome_funnel.
Used to power the Reporting tab in the CRM shell.

Called by: app/routers/crm/views.py (crm_reporting route)
Depends on: app/models (Company, Requisition, Quote, ActivityLog, User),
    app/services/crm_service (cadence_state)
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..constants import RequisitionStatus as RS
from ..models.auth import User
from ..models.crm import Company
from ..models.intelligence import ActivityLog
from ..models.quotes import Quote
from ..models.sourcing import Requisition
from .crm_service import cadence_state

TIER_ORDER = ["key", "core", "standard", "prospect"]

_ACTIVE_STATUSES = {RS.DRAFT, RS.ACTIVE}
_SOURCING_STATUSES = {RS.SOURCING, RS.OFFERS}
_QUOTING_STATUSES = {RS.QUOTING, RS.QUOTED, RS.REOPENED}
_WON_STATUSES = {RS.WON}
_LOST_STATUSES = {RS.LOST}

_INTERACTION_TYPES = {"email_sent", "email_received", "call_logged", "teams_message"}


def coverage_report(db: Session) -> dict:
    """Compute cadence coverage across all active companies.

    Returns {by_tier: list[dict], by_rep: list[dict], summary: dict}.
    by_tier rows: {tier, total, on_target, due, overdue, new, coverage_pct}
    by_rep rows:  {rep, total, on_target, due, overdue, new, coverage_pct}
    summary:      {total, overdue, overdue_pct}
    """
    rows = db.execute(
        select(
            Company.tier,
            Company.last_outbound_at,
            Company.account_owner_id,
        ).where(Company.is_active.is_(True))
    ).all()

    # Load owner names
    owner_ids = {r.account_owner_id for r in rows if r.account_owner_id}
    owner_map: dict[int, str] = {}
    if owner_ids:
        users = db.execute(select(User.id, User.name, User.email).where(User.id.in_(owner_ids))).all()
        owner_map = {u.id: (u.name or u.email) for u in users}

    # Aggregate by tier
    tier_buckets: dict[str, dict] = {
        t: {"tier": t, "total": 0, "on_target": 0, "due": 0, "overdue": 0, "new": 0} for t in TIER_ORDER
    }
    rep_buckets: dict[str | None, dict] = {}

    for r in rows:
        tier = r.tier if r.tier in tier_buckets else "standard"
        state = cadence_state(tier, r.last_outbound_at)

        tier_buckets[tier]["total"] += 1
        tier_buckets[tier][state] += 1

        rep_name = owner_map.get(r.account_owner_id, "Unassigned") if r.account_owner_id else "Unassigned"
        if rep_name not in rep_buckets:
            rep_buckets[rep_name] = {
                "rep": rep_name,
                "total": 0,
                "on_target": 0,
                "due": 0,
                "overdue": 0,
                "new": 0,
            }
        rep_buckets[rep_name]["total"] += 1
        rep_buckets[rep_name][state] += 1

    def _coverage_pct(bucket: dict) -> float:
        total = bucket["total"]
        if total == 0:
            return 0.0
        touched = bucket["on_target"] + bucket["due"]
        return round(touched / total * 100, 1)

    by_tier = []
    for t in TIER_ORDER:
        b = tier_buckets.get(t)
        if b:
            by_tier.append({**b, "coverage_pct": _coverage_pct(b)})

    by_rep = sorted(
        [{**b, "coverage_pct": _coverage_pct(b)} for b in rep_buckets.values()],
        key=lambda x: x["total"],
        reverse=True,
    )

    total = sum(b["total"] for b in tier_buckets.values())
    total_overdue = sum(b["overdue"] for b in tier_buckets.values())
    overdue_pct = round(total_overdue / total * 100, 1) if total else 0.0

    return {
        "by_tier": by_tier,
        "by_rep": by_rep,
        "summary": {
            "total": total,
            "overdue": total_overdue,
            "overdue_pct": overdue_pct,
        },
    }


def pipeline_report(db: Session, *, days: int | None = None) -> dict:
    """Aggregate requisition pipeline by stage group.

    Returns {stages: list[dict], win_rate: float, total_open_value: float,
             avg_deal_value: float, period_label: str}.
    stage dict: {name, statuses, count, value}
    Excluded statuses: archived, cancelled.
    """
    stage_defs = [
        ("Active", _ACTIVE_STATUSES),
        ("Sourcing", _SOURCING_STATUSES),
        ("Quoting", _QUOTING_STATUSES),
        ("Won", _WON_STATUSES),
        ("Lost", _LOST_STATUSES),
    ]

    all_included = _ACTIVE_STATUSES | _SOURCING_STATUSES | _QUOTING_STATUSES | _WON_STATUSES | _LOST_STATUSES

    stmt = select(
        Requisition.status, func.count().label("cnt"), func.sum(Requisition.opportunity_value).label("val")
    ).where(Requisition.status.in_(list(all_included)))
    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = stmt.where(Requisition.created_at >= cutoff)
    stmt = stmt.group_by(Requisition.status)

    status_rows = db.execute(stmt).all()
    by_status: dict[str, dict] = {r.status: {"count": r.cnt, "value": float(r.val or 0)} for r in status_rows}

    stages = []
    total_open_value = 0.0
    won_count = 0
    won_value = 0.0
    lost_count = 0

    for stage_name, status_set in stage_defs:
        count = sum(by_status.get(s, {}).get("count", 0) for s in status_set)
        value = sum(by_status.get(s, {}).get("value", 0.0) for s in status_set)
        stages.append({"name": stage_name, "count": count, "value": value})
        if stage_name == "Won":
            won_count = count
            won_value = value
        elif stage_name == "Lost":
            lost_count = count
        elif stage_name in ("Active", "Sourcing", "Quoting"):
            total_open_value += value

    decided = won_count + lost_count
    win_rate = round(won_count / decided * 100, 1) if decided else 0.0
    avg_deal_value = round(won_value / won_count, 2) if won_count else 0.0

    if days:
        period_label = f"Last {days} days"
    else:
        period_label = "All time"

    return {
        "stages": stages,
        "win_rate": win_rate,
        "total_open_value": total_open_value,
        "avg_deal_value": avg_deal_value,
        "period_label": period_label,
    }


def outcome_funnel(db: Session, *, days: int = 90) -> dict:
    """Compute the 4-step sales funnel for the last N days.

    Steps: Interactions → RFQs → Quotes Sent → Won.
    Returns {days, steps: list[dict], conv_rfq, conv_quote, conv_won}.
    step dict: {label, count, pct_of_first}
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    interactions = (
        db.execute(
            select(func.count()).where(
                ActivityLog.activity_type.in_(list(_INTERACTION_TYPES)),
                ActivityLog.company_id.isnot(None),
                ActivityLog.created_at >= cutoff,
            )
        ).scalar()
        or 0
    )

    rfqs = (
        db.execute(
            select(func.count()).where(
                ActivityLog.activity_type == "rfq_sent",
                ActivityLog.created_at >= cutoff,
            )
        ).scalar()
        or 0
    )

    quotes_sent = db.execute(select(func.count()).where(Quote.sent_at >= cutoff)).scalar() or 0

    won = (
        db.execute(
            select(func.count()).where(
                Quote.result == "won",
                Quote.result_at >= cutoff,
            )
        ).scalar()
        or 0
    )

    def _pct(value: int, base: int) -> float:
        return round(value / base * 100, 1) if base else 0.0

    steps = [
        {"label": "Interactions", "count": interactions, "pct_of_first": 100.0},
        {"label": "RFQs Sent", "count": rfqs, "pct_of_first": _pct(rfqs, interactions)},
        {"label": "Quotes Sent", "count": quotes_sent, "pct_of_first": _pct(quotes_sent, interactions)},
        {"label": "Won", "count": won, "pct_of_first": _pct(won, interactions)},
    ]

    return {
        "days": days,
        "steps": steps,
        "conv_rfq": _pct(rfqs, interactions),
        "conv_quote": _pct(quotes_sent, rfqs),
        "conv_won": _pct(won, quotes_sent),
    }
