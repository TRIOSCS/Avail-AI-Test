"""Verify-retest service -- post-fix Playwright verification for the self-heal pipeline.

After a fix is applied and the container rebuilt, this service runs SiteTester
on just the affected area. If issues persist, it creates a regression ticket
and escalates.

Called by: routers/trouble_tickets.py (verify-retest endpoint), host watcher script
Depends on: services/site_tester.py, services/trouble_ticket_service.py,
            services/notification_service.py
"""

from loguru import logger
from sqlalchemy.orm import Session

from app.models.trouble_ticket import TroubleTicket
from app.services.notification_service import create_notification
from app.services.site_tester import TEST_AREAS, SiteTester
from app.services.trouble_ticket_service import create_ticket, update_ticket


async def verify_and_retest(
    ticket_id: int,
    db: Session,
    *,
    base_url: str,
    session_cookie: str,
) -> dict:
    """Run SiteTester on the ticket's affected area and resolve or escalate.

    Returns:
        {passed: True, issues: []} on success
        {passed: False, issues: [...], regression_ticket_id: int} on failure
        {passed: False, error: str} if ticket not found
    """
    ticket = db.get(TroubleTicket, ticket_id)
    if not ticket:
        return {"passed": False, "error": "Ticket not found"}

    # Determine which test area to check
    test_area = ticket.tested_area or ticket.category or "search"

    # Find matching area config from TEST_AREAS
    area_config = None
    for area in TEST_AREAS:
        if area["name"] == test_area:
            area_config = area
            break

    if not area_config:
        logger.warning("No matching test area '{}' found, falling back to 'search'", test_area)
        test_area = "search"
        area_config = TEST_AREAS[0]  # search is first

    # Run the site tester
    tester = SiteTester(base_url, session_cookie)
    await tester.run_full_sweep()

    # Filter issues to just the matching test area
    area_issues = [i for i in tester.issues if i.get("area") == test_area]

    if not area_issues:
        # Pass -- resolve the ticket
        update_ticket(db, ticket_id, status="resolved", resolution_notes="Verified by automated retest")
        if ticket.submitted_by:
            create_notification(
                db,
                user_id=ticket.submitted_by,
                event_type="resolved",
                title=f"Ticket #{ticket_id}: Retest passed",
                body="Automated retest verified the fix is working.",
                ticket_id=ticket_id,
            )
        logger.info("Verify-retest passed for ticket {}", ticket_id)
        return {"passed": True, "issues": []}

    # Fail -- create regression child ticket and escalate
    issue_desc = "\n".join(
        f"- {i.get('title', 'Unknown')}: {i.get('description', '')}"
        for i in area_issues
    )

    regression = create_ticket(
        db,
        user_id=ticket.submitted_by or 0,
        title=f"Retest failed: {ticket.title[:150]}",
        description=f"Automated retest found issues in area '{test_area}':\n{issue_desc}",
        source="retest",
        current_view=test_area,
    )
    regression.parent_ticket_id = ticket_id
    regression.tested_area = test_area
    db.commit()

    update_ticket(
        db,
        ticket_id,
        status="escalated",
        resolution_notes=f"Retest failed with {len(area_issues)} issue(s) in '{test_area}'",
    )

    if ticket.submitted_by:
        create_notification(
            db,
            user_id=ticket.submitted_by,
            event_type="failed",
            title=f"Ticket #{ticket_id}: Retest failed",
            body=f"Retest found {len(area_issues)} issue(s). Regression ticket #{regression.id} created.",
            ticket_id=ticket_id,
        )

    logger.warning("Verify-retest failed for ticket {} — {} issues, regression #{}", ticket_id, len(area_issues), regression.id)
    return {"passed": False, "issues": area_issues, "regression_ticket_id": regression.id}
