"""Rollback service — post-fix verification using SiteTester.

After a fix is deployed, retests the affected area using Playwright.
On pass: resolves the ticket. On fail: creates a regression child ticket
and escalates the original.

Called by: routers/trouble_tickets.py (verify-retest endpoint)
Depends on: services/site_tester.py, services/trouble_ticket_service.py,
            services/notification_service.py
"""

from loguru import logger
from sqlalchemy.orm import Session

from app.models.trouble_ticket import TroubleTicket
from app.services.notification_service import create_notification
from app.services.trouble_ticket_service import create_ticket, update_ticket


async def verify_and_retest(
    ticket_id: int,
    db: Session,
    *,
    base_url: str = "http://localhost:8000",
    session_cookie: str | None = None,
) -> dict:
    """Re-run SiteTester on the affected area and update ticket accordingly.

    Returns: {ok: bool, status: str, issues?: list}
    """
    ticket = db.get(TroubleTicket, ticket_id)
    if not ticket:
        return {"error": "Ticket not found"}

    test_area = ticket.tested_area or ticket.category or "general"
    logger.info("verify_and_retest: ticket #{} area={}", ticket_id, test_area)

    # Import here to avoid circular imports
    from app.services.site_tester import SiteTester

    tester = SiteTester(base_url=base_url, session_cookie=session_cookie)

    try:
        issues = await tester.run_full_sweep()
    except Exception as e:
        logger.error("verify_and_retest: sweep failed for ticket #{}: {}", ticket_id, e)
        return {"error": f"Retest sweep failed: {e}"}

    # Filter issues related to this ticket's area
    related = [i for i in issues if i.get("area") == test_area] if issues else []

    if not related:
        # Pass — resolve the ticket
        update_ticket(
            db, ticket_id,
            status="resolved",
            resolution_notes=f"Verified by automated retest ({len(issues or [])} total issues, 0 in {test_area})",
        )
        if ticket.submitted_by:
            create_notification(
                db, user_id=ticket.submitted_by, event_type="resolved",
                title=f"Ticket #{ticket_id}: Verified & resolved",
                body=f"Automated retest passed for area '{test_area}'",
                ticket_id=ticket_id,
            )
        logger.info("verify_and_retest: ticket #{} PASSED — resolved", ticket_id)
        return {"ok": True, "status": "resolved"}
    else:
        # Fail — create regression child ticket, escalate original
        for issue in related:
            child = create_ticket(
                db=db,
                user_id=ticket.submitted_by or 1,
                title=f"Regression: {issue.get('title', test_area)}",
                description=f"Post-fix retest found issue after resolving ticket #{ticket_id}.\n\n{issue.get('description', '')}",
                source="playwright",
                tested_area=test_area,
            )
            child.parent_ticket_id = ticket_id
            db.commit()
            logger.info("verify_and_retest: created regression ticket #{} for parent #{}", child.id, ticket_id)

        update_ticket(
            db, ticket_id,
            status="escalated",
            resolution_notes=f"Retest failed: {len(related)} issue(s) found in {test_area}",
        )
        if ticket.submitted_by:
            create_notification(
                db, user_id=ticket.submitted_by, event_type="escalated",
                title=f"Ticket #{ticket_id}: Retest failed",
                body=f"Found {len(related)} regression issue(s) in '{test_area}'",
                ticket_id=ticket_id,
            )
        logger.warning("verify_and_retest: ticket #{} FAILED — {} issues", ticket_id, len(related))
        return {"ok": False, "status": "escalated", "issues": related}
