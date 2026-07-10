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

from datetime import UTC, datetime
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
async def test_in_app_notification_body_carries_comment(db_session):
    """The in-app Notification.body == the decision comment from the outbox payload."""
    from app.jobs.approval_outbox import dispatch_pending

    user = _make_user(db_session, "commenter@example.com")
    req = _make_request(db_session, user)
    row = ApprovalOutbox(
        request_id=req.id,
        recipient_user_id=user.id,
        channel="in_app",
        payload={"event_type": "decided", "decision": "rejected", "comment": "Vendor unverified"},
    )
    db_session.add(row)
    db_session.commit()

    await dispatch_pending(db_session)

    notif = db_session.execute(select(Notification).where(Notification.user_id == user.id)).scalar_one()
    assert notif.body == "Vendor unverified"


@pytest.mark.asyncio
async def test_in_app_notification_body_none_when_no_comment(db_session):
    """No comment in the payload → the in-app Notification.body is None."""
    from app.jobs.approval_outbox import dispatch_pending

    user = _make_user(db_session, "nocomment@example.com")
    req = _make_request(db_session, user)
    row = ApprovalOutbox(
        request_id=req.id,
        recipient_user_id=user.id,
        channel="in_app",
        payload={"event_type": "decided", "decision": "approved", "comment": None},
    )
    db_session.add(row)
    db_session.commit()

    await dispatch_pending(db_session)

    notif = db_session.execute(select(Notification).where(Notification.user_id == user.id)).scalar_one()
    assert notif.body is None


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


@pytest.mark.asyncio
async def test_deleted_recipient_increments_fail_count_not_sent(db_session):
    """A row whose recipient no longer exists is failed (not silently skipped):

    fail_count++, last_error set, sent_at stays NULL — so the dead-letter cap can retire
    it. The deleted recipient is simulated by the User lookup returning None (in prod
    the FK is ON DELETE SET NULL / the row outlives the user); patching the lookup is
    the deterministic way to exercise the branch without fighting the test DB's cascade.
    """
    from unittest.mock import patch

    from app.jobs.approval_outbox import dispatch_pending

    user = _make_user(db_session, "ghost@example.com")
    req = _make_request(db_session, user)
    row = _pending_outbox(db_session, req, user, channel="in_app")
    db_session.commit()

    # Simulate the recipient being gone: db.get(User, ...) → None for this drain.
    real_get = db_session.get

    def _get(model, ident, **kw):
        if getattr(model, "__name__", "") == "User":
            return None
        return real_get(model, ident, **kw)

    with patch.object(db_session, "get", side_effect=_get):
        await dispatch_pending(db_session)

    db_session.refresh(row)
    assert row.sent_at is None, "deleted-recipient row must NOT be marked sent"
    assert row.fail_count == 1, "fail_count must increment for a deleted recipient"
    assert "not found" in (row.last_error or "").lower()


@pytest.mark.asyncio
async def test_dead_letter_cap_stops_fetching_row(db_session):
    """A row already at MAX_OUTBOX_FAIL_COUNT is not fetched (dead-lettered)."""
    from app.jobs.approval_outbox import MAX_OUTBOX_FAIL_COUNT, dispatch_pending

    user = _make_user(db_session, "capped@example.com")
    req = _make_request(db_session, user)
    row = _pending_outbox(db_session, req, user, channel="in_app")
    row.fail_count = MAX_OUTBOX_FAIL_COUNT  # already at the cap
    db_session.commit()

    dispatched = await dispatch_pending(db_session)

    assert dispatched == 0, "a capped row must not be dispatched"
    db_session.refresh(row)
    assert row.sent_at is None
    # Untouched — the SELECT filtered it out, so fail_count did not change.
    assert row.fail_count == MAX_OUTBOX_FAIL_COUNT


@pytest.mark.asyncio
async def test_unknown_channel_is_failed_not_marked_sent(db_session):
    """An unknown channel is a permanent failure: fail_count++, last_error set,
    sent_at stays NULL (FIX 5 — no longer silently marked sent)."""
    from app.jobs.approval_outbox import dispatch_pending

    user = _make_user(db_session, "weird@example.com")
    req = _make_request(db_session, user)
    row = _pending_outbox(db_session, req, user, channel="carrier_pigeon")
    db_session.commit()

    await dispatch_pending(db_session)

    db_session.refresh(row)
    assert row.sent_at is None, "unknown channel must NOT be marked sent"
    assert row.fail_count == 1
    assert "carrier_pigeon" in (row.last_error or "")

    notifs = db_session.execute(select(Notification).where(Notification.user_id == user.id)).scalars().all()
    assert notifs == [], "unknown channel must not write a Notification"


@pytest.mark.asyncio
async def test_in_app_failure_does_not_poison_batch(db_session):
    """If the in_app write raises, that row is rolled back and failed (fail_count/
    last_error recorded, no dirty Notification persisted), and a healthy later row in
    the same batch still dispatches (batch not poisoned by per-row isolation)."""
    from app.jobs import approval_outbox
    from app.jobs.approval_outbox import dispatch_pending

    user = _make_user(db_session, "mixed@example.com")
    req = _make_request(db_session, user)
    bad = _pending_outbox(db_session, req, user, channel="in_app")
    good = _pending_outbox(db_session, req, user, channel="in_app")
    db_session.commit()

    # Make ONLY the first in_app write raise; the second proceeds normally.
    calls = {"n": 0}
    real_write = approval_outbox._ns.write_in_app

    def _flaky_write(db, user_id, event_type, title, body=None):
        calls["n"] += 1
        if calls["n"] == 1:
            db.add(  # dirty object that must NOT survive the failed row
                Notification(user_id=user_id, event_type=event_type, title=title, body=body, is_read=False)
            )
            raise RuntimeError("boom in_app")
        return real_write(db, user_id, event_type, title, body)

    with patch.object(approval_outbox._ns, "write_in_app", side_effect=_flaky_write):
        dispatched = await dispatch_pending(db_session)

    assert dispatched == 1, "the healthy row must still dispatch despite the bad row"

    db_session.refresh(bad)
    db_session.refresh(good)
    assert bad.sent_at is None and bad.fail_count == 1 and "boom in_app" in (bad.last_error or "")
    assert good.sent_at is not None, "the good row must be marked sent"

    # Exactly one Notification — the bad row's dirty Notification was rolled back.
    notifs = db_session.execute(select(Notification).where(Notification.user_id == user.id)).scalars().all()
    assert len(notifs) == 1, "the failed row's Notification must have been rolled back"


@pytest.mark.asyncio
async def test_email_dispatch_path_committing_session_does_not_abort_batch(db_session):
    """Regression for the staging ResourceClosedError.

    The real email path is ``send_email`` → ``token_manager.get_valid_token(user, db)``,
    which on an expired/near-expiry token refreshes it and calls ``db.commit()`` MID-ROW.
    A mid-row commit ends any enclosing ``begin_nested()`` SAVEPOINT, so the old drain
    blew up at ``savepoint.commit()`` with ``sqlalchemy.exc.ResourceClosedError: This
    transaction is closed`` — and the failing ``except savepoint.rollback()`` propagated,
    aborting the whole 60s batch and leaving the email row stuck (sent_at NULL,
    fail_count 0) while the sibling in_app row had already been delivered.

    The drain MUST tolerate a dispatch path that commits/closes the transaction mid-row:
    no ResourceClosedError, the committing email row is delivered, and a sibling in_app
    row in the same batch still delivers.
    """
    from app.jobs.approval_outbox import dispatch_pending

    sender = _make_user(db_session, "refresher@example.com")
    other = _make_user(db_session, "inapp-sibling@example.com")
    req = _make_request(db_session, sender)
    email_row = _pending_outbox(db_session, req, sender, channel="email")
    inapp_row = _pending_outbox(db_session, req, other, channel="in_app")
    db_session.commit()

    # Faithfully simulate get_valid_token's token-refresh write: mutate the user and
    # commit the SESSION mid-row, exactly as token_manager does, then return a token.
    async def _refresh_then_token(user, db):
        user.m365_last_healthy = datetime.now(UTC)
        db.commit()  # this is what closed the savepoint in production
        return "tok"

    with (
        patch(
            "app.services.approvals.notifications.get_valid_token",
            side_effect=_refresh_then_token,
        ),
        patch("app.services.approvals.notifications.GraphClient") as MockGC,
    ):
        gc_instance = MagicMock()
        gc_instance.post_json = AsyncMock(return_value=None)
        MockGC.return_value = gc_instance

        # Must NOT raise ResourceClosedError.
        dispatched = await dispatch_pending(db_session)

    assert dispatched == 2, "both the committing email row and the in_app sibling dispatch"

    db_session.refresh(email_row)
    db_session.refresh(inapp_row)
    assert email_row.sent_at is not None, "the committing email row must be marked sent"
    assert email_row.fail_count == 0
    assert inapp_row.sent_at is not None, "the sibling in_app row must NOT be aborted"

    notifs = db_session.execute(select(Notification).where(Notification.user_id == other.id)).scalars().all()
    assert len(notifs) == 1, "the in_app sibling's Notification must be written"
