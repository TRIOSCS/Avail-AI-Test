"""Rollback service — post-fix health monitoring (alert-only, no auto-revert).

After a fix is deployed, monitors for regressions via a pluggable health
check. In v1 this is a stub; future versions can query Sentry for new errors.
Notifies admin if issues are detected. Does NOT auto-revert — unsafe on
single-server Docker Compose deployment.

Called by: execution_service.py (after fix applied)
Depends on: services/notification_service.py, models/trouble_ticket.py
"""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from app.models.trouble_ticket import TroubleTicket
from app.services.notification_service import create_notification
from app.services.trouble_ticket_service import update_ticket


async def check_post_fix_health(ticket_id: int, db: Session) -> dict:
    """Check for regressions after a fix was applied.

    Returns: {healthy: bool, issues: list[str]}
    """
    ticket = db.get(TroubleTicket, ticket_id)
    if not ticket:
        return {"healthy": True, "issues": []}

    issues = await _check_health(ticket)

    if issues:
        logger.warning("Post-fix health issues for ticket {}: {}", ticket_id, issues)
        _alert_admin(db, ticket, issues)
        return {"healthy": False, "issues": issues}

    logger.info("Post-fix health check passed for ticket {}", ticket_id)
    return {"healthy": True, "issues": []}


async def _check_health(ticket: TroubleTicket) -> list[str]:
    """Pluggable health check — stub in v1.

    Future: query Sentry API for new errors matching ticket's file_mapping
    in the last N minutes after fix deployment.
    """
    # v1 stub — always healthy
    # Future implementation:
    # - Query Sentry for new issues since ticket.resolved_at
    # - Filter by affected files from ticket.file_mapping
    # - Return list of new error descriptions
    return []


def _alert_admin(db: Session, ticket: TroubleTicket, issues: list[str]) -> None:
    """Notify the ticket submitter about post-fix regressions."""
    body = "Issues detected after fix:\n" + "\n".join(f"- {i}" for i in issues)
    if ticket.submitted_by:
        create_notification(
            db, user_id=ticket.submitted_by,
            event_type="failed",
            title=f"Ticket #{ticket.id}: Post-fix regression detected",
            body=body,
            ticket_id=ticket.id,
        )
    # Mark ticket for re-investigation
    update_ticket(db, ticket.id, resolution_notes=f"Post-fix regression: {', '.join(issues)}")
