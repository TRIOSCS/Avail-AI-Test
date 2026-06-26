"""test_approval_outbox.py — TDD tests for outbox dispatcher (Task 6).

Tests:
  - After dispatch_pending, a pending outbox row has sent_at set and one Notification
    written (in_app channel).
  - Running dispatch_pending twice does NOT double-send (idempotency: row already
    sent_at is skipped).
  - Email channel: Graph send is attempted; failure increments fail_count + sets
    last_error; sent_at remains NULL.
  - Email channel: Graph send success sets sent_at.

Called by: pytest
Depends on: conftest (db_session), app.jobs.approval_outbox,
            app.models.approvals, app.models.notification, app.models.auth
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.models.approvals import ApprovalOutbox
from app.models.notification import Notification

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_user(db, email: str = "user@example.com"):
    from app.models import User

    u = User(email=email, name=email.split("@")[0])
    db.add(u)
    db.flush()
    return u


def _make_request(db, owner):
    """Create a bare ApprovalRequest without full routing (Task 6 doesn't need it)."""
    from app.constants import ApprovalGateType, ApprovalRequestStatus
    from app.models.approvals import ApprovalRequest

    req = ApprovalRequest(
        gate_type=ApprovalGateType.PREPAYMENT,
        requested_by_id=owner.id,
        owner_id=owner.id,
        status=ApprovalRequestStatus.REQUESTED,
        amount=100.00,
    )
    db.add(req)
    db.flush()
    return req


def _pending_outbox(db, request, recipient, channel="in_app"):
    row = ApprovalOutbox(
        request_id=request.id,
        recipient_user_id=recipient.id,
        channel=channel,
        payload={"event_type": "decided", "decision": "approved"},
    )
    db.add(row)
    db.flush()
    return row


# ── Tests ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_sets_sent_at_and_writes_notification(db_session):
    """After dispatch_pending an in_app row has sent_at set and one Notification
    exists."""
    from app.jobs.approval_outbox import dispatch_pending

    user = _make_user(db_session)
    req = _make_request(db_session, user)
    row = _pending_outbox(db_session, req, user, channel="in_app")
    db_session.commit()

    await dispatch_pending(db_session)

    db_session.refresh(row)
    assert row.sent_at is not None, "sent_at should be set after dispatch"

    notifs = db_session.execute(select(Notification).where(Notification.user_id == user.id)).scalars().all()
    assert len(notifs) == 1, "Exactly one Notification should be written"
    assert "approval" in notifs[0].event_type.lower() or notifs[0].event_type


@pytest.mark.asyncio
async def test_dispatch_idempotent_no_double_send(db_session):
    """Running dispatch_pending twice does NOT double-send (sent_at row is skipped)."""
    from app.jobs.approval_outbox import dispatch_pending

    user = _make_user(db_session)
    req = _make_request(db_session, user)
    _pending_outbox(db_session, req, user, channel="in_app")
    db_session.commit()

    await dispatch_pending(db_session)
    await dispatch_pending(db_session)

    notifs = db_session.execute(select(Notification).where(Notification.user_id == user.id)).scalars().all()
    assert len(notifs) == 1, "Second dispatch must NOT write a second Notification"


@pytest.mark.asyncio
async def test_email_channel_graph_failure_increments_fail_count(db_session):
    """On Graph send failure: fail_count increments, last_error set, sent_at NULL."""
    from app.jobs.approval_outbox import dispatch_pending

    user = _make_user(db_session, "sender@example.com")
    req = _make_request(db_session, user)
    row = _pending_outbox(db_session, req, user, channel="email")
    db_session.commit()

    with (
        patch(
            "app.services.approvals.notifications.get_valid_token",
            new_callable=AsyncMock,
            return_value="tok",
        ),
        patch("app.services.approvals.notifications.GraphClient") as MockGC,
    ):
        gc_instance = MagicMock()
        gc_instance.post_json = AsyncMock(side_effect=RuntimeError("network error"))
        MockGC.return_value = gc_instance

        await dispatch_pending(db_session)

    db_session.refresh(row)
    assert row.sent_at is None, "sent_at must remain NULL on failure"
    assert row.fail_count == 1, "fail_count must increment"
    assert "network error" in (row.last_error or ""), "last_error should record the exception"


@pytest.mark.asyncio
async def test_email_channel_graph_success_sets_sent_at(db_session):
    """On Graph send success: sent_at set, fail_count stays 0."""
    from app.jobs.approval_outbox import dispatch_pending

    user = _make_user(db_session, "ok@example.com")
    req = _make_request(db_session, user)
    row = _pending_outbox(db_session, req, user, channel="email")
    db_session.commit()

    with (
        patch(
            "app.services.approvals.notifications.get_valid_token",
            new_callable=AsyncMock,
            return_value="tok",
        ),
        patch("app.services.approvals.notifications.GraphClient") as MockGC,
    ):
        gc_instance = MagicMock()
        gc_instance.post_json = AsyncMock(return_value=None)
        MockGC.return_value = gc_instance

        await dispatch_pending(db_session)

    db_session.refresh(row)
    assert row.sent_at is not None, "sent_at should be set after successful email send"
    assert row.fail_count == 0
