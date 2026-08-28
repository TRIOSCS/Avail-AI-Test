"""test_webhook_calendar_p4.py — Graph calendar subscriptions (Phase 4 Task 2).

Ported from the parked commit ba7ef84d (tag archive/crm-phase4-calendar-webhooks) and
adapted to current main's shapes:
  - webhook_service now splits validate_notifications()/handle_notification(validated=)
    (donor validated inline)
  - changeType is locked to "created" only here (donor used "created,updated" — see
    the ruling comment on create_calendar_subscription / _handle_calendar_notification)
  - calendar activities are logged via activity_service.log_meeting_activity directly
    (donor routed through calendar_intelligence._log_calendar_activity, the daily-scan
    wrapper that forces create_unlinked_fallback=True; the webhook path does not, so a
    meeting with zero CRM-matched attendees logs nothing in real time — see report)
  - the donor's 8x8-poll-interval section (unrelated Phase-4 delta bundled into the
    same parked commit) is out of scope for this task and is not ported

Covers:
1. Resource-scoped subscription guard (mail vs calendar don't collide)
2. create_calendar_subscription — create, reuse, failure paths
3. handle_notification calendar routing (_handle_calendar_notification)
4. ensure_all_users_subscribed wires calendar sub (not mvp_mode-gated)
5. validate_notifications' shared clientState/replay path covers calendar rows

Called by: pytest
Depends on: app/services/webhook_service.py, app/services/activity_service.py
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

os.environ["TESTING"] = "1"

from app.models import ActivityLog, GraphSubscription, User, VendorCard, VendorContact
from app.services.webhook_service import _seen_notifications, validate_notifications

# ── Patch targets ────────────────────────────────────────────────────
# webhook_service does local imports inside each function, so we patch at the
# canonical source modules (same convention as test_services_webhook.py).
_PATCH_GET_TOKEN = "app.scheduler.get_valid_token"
_PATCH_GRAPH_CLIENT = "app.utils.graph_client.GraphClient"
_PATCH_LOG_MEETING = "app.services.activity_service.log_meeting_activity"
_PATCH_LOG_EMAIL = "app.services.activity_service.log_email_activity"
_PATCH_POLL_INBOX = "app.email_service.poll_inbox"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Shared builders ──────────────────────────────────────────────────


def _make_mail_sub(db, user, sub_id="msub-001", client_state="mail-state", expires_in_hours=48):
    sub = GraphSubscription(
        user_id=user.id,
        subscription_id=sub_id,
        resource="/me/messages",
        change_type="created",
        expiration_dt=datetime.now(UTC) + timedelta(hours=expires_in_hours),
        client_state=client_state,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _make_calendar_sub(db, user, sub_id="csub-001", client_state="cal-state", expires_in_hours=48):
    sub = GraphSubscription(
        user_id=user.id,
        subscription_id=sub_id,
        resource="/me/events",
        change_type="created",
        expiration_dt=datetime.now(UTC) + timedelta(hours=expires_in_hours),
        client_state=client_state,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _graph_event(
    event_id="evt-001",
    subject="Vendor Meeting",
    is_cancelled=False,
    start="2026-06-23T14:00:00Z",
    end="2026-06-23T15:00:00Z",
    attendee_email="vendor@external.com",
):
    return {
        "id": event_id,
        "subject": subject,
        "start": {"dateTime": start, "timeZone": "UTC"},
        "end": {"dateTime": end, "timeZone": "UTC"},
        "organizer": {"emailAddress": {"address": "user@trioscs.com", "name": "User"}},
        "attendees": [
            {"emailAddress": {"address": attendee_email, "name": "Vendor Rep"}},
        ],
        "isCancelled": is_cancelled,
        "location": {"displayName": "Zoom"},
    }


# ══════════════════════════════════════════════════════════════════════
#  1. RESOURCE-SCOPED SUBSCRIPTION GUARD
# ══════════════════════════════════════════════════════════════════════


class TestResourceScopedGuard:
    """create_mail_subscription must be idempotent for /me/messages only; same for
    create_calendar_subscription for /me/events.

    A user can hold both simultaneously. (The mail-side half of this guard was already
    locked down in the prior commit, d0db0634; these tests exercise the calendar side
    added here.)
    """

    def test_calendar_sub_not_blocked_by_mail_sub(self, db_session, test_user):
        from app.services.webhook_service import create_calendar_subscription

        _make_mail_sub(db_session, test_user, sub_id="existing-mail")

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(return_value={"id": "new-cal-sub"})

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
            patch("app.services.webhook_service.settings") as ms,
        ):
            ms.app_url = "https://app.example.com"
            result = _run(create_calendar_subscription(test_user, db_session))

        assert result is not None
        assert result.subscription_id == "new-cal-sub"
        assert result.resource == "/me/events"
        mock_gc.post_json.assert_called_once()

    def test_mail_sub_not_blocked_by_existing_calendar_sub(self, db_session, test_user):
        from app.services.webhook_service import create_mail_subscription

        _make_calendar_sub(db_session, test_user, sub_id="existing-calendar")

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(return_value={"id": "new-mail-sub"})

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
            patch("app.services.webhook_service.settings") as ms,
        ):
            ms.app_url = "https://app.example.com"
            result = _run(create_mail_subscription(test_user, db_session))

        assert result is not None
        assert result.resource == "/me/messages"
        mock_gc.post_json.assert_called_once()

    def test_user_can_hold_both_mail_and_calendar_subs(self, db_session, test_user):
        _make_mail_sub(db_session, test_user, sub_id="mail-sub")
        _make_calendar_sub(db_session, test_user, sub_id="cal-sub")

        subs = db_session.query(GraphSubscription).filter(GraphSubscription.user_id == test_user.id).all()
        resources = {s.resource for s in subs}
        assert "/me/messages" in resources
        assert "/me/events" in resources
        assert len(subs) == 2

    def test_calendar_sub_idempotent_with_both_subs_present(self, db_session, test_user):
        from app.services.webhook_service import create_calendar_subscription

        _make_mail_sub(db_session, test_user, sub_id="mail-other2")
        _make_calendar_sub(db_session, test_user, sub_id="cal-idem")

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock()

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            result = _run(create_calendar_subscription(test_user, db_session))

        assert result.subscription_id == "cal-idem"
        mock_gc.post_json.assert_not_called()


# ══════════════════════════════════════════════════════════════════════
#  2. create_calendar_subscription
# ══════════════════════════════════════════════════════════════════════


class TestCreateCalendarSubscription:
    def test_no_token_returns_none(self, db_session, test_user):
        from app.services.webhook_service import create_calendar_subscription

        with patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value=None):
            result = _run(create_calendar_subscription(test_user, db_session))
        assert result is None

    def test_creates_new_calendar_subscription(self, db_session, test_user):
        from app.services.webhook_service import create_calendar_subscription

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(return_value={"id": "cal-new-sub"})

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
            patch("app.services.webhook_service.settings") as ms,
        ):
            ms.app_url = "https://app.example.com"
            result = _run(create_calendar_subscription(test_user, db_session))

        assert result is not None
        assert result.subscription_id == "cal-new-sub"
        assert result.resource == "/me/events"
        assert result.change_type == "created"
        assert result.user_id == test_user.id
        assert result.client_state is not None

    def test_calendar_subscription_payload(self, db_session, test_user):
        """Verifies the Graph subscription payload shape for calendar — changeType is
        locked to "created" only (NOT "created,updated" like the donor commit): Graph
        would also notify on reschedules/edits, but log_meeting_activity dedupes on
        external_id "calendar-{graph_event_id}", so an "updated" notification for an
        already-logged event is a no-op that burns a Graph fetch for nothing."""
        from app.services.webhook_service import create_calendar_subscription

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(return_value={"id": "cal-payload-sub"})

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
            patch("app.services.webhook_service.settings") as ms,
        ):
            ms.app_url = "https://myapp.example.com"
            _run(create_calendar_subscription(test_user, db_session))

        call_args = mock_gc.post_json.call_args
        path = call_args[0][0]
        payload = call_args[0][1]
        assert path == "/subscriptions"
        assert payload["resource"] == "/me/events"
        assert payload["changeType"] == "created"
        assert payload["notificationUrl"] == "https://myapp.example.com/api/webhooks/graph"
        assert "expirationDateTime" in payload
        assert "clientState" in payload

    def test_idempotent_returns_existing(self, db_session, test_user):
        from app.services.webhook_service import create_calendar_subscription

        _make_calendar_sub(db_session, test_user, sub_id="cal-exist")

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock()

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            result = _run(create_calendar_subscription(test_user, db_session))

        assert result.subscription_id == "cal-exist"
        mock_gc.post_json.assert_not_called()

    def test_expired_calendar_sub_triggers_creation(self, db_session, test_user):
        from app.services.webhook_service import create_calendar_subscription

        _make_calendar_sub(db_session, test_user, sub_id="cal-expired", expires_in_hours=-1)

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(return_value={"id": "cal-fresh"})

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
            patch("app.services.webhook_service.settings") as ms,
        ):
            ms.app_url = "https://app.example.com"
            result = _run(create_calendar_subscription(test_user, db_session))

        assert result is not None
        assert result.subscription_id == "cal-fresh"

    def test_graph_error_returns_none(self, db_session, test_user):
        from app.services.webhook_service import create_calendar_subscription

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(side_effect=Exception("Graph error"))

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
            patch("app.services.webhook_service.settings") as ms,
        ):
            ms.app_url = "https://app.example.com"
            result = _run(create_calendar_subscription(test_user, db_session))

        assert result is None

    def test_no_subscription_id_in_response_returns_none(self, db_session, test_user):
        from app.services.webhook_service import create_calendar_subscription

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(return_value={"error": "something failed"})

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
            patch("app.services.webhook_service.settings") as ms,
        ):
            ms.app_url = "https://app.example.com"
            result = _run(create_calendar_subscription(test_user, db_session))

        assert result is None


# ══════════════════════════════════════════════════════════════════════
#  3. handle_notification — calendar routing
# ══════════════════════════════════════════════════════════════════════


class TestHandleNotificationCalendar:
    """Calendar events routed via _subscription.resource == '/me/events'."""

    def setup_method(self):
        _seen_notifications.clear()

    def _validated_calendar_item(self, user, sub, change_type="created", resource="Users('u1')/Events('evt-001')"):
        """Build a pre-validated calendar notification item (validate_notifications'
        output shape)."""
        return {
            "subscriptionId": sub.subscription_id,
            "changeType": change_type,
            "resource": resource,
            "clientState": sub.client_state,
            "_subscription": sub,
            "_user": user,
        }

    def test_calendar_notification_calls_log_meeting_activity(self, db_session, test_user):
        """Calendar notification fetches event and calls log_meeting_activity (not the
        mail-side log_email_activity)."""
        from app.services.webhook_service import handle_notification

        cal_sub = _make_calendar_sub(db_session, test_user, sub_id="cal-route-001")
        item = self._validated_calendar_item(test_user, cal_sub, resource="Users('u1')/Events('evt-xyz')")

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value=_graph_event(event_id="evt-xyz"))

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
            patch(_PATCH_LOG_MEETING, return_value=[MagicMock()]) as mock_log_meeting,
            patch(_PATCH_LOG_EMAIL) as mock_log_email,
        ):
            _run(handle_notification({}, db_session, validated=[item]))

        mock_log_meeting.assert_called_once()
        mock_log_email.assert_not_called()

    def test_calendar_select_fields_are_event_shaped(self, db_session, test_user):
        """The $select sent to Graph is the event field list, not the mail field list —
        proves calendar items never fall through into the mail branch (whose $select
        requests from/toRecipients/isDraft/parentFolderId, all meaningless for an
        event)."""
        from app.services.webhook_service import handle_notification

        cal_sub = _make_calendar_sub(db_session, test_user, sub_id="cal-select-001")
        item = self._validated_calendar_item(test_user, cal_sub, resource="Users('u1')/Events('evt-sel')")

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value=_graph_event(event_id="evt-sel"))

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
            patch(_PATCH_LOG_MEETING, return_value=[]),
        ):
            _run(handle_notification({}, db_session, validated=[item]))

        call_args = mock_gc.get_json.call_args
        assert call_args[0][0] == "/Users('u1')/Events('evt-sel')"
        select = call_args[1]["params"]["$select"]
        for field in ("id", "subject", "attendees", "start", "end", "location", "organizer", "isCancelled"):
            assert field in select
        assert "toRecipients" not in select
        assert "isDraft" not in select

    def test_calendar_notification_cancelled_event_skipped(self, db_session, test_user):
        """Cancelled calendar events are skipped (no activity logged)."""
        from app.services.webhook_service import handle_notification

        cal_sub = _make_calendar_sub(db_session, test_user, sub_id="cal-cancel-001")
        item = self._validated_calendar_item(test_user, cal_sub, resource="Users('u1')/Events('evt-cancelled')")

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value=_graph_event(event_id="evt-cancelled", is_cancelled=True))

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
            patch(_PATCH_LOG_MEETING) as mock_log_meeting,
        ):
            _run(handle_notification({}, db_session, validated=[item]))

        mock_log_meeting.assert_not_called()

    def test_calendar_notification_deleted_change_type_skipped(self, db_session, test_user):
        """ChangeType='deleted' calendar notifications are skipped without a Graph
        fetch."""
        from app.services.webhook_service import handle_notification

        cal_sub = _make_calendar_sub(db_session, test_user, sub_id="cal-del-001")
        item = self._validated_calendar_item(
            test_user, cal_sub, change_type="deleted", resource="Users('u1')/Events('evt-del')"
        )

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock()

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
            patch(_PATCH_LOG_MEETING) as mock_log_meeting,
        ):
            _run(handle_notification({}, db_session, validated=[item]))

        mock_gc.get_json.assert_not_called()
        mock_log_meeting.assert_not_called()

    def test_calendar_notification_updated_change_type_skipped(self, db_session, test_user):
        """ChangeType='updated' calendar notifications are ALSO skipped here — this
        deployment locks calendar subscriptions to changeType="created" only (see
        create_calendar_subscription), so Graph should never send "updated" for our
        subscriptions, but the handler drops it defensively too rather than trusting
        Graph to honor the subscribed changeType."""
        from app.services.webhook_service import handle_notification

        cal_sub = _make_calendar_sub(db_session, test_user, sub_id="cal-upd-001")
        item = self._validated_calendar_item(
            test_user, cal_sub, change_type="updated", resource="Users('u1')/Events('evt-upd')"
        )

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value=_graph_event(event_id="evt-upd"))

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
            patch(_PATCH_LOG_MEETING) as mock_log_meeting,
        ):
            _run(handle_notification({}, db_session, validated=[item]))

        mock_gc.get_json.assert_not_called()
        mock_log_meeting.assert_not_called()

    def test_calendar_notification_no_token_skipped(self, db_session, test_user):
        """Calendar notification with no valid token is skipped gracefully."""
        from app.services.webhook_service import handle_notification

        cal_sub = _make_calendar_sub(db_session, test_user, sub_id="cal-notoken-001")
        item = self._validated_calendar_item(test_user, cal_sub, resource="Users('u1')/Events('evt-notoken')")

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock()

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value=None),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
            patch(_PATCH_LOG_MEETING) as mock_log_meeting,
        ):
            _run(handle_notification({}, db_session, validated=[item]))

        mock_gc.get_json.assert_not_called()
        mock_log_meeting.assert_not_called()

    def test_calendar_graph_fetch_error_continues(self, db_session, test_user):
        """Graph fetch failure for calendar event is caught; processing continues."""
        from app.services.webhook_service import handle_notification

        cal_sub = _make_calendar_sub(db_session, test_user, sub_id="cal-fetcherr-001")
        item = self._validated_calendar_item(test_user, cal_sub, resource="Users('u1')/Events('evt-err')")

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(side_effect=Exception("Graph 404"))

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
            patch(_PATCH_LOG_MEETING) as mock_log_meeting,
        ):
            _run(handle_notification({}, db_session, validated=[item]))

        mock_log_meeting.assert_not_called()

    def test_calendar_notification_missing_event_id_skipped(self, db_session, test_user):
        """An event payload with no id (deleted between notify and fetch) is skipped."""
        from app.services.webhook_service import handle_notification

        cal_sub = _make_calendar_sub(db_session, test_user, sub_id="cal-noid-001")
        item = self._validated_calendar_item(test_user, cal_sub, resource="Users('u1')/Events('evt-noid')")

        event = _graph_event(event_id="evt-noid")
        event["id"] = ""

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value=event)

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
            patch(_PATCH_LOG_MEETING) as mock_log_meeting,
        ):
            _run(handle_notification({}, db_session, validated=[item]))

        mock_log_meeting.assert_not_called()

    def test_calendar_notification_explicit_null_start_organizer_tolerated(self, db_session, test_user):
        """A Graph event with EXPLICIT null start/organizer (not merely absent keys) is
        skipped without raising, and the batch continues to process the next item."""
        from app.services.webhook_service import handle_notification

        cal_sub1 = _make_calendar_sub(db_session, test_user, sub_id="cal-null-001", client_state="cal-null-state-1")
        cal_sub2 = _make_calendar_sub(db_session, test_user, sub_id="cal-null-002", client_state="cal-null-state-2")
        null_item = self._validated_calendar_item(test_user, cal_sub1, resource="Users('u1')/Events('evt-null')")
        ok_item = self._validated_calendar_item(test_user, cal_sub2, resource="Users('u1')/Events('evt-ok')")

        null_event = _graph_event(event_id="evt-null")
        null_event["start"] = None
        null_event["organizer"] = None
        null_event["end"] = None

        async def mock_get_json(path, params=None):
            if "evt-null" in path:
                return null_event
            return _graph_event(event_id="evt-ok")

        mock_gc = MagicMock()
        mock_gc.get_json = mock_get_json

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
            patch(_PATCH_LOG_MEETING, return_value=[MagicMock()]) as mock_log_meeting,
        ):
            _run(handle_notification({}, db_session, validated=[null_item, ok_item]))

        # No start_dt means _handle_calendar_notification returns early for evt-null
        # (nothing to stamp occurred_at with) without raising — the batch then
        # continues on to evt-ok, which logs normally.
        mock_log_meeting.assert_called_once()

    def test_mail_and_calendar_mixed_batch(self, db_session, test_user):
        """Mixed batch: mail items -> log_email_activity; calendar items ->
        log_meeting_activity. Also proves the mail branch's own
        `changeType != "created"` drop (:434 area) is never reached for calendar rows —
        if it were, the calendar item here (changeType="created") would still pass that
        check, so this alone isn't a negative proof; the "updated"/"deleted" skip tests
        above cover the negative case."""
        from app.services.webhook_service import handle_notification

        mail_sub = _make_mail_sub(db_session, test_user, sub_id="mail-mix-001", client_state="ms")
        cal_sub = _make_calendar_sub(db_session, test_user, sub_id="cal-mix-001", client_state="cs")

        mail_item = {
            "subscriptionId": "mail-mix-001",
            "changeType": "created",
            "resource": "Users('u1')/Messages('msg-001')",
            "clientState": "ms",
            "_subscription": mail_sub,
            "_user": test_user,
        }
        cal_item = {
            "subscriptionId": "cal-mix-001",
            "changeType": "created",
            "resource": "Users('u1')/Events('evt-mix-001')",
            "clientState": "cs",
            "_subscription": cal_sub,
            "_user": test_user,
        }

        graph_message = {
            "id": "msg-001",
            "subject": "RFQ reply",
            "from": {"emailAddress": {"address": "vendor@ext.com", "name": "Vendor"}},
            "toRecipients": [],
            "isDraft": False,
            "parentFolderId": "inbox",
        }

        async def mock_get_json(path, params=None):
            if "Messages" in path:
                return graph_message
            return _graph_event()

        mock_gc = MagicMock()
        mock_gc.get_json = mock_get_json

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
            patch(_PATCH_LOG_EMAIL) as mock_log_email,
            patch(_PATCH_LOG_MEETING, return_value=[MagicMock()]) as mock_log_meeting,
            patch(_PATCH_POLL_INBOX, new_callable=AsyncMock, return_value=[]),
        ):
            _run(handle_notification({}, db_session, validated=[mail_item, cal_item]))

        mock_log_email.assert_called_once()
        mock_log_meeting.assert_called_once()

    def test_calendar_log_meeting_receives_correct_kwargs(self, db_session, test_user):
        """log_meeting_activity is called with the fields extracted from the fetched
        Graph event."""
        from app.services.webhook_service import handle_notification

        cal_sub = _make_calendar_sub(db_session, test_user, sub_id="cal-kwargs-001")
        item = self._validated_calendar_item(test_user, cal_sub, resource="Users('u1')/Events('evt-kwargs')")
        event_data = _graph_event(event_id="evt-kwargs", subject="Specific Subject")

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value=event_data)

        captured_kwargs = {}

        def _capture_log_meeting(**kwargs):
            captured_kwargs.update(kwargs)
            return []

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
            patch(_PATCH_LOG_MEETING, side_effect=_capture_log_meeting),
        ):
            _run(handle_notification({}, db_session, validated=[item]))

        assert captured_kwargs["user_id"] == test_user.id
        assert captured_kwargs["graph_event_id"] == "evt-kwargs"
        assert captured_kwargs["subject"] == "Specific Subject"
        assert captured_kwargs["organizer_email"] == "user@trioscs.com"
        assert captured_kwargs["attendee_emails"] == ["vendor@external.com"]
        assert captured_kwargs["location"] == "Zoom"
        assert captured_kwargs["db"] is db_session

    def test_calendar_notification_creates_real_activity_log_row(self, db_session, test_user):
        """End-to-end (no log_meeting_activity mock): a created-event notification for a
        matched vendor attendee lands as a real ActivityLog row, deduped on external_id
        "calendar-{graph_event_id}" — the same key the daily 06:00 scan uses, so a
        webhook notification and a later poll of the same event can never double-log
        it."""
        from app.services.webhook_service import handle_notification

        card = VendorCard(
            normalized_name="arrow electronics",
            display_name="Arrow Electronics",
            domain="arrow-real.com",
            is_blacklisted=False,
            sighting_count=1,
            created_at=datetime.now(UTC),
        )
        db_session.add(card)
        db_session.flush()
        db_session.add(
            VendorContact(vendor_card_id=card.id, email="sales@arrow-real.com", full_name="Rep", source="manual")
        )
        db_session.commit()

        cal_sub = _make_calendar_sub(db_session, test_user, sub_id="cal-real-001")
        item = self._validated_calendar_item(test_user, cal_sub, resource="Users('u1')/Events('evt-real')")

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(
            return_value=_graph_event(event_id="evt-real", attendee_email="sales@arrow-real.com")
        )

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            _run(handle_notification({}, db_session, validated=[item]))

        row = db_session.query(ActivityLog).filter(ActivityLog.external_id == "calendar-evt-real").first()
        assert row is not None
        assert row.vendor_card_id == card.id


# ══════════════════════════════════════════════════════════════════════
#  4. ensure_all_users_subscribed — calendar wiring
# ══════════════════════════════════════════════════════════════════════


class TestEnsureAllUsersSubscribedCalendar:
    def test_creates_calendar_sub_for_user_without_one(self, db_session, test_user):
        """Calendar sub is created for M365 users that have none."""
        from app.services.webhook_service import ensure_all_users_subscribed

        with (
            patch("app.services.webhook_service.create_mail_subscription", new_callable=AsyncMock),
            patch("app.services.webhook_service.create_calendar_subscription", new_callable=AsyncMock) as mock_cal,
        ):
            _run(ensure_all_users_subscribed(db_session))
            mock_cal.assert_called_once_with(test_user, db_session)

    def test_skips_calendar_create_when_active_cal_sub_exists(self, db_session, test_user):
        """Does not call create_calendar_subscription when active calendar sub
        exists."""
        from app.services.webhook_service import ensure_all_users_subscribed

        _make_mail_sub(db_session, test_user, sub_id="mail-active")
        _make_calendar_sub(db_session, test_user, sub_id="cal-active")

        with (
            patch("app.services.webhook_service.create_mail_subscription", new_callable=AsyncMock) as mock_mail,
            patch("app.services.webhook_service.create_calendar_subscription", new_callable=AsyncMock) as mock_cal,
        ):
            _run(ensure_all_users_subscribed(db_session))

        mock_mail.assert_not_called()
        mock_cal.assert_not_called()

    def test_calendar_sub_created_even_when_mail_sub_exists(self, db_session, test_user):
        """If mail sub exists but calendar sub doesn't, calendar sub is created."""
        from app.services.webhook_service import ensure_all_users_subscribed

        _make_mail_sub(db_session, test_user, sub_id="mail-only")

        with (
            patch("app.services.webhook_service.create_mail_subscription", new_callable=AsyncMock) as mock_mail,
            patch("app.services.webhook_service.create_calendar_subscription", new_callable=AsyncMock) as mock_cal,
        ):
            _run(ensure_all_users_subscribed(db_session))

        mock_mail.assert_not_called()
        mock_cal.assert_called_once_with(test_user, db_session)

    def test_mail_sub_created_even_when_calendar_sub_exists(self, db_session, test_user):
        """If calendar sub exists but mail sub doesn't, mail sub is created."""
        from app.services.webhook_service import ensure_all_users_subscribed

        _make_calendar_sub(db_session, test_user, sub_id="cal-only")

        with (
            patch("app.services.webhook_service.create_mail_subscription", new_callable=AsyncMock) as mock_mail,
            patch("app.services.webhook_service.create_calendar_subscription", new_callable=AsyncMock) as mock_cal,
        ):
            _run(ensure_all_users_subscribed(db_session))

        mock_mail.assert_called_once_with(test_user, db_session)
        mock_cal.assert_not_called()

    def test_calendar_sub_created_regardless_of_mvp_mode(self, db_session, test_user):
        """Calendar is NOT mvp_mode-gated — only Teams is (per ba7ef84d and the task
        brief).

        With mvp_mode=True, Teams is skipped but calendar still runs.
        """
        from app.services.webhook_service import ensure_all_users_subscribed

        with (
            patch("app.services.webhook_service.create_mail_subscription", new_callable=AsyncMock),
            patch("app.services.webhook_service.create_calendar_subscription", new_callable=AsyncMock) as mock_cal,
            patch("app.services.webhook_service.create_teams_subscription", new_callable=AsyncMock) as mock_teams,
            patch("app.services.webhook_service.settings") as ms,
        ):
            ms.mvp_mode = True
            _run(ensure_all_users_subscribed(db_session))

        mock_cal.assert_called_once_with(test_user, db_session)
        mock_teams.assert_not_called()

    def test_both_subs_created_for_multiple_users(self, db_session):
        """Both mail and calendar subs created for each eligible user."""
        from app.services.webhook_service import ensure_all_users_subscribed

        for role in ("buyer", "sales"):
            u = User(
                email=f"{role}@trioscs.com",
                name=role,
                role=role,
                azure_id=f"az-{role}-multi",
                m365_connected=True,
                created_at=datetime.now(UTC),
            )
            db_session.add(u)
        db_session.commit()

        with (
            patch("app.services.webhook_service.create_mail_subscription", new_callable=AsyncMock) as mock_mail,
            patch("app.services.webhook_service.create_calendar_subscription", new_callable=AsyncMock) as mock_cal,
        ):
            _run(ensure_all_users_subscribed(db_session))

        assert mock_mail.call_count == 2
        assert mock_cal.call_count == 2


# ══════════════════════════════════════════════════════════════════════
#  5. validate_notifications — shared clientState/replay path covers
#     calendar rows (no calendar-specific validation code exists; this
#     proves the existing generic path is sufficient)
# ══════════════════════════════════════════════════════════════════════


class TestValidateNotificationsCalendar:
    def setup_method(self):
        _seen_notifications.clear()

    def test_calendar_subscription_client_state_validated(self, db_session, test_user):
        """A calendar notification with the correct clientState passes validation and is
        enriched with _subscription/_user, same as mail."""
        cal_sub = _make_calendar_sub(db_session, test_user, sub_id="cal-valid-001", client_state="secret-cal")
        payload = {
            "value": [
                {
                    "subscriptionId": "cal-valid-001",
                    "clientState": "secret-cal",
                    "changeType": "created",
                    "resource": "Users('u1')/Events('evt-v1')",
                }
            ]
        }

        result = validate_notifications(payload, db_session)

        assert len(result) == 1
        assert result[0]["_subscription"].id == cal_sub.id
        assert result[0]["_user"].id == test_user.id

    def test_calendar_subscription_wrong_client_state_rejected(self, db_session, test_user):
        """A calendar notification with a mismatched clientState is dropped — the same
        timing-safe check used for mail, with no calendar-specific carve-out."""
        _make_calendar_sub(db_session, test_user, sub_id="cal-invalid-001", client_state="secret-cal")
        payload = {
            "value": [
                {
                    "subscriptionId": "cal-invalid-001",
                    "clientState": "wrong-secret",
                    "changeType": "created",
                    "resource": "Users('u1')/Events('evt-v2')",
                }
            ]
        }

        result = validate_notifications(payload, db_session)

        assert result == []
