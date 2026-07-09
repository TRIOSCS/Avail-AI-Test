"""approval_outbox.py — Drains pending ApprovalOutbox rows.

Purpose: Polls approval_outbox for rows where sent_at IS NULL and fail_count is
         below MAX_OUTBOX_FAIL_COUNT, dispatching each one: email via Graph API
         and/or writes an in-app Notification, depending on the row's channel.  On
         success sets sent_at (idempotent — a row already sent is skipped).  On any
         failure (send error, deleted recipient, unknown channel) it increments
         fail_count and records last_error WITHOUT marking sent; once fail_count
         reaches the cap the row is dead-lettered (no longer fetched), so a
         permanently-broken row can't retry forever and mask real failures.

Called by: app/scheduler.py register_approval_outbox_job (periodic drain)
Depends on: app.models.approvals.ApprovalOutbox, app.models.auth.User,
            app.services.approvals.notifications
"""

from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.approvals import ApprovalOutbox
from app.services.approvals import notifications as _ns

# A row that has failed this many times is "dead-lettered": it stops being fetched
# by the drain so a permanently-broken row (deleted recipient, unknown channel,
# repeated transient errors) can no longer clog the outbox or flood the logs every
# 60s forever, masking real failures. The fail_count/last_error fields on the row
# preserve the diagnosis for an operator.
MAX_OUTBOX_FAIL_COUNT = 5


def _mark_failed(db: Session, row: ApprovalOutbox, reason: str) -> None:
    """Record a permanent/recoverable failure on a row WITHOUT marking it sent.

    Increments fail_count and stores last_error, then commits so the diagnosis is
    durable on its own (independent of any other row's outcome). Once fail_count hits
    MAX_OUTBOX_FAIL_COUNT the row is no longer selected by dispatch_pending (dead-
    letter). Never sets sent_at — the row genuinely did not send.
    """
    row.fail_count = (row.fail_count or 0) + 1
    row.last_error = reason
    db.commit()


async def dispatch_pending(db: Session) -> int:
    """Drain all unsent ApprovalOutbox rows.

    Processes rows where sent_at IS NULL and fail_count < MAX_OUTBOX_FAIL_COUNT in
    ascending id order. For each row:
      - channel == "email": sends via Graph API using the recipient's token.
      - channel == "in_app": writes a Notification row.
      - recipient deleted / unknown channel: treated as a failure — fail_count is
        incremented and last_error recorded (NOT marked sent), so the dead-letter cap
        retires the row instead of it retrying forever.

    Each row is committed in its OWN transaction: on success
    ``row.sent_at = now; db.commit()``; on any failure ``db.rollback()`` discards the
    row's partial state (e.g. a dirty Notification) and ``_mark_failed`` records
    fail_count/last_error in a fresh transaction that commits independently. This per-row
    isolation — not a SAVEPOINT — is what keeps a failing row from poisoning the batch.

    A SAVEPOINT (``db.begin_nested``) is deliberately NOT used: the email dispatch path
    (``send_email`` → ``token_manager.get_valid_token``) commits the session mid-row when
    it refreshes an expired Graph token, which ends any enclosing savepoint and made the
    old ``savepoint.commit()`` raise ``ResourceClosedError`` — aborting the whole batch and
    stranding the email row. A dispatch path that commits internally is fundamentally
    incompatible with an enclosing savepoint, so each row owns its own commit instead.

    Returns the count of rows successfully dispatched.
    """
    from app.models import User

    rows = (
        db.execute(
            select(ApprovalOutbox)
            .where(
                ApprovalOutbox.sent_at.is_(None),
                ApprovalOutbox.fail_count < MAX_OUTBOX_FAIL_COUNT,
            )
            .order_by(ApprovalOutbox.id)
        )
        .scalars()
        .all()
    )

    dispatched = 0
    for row in rows:
        recipient = db.get(User, row.recipient_user_id)
        if recipient is None:
            reason = f"recipient_user_id {row.recipient_user_id} not found — user deleted"
            _mark_failed(db, row, reason)
            logger.error(
                "approval_outbox row {} — {} (attempt {}); will retire after {} attempts",
                row.id,
                reason,
                row.fail_count,
                MAX_OUTBOX_FAIL_COUNT,
            )
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
                # Unknown channel is a permanent code/data mismatch — fail it (no sent_at)
                # so the dead-letter cap retires it, rather than silently marking it sent.
                raise ValueError(f"unknown channel '{row.channel}'")

            row.sent_at = datetime.now(UTC)
            db.commit()
            dispatched += 1

        except Exception as exc:
            # Discard this row's partial state (e.g. a dirty Notification, or a session
            # already committed/closed by the dispatch path) and record the failure in a
            # fresh transaction so it survives independently of the rest of the batch.
            db.rollback()
            _mark_failed(db, row, str(exc))
            logger.error(
                "approval_outbox row {} dispatch failed (attempt {}): {}",
                row.id,
                row.fail_count,
                exc,
            )

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
