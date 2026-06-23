"""test_webhook_calendar_p4.py — Phase 4 webhook delta tests.

Covers:
1. Resource-scoped subscription guard (mail vs calendar don't collide)
2. create_calendar_subscription — create, reuse, failure paths
3. handle_notification calendar routing (_handle_calendar_notification)
4. ensure_all_users_subscribed wires calendar sub
5. 8x8 IntervalTrigger uses config value (5 min default)

Called by: pytest
Depends on: app/services/webhook_service.py, app/jobs/eight_by_eight_jobs.py
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

os.environ["TESTING"] = "1"

from app.models import GraphSubscription, User
from app.services.webhook_service import _seen_notifications

# ── Patch targets ────────────────────────────────────────────────────
_PATCH_GET_TOKEN = "app.scheduler.get_valid_token"
_PATCH_GRAPH_CLIENT = "app.utils.graph_client.GraphClient"
_PATCH_LOG_CALENDAR = "app.services.calendar_intelligence._log_calendar_activity"
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
        expiration_dt=datetime.now(timezone.utc) + timedelta(hours=expires_in_hours),
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
        change_type="created,updated",
        expiration_dt=datetime.now(timezone.utc) + timedelta(hours=expires_in_hours),
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
):
    return {
        "id": event_id,
        "subject": subject,
        "start": {"dateTime": start, "timeZone": "UTC"},
        "end": {"dateTime": end, "timeZone": "UTC"},
        "organizer": {"emailAddress": {"address": "user@trioscs.com", "name": "User"}},
        "attendees": [
            {"emailAddress": {"address": "vendor@external.com", "name": "Vendor Rep"}},
        ],
        "isCancelled": is_cancelled,
        "location": {"displayName": "Zoom"},
        "bodyPreview": "Looking forward to connecting.",
    }


# ══════════════════════════════════════════════════════════════════════
#  1. RESOURCE-SCOPED SUBSCRIPTION GUARD
# ══════════════════════════════════════════════════════════════════════


class TestResourceScopedGuard:
    """create_mail_subscription must be idempotent for /me/messages only; same for
    create_calendar_subscription for /me/events.

    A user can hold both simultaneously.
    """

    def test_mail_sub_not_blocked_by_calendar_sub(self, db_session, test_user):
        """Existing calendar sub does NOT short-circuit create_mail_subscription."""
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
        assert result.subscription_id == "new-mail-sub"
        assert result.resource == "/me/messages"
        mock_gc.post_json.assert_called_once()

    def test_calendar_sub_not_blocked_by_mail_sub(self, db_session, test_user):
        """Existing mail sub does NOT short-circuit create_calendar_subscription."""
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

    def test_user_can_hold_both_mail_and_calendar_subs(self, db_session, test_user):
        """After both create functions succeed, user has exactly one of each
        resource."""
        _make_mail_sub(db_session, test_user, sub_id="mail-sub")
        _make_calendar_sub(db_session, test_user, sub_id="cal-sub")

        subs = db_session.query(GraphSubscription).filter(GraphSubscription.user_id == test_user.id).all()
        resources = {s.resource for s in subs}
        assert "/me/messages" in resources
        assert "/me/events" in resources
        assert len(subs) == 2

    def test_mail_sub_idempotent_with_both_subs_present(self, db_session, test_user):
        """When both subs exist, create_mail_subscription returns existing mail sub."""
        from app.services.webhook_service import create_mail_subscription

        _make_mail_sub(db_session, test_user, sub_id="mail-idem")
        _make_calendar_sub(db_session, test_user, sub_id="cal-other")

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock()

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            result = _run(create_mail_subscription(test_user, db_session))

        assert result.subscription_id == "mail-idem"
        mock_gc.post_json.assert_not_called()

    def test_calendar_sub_idempotent_with_both_subs_present(self, db_session, test_user):
        """When both subs exist, create_calendar_subscription returns existing cal
        sub."""
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
        assert result.change_type == "created,updated"
        assert result.user_id == test_user.id
        assert result.client_state is not None

    def test_calendar_subscription_payload(self, db_session, test_user):
        """Verifies the Graph subscription payload shape for calendar."""
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
        assert payload["changeType"] == "created,updated"
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

    def _validated_calendar_item(self, db, user, sub, change_type="created", resource="Users('u1')/Events('evt-001')"):
        """Build a pre-validated calendar notification item."""
        return {
            "subscriptionId": sub.subscription_id,
            "changeType": change_type,
            "resource": resource,
            "clientState": sub.client_state,
            "_subscription": sub,
            "_user": user,
        }

    def test_calendar_notification_calls_log_calendar_activity(self, db_session, test_user):
        """Calendar notification fetches event and calls _log_calendar_activity."""
        from app.services.webhook_service import handle_notification

        cal_sub = _make_calendar_sub(db_session, test_user, sub_id="cal-route-001")
        item = self._validated_calendar_item(
            db_session,
            test_user,
            cal_sub,
            resource="Users('u1')/Events('evt-xyz')",
        )

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value=_graph_event(event_id="evt-xyz"))

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
            patch(_PATCH_LOG_CALENDAR, return_value=[MagicMock()]) as mock_log_cal,
            patch(_PATCH_LOG_EMAIL) as mock_log_email,
        ):
            _run(handle_notification({}, db_session, validated=[item]))

        mock_log_cal.assert_called_once()
        mock_log_email.assert_not_called()

    def test_calendar_notification_cancelled_event_skipped(self, db_session, test_user):
        """Cancelled calendar events are skipped (no activity logged)."""
        from app.services.webhook_service import handle_notification

        cal_sub = _make_calendar_sub(db_session, test_user, sub_id="cal-cancel-001")
        item = self._validated_calendar_item(
            db_session,
            test_user,
            cal_sub,
            resource="Users('u1')/Events('evt-cancelled')",
        )

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value=_graph_event(event_id="evt-cancelled", is_cancelled=True))

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
            patch(_PATCH_LOG_CALENDAR) as mock_log_cal,
        ):
            _run(handle_notification({}, db_session, validated=[item]))

        mock_log_cal.assert_not_called()

    def test_calendar_notification_deleted_change_type_skipped(self, db_session, test_user):
        """ChangeType='deleted' calendar notifications are skipped."""
        from app.services.webhook_service import handle_notification

        cal_sub = _make_calendar_sub(db_session, test_user, sub_id="cal-del-001")
        item = self._validated_calendar_item(
            db_session,
            test_user,
            cal_sub,
            change_type="deleted",
            resource="Users('u1')/Events('evt-del')",
        )

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock()

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
            patch(_PATCH_LOG_CALENDAR) as mock_log_cal,
        ):
            _run(handle_notification({}, db_session, validated=[item]))

        mock_gc.get_json.assert_not_called()
        mock_log_cal.assert_not_called()

    def test_calendar_notification_updated_change_type_processed(self, db_session, test_user):
        """ChangeType='updated' calendar notifications ARE processed."""
        from app.services.webhook_service import handle_notification

        cal_sub = _make_calendar_sub(db_session, test_user, sub_id="cal-upd-001")
        item = self._validated_calendar_item(
            db_session,
            test_user,
            cal_sub,
            change_type="updated",
            resource="Users('u1')/Events('evt-upd')",
        )

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value=_graph_event(event_id="evt-upd"))

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
            patch(_PATCH_LOG_CALENDAR, return_value=[]) as mock_log_cal,
        ):
            _run(handle_notification({}, db_session, validated=[item]))

        mock_log_cal.assert_called_once()

    def test_calendar_notification_no_token_skipped(self, db_session, test_user):
        """Calendar notification with no valid token is skipped gracefully."""
        from app.services.webhook_service import handle_notification

        cal_sub = _make_calendar_sub(db_session, test_user, sub_id="cal-notoken-001")
        item = self._validated_calendar_item(
            db_session,
            test_user,
            cal_sub,
            resource="Users('u1')/Events('evt-notoken')",
        )

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock()

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value=None),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
            patch(_PATCH_LOG_CALENDAR) as mock_log_cal,
        ):
            _run(handle_notification({}, db_session, validated=[item]))

        mock_gc.get_json.assert_not_called()
        mock_log_cal.assert_not_called()

    def test_calendar_graph_fetch_error_continues(self, db_session, test_user):
        """Graph fetch failure for calendar event is caught; processing continues."""
        from app.services.webhook_service import handle_notification

        cal_sub = _make_calendar_sub(db_session, test_user, sub_id="cal-fetcherr-001")
        item = self._validated_calendar_item(
            db_session,
            test_user,
            cal_sub,
            resource="Users('u1')/Events('evt-err')",
        )

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(side_effect=Exception("Graph 404"))

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
            patch(_PATCH_LOG_CALENDAR) as mock_log_cal,
        ):
            _run(handle_notification({}, db_session, validated=[item]))

        mock_log_cal.assert_not_called()

    def test_mail_and_calendar_mixed_batch(self, db_session, test_user):
        """Mixed batch: mail items -> log_email_activity; calendar items -> _log_calendar_activity."""
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
            patch(_PATCH_LOG_CALENDAR, return_value=[MagicMock()]) as mock_log_cal,
            patch(_PATCH_POLL_INBOX, new_callable=AsyncMock, return_value=[]),
        ):
            _run(handle_notification({}, db_session, validated=[mail_item, cal_item]))

        mock_log_email.assert_called_once()
        mock_log_cal.assert_called_once()

    def test_calendar_log_activity_receives_correct_kwargs(self, db_session, test_user):
        """_log_calendar_activity is called with correct
        db/user_id/graph_event_id/subject."""
        from app.services.webhook_service import handle_notification

        cal_sub = _make_calendar_sub(db_session, test_user, sub_id="cal-kwargs-001")
        item = self._validated_calendar_item(
            db_session,
            test_user,
            cal_sub,
            resource="Users('u1')/Events('evt-kwargs')",
        )
        event_data = _graph_event(event_id="evt-kwargs", subject="Specific Subject")

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value=event_data)

        captured_kwargs = {}

        def _capture_log_cal(db, user_id, graph_event_id, **kwargs):
            captured_kwargs.update({"db": db, "user_id": user_id, "graph_event_id": graph_event_id})
            captured_kwargs.update(kwargs)
            return []

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
            patch(_PATCH_LOG_CALENDAR, side_effect=_capture_log_cal),
        ):
            _run(handle_notification({}, db_session, validated=[item]))

        assert captured_kwargs["user_id"] == test_user.id
        assert captured_kwargs["graph_event_id"] == "evt-kwargs"
        assert captured_kwargs["subject"] == "Specific Subject"


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

        mock_mail.assert_not_called()  # mail sub exists, no need to create
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
        mock_cal.assert_not_called()  # calendar sub exists

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
                created_at=datetime.now(timezone.utc),
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
#  5. 8x8 poll interval uses config value
# ══════════════════════════════════════════════════════════════════════


class TestEightByEightPollInterval:
    def test_default_poll_interval_is_5_minutes(self):
        """eight_by_eight_poll_interval_minutes default is 5 (not 30)."""
        from app.config import Settings

        s = Settings()
        assert s.eight_by_eight_poll_interval_minutes == 5

    def test_interval_trigger_uses_config_value(self):
        """IntervalTrigger is registered with the config-specified interval."""
        import types

        from apscheduler.triggers.interval import IntervalTrigger

        from app.jobs.eight_by_eight_jobs import register_eight_by_eight_jobs

        mock_settings = types.SimpleNamespace(
            eight_by_eight_enabled=True,
            eight_by_eight_api_key="key",
            eight_by_eight_username="user",
            eight_by_eight_password="pass",
            eight_by_eight_pbx_id="pbx",
            eight_by_eight_poll_interval_minutes=7,  # custom value
        )

        captured_trigger = {}

        class FakeScheduler:
            def add_job(self, func, trigger, **kwargs):
                captured_trigger["trigger"] = trigger

        register_eight_by_eight_jobs(FakeScheduler(), mock_settings)

        assert "trigger" in captured_trigger
        trigger = captured_trigger["trigger"]
        assert isinstance(trigger, IntervalTrigger)
        # IntervalTrigger stores interval as a timedelta
        assert trigger.interval.total_seconds() == 7 * 60

    def test_interval_trigger_default_5_min_end_to_end(self):
        """With default settings (5 min), the trigger is 300 seconds."""
        import types

        from apscheduler.triggers.interval import IntervalTrigger

        from app.jobs.eight_by_eight_jobs import register_eight_by_eight_jobs

        mock_settings = types.SimpleNamespace(
            eight_by_eight_enabled=True,
            eight_by_eight_api_key="key",
            eight_by_eight_username="user",
            eight_by_eight_password="pass",
            eight_by_eight_pbx_id="pbx",
            eight_by_eight_poll_interval_minutes=5,
        )

        captured_trigger = {}

        class FakeScheduler:
            def add_job(self, func, trigger, **kwargs):
                captured_trigger["trigger"] = trigger

        register_eight_by_eight_jobs(FakeScheduler(), mock_settings)

        trigger = captured_trigger["trigger"]
        assert isinstance(trigger, IntervalTrigger)
        assert trigger.interval.total_seconds() == 300
