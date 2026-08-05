"""test_approval_outbox.py — outbox dispatcher tests (email-only channel).

Tests:
  - Running dispatch_pending twice does NOT double-send (idempotency: row already
    sent_at is skipped).
  - Email channel: Graph send is attempted; failure increments fail_count + sets
    last_error; sent_at remains NULL.
  - Email channel: Graph send success sets sent_at.
  - Deleted recipient / unknown channel (including legacy "in_app" rows — that
    write-only delivery path was deleted, W2.9/§5.5) → failed toward the
    dead-letter cap, never marked sent.
  - Per-row isolation: a failing row cannot poison the batch, and a dispatch
    path that commits the session mid-row (token refresh) cannot abort it.
  - Payload contract (W3.8): pre-rendered subject/html payloads pass through
    verbatim; payload["to"] sends to external DL addresses with the recipient
    user as the delegated Graph sender; the legacy "decided" payload appends the
    decision comment.

Called by: pytest
Depends on: conftest (db_session), app.jobs.approval_outbox,
            app.models.approvals, app.models.auth
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.approvals import ApprovalOutbox

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


def _pending_outbox(db, request, recipient, channel="email"):
    row = ApprovalOutbox(
        request_id=request.id,
        recipient_user_id=recipient.id,
        channel=channel,
        payload={"event_type": "decided", "decision": "approved"},
    )
    db.add(row)
    db.flush()
    return row


def _graph_success_patches():
    """Patch token fetch + GraphClient so the email path succeeds; returns the mock."""
    gc_instance = MagicMock()
    gc_instance.post_json = AsyncMock(return_value=None)
    token_patch = patch(
        "app.services.approvals.notifications.get_valid_token",
        new_callable=AsyncMock,
        return_value="tok",
    )
    client_patch = patch("app.services.approvals.notifications.GraphClient", return_value=gc_instance)
    return token_patch, client_patch, gc_instance


# ── Tests ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_idempotent_no_double_send(db_session):
    """Running dispatch_pending twice does NOT double-send (sent_at row is skipped)."""
    from app.jobs.approval_outbox import dispatch_pending

    user = _make_user(db_session)
    req = _make_request(db_session, user)
    row = _pending_outbox(db_session, req, user, channel="email")
    db_session.commit()

    token_patch, client_patch, gc = _graph_success_patches()
    with token_patch, client_patch:
        await dispatch_pending(db_session)
        await dispatch_pending(db_session)

    db_session.refresh(row)
    assert row.sent_at is not None
    assert gc.post_json.await_count == 1, "Second dispatch must NOT send the email again"


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

    token_patch, client_patch, _gc = _graph_success_patches()
    with token_patch, client_patch:
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
    from app.jobs.approval_outbox import dispatch_pending

    user = _make_user(db_session, "ghost@example.com")
    req = _make_request(db_session, user)
    row = _pending_outbox(db_session, req, user, channel="email")
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
    row = _pending_outbox(db_session, req, user, channel="email")
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


@pytest.mark.asyncio
async def test_legacy_in_app_channel_is_failed_to_dead_letter(db_session):
    """A legacy "in_app" row (the write-only delivery path deleted in W2.9/§5.5) is
    treated like any unknown channel: failed toward the dead-letter cap, never sent."""
    from app.jobs.approval_outbox import dispatch_pending

    user = _make_user(db_session, "legacy@example.com")
    req = _make_request(db_session, user)
    row = _pending_outbox(db_session, req, user, channel="in_app")
    db_session.commit()

    await dispatch_pending(db_session)

    db_session.refresh(row)
    assert row.sent_at is None, "legacy in_app row must NOT be marked sent"
    assert row.fail_count == 1
    assert "in_app" in (row.last_error or "")


@pytest.mark.asyncio
async def test_email_failure_does_not_poison_batch(db_session):
    """If one email send raises, that row is rolled back and failed (fail_count/
    last_error recorded), and a healthy later row in the same batch still dispatches
    (batch not poisoned — per-row isolation)."""
    from app.jobs import approval_outbox
    from app.jobs.approval_outbox import dispatch_pending

    user = _make_user(db_session, "mixed@example.com")
    req = _make_request(db_session, user)
    bad = _pending_outbox(db_session, req, user, channel="email")
    good = _pending_outbox(db_session, req, user, channel="email")
    db_session.commit()

    # Make ONLY the first send raise; the second proceeds normally.
    calls = {"n": 0}

    async def _flaky_send(user_, subject, html_body, db, *, to_addresses=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom email")

    with patch.object(approval_outbox._ns, "send_email", side_effect=_flaky_send):
        dispatched = await dispatch_pending(db_session)

    assert dispatched == 1, "the healthy row must still dispatch despite the bad row"

    db_session.refresh(bad)
    db_session.refresh(good)
    assert bad.sent_at is None and bad.fail_count == 1 and "boom email" in (bad.last_error or "")
    assert good.sent_at is not None, "the good row must be marked sent"


@pytest.mark.asyncio
async def test_email_dispatch_path_committing_session_does_not_abort_batch(db_session):
    """Regression for the staging ResourceClosedError.

    The real email path is ``send_email`` → ``token_manager.get_valid_token(user, db)``,
    which on an expired/near-expiry token refreshes it and calls ``db.commit()`` MID-ROW.
    A mid-row commit ends any enclosing ``begin_nested()`` SAVEPOINT, so the old drain
    blew up at ``savepoint.commit()`` with ``sqlalchemy.exc.ResourceClosedError: This
    transaction is closed`` — and the failing ``except savepoint.rollback()`` propagated,
    aborting the whole 60s batch and leaving the email row stuck (sent_at NULL,
    fail_count 0).

    The drain MUST tolerate a dispatch path that commits/closes the transaction mid-row:
    no ResourceClosedError, the committing email row is delivered, and a sibling email
    row in the same batch still delivers.
    """
    from app.jobs.approval_outbox import dispatch_pending

    sender = _make_user(db_session, "refresher@example.com")
    other = _make_user(db_session, "sibling@example.com")
    req = _make_request(db_session, sender)
    email_row = _pending_outbox(db_session, req, sender, channel="email")
    sibling_row = _pending_outbox(db_session, req, other, channel="email")
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

    assert dispatched == 2, "both the committing email row and the sibling dispatch"

    db_session.refresh(email_row)
    db_session.refresh(sibling_row)
    assert email_row.sent_at is not None, "the committing email row must be marked sent"
    assert email_row.fail_count == 0
    assert sibling_row.sent_at is not None, "the sibling email row must NOT be aborted"


# ── W3.8 payload contract: pre-rendered subject/html + external "to" ───────────


@pytest.mark.asyncio
async def test_prerendered_payload_sent_verbatim(db_session):
    """A payload carrying subject+html (the single-path enqueue seam) is sent as-is."""
    from app.jobs.approval_outbox import dispatch_pending

    user = _make_user(db_session, "rich@example.com")
    req = _make_request(db_session, user)
    row = ApprovalOutbox(
        request_id=req.id,
        recipient_user_id=user.id,
        channel="email",
        payload={"subject": "[AVAIL] POs Required — Acme", "html": "<p>rich body</p>"},
    )
    db_session.add(row)
    db_session.commit()

    token_patch, client_patch, gc = _graph_success_patches()
    with token_patch, client_patch:
        await dispatch_pending(db_session)

    db_session.refresh(row)
    assert row.sent_at is not None
    message = gc.post_json.call_args[0][1]["message"]
    assert message["subject"] == "[AVAIL] POs Required — Acme"
    assert message["body"]["content"] == "<p>rich body</p>"
    assert message["toRecipients"] == [{"emailAddress": {"address": user.email}}]


@pytest.mark.asyncio
async def test_payload_to_sends_to_external_addresses(db_session):
    """Payload["to"] = external DLs → sent to those addresses, recipient is the
    delegated Graph SENDER (their token is used, they are not an addressee)."""
    from app.jobs.approval_outbox import dispatch_pending

    sender = _make_user(db_session, "admin@example.com")
    req = _make_request(db_session, sender)
    row = ApprovalOutbox(
        request_id=req.id,
        recipient_user_id=sender.id,
        channel="email",
        payload={
            "subject": "[AVAIL] Prepayment OK TO WIRE",
            "html": "<p>wire it</p>",
            "to": ["accounting@trio.test", "ap@trio.test"],
        },
    )
    db_session.add(row)
    db_session.commit()

    token_patch, client_patch, gc = _graph_success_patches()
    with token_patch, client_patch:
        await dispatch_pending(db_session)

    db_session.refresh(row)
    assert row.sent_at is not None
    message = gc.post_json.call_args[0][1]["message"]
    assert message["toRecipients"] == [
        {"emailAddress": {"address": "accounting@trio.test"}},
        {"emailAddress": {"address": "ap@trio.test"}},
    ]


@pytest.mark.asyncio
async def test_decided_payload_includes_comment(db_session):
    """The legacy decide() payload appends the decision comment (the reject reason)."""
    from app.jobs.approval_outbox import dispatch_pending

    user = _make_user(db_session, "owner@example.com")
    req = _make_request(db_session, user)
    row = ApprovalOutbox(
        request_id=req.id,
        recipient_user_id=user.id,
        channel="email",
        payload={"event_type": "decided", "decision": "rejected", "comment": "Too expensive"},
    )
    db_session.add(row)
    db_session.commit()

    token_patch, client_patch, gc = _graph_success_patches()
    with token_patch, client_patch:
        await dispatch_pending(db_session)

    message = gc.post_json.call_args[0][1]["message"]
    assert "rejected" in message["subject"]
    assert "Too expensive" in message["body"]["content"]
