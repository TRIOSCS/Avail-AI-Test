#!/usr/bin/env python3
"""Continuous self-heal sweep — runs SiteTester in a loop until no new issues found.

Connects directly to the DB and app services inside Docker.
Creates a signed session cookie for Playwright browser auth.

Called by: screen session on host (via docker compose exec)
Depends on: app.services.site_tester, app.services.trouble_ticket_service
"""

import asyncio
import os
import sys
import time

# Ensure app is importable
sys.path.insert(0, "/app")
os.environ.setdefault("TESTING", "")

from loguru import logger

from app.database import SessionLocal
from app.services.site_tester import SiteTester, create_tickets_from_issues
from app.services.trouble_ticket_service import auto_process_ticket


BASE_URL = "http://localhost:8000"
ADMIN_USER_ID = 1  # mkhoury — used for ticket creation
MAX_ROUNDS = 20
WAIT_BETWEEN_ROUNDS = 120  # seconds — wait for watcher to apply fixes


def get_session_cookie() -> str:
    """Create a signed session cookie for admin user.

    Uses the same signing mechanism as Starlette SessionMiddleware.
    """
    from itsdangerous import URLSafeTimedSerializer
    from app.config import settings

    signer = URLSafeTimedSerializer(settings.secret_key)
    session_data = {"user_id": ADMIN_USER_ID}
    return signer.dumps(session_data)


async def run_sweep(session_cookie: str) -> list[dict]:
    """Run a full SiteTester sweep and return issues found."""
    tester = SiteTester(base_url=BASE_URL, session_cookie=session_cookie)

    logger.info("Starting full sweep...")
    try:
        issues = await tester.run_full_sweep()
    except Exception as e:
        logger.error("Sweep failed: {}", e)
        return []

    logger.info("Sweep complete: {} issues found, {} areas tested",
                len(issues), len(tester.progress))
    return issues


async def create_and_process_tickets(issues: list[dict]) -> int:
    """Create tickets from issues and trigger auto-processing."""
    if not issues:
        return 0

    db = SessionLocal()
    try:
        count = await create_tickets_from_issues(issues, db)
        logger.info("Created {} tickets from sweep issues", count)

        # Auto-process each new ticket (diagnose + queue fix)
        # The tickets were just created, get the latest ones
        from app.models.trouble_ticket import TroubleTicket
        recent = (
            db.query(TroubleTicket)
            .filter(TroubleTicket.source == "playwright")
            .filter(TroubleTicket.status == "submitted")
            .order_by(TroubleTicket.id.desc())
            .limit(count)
            .all()
        )

        for ticket in recent:
            logger.info("Auto-processing ticket #{}: {}", ticket.id, ticket.title)
            try:
                await auto_process_ticket(ticket.id)
            except Exception as e:
                logger.warning("Auto-process failed for ticket #{}: {}", ticket.id, e)

        return count
    finally:
        db.close()


async def main():
    logger.info("=== Continuous Self-Heal Sweep Starting ===")
    logger.info("Max rounds: {}, Wait between: {}s", MAX_ROUNDS, WAIT_BETWEEN_ROUNDS)

    session_cookie = get_session_cookie()
    logger.info("Session cookie generated for admin user {}", ADMIN_USER_ID)

    consecutive_clean = 0

    for round_num in range(1, MAX_ROUNDS + 1):
        logger.info("\n{'='*60}")
        logger.info("ROUND {}/{}", round_num, MAX_ROUNDS)
        logger.info("{'='*60}")

        # Run sweep
        issues = await run_sweep(session_cookie)

        if not issues:
            consecutive_clean += 1
            logger.info("Clean sweep! ({} consecutive)", consecutive_clean)

            if consecutive_clean >= 2:
                logger.info("Two consecutive clean sweeps — stopping.")
                break

            logger.info("Waiting {}s before confirmation sweep...", 30)
            time.sleep(30)
            continue

        consecutive_clean = 0

        # Create tickets and auto-process
        new_count = await create_and_process_tickets(issues)

        if new_count == 0:
            logger.info("No new tickets created (all duplicates?) — stopping.")
            break

        # Wait for watcher to pick up and apply fixes
        logger.info("Waiting {}s for watcher to apply fixes...", WAIT_BETWEEN_ROUNDS)
        time.sleep(WAIT_BETWEEN_ROUNDS)

    # Final summary
    db = SessionLocal()
    try:
        from app.models.trouble_ticket import TroubleTicket
        from sqlalchemy import func

        stats = dict(
            db.query(TroubleTicket.status, func.count())
            .group_by(TroubleTicket.status)
            .all()
        )
        logger.info("\n=== Final Ticket Status ===")
        for status, count in sorted(stats.items()):
            logger.info("  {}: {}", status, count)
    finally:
        db.close()

    logger.info("=== Continuous sweep complete ===")


if __name__ == "__main__":
    asyncio.run(main())
