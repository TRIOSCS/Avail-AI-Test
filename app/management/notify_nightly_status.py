"""notify_nightly_status.py — alert admins when the nightly test cron fails.

Purpose: Turns the nightly test runner's STATUS line into real alerts instead of
         an unwatched log echo: one Teams Adaptive Card on the shared webhook
         channel (config single-sourced via credential_service — no URL in the
         cron script) plus one in-app Notification row per ACTIVE admin. A PASS
         status is a no-op (the cron only invokes this on non-PASS, but the guard
         keeps a stray invocation harmless). A missing webhook logs-and-skips
         (post_teams_channel's own behavior); an in-app write failure is logged
         and swallowed — the alert path must never crash the cron.

Usage: python -m app.management.notify_nightly_status "<status line>"
       e.g. python -m app.management.notify_nightly_status \
            "2026-07-24: FAIL (exit=1, coverage=93%)"

Called by: scripts/nightly_tests.sh (root crontab 02:30 UTC) via
           `docker compose exec -T app python -m ...` on any non-PASS STATUS.
Depends on: app.services.teams_notifications.post_teams_channel,
            app.services.in_app_notifications.write_in_app,
            app.models.auth.User, app.constants.UserRole
"""

import argparse
import asyncio

from loguru import logger
from sqlalchemy.orm import Session

from app.constants import UserRole
from app.models.auth import User
from app.services.in_app_notifications import write_in_app
from app.services.teams_notifications import post_teams_channel

NIGHTLY_LOG_DIR = "/var/log/avail/nightly_tests"

EVENT_TYPE = "nightly_tests_failed"


def _status_part(status_line: str) -> str:
    """Extract the STATUS portion of a ``YYYY-MM-DD: STATUS`` line.

    Returns the whole line when there is no ``": "`` date prefix.
    """
    _, sep, rest = status_line.partition(": ")
    return (rest if sep else status_line).strip()


def notify_nightly_status(status_line: str, db: Session | None = None) -> bool:
    """Alert admins about a non-PASS nightly STATUS line.

    Returns True when alerts were dispatched, False for the PASS no-op. ``db`` is
    injectable for tests; when None a fresh SessionLocal is opened and closed.
    """
    status = _status_part(status_line)
    if status.upper().startswith("PASS"):
        logger.info("notify_nightly_status: PASS status — nothing to alert ({})", status_line)
        return False

    message = f"**Nightly test run failed**\n\n`{status_line}`\n\nFull log: `{NIGHTLY_LOG_DIR}/` on the app host."
    # Teams first (primary channel — no DB dependency). post_teams_channel
    # logs-and-skips when no webhook is configured and swallows HTTP errors.
    asyncio.run(post_teams_channel(message))

    owns_session = db is None
    if db is None:
        from app.database import SessionLocal

        db = SessionLocal()
    try:
        admin_ids = [
            row[0] for row in db.query(User.id).filter(User.role == UserRole.ADMIN, User.is_active.is_(True)).all()
        ]
        for admin_id in admin_ids:
            write_in_app(db, admin_id, EVENT_TYPE, "Nightly test run failed", status_line)
        db.commit()
        logger.info(
            "notify_nightly_status: alerted {} active admin(s) in-app (+ Teams) for '{}'",
            len(admin_ids),
            status_line,
        )
    except Exception:  # noqa: BLE001 — best-effort: the alert path must never crash the cron
        db.rollback()
        # In-app rows are best-effort — the Teams post already went out and the
        # cron log carries the ALERT line; never crash the alert path.
        logger.exception("notify_nightly_status: in-app admin notification write failed")
    finally:
        if owns_session:
            db.close()
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Alert admins about a non-PASS nightly test STATUS line.")
    parser.add_argument(
        "status_line",
        help='Nightly STATUS line, e.g. "2026-07-24: FAIL (exit=1, coverage=93%%)"',
    )
    args = parser.parse_args()
    notify_nightly_status(args.status_line)


if __name__ == "__main__":
    main()
