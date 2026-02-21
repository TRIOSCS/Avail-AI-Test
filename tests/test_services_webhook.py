"""
test_services_webhook.py — Tests for Graph webhook service.

Covers:
- handle_notification() with various payloads (valid, invalid, missing fields)
- _extract_email() and _extract_name() helper functions
- create_mail_subscription() — create, reuse, failure paths
- renew_subscription() — success, failure, missing user
- renew_expiring_subscriptions() — batch renewal logic
- ensure_all_users_subscribed() — subscribe unsubscribed users
- Edge cases: empty payloads, client_state mismatch, drafts, outbound, etc.

All external calls (Graph API, scheduler token, email polling) are mocked.
Because webhook_service uses local imports inside each function, we patch
at the source modules (app.scheduler, app.utils.graph_client, etc.).

Called by: pytest
Depends on: app/services/webhook_service.py
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import GraphSubscription, User
from app.services.webhook_service import (
    RENEW_BUFFER_HOURS,
    REPLAY_WINDOW_SECONDS,
    SUBSCRIPTION_LIFETIME_HOURS,
    _extract_email,
    _extract_name,
    _seen_notifications,
    validate_notifications,
)

# ── Patch targets ────────────────────────────────────────────────────
# webhook_service does local imports inside each function, so we patch
# at the canonical source modules.
_PATCH_GET_TOKEN = "app.scheduler.get_valid_token"
_PATCH_GRAPH_CLIENT = "app.utils.graph_client.GraphClient"
_PATCH_LOG_ACTIVITY = "app.services.activity_service.log_email_activity"
_PATCH_POLL_INBOX = "app.email_service.poll_inbox"


def _run(coro):
    """Run an async coroutine synchronously in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ══════════════════════════════════════════════════════════════════════
#  HELPERS: _extract_email / _extract_name
# ══════════════════════════════════════════════════════════════════════


class TestExtractEmail:
    def test_valid_recipient(self):
        recipient = {"emailAddress": {"address": "alice@example.com", "name": "Alice"}}
        assert _extract_email(recipient) == "alice@example.com"

    def test_none_recipient(self):
        assert _extract_email(None) is None

    def test_empty_dict(self):
        assert _extract_email({}) is None

    def test_missing_address_key(self):
        recipient = {"emailAddress": {"name": "Alice"}}
        assert _extract_email(recipient) is None

    def test_missing_email_address_key(self):
        recipient = {"other": "data"}
        assert _extract_email(recipient) is None

    def test_empty_email_address(self):
        recipient = {"emailAddress": {}}
        assert _extract_email(recipient) is None


class TestExtractName:
    def test_valid_recipient(self):
        recipient = {"emailAddress": {"address": "alice@example.com", "name": "Alice Smith"}}
        assert _extract_name(recipient) == "Alice Smith"

    def test_none_recipient(self):
        assert _extract_name(None) is None

    def test_empty_dict(self):
        assert _extract_name({}) is None

    def test_missing_name_key(self):
        recipient = {"emailAddress": {"address": "alice@example.com"}}
        assert _extract_name(recipient) is None

    def test_missing_email_address_key(self):
        recipient = {"other": "data"}
        assert _extract_name(recipient) is None


# ══════════════════════════════════════════════════════════════════════
#  handle_notification()
# ══════════════════════════════════════════════════════════════════════


def _make_subscription(db: Session, user: User, sub_id: str = "sub-001",
                       client_state: str = "state123") -> GraphSubscription:
    """Create a GraphSubscription record for testing."""
    sub = GraphSubscription(
        user_id=user.id,
        subscription_id=sub_id,
        resource="/me/messages",
        change_type="created",
        expiration_dt=datetime.now(timezone.utc) + timedelta(hours=48),
        client_state=client_state,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _make_notification(sub_id: str = "sub-001", client_state: str = "state123",
                       change_type: str = "created",
                       resource: str = "Users('abc')/Messages('msg-001')") -> dict:
    """Build a Graph webhook notification entry."""
    return {
        "subscriptionId": sub_id,
        "clientState": client_state,
        "changeType": change_type,
        "resource": resource,
    }


def _make_message(msg_id: str = "msg-001", subject: str = "RE: RFQ LM317T",
                  from_email: str = "vendor@supplier.com",
                  from_name: str = "Vendor Sales",
                  is_draft: bool = False,
                  parent_folder_id: str = "inbox-folder") -> dict:
    """Build a Graph message response."""
    return {
        "id": msg_id,
        "subject": subject,
        "from": {
            "emailAddress": {
                "address": from_email,
                "name": from_name,
            }
        },
        "toRecipients": [
            {"emailAddress": {"address": "testbuyer@trioscs.com", "name": "Test Buyer"}}
        ],
        "sentDateTime": "2026-02-20T10:00:00Z",
        "isDraft": is_draft,
        "parentFolderId": parent_folder_id,
    }


class TestHandleNotification:
    """Tests for handle_notification() processing of webhook payloads."""

    def test_empty_payload(self, db_session):
        """Empty payload (no 'value' key) should return without error."""
        from app.services.webhook_service import handle_notification

        _run(handle_notification({}, db_session))
        # No crash -- nothing to process

    def test_empty_value_list(self, db_session):
        """Payload with empty 'value' list should return without error."""
        from app.services.webhook_service import handle_notification

        _run(handle_notification({"value": []}, db_session))

    def test_unknown_subscription_ignored(self, db_session, test_user):
        """Notification with unknown subscription ID is skipped."""
        from app.services.webhook_service import handle_notification

        payload = {"value": [_make_notification(sub_id="nonexistent-sub")]}

        with patch(_PATCH_GET_TOKEN, new_callable=AsyncMock) as mock_token:
            _run(handle_notification(payload, db_session))
            mock_token.assert_not_called()

    def test_client_state_mismatch_ignored(self, db_session, test_user):
        """Notification with wrong client_state is skipped."""
        from app.services.webhook_service import handle_notification

        _make_subscription(db_session, test_user, sub_id="sub-002", client_state="correct-state")
        payload = {"value": [_make_notification(sub_id="sub-002", client_state="wrong-state")]}

        with patch(_PATCH_GET_TOKEN, new_callable=AsyncMock) as mock_token:
            _run(handle_notification(payload, db_session))
            mock_token.assert_not_called()

    def test_client_state_none_allows_any(self, db_session, test_user):
        """Subscription with client_state=None accepts any client_state."""
        from app.services.webhook_service import handle_notification

        _make_subscription(db_session, test_user, sub_id="sub-open", client_state=None)
        payload = {"value": [_make_notification(sub_id="sub-open", client_state="anything")]}

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value=_make_message())

        with patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"), \
             patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc), \
             patch(_PATCH_LOG_ACTIVITY) as mock_log, \
             patch(_PATCH_POLL_INBOX, new_callable=AsyncMock, return_value=[]):
            _run(handle_notification(payload, db_session))
            # Should have proceeded to fetch message
            mock_gc.get_json.assert_called_once()

    def test_non_created_change_type_skipped(self, db_session, test_user):
        """Notifications with changeType != 'created' are skipped."""
        from app.services.webhook_service import handle_notification

        _make_subscription(db_session, test_user, sub_id="sub-upd")
        payload = {"value": [_make_notification(sub_id="sub-upd", change_type="updated")]}

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock()

        with patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"), \
             patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc):
            _run(handle_notification(payload, db_session))
            mock_gc.get_json.assert_not_called()

    def test_no_valid_token_skipped(self, db_session, test_user):
        """Notification is skipped when get_valid_token returns None."""
        from app.services.webhook_service import handle_notification

        _make_subscription(db_session, test_user, sub_id="sub-notoken")
        payload = {"value": [_make_notification(sub_id="sub-notoken")]}

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock()

        with patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value=None), \
             patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc):
            _run(handle_notification(payload, db_session))
            mock_gc.get_json.assert_not_called()

    def test_draft_message_skipped(self, db_session, test_user):
        """Draft messages are skipped (no activity logged)."""
        from app.services.webhook_service import handle_notification

        _make_subscription(db_session, test_user, sub_id="sub-draft")
        payload = {"value": [_make_notification(sub_id="sub-draft")]}

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value=_make_message(is_draft=True))

        with patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"), \
             patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc), \
             patch(_PATCH_LOG_ACTIVITY) as mock_log:
            _run(handle_notification(payload, db_session))
            mock_log.assert_not_called()

    def test_outbound_message_skipped(self, db_session, test_user):
        """Outbound messages (from the user themselves) are skipped."""
        from app.services.webhook_service import handle_notification

        _make_subscription(db_session, test_user, sub_id="sub-out")
        payload = {"value": [_make_notification(sub_id="sub-out")]}

        # Message sent BY the user
        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value=_make_message(
            from_email="testbuyer@trioscs.com",  # same as test_user.email
            from_name="Test Buyer",
        ))

        with patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"), \
             patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc), \
             patch(_PATCH_LOG_ACTIVITY) as mock_log, \
             patch(_PATCH_POLL_INBOX, new_callable=AsyncMock) as mock_poll:
            _run(handle_notification(payload, db_session))
            mock_log.assert_not_called()
            mock_poll.assert_not_called()

    def test_outbound_case_insensitive(self, db_session, test_user):
        """Outbound detection is case-insensitive."""
        from app.services.webhook_service import handle_notification

        _make_subscription(db_session, test_user, sub_id="sub-case")
        payload = {"value": [_make_notification(sub_id="sub-case")]}

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value=_make_message(
            from_email="TestBuyer@TRIOSCS.com",
        ))

        with patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"), \
             patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc), \
             patch(_PATCH_LOG_ACTIVITY) as mock_log, \
             patch(_PATCH_POLL_INBOX, new_callable=AsyncMock) as mock_poll:
            _run(handle_notification(payload, db_session))
            mock_log.assert_not_called()
            mock_poll.assert_not_called()

    def test_inbound_message_logs_activity(self, db_session, test_user):
        """Inbound messages are logged via log_email_activity."""
        from app.services.webhook_service import handle_notification

        _make_subscription(db_session, test_user, sub_id="sub-in")
        payload = {"value": [_make_notification(sub_id="sub-in")]}

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value=_make_message(
            msg_id="msg-inbound-001",
            subject="RE: RFQ LM317T",
            from_email="vendor@supplier.com",
            from_name="Vendor Sales",
        ))

        with patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"), \
             patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc), \
             patch(_PATCH_LOG_ACTIVITY) as mock_log, \
             patch(_PATCH_POLL_INBOX, new_callable=AsyncMock, return_value=[]):
            _run(handle_notification(payload, db_session))
            mock_log.assert_called_once_with(
                user_id=test_user.id,
                direction="received",
                email_addr="vendor@supplier.com",
                subject="RE: RFQ LM317T",
                external_id="msg-inbound-001",
                contact_name="Vendor Sales",
                db=db_session,
            )

    def test_inbound_triggers_poll_inbox(self, db_session, test_user):
        """Inbound messages trigger poll_inbox for RFQ reply matching."""
        from app.services.webhook_service import handle_notification

        _make_subscription(db_session, test_user, sub_id="sub-poll")
        payload = {"value": [_make_notification(sub_id="sub-poll")]}

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value=_make_message())

        with patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token-abc"), \
             patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc), \
             patch(_PATCH_LOG_ACTIVITY), \
             patch(_PATCH_POLL_INBOX, new_callable=AsyncMock, return_value=[]) as mock_poll:
            _run(handle_notification(payload, db_session))
            mock_poll.assert_called_once_with(
                token="token-abc",
                db=db_session,
                scanned_by_user_id=test_user.id,
            )

    def test_inbound_poll_with_new_responses_logged(self, db_session, test_user):
        """When poll_inbox returns responses, no exception is raised."""
        from app.services.webhook_service import handle_notification

        _make_subscription(db_session, test_user, sub_id="sub-resp")
        payload = {"value": [_make_notification(sub_id="sub-resp")]}

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value=_make_message())

        with patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="tok"), \
             patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc), \
             patch(_PATCH_LOG_ACTIVITY), \
             patch(_PATCH_POLL_INBOX, new_callable=AsyncMock,
                   return_value=[{"id": "r1"}, {"id": "r2"}]):
            # Should not raise, even with responses
            _run(handle_notification(payload, db_session))

    def test_poll_inbox_error_does_not_propagate(self, db_session, test_user):
        """Errors in poll_inbox are caught and do not crash handle_notification."""
        from app.services.webhook_service import handle_notification

        _make_subscription(db_session, test_user, sub_id="sub-err")
        payload = {"value": [_make_notification(sub_id="sub-err")]}

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value=_make_message())

        with patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="tok"), \
             patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc), \
             patch(_PATCH_LOG_ACTIVITY), \
             patch(_PATCH_POLL_INBOX, new_callable=AsyncMock,
                   side_effect=Exception("Graph timeout")):
            # Should not raise
            _run(handle_notification(payload, db_session))

    def test_graph_fetch_error_continues(self, db_session, test_user):
        """When fetching message details fails, processing continues."""
        from app.services.webhook_service import handle_notification

        _make_subscription(db_session, test_user, sub_id="sub-fetcherr")
        payload = {"value": [_make_notification(sub_id="sub-fetcherr")]}

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(side_effect=Exception("404 Not Found"))

        with patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="tok"), \
             patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc), \
             patch(_PATCH_LOG_ACTIVITY) as mock_log:
            _run(handle_notification(payload, db_session))
            mock_log.assert_not_called()

    def test_multiple_notifications_same_user(self, db_session, test_user):
        """Multiple inbound notifications for same user trigger only one poll_inbox."""
        from app.services.webhook_service import handle_notification

        _make_subscription(db_session, test_user, sub_id="sub-multi")
        payload = {
            "value": [
                _make_notification(sub_id="sub-multi", resource="Users('a')/Messages('m1')"),
                _make_notification(sub_id="sub-multi", resource="Users('a')/Messages('m2')"),
            ]
        }

        call_count = 0

        async def mock_get_json(path, params=None):
            nonlocal call_count
            call_count += 1
            return _make_message(
                msg_id=f"msg-{call_count}",
                from_email=f"vendor{call_count}@supplier.com",
            )

        mock_gc = MagicMock()
        mock_gc.get_json = mock_get_json

        with patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="tok"), \
             patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc), \
             patch(_PATCH_LOG_ACTIVITY) as mock_log, \
             patch(_PATCH_POLL_INBOX, new_callable=AsyncMock, return_value=[]) as mock_poll:
            _run(handle_notification(payload, db_session))
            # Two log calls (one per message)
            assert mock_log.call_count == 2
            # Only one poll (user de-duplicated)
            mock_poll.assert_called_once()

    def test_multiple_notifications_different_users(self, db_session, test_user, sales_user):
        """Notifications for different users trigger separate poll_inbox calls."""
        from app.services.webhook_service import handle_notification

        sales_user.m365_connected = True
        sales_user.email = "testsales@trioscs.com"
        db_session.commit()

        _make_subscription(db_session, test_user, sub_id="sub-u1", client_state="st1")
        _make_subscription(db_session, sales_user, sub_id="sub-u2", client_state="st2")

        payload = {
            "value": [
                _make_notification(sub_id="sub-u1", client_state="st1",
                                   resource="Users('a')/Messages('m1')"),
                _make_notification(sub_id="sub-u2", client_state="st2",
                                   resource="Users('b')/Messages('m2')"),
            ]
        }

        msg_map = {
            "/Users('a')/Messages('m1')": _make_message(
                msg_id="m1", from_email="vendor1@ext.com",
            ),
            "/Users('b')/Messages('m2')": _make_message(
                msg_id="m2", from_email="vendor2@ext.com",
            ),
        }

        async def mock_get_json(path, params=None):
            return msg_map[path]

        mock_gc = MagicMock()
        mock_gc.get_json = mock_get_json

        with patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="tok"), \
             patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc), \
             patch(_PATCH_LOG_ACTIVITY) as mock_log, \
             patch(_PATCH_POLL_INBOX, new_callable=AsyncMock, return_value=[]) as mock_poll:
            _run(handle_notification(payload, db_session))
            assert mock_log.call_count == 2
            assert mock_poll.call_count == 2

    def test_user_not_found_skipped(self, db_session, test_user):
        """Notification for a subscription whose user_id doesn't exist is skipped."""
        from app.services.webhook_service import handle_notification

        _make_subscription(db_session, test_user, sub_id="sub-nouser", client_state="st")
        payload = {"value": [_make_notification(sub_id="sub-nouser", client_state="st")]}

        # Mock db.get to return None for User lookups, simulating a deleted user
        original_get = db_session.get

        def mock_db_get(model, ident):
            if model is User:
                return None
            return original_get(model, ident)

        with patch.object(db_session, "get", side_effect=mock_db_get), \
             patch(_PATCH_GET_TOKEN, new_callable=AsyncMock) as mock_token:
            _run(handle_notification(payload, db_session))
            mock_token.assert_not_called()

    def test_inbound_no_from_address_skips_logging(self, db_session, test_user):
        """Inbound message without a from address does not log activity."""
        from app.services.webhook_service import handle_notification

        _make_subscription(db_session, test_user, sub_id="sub-nofrom")
        payload = {"value": [_make_notification(sub_id="sub-nofrom")]}

        msg = _make_message()
        msg["from"] = None  # No sender

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value=msg)

        with patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="tok"), \
             patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc), \
             patch(_PATCH_LOG_ACTIVITY) as mock_log, \
             patch(_PATCH_POLL_INBOX, new_callable=AsyncMock) as mock_poll:
            _run(handle_notification(payload, db_session))
            mock_log.assert_not_called()
            # No poll since no inbound user tracked
            mock_poll.assert_not_called()

    def test_message_with_empty_subject(self, db_session, test_user):
        """Messages with empty subjects are handled correctly."""
        from app.services.webhook_service import handle_notification

        _make_subscription(db_session, test_user, sub_id="sub-empty-subj")
        payload = {"value": [_make_notification(sub_id="sub-empty-subj")]}

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value=_make_message(subject=""))

        with patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="tok"), \
             patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc), \
             patch(_PATCH_LOG_ACTIVITY) as mock_log, \
             patch(_PATCH_POLL_INBOX, new_callable=AsyncMock, return_value=[]):
            _run(handle_notification(payload, db_session))
            mock_log.assert_called_once()
            assert mock_log.call_args.kwargs["subject"] == ""

    def test_notification_missing_resource(self, db_session, test_user):
        """Notification with no resource string still works (empty resource)."""
        from app.services.webhook_service import handle_notification

        _make_subscription(db_session, test_user, sub_id="sub-nores")
        notif = _make_notification(sub_id="sub-nores")
        del notif["resource"]
        payload = {"value": [notif]}

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value=_make_message())

        with patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="tok"), \
             patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc), \
             patch(_PATCH_LOG_ACTIVITY), \
             patch(_PATCH_POLL_INBOX, new_callable=AsyncMock, return_value=[]):
            # resource defaults to "" via .get("resource", "")
            _run(handle_notification(payload, db_session))
            # Should still try to fetch "/"
            mock_gc.get_json.assert_called_once()


# ══════════════════════════════════════════════════════════════════════
#  create_mail_subscription()
# ══════════════════════════════════════════════════════════════════════


class TestCreateMailSubscription:

    def test_no_valid_token_returns_none(self, db_session, test_user):
        """Returns None when the user has no valid token."""
        from app.services.webhook_service import create_mail_subscription

        with patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value=None):
            result = _run(create_mail_subscription(test_user, db_session))
            assert result is None

    def test_existing_active_subscription_returned(self, db_session, test_user):
        """Returns existing active subscription without creating a new one."""
        from app.services.webhook_service import create_mail_subscription

        existing = _make_subscription(db_session, test_user, sub_id="existing-sub")

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock()

        with patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"), \
             patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc):
            result = _run(create_mail_subscription(test_user, db_session))
            assert result.subscription_id == "existing-sub"
            mock_gc.post_json.assert_not_called()

    def test_creates_new_subscription(self, db_session, test_user):
        """Creates a new Graph subscription when none exists."""
        from app.services.webhook_service import create_mail_subscription

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(return_value={"id": "new-sub-id-123"})

        with patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"), \
             patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc), \
             patch("app.services.webhook_service.settings") as mock_settings:
            mock_settings.app_url = "https://app.example.com"
            result = _run(create_mail_subscription(test_user, db_session))
            assert result is not None
            assert result.subscription_id == "new-sub-id-123"
            assert result.user_id == test_user.id
            assert result.resource == "/me/messages"
            assert result.change_type == "created"
            assert result.client_state is not None

    def test_graph_api_error_returns_none(self, db_session, test_user):
        """Returns None when Graph API call fails."""
        from app.services.webhook_service import create_mail_subscription

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(side_effect=Exception("Graph error"))

        with patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"), \
             patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc), \
             patch("app.services.webhook_service.settings") as mock_settings:
            mock_settings.app_url = "https://app.example.com"
            result = _run(create_mail_subscription(test_user, db_session))
            assert result is None

    def test_no_subscription_id_in_response(self, db_session, test_user):
        """Returns None when Graph response lacks an 'id' field."""
        from app.services.webhook_service import create_mail_subscription

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(return_value={"error": "something went wrong"})

        with patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"), \
             patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc), \
             patch("app.services.webhook_service.settings") as mock_settings:
            mock_settings.app_url = "https://app.example.com"
            result = _run(create_mail_subscription(test_user, db_session))
            assert result is None

    def test_expired_subscription_ignored(self, db_session, test_user):
        """An expired subscription does not block creation of a new one."""
        from app.services.webhook_service import create_mail_subscription

        # Create an expired subscription
        expired_sub = GraphSubscription(
            user_id=test_user.id,
            subscription_id="expired-sub",
            resource="/me/messages",
            change_type="created",
            expiration_dt=datetime.now(timezone.utc) - timedelta(hours=1),
            client_state="old-state",
        )
        db_session.add(expired_sub)
        db_session.commit()

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(return_value={"id": "fresh-sub-id"})

        with patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"), \
             patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc), \
             patch("app.services.webhook_service.settings") as mock_settings:
            mock_settings.app_url = "https://app.example.com"
            result = _run(create_mail_subscription(test_user, db_session))
            assert result is not None
            assert result.subscription_id == "fresh-sub-id"

    def test_subscription_payload_format(self, db_session, test_user):
        """Validates that the payload sent to Graph has the correct shape."""
        from app.services.webhook_service import create_mail_subscription

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(return_value={"id": "sub-payload-test"})

        with patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"), \
             patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc), \
             patch("app.services.webhook_service.settings") as mock_settings:
            mock_settings.app_url = "https://myapp.example.com"
            _run(create_mail_subscription(test_user, db_session))

            call_args = mock_gc.post_json.call_args
            path = call_args[0][0]
            payload = call_args[0][1]
            assert path == "/subscriptions"
            assert payload["changeType"] == "created"
            assert payload["resource"] == "/me/messages"
            assert payload["notificationUrl"] == "https://myapp.example.com/api/webhooks/graph"
            assert "expirationDateTime" in payload
            assert "clientState" in payload


# ══════════════════════════════════════════════════════════════════════
#  renew_subscription()
# ══════════════════════════════════════════════════════════════════════


class TestRenewSubscription:

    def test_renew_success(self, db_session, test_user):
        """Successful renewal updates expiration_dt and returns True."""
        from app.services.webhook_service import renew_subscription

        sub = _make_subscription(db_session, test_user, sub_id="sub-renew")
        # Store the naive timestamp (SQLite strips tz info on round-trip)
        old_expiration = sub.expiration_dt.replace(tzinfo=None) if sub.expiration_dt.tzinfo else sub.expiration_dt

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(return_value={})

        with patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"), \
             patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc):
            result = _run(renew_subscription(sub, db_session))
            assert result is True
            new_exp = sub.expiration_dt.replace(tzinfo=None) if sub.expiration_dt.tzinfo else sub.expiration_dt
            assert new_exp > old_expiration

    def test_renew_user_not_found_returns_false(self, db_session, test_user):
        """Returns False when the subscription's user doesn't exist."""
        from app.services.webhook_service import renew_subscription

        sub = _make_subscription(db_session, test_user, sub_id="sub-nouser-renew")

        # Mock db.get to return None for User lookups
        original_get = db_session.get

        def mock_db_get(model, ident):
            if model is User:
                return None
            return original_get(model, ident)

        with patch.object(db_session, "get", side_effect=mock_db_get):
            result = _run(renew_subscription(sub, db_session))
            assert result is False

    def test_renew_no_valid_token_returns_false(self, db_session, test_user):
        """Returns False when token refresh fails."""
        from app.services.webhook_service import renew_subscription

        sub = _make_subscription(db_session, test_user, sub_id="sub-notoken-renew")

        with patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value=None):
            result = _run(renew_subscription(sub, db_session))
            assert result is False

    def test_renew_graph_error_deletes_subscription(self, db_session, test_user):
        """Graph error deletes the subscription and returns False."""
        from app.services.webhook_service import renew_subscription

        sub = _make_subscription(db_session, test_user, sub_id="sub-graph-err")
        sub_id_val = sub.id

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(side_effect=Exception("Graph 404"))

        with patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"), \
             patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc):
            result = _run(renew_subscription(sub, db_session))
            assert result is False
            # Subscription should be deleted
            deleted = db_session.get(GraphSubscription, sub_id_val)
            assert deleted is None

    def test_renew_calls_correct_graph_endpoint(self, db_session, test_user):
        """Renewal calls PATCH on the correct subscription endpoint."""
        from app.services.webhook_service import renew_subscription

        sub = _make_subscription(db_session, test_user, sub_id="sub-endpoint-check")

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(return_value={})

        with patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"), \
             patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc):
            _run(renew_subscription(sub, db_session))
            call_args = mock_gc.post_json.call_args
            path = call_args[0][0]
            assert path == "/subscriptions/sub-endpoint-check"
            payload = call_args[0][1]
            assert "expirationDateTime" in payload


# ══════════════════════════════════════════════════════════════════════
#  renew_expiring_subscriptions()
# ══════════════════════════════════════════════════════════════════════


class TestRenewExpiringSubscriptions:

    def test_renews_expiring_subs(self, db_session, test_user):
        """Subscriptions expiring within the buffer window are renewed."""
        from app.services.webhook_service import renew_expiring_subscriptions

        # Sub expiring in 2 hours (< RENEW_BUFFER_HOURS)
        sub = GraphSubscription(
            user_id=test_user.id,
            subscription_id="sub-expiring",
            resource="/me/messages",
            change_type="created",
            expiration_dt=datetime.now(timezone.utc) + timedelta(hours=2),
            client_state="st",
        )
        db_session.add(sub)
        db_session.commit()

        with patch("app.services.webhook_service.renew_subscription", new_callable=AsyncMock) as mock_renew:
            mock_renew.return_value = True
            _run(renew_expiring_subscriptions(db_session))
            mock_renew.assert_called_once()

    def test_skips_non_expiring_subs(self, db_session, test_user):
        """Subscriptions not expiring soon are not renewed."""
        from app.services.webhook_service import renew_expiring_subscriptions

        # Sub expiring far in the future
        sub = GraphSubscription(
            user_id=test_user.id,
            subscription_id="sub-fresh",
            resource="/me/messages",
            change_type="created",
            expiration_dt=datetime.now(timezone.utc) + timedelta(hours=48),
            client_state="st",
        )
        db_session.add(sub)
        db_session.commit()

        with patch("app.services.webhook_service.renew_subscription", new_callable=AsyncMock) as mock_renew:
            _run(renew_expiring_subscriptions(db_session))
            mock_renew.assert_not_called()

    def test_no_subscriptions(self, db_session):
        """No subscriptions at all -- runs without error."""
        from app.services.webhook_service import renew_expiring_subscriptions

        with patch("app.services.webhook_service.renew_subscription", new_callable=AsyncMock) as mock_renew:
            _run(renew_expiring_subscriptions(db_session))
            mock_renew.assert_not_called()

    def test_multiple_expiring_subs(self, db_session, test_user, sales_user):
        """Multiple expiring subscriptions are each renewed."""
        from app.services.webhook_service import renew_expiring_subscriptions

        for i, user in enumerate([test_user, sales_user]):
            sub = GraphSubscription(
                user_id=user.id,
                subscription_id=f"sub-expiring-{i}",
                resource="/me/messages",
                change_type="created",
                expiration_dt=datetime.now(timezone.utc) + timedelta(hours=1),
                client_state=f"st-{i}",
            )
            db_session.add(sub)
        db_session.commit()

        with patch("app.services.webhook_service.renew_subscription", new_callable=AsyncMock) as mock_renew:
            mock_renew.return_value = True
            _run(renew_expiring_subscriptions(db_session))
            assert mock_renew.call_count == 2


# ══════════════════════════════════════════════════════════════════════
#  ensure_all_users_subscribed()
# ══════════════════════════════════════════════════════════════════════


class TestEnsureAllUsersSubscribed:

    def test_creates_subscription_for_unsubscribed_user(self, db_session, test_user):
        """Creates subscriptions for M365-connected users without one."""
        from app.services.webhook_service import ensure_all_users_subscribed

        with patch("app.services.webhook_service.create_mail_subscription", new_callable=AsyncMock) as mock_create:
            _run(ensure_all_users_subscribed(db_session))
            mock_create.assert_called_once_with(test_user, db_session)

    def test_skips_already_subscribed_user(self, db_session, test_user):
        """Does not create subscriptions for users who already have active ones."""
        from app.services.webhook_service import ensure_all_users_subscribed

        _make_subscription(db_session, test_user, sub_id="already-active")

        with patch("app.services.webhook_service.create_mail_subscription", new_callable=AsyncMock) as mock_create:
            _run(ensure_all_users_subscribed(db_session))
            mock_create.assert_not_called()

    def test_skips_non_m365_user(self, db_session):
        """Does not create subscriptions for users without m365_connected."""
        from app.services.webhook_service import ensure_all_users_subscribed

        user = User(
            email="noconnection@trioscs.com",
            name="No Connection",
            role="buyer",
            azure_id="az-noconn",
            m365_connected=False,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()

        with patch("app.services.webhook_service.create_mail_subscription", new_callable=AsyncMock) as mock_create:
            _run(ensure_all_users_subscribed(db_session))
            mock_create.assert_not_called()

    def test_skips_admin_role(self, db_session):
        """Admin-role users are not subscribed (only buyer/sales/trader)."""
        from app.services.webhook_service import ensure_all_users_subscribed

        admin = User(
            email="admin@trioscs.com",
            name="Admin",
            role="admin",
            azure_id="az-admin",
            m365_connected=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(admin)
        db_session.commit()

        with patch("app.services.webhook_service.create_mail_subscription", new_callable=AsyncMock) as mock_create:
            _run(ensure_all_users_subscribed(db_session))
            mock_create.assert_not_called()

    def test_subscribes_multiple_roles(self, db_session):
        """Creates subscriptions for buyer, sales, and trader users."""
        from app.services.webhook_service import ensure_all_users_subscribed

        for role in ("buyer", "sales", "trader"):
            u = User(
                email=f"{role}@trioscs.com",
                name=f"Test {role}",
                role=role,
                azure_id=f"az-{role}",
                m365_connected=True,
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(u)
        db_session.commit()

        with patch("app.services.webhook_service.create_mail_subscription", new_callable=AsyncMock) as mock_create:
            _run(ensure_all_users_subscribed(db_session))
            assert mock_create.call_count == 3

    def test_expired_subscription_triggers_recreation(self, db_session, test_user):
        """Users with only expired subscriptions get new ones created."""
        from app.services.webhook_service import ensure_all_users_subscribed

        # Create an expired subscription
        expired = GraphSubscription(
            user_id=test_user.id,
            subscription_id="expired-ensure",
            resource="/me/messages",
            change_type="created",
            expiration_dt=datetime.now(timezone.utc) - timedelta(hours=1),
            client_state="old",
        )
        db_session.add(expired)
        db_session.commit()

        with patch("app.services.webhook_service.create_mail_subscription", new_callable=AsyncMock) as mock_create:
            _run(ensure_all_users_subscribed(db_session))
            mock_create.assert_called_once()

    def test_skips_manager_role(self, db_session):
        """Manager-role users are not subscribed."""
        from app.services.webhook_service import ensure_all_users_subscribed

        mgr = User(
            email="manager@trioscs.com",
            name="Manager",
            role="manager",
            azure_id="az-mgr",
            m365_connected=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(mgr)
        db_session.commit()

        with patch("app.services.webhook_service.create_mail_subscription", new_callable=AsyncMock) as mock_create:
            _run(ensure_all_users_subscribed(db_session))
            mock_create.assert_not_called()


# ══════════════════════════════════════════════════════════════════════
#  validate_notifications()
# ══════════════════════════════════════════════════════════════════════


class TestValidateNotifications:
    """Tests for validate_notifications() — timing-safe comparison + replay protection."""

    def setup_method(self):
        """Clear the replay cache before each test."""
        _seen_notifications.clear()

    def test_validate_unknown_subscription(self, db_session, test_user):
        """Notifications with unknown subscription IDs are filtered out."""
        payload = {"value": [_make_notification(sub_id="nonexistent-sub")]}
        result = validate_notifications(payload, db_session)
        assert result == []

    def test_validate_client_state_mismatch(self, db_session, test_user):
        """Notifications with wrong clientState are rejected."""
        _make_subscription(db_session, test_user, sub_id="sub-val-1", client_state="correct-secret")
        payload = {"value": [_make_notification(sub_id="sub-val-1", client_state="wrong-secret")]}
        result = validate_notifications(payload, db_session)
        assert result == []

    def test_validate_client_state_match(self, db_session, test_user):
        """Notifications with matching clientState are accepted."""
        _make_subscription(db_session, test_user, sub_id="sub-val-2", client_state="my-secret")
        payload = {"value": [_make_notification(sub_id="sub-val-2", client_state="my-secret")]}
        result = validate_notifications(payload, db_session)
        assert len(result) == 1
        assert result[0]["_user"].id == test_user.id
        assert result[0]["_subscription"].subscription_id == "sub-val-2"

    def test_validate_replay_protection(self, db_session, test_user):
        """Duplicate notifications (same sub+resource) within window are rejected."""
        _make_subscription(db_session, test_user, sub_id="sub-val-3", client_state="secret")
        notif = _make_notification(sub_id="sub-val-3", client_state="secret",
                                   resource="Users('abc')/Messages('msg-dup')")

        # First call should accept
        result1 = validate_notifications({"value": [notif.copy()]}, db_session)
        assert len(result1) == 1

        # Second call with same sub+resource should reject (replay)
        result2 = validate_notifications({"value": [notif.copy()]}, db_session)
        assert result2 == []

    def test_validate_replay_expired(self, db_session, test_user):
        """After the replay window expires, the same notification is accepted again."""
        import time

        _make_subscription(db_session, test_user, sub_id="sub-val-4", client_state="secret")
        notif = _make_notification(sub_id="sub-val-4", client_state="secret",
                                   resource="Users('abc')/Messages('msg-replay')")

        # First call
        result1 = validate_notifications({"value": [notif.copy()]}, db_session)
        assert len(result1) == 1

        # Manually expire the cache entry by back-dating the timestamp
        replay_key = "sub-val-4:Users('abc')/Messages('msg-replay')"
        _seen_notifications[replay_key] = time.monotonic() - REPLAY_WINDOW_SECONDS - 1

        # Should be accepted again after expiry
        result2 = validate_notifications({"value": [notif.copy()]}, db_session)
        assert len(result2) == 1

    def test_validate_timing_safe(self, db_session, test_user):
        """Verify that hmac.compare_digest is used for clientState comparison."""
        import hmac as hmac_mod

        _make_subscription(db_session, test_user, sub_id="sub-val-5", client_state="timed-secret")
        payload = {"value": [_make_notification(sub_id="sub-val-5", client_state="timed-secret")]}

        with patch.object(hmac_mod, "compare_digest", wraps=hmac_mod.compare_digest) as mock_cmp:
            result = validate_notifications(payload, db_session)
            assert len(result) == 1
            mock_cmp.assert_called_once_with("timed-secret", "timed-secret")

    def test_validate_empty_payload(self, db_session):
        """Empty payload returns empty list."""
        assert validate_notifications({}, db_session) == []
        assert validate_notifications({"value": []}, db_session) == []

    def test_validate_null_client_state_accepts_any(self, db_session, test_user):
        """Subscription with client_state=None accepts any clientState."""
        _make_subscription(db_session, test_user, sub_id="sub-val-6", client_state=None)
        payload = {"value": [_make_notification(sub_id="sub-val-6", client_state="anything")]}
        result = validate_notifications(payload, db_session)
        assert len(result) == 1


# ══════════════════════════════════════════════════════════════════════
#  CONSTANTS
# ══════════════════════════════════════════════════════════════════════


class TestConstants:
    def test_subscription_lifetime_reasonable(self):
        """Subscription lifetime should be <= 72h (Graph max is ~71h)."""
        assert 0 < SUBSCRIPTION_LIFETIME_HOURS <= 72

    def test_renew_buffer_positive(self):
        """Renew buffer should be a positive value smaller than lifetime."""
        assert 0 < RENEW_BUFFER_HOURS < SUBSCRIPTION_LIFETIME_HOURS
