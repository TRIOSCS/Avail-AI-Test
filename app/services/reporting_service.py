"""services/reporting_service.py — CRM cadence-coverage aggregation.

Public function: coverage_report — cadence coverage across active companies, by
tier and by rep. Pipeline/forecast and the conversion funnel live in
app/services/forecast_service.py (the Requisition is the opportunity).

Called by: app/routers/crm/views.py (reporting_dashboard route)
Depends on: app/models (Company, User), app/services/crm_service (cadence_state)
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.auth import User
from ..models.crm import Company
from .crm_service import cadence_state

TIER_ORDER = ["key", "core", "standard", "prospect"]


def coverage_report(db: Session) -> dict:
    """Compute cadence coverage across all active companies.

    Returns {by_tier: list[dict], by_rep: list[dict], summary: dict}.
    by_tier rows: {tier, total, on_target, due, overdue, new, coverage_pct}
    by_rep rows:  {rep, total, on_target, due, overdue, new, coverage_pct}
    summary:      {total, overdue, overdue_pct, coverage_pct}
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

    overall = {
        "total": sum(b["total"] for b in tier_buckets.values()),
        "on_target": sum(b["on_target"] for b in tier_buckets.values()),
        "due": sum(b["due"] for b in tier_buckets.values()),
        "overdue": sum(b["overdue"] for b in tier_buckets.values()),
    }
    total = overall["total"]
    overdue_pct = round(overall["overdue"] / total * 100, 1) if total else 0.0

    return {
        "by_tier": by_tier,
        "by_rep": by_rep,
        "summary": {
            "total": total,
            "overdue": overall["overdue"],
            "overdue_pct": overdue_pct,
            "coverage_pct": _coverage_pct(overall),
        },
    }
