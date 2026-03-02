"""Cost controller — per-ticket and weekly budget caps for the self-heal pipeline.

Prevents runaway AI spending by enforcing configurable limits.
Per-ticket cap (default $2) and weekly cap (default $50).

Called by: services/execution_service.py
Depends on: models/self_heal_log.py, config.py
"""

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.models.self_heal_log import SelfHealLog


def check_budget(db: Session, ticket_id: int) -> dict:
    """Check if a ticket is within budget for execution.

    Returns: {allowed: bool, reason: str, ticket_spend: float, weekly_spend: float}
    """
    ticket_spend = get_ticket_spend(db, ticket_id)
    weekly_spend = get_weekly_spend(db)

    if ticket_spend >= settings.self_heal_ticket_budget:
        reason = (
            f"Ticket #{ticket_id} budget exceeded: "
            f"${ticket_spend:.2f} >= ${settings.self_heal_ticket_budget:.2f}"
        )
        logger.warning(reason)
        return {
            "allowed": False,
            "reason": reason,
            "ticket_spend": ticket_spend,
            "weekly_spend": weekly_spend,
        }

    if weekly_spend >= settings.self_heal_weekly_budget:
        reason = (
            f"Weekly budget exceeded: "
            f"${weekly_spend:.2f} >= ${settings.self_heal_weekly_budget:.2f}"
        )
        logger.warning(reason)
        return {
            "allowed": False,
            "reason": reason,
            "ticket_spend": ticket_spend,
            "weekly_spend": weekly_spend,
        }

    return {
        "allowed": True,
        "reason": "Within budget",
        "ticket_spend": ticket_spend,
        "weekly_spend": weekly_spend,
    }


def record_cost(db: Session, ticket_id: int, cost_usd: float) -> None:
    """Record cost on the most recent SelfHealLog entry for a ticket."""
    log = (
        db.query(SelfHealLog)
        .filter(SelfHealLog.ticket_id == ticket_id)
        .order_by(SelfHealLog.id.desc())
        .first()
    )
    if log:
        log.cost_usd = (log.cost_usd or 0.0) + cost_usd
        db.commit()
        logger.info("Recorded ${:.4f} for ticket {}", cost_usd, ticket_id)


def get_ticket_spend(db: Session, ticket_id: int) -> float:
    """Total spend across all SelfHealLog entries for a ticket."""
    result = (
        db.query(func.coalesce(func.sum(SelfHealLog.cost_usd), 0.0))
        .filter(SelfHealLog.ticket_id == ticket_id)
        .scalar()
    )
    return float(result)


def get_weekly_spend(db: Session) -> float:
    """Total spend across all tickets in the current week (Mon-Sun UTC)."""
    now = datetime.now(timezone.utc)
    week_start = now - timedelta(days=now.weekday())
    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)

    result = (
        db.query(func.coalesce(func.sum(SelfHealLog.cost_usd), 0.0))
        .filter(SelfHealLog.created_at >= week_start)
        .scalar()
    )
    return float(result)
