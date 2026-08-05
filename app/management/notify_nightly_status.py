"""notify_nightly_status.py — alert admins when the nightly test cron fails.

Purpose: Turns the nightly test runner's STATUS line into a real alert instead of
         an unwatched log echo: one Teams Adaptive Card on the shared webhook
         channel (config single-sourced via credential_service — no URL in the
         cron script). A PASS status is a no-op (the cron only invokes this on
         non-PASS, but the guard keeps a stray invocation harmless). A missing
         webhook logs-and-skips (post_teams_channel's own behavior) — the alert
         path must never crash the cron. The former per-admin in-app Notification
         rows were deleted with the write-only notifications channel (W2.9/§5.5).

Usage: python -m app.management.notify_nightly_status "<status line>"
       e.g. python -m app.management.notify_nightly_status \
            "2026-07-24: FAIL (exit=1, coverage=93%)"

Called by: scripts/nightly_tests.sh (root crontab 02:30 UTC) via
           `docker compose exec -T app python -m ...` on any non-PASS STATUS.
Depends on: app.services.teams_notifications.post_teams_channel
"""

import argparse
import asyncio

from loguru import logger

from app.services.teams_notifications import post_teams_channel

NIGHTLY_LOG_DIR = "/var/log/avail/nightly_tests"


def _status_part(status_line: str) -> str:
    """Extract the STATUS portion of a ``YYYY-MM-DD: STATUS`` line.

    Returns the whole line when there is no ``": "`` date prefix.
    """
    _, sep, rest = status_line.partition(": ")
    return (rest if sep else status_line).strip()


def notify_nightly_status(status_line: str) -> bool:
    """Alert admins about a non-PASS nightly STATUS line.

    Returns True when the alert was dispatched, False for the PASS no-op.
    """
    status = _status_part(status_line)
    if status.upper().startswith("PASS"):
        logger.info("notify_nightly_status: PASS status — nothing to alert ({})", status_line)
        return False

    message = f"**Nightly test run failed**\n\n`{status_line}`\n\nFull log: `{NIGHTLY_LOG_DIR}/` on the app host."
    # post_teams_channel logs-and-skips when no webhook is configured and
    # swallows HTTP errors — the alert path never crashes the cron.
    asyncio.run(post_teams_channel(message))
    logger.info("notify_nightly_status: Teams alert dispatched for '{}'", status_line)
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
