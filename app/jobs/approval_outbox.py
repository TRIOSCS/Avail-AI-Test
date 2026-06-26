"""approval_outbox.py — Drains pending ApprovalOutbox rows.

Purpose: Polls approval_outbox for rows where sent_at IS NULL and dispatches
         each one: email via Graph API and/or writes an in-app Notification,
         depending on the row's channel.  On success sets sent_at (idempotent —
         a row already sent is skipped).  On failure increments fail_count and
         records last_error without marking sent.

Called by: app/scheduler.py register_approval_outbox_job (periodic drain)
Depends on: app.models.approvals.ApprovalOutbox, app.models.auth.User,
            app.services.approvals.notifications
"""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.approvals import ApprovalOutbox
from app.services.approvals import notifications as _ns


async def dispatch_pending(db: Session) -> int:
    """Drain all unsent ApprovalOutbox rows.

    Processes rows where sent_at IS NULL in ascending id order.  For each row:
      - channel == "email": sends via Graph API using the recipient's token.
      - channel == "in_app": writes a Notification row.
      - Any other channel: logs a warning and marks sent to avoid perpetual retry.

    Returns the count of rows successfully dispatched.
    """
    from app.models import User

    rows = (
        db.execute(select(ApprovalOutbox).where(ApprovalOutbox.sent_at.is_(None)).order_by(ApprovalOutbox.id))
        .scalars()
        .all()
    )

    dispatched = 0
    for row in rows:
        recipient = db.get(User, row.recipient_user_id)
        if recipient is None:
            logger.warning("approval_outbox row {} — recipient {} not found; skipping", row.id, row.recipient_user_id)
            continue

        payload: dict = row.payload or {}

        try:
            if row.channel == "email":
                subject, html = _ns._build_email_html(payload)
                await _ns.send_email(recipient, subject, html, db)

            elif row.channel == "in_app":
                event_type, title, body = _ns._build_in_app(payload)
                _ns.write_in_app(db, recipient.id, event_type, title, body)

            else:
                logger.warning("approval_outbox row {} — unknown channel '{}'; marking sent", row.id, row.channel)

            row.sent_at = datetime.now(timezone.utc)
            db.flush()
            dispatched += 1

        except Exception as exc:
            row.fail_count = (row.fail_count or 0) + 1
            row.last_error = str(exc)
            db.flush()
            logger.error(
                "approval_outbox row {} dispatch failed (attempt {}): {}",
                row.id,
                row.fail_count,
                exc,
            )

    if rows:
        db.commit()

    return dispatched


# ── Scheduled job wrapper ─────────────────────────────────────────────────────


async def _job_drain_approval_outbox() -> None:
    """Scheduled job: drain pending approval outbox rows."""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        count = await dispatch_pending(db)
        if count:
            logger.info("approval_outbox: dispatched {} row(s)", count)
    except Exception:
        logger.exception("approval_outbox drain failed")
        db.rollback()
    finally:
        db.close()


def register_approval_outbox_job(scheduler) -> None:
    """Register the outbox drain as a 60-second interval job."""
    from apscheduler.triggers.interval import IntervalTrigger

    from app.scheduler import _traced_job

    scheduler.add_job(
        _traced_job(_job_drain_approval_outbox),
        IntervalTrigger(seconds=60),
        id="approval_outbox_drain",
        name="Approval outbox drain",
    )
