"""Pattern tracker — weekly stats and recurring pattern detection.

Analyzes self-heal logs and tickets to identify trends: which categories
recur, success rates, average resolution times, and risk recommendations.

Called by: scheduler.py (weekly report job)
Depends on: models/self_heal_log.py, models/trouble_ticket.py
"""

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.self_heal_log import SelfHealLog
from app.models.trouble_ticket import TroubleTicket


def get_weekly_stats(db: Session, weeks_back: int = 1) -> dict:
    """Generate stats for the last N weeks.

    Returns: {period, tickets_created, tickets_resolved, by_category,
              by_risk, success_rate, avg_resolution_hours, total_cost}
    """
    now = datetime.now(timezone.utc)
    start = now - timedelta(weeks=weeks_back)

    # Tickets created in period
    created = (
        db.query(func.count(TroubleTicket.id))
        .filter(TroubleTicket.created_at >= start)
        .scalar()
    ) or 0

    # Tickets resolved in period
    resolved = (
        db.query(func.count(TroubleTicket.id))
        .filter(
            TroubleTicket.resolved_at >= start,
            TroubleTicket.status == "resolved",
        )
        .scalar()
    ) or 0

    # By category
    cat_rows = (
        db.query(TroubleTicket.category, func.count(TroubleTicket.id))
        .filter(TroubleTicket.created_at >= start, TroubleTicket.category.isnot(None))
        .group_by(TroubleTicket.category)
        .all()
    )
    by_category = {row[0]: row[1] for row in cat_rows}

    # By risk tier
    risk_rows = (
        db.query(TroubleTicket.risk_tier, func.count(TroubleTicket.id))
        .filter(TroubleTicket.created_at >= start, TroubleTicket.risk_tier.isnot(None))
        .group_by(TroubleTicket.risk_tier)
        .all()
    )
    by_risk = {row[0]: row[1] for row in risk_rows}

    # Success rate from SelfHealLog
    total_fixes = (
        db.query(func.count(SelfHealLog.id))
        .filter(SelfHealLog.created_at >= start, SelfHealLog.fix_succeeded.isnot(None))
        .scalar()
    ) or 0
    successful = (
        db.query(func.count(SelfHealLog.id))
        .filter(SelfHealLog.created_at >= start, SelfHealLog.fix_succeeded.is_(True))
        .scalar()
    ) or 0
    success_rate = (successful / total_fixes * 100) if total_fixes > 0 else 0.0

    # Average resolution time (hours)
    resolved_tickets = (
        db.query(TroubleTicket)
        .filter(
            TroubleTicket.resolved_at >= start,
            TroubleTicket.status == "resolved",
            TroubleTicket.created_at.isnot(None),
            TroubleTicket.resolved_at.isnot(None),
        )
        .all()
    )
    if resolved_tickets:
        total_hours = sum(
            (t.resolved_at - t.created_at).total_seconds() / 3600
            for t in resolved_tickets
        )
        avg_resolution_hours = round(total_hours / len(resolved_tickets), 1)
    else:
        avg_resolution_hours = 0.0

    # Total cost
    total_cost = (
        db.query(func.coalesce(func.sum(SelfHealLog.cost_usd), 0.0))
        .filter(SelfHealLog.created_at >= start)
        .scalar()
    )

    return {
        "period_start": start.isoformat(),
        "period_end": now.isoformat(),
        "tickets_created": created,
        "tickets_resolved": resolved,
        "by_category": by_category,
        "by_risk": by_risk,
        "success_rate": round(success_rate, 1),
        "avg_resolution_hours": avg_resolution_hours,
        "total_cost": round(float(total_cost), 2),
    }


def detect_recurring_patterns(db: Session, min_occurrences: int = 3) -> list[dict]:
    """Find recurring issue patterns (same category + page appearing multiple times).

    Returns: [{category, page, count, latest_ticket_id}]
    """
    now = datetime.now(timezone.utc)
    lookback = now - timedelta(days=30)

    rows = (
        db.query(
            TroubleTicket.category,
            TroubleTicket.current_page,
            func.count(TroubleTicket.id).label("cnt"),
            func.max(TroubleTicket.id).label("latest_id"),
        )
        .filter(
            TroubleTicket.created_at >= lookback,
            TroubleTicket.category.isnot(None),
        )
        .group_by(TroubleTicket.category, TroubleTicket.current_page)
        .having(func.count(TroubleTicket.id) >= min_occurrences)
        .all()
    )

    patterns = []
    for row in rows:
        patterns.append({
            "category": row[0],
            "page": row[1],
            "count": row[2],
            "latest_ticket_id": row[3],
        })

    if patterns:
        logger.info("Detected {} recurring patterns", len(patterns))

    return patterns


def get_health_status(db: Session) -> dict:
    """System health indicator based on recent ticket activity.

    Green: < 3 open tickets, no high-risk
    Yellow: 3-10 open tickets or any high-risk
    Red: > 10 open tickets or > 3 high-risk

    Returns: {status: green|yellow|red, open_count, high_risk_count, message}
    """
    open_statuses = ("submitted", "triaging", "diagnosed", "prompt_ready", "fix_in_progress")
    open_count = (
        db.query(func.count(TroubleTicket.id))
        .filter(TroubleTicket.status.in_(open_statuses))
        .scalar()
    ) or 0

    high_risk_count = (
        db.query(func.count(TroubleTicket.id))
        .filter(
            TroubleTicket.status.in_(open_statuses),
            TroubleTicket.risk_tier == "high",
        )
        .scalar()
    ) or 0

    if open_count > 10 or high_risk_count > 3:
        status = "red"
        message = f"{open_count} open tickets ({high_risk_count} high-risk)"
    elif open_count >= 3 or high_risk_count > 0:
        status = "yellow"
        message = f"{open_count} open tickets ({high_risk_count} high-risk)"
    else:
        status = "green"
        message = f"System healthy ({open_count} open tickets)"

    return {
        "status": status,
        "open_count": open_count,
        "high_risk_count": high_risk_count,
        "message": message,
    }
