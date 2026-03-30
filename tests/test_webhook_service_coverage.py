"""tests/test_webhook_service_coverage.py — Additional coverage for webhook_service.

Covers the remaining uncovered line (validate_notifications: user not found path)
and adds extra robustness tests for edge cases.

Called by: pytest
Depends on: app/services/webhook_service.py, conftest.py
"""

import os

os.environ["TESTING"] = "1"

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.models import GraphSubscription, User
from app.services.webhook_service import _seen_notifications, validate_notifications

_PATCH_GET_TOKEN = "app.scheduler.get_valid_token"
_PATCH_GRAPH_CLIENT = "app.utils.graph_client.GraphClient"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_subscription(db, user, sub_id="sub-cov-001", client_state="state123"):
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


class TestValidateNotificationsUserNotFound:
    def setup_method(self):
        _seen_notifications.clear()

    def test_user_not_found_skips_notification(self, db_session, test_user):
        """When subscription exists but user is deleted, notification is skipped."""
        _make_subscription(db_session, test_user, sub_id="sub-nouser-val", client_state="secret")
        payload = {
            "value": [
                {
                    "subscriptionId": "sub-nouser-val",
                    "clientState": "secret",
                    "changeType": "created",
                    "resource": "Users('abc')/Messages('m1')",
                }
            ]
        }

        original_get = db_session.get

        def mock_db_get(model, ident):
            if model is User:
                return None
            return original_get(model, ident)

        with patch.object(db_session, "get", side_effect=mock_db_get):
            result = validate_notifications(payload, db_session)

        assert result == []

    def test_valid_notification_enriched(self, db_session, test_user):
        """Valid notification gets _user and _subscription keys."""
        _make_subscription(db_session, test_user, sub_id="sub-enrich", client_state="enrich-secret")
        payload = {
            "value": [
                {
                    "subscriptionId": "sub-enrich",
                    "clientState": "enrich-secret",
                    "changeType": "created",
                    "resource": "Users('abc')/Messages('msg-enrich')",
                }
            ]
        }
        result = validate_notifications(payload, db_session)
        assert len(result) == 1
        assert result[0]["_user"].id == test_user.id

    def test_mixed_batch_only_valid_returned(self, db_session, test_user):
        """Only valid notifications are returned from a mixed batch."""
        _make_subscription(db_session, test_user, sub_id="sub-mixed-valid", client_state="valid-state")
        payload = {
            "value": [
                {
                    "subscriptionId": "sub-mixed-valid",
                    "clientState": "valid-state",
                    "changeType": "created",
                    "resource": "Users('a')/Messages('m1')",
                },
                {
                    "subscriptionId": "nonexistent-sub",
                    "clientState": "anything",
                    "changeType": "created",
                    "resource": "Users('b')/Messages('m2')",
                },
            ]
        }
        result = validate_notifications(payload, db_session)
        assert len(result) == 1
        assert result[0]["_subscription"].subscription_id == "sub-mixed-valid"

    def test_two_different_resources_both_accepted(self, db_session, test_user):
        """Two notifs with same sub but different resources are both accepted."""
        _make_subscription(db_session, test_user, sub_id="sub-diff-res", client_state="s1")
        payload = {
            "value": [
                {
                    "subscriptionId": "sub-diff-res",
                    "clientState": "s1",
                    "changeType": "created",
                    "resource": "Users('a')/Messages('m1')",
                },
                {
                    "subscriptionId": "sub-diff-res",
                    "clientState": "s1",
                    "changeType": "created",
                    "resource": "Users('a')/Messages('m2')",
                },
            ]
        }
        result = validate_notifications(payload, db_session)
        assert len(result) == 2


class TestEnsureAllUsersSubscribedEdgeCases:
    def test_no_m365_users_at_all(self, db_session):
        """No M365-connected users means no subscriptions created."""
        from app.services.webhook_service import ensure_all_users_subscribed

        with patch("app.services.webhook_service.create_mail_subscription", new_callable=AsyncMock) as mock_create:
            _run(ensure_all_users_subscribed(db_session))
            mock_create.assert_not_called()

    def test_trader_user_subscribed(self, db_session, trader_user):
        """Trader-role M365 users get subscriptions created."""
        from app.services.webhook_service import ensure_all_users_subscribed

        trader_user.m365_connected = True
        db_session.commit()

        with patch("app.services.webhook_service.create_mail_subscription", new_callable=AsyncMock) as mock_create:
            _run(ensure_all_users_subscribed(db_session))
            mock_create.assert_called_once_with(trader_user, db_session)

    def test_sales_user_subscribed(self, db_session, sales_user):
        """Sales-role M365 users get subscriptions created."""
        from app.services.webhook_service import ensure_all_users_subscribed

        sales_user.m365_connected = True
        db_session.commit()

        with patch("app.services.webhook_service.create_mail_subscription", new_callable=AsyncMock) as mock_create:
            _run(ensure_all_users_subscribed(db_session))
            mock_create.assert_called_once_with(sales_user, db_session)


class TestRenewSubscriptionEdgeCases:
    def test_renew_with_error_only_no_detail(self, db_session, test_user):
        """Graph response with 'error' field but no 'detail' deletes sub."""
        from app.services.webhook_service import renew_subscription

        sub = GraphSubscription(
            user_id=test_user.id,
            subscription_id="sub-error-only",
            resource="/me/messages",
            change_type="created",
            expiration_dt=datetime.now(timezone.utc) + timedelta(hours=48),
            client_state="state",
        )
        db_session.add(sub)
        db_session.commit()
        sub_pk = sub.id

        mock_gc = MagicMock()
        mock_gc.patch_json = AsyncMock(return_value={"error": "subscription_not_found"})

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            result = _run(renew_subscription(sub, db_session))

        assert result is False
        assert db_session.get(GraphSubscription, sub_pk) is None


class TestCreateTeamsSubscription:
    """Tests for create_teams_subscription (lines 113-176)."""

    def test_returns_existing_active_subscription(self, db_session, test_user):
        """Returns existing active Teams sub without calling Graph API."""
        from app.services.webhook_service import create_teams_subscription

        sub = GraphSubscription(
            user_id=test_user.id,
            subscription_id="teams-existing-001",
            resource="/me/chats/getAllMessages",
            change_type="created",
            expiration_dt=datetime.now(timezone.utc) + timedelta(hours=48),
            client_state="state-existing",
        )
        db_session.add(sub)
        db_session.commit()

        with patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"):
            result = _run(create_teams_subscription(test_user, db_session))

        assert result is not None
        assert result.subscription_id == "teams-existing-001"

    def test_no_token_returns_none(self, db_session, test_user):
        """Returns None when no valid token available."""
        from app.services.webhook_service import create_teams_subscription

        with patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value=None):
            result = _run(create_teams_subscription(test_user, db_session))

        assert result is None

    def test_graph_exception_returns_none(self, db_session, test_user):
        """Returns None when Graph API raises an exception."""
        from app.services.webhook_service import create_teams_subscription

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(side_effect=Exception("Graph error"))

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            result = _run(create_teams_subscription(test_user, db_session))

        assert result is None

    def test_no_sub_id_in_response_returns_none(self, db_session, test_user):
        """Returns None when response has no 'id' field."""
        from app.services.webhook_service import create_teams_subscription

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(return_value={"error": "no id"})

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            result = _run(create_teams_subscription(test_user, db_session))

        assert result is None

    def test_creates_new_subscription_successfully(self, db_session, test_user):
        """Creates and persists a new Teams subscription."""
        from app.services.webhook_service import create_teams_subscription

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(return_value={"id": "teams-new-sub-001"})

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            result = _run(create_teams_subscription(test_user, db_session))

        assert result is not None
        assert result.subscription_id == "teams-new-sub-001"
        assert result.resource == "/me/chats/getAllMessages"


class TestEnsureAllUsersSubscribedTeams:
    """Tests for ensure_all_users_subscribed Teams path (lines 251-261)."""

    def test_teams_subscription_created_when_not_mvp(self, db_session, trader_user):
        """Teams subscription created for M365 users when not in MVP mode."""
        from app.services.webhook_service import ensure_all_users_subscribed

        trader_user.m365_connected = True
        db_session.commit()

        with (
            patch("app.services.webhook_service.create_mail_subscription", new_callable=AsyncMock),
            patch("app.services.webhook_service.create_teams_subscription", new_callable=AsyncMock) as mock_teams,
            patch("app.services.webhook_service.settings") as mock_settings,
        ):
            mock_settings.mvp_mode = False
            _run(ensure_all_users_subscribed(db_session))
            mock_teams.assert_called_once_with(trader_user, db_session)

    def test_teams_subscription_skipped_in_mvp_mode(self, db_session, trader_user):
        """Teams subscription skipped when mvp_mode is True."""
        from app.services.webhook_service import ensure_all_users_subscribed

        trader_user.m365_connected = True
        db_session.commit()

        with (
            patch("app.services.webhook_service.create_mail_subscription", new_callable=AsyncMock),
            patch("app.services.webhook_service.create_teams_subscription", new_callable=AsyncMock) as mock_teams,
            patch("app.services.webhook_service.settings") as mock_settings,
        ):
            mock_settings.mvp_mode = True
            _run(ensure_all_users_subscribed(db_session))
            mock_teams.assert_not_called()


class TestResolveTeamsUserEmail:
    """Tests for _resolve_teams_user_email (lines 462-481)."""

    def setup_method(self):
        from app.services.webhook_service import _teams_user_email_cache
        _teams_user_email_cache.clear()

    def test_returns_cached_email(self):
        """Returns cached email without calling Graph API."""
        from app.services.webhook_service import _resolve_teams_user_email, _teams_user_email_cache

        _teams_user_email_cache["guid-cached"] = "cached@example.com"
        mock_gc = MagicMock()

        result = _run(_resolve_teams_user_email("guid-cached", mock_gc))
        assert result == "cached@example.com"
        mock_gc.get_json.assert_not_called()

    def test_fetches_and_caches_email(self):
        """Fetches email from Graph and caches it."""
        from app.services.webhook_service import _resolve_teams_user_email, _teams_user_email_cache

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value={"mail": "User@Example.com"})

        result = _run(_resolve_teams_user_email("guid-new", mock_gc))
        assert result == "user@example.com"
        assert _teams_user_email_cache.get("guid-new") == "user@example.com"

    def test_uses_user_principal_name_as_fallback(self):
        """Falls back to userPrincipalName when mail is absent."""
        from app.services.webhook_service import _resolve_teams_user_email

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value={"userPrincipalName": "upn@example.com"})

        result = _run(_resolve_teams_user_email("guid-upn", mock_gc))
        assert result == "upn@example.com"

    def test_exception_returns_none(self):
        """Returns None when Graph API raises an exception."""
        from app.services.webhook_service import _resolve_teams_user_email

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(side_effect=Exception("API error"))

        result = _run(_resolve_teams_user_email("guid-err", mock_gc))
        assert result is None

    def test_cache_cleared_when_full(self):
        """Cache is cleared when it reaches max size."""
        from app.services.webhook_service import (
            _TEAMS_CACHE_MAX,
            _resolve_teams_user_email,
            _teams_user_email_cache,
        )

        # Fill cache to max
        for i in range(_TEAMS_CACHE_MAX):
            _teams_user_email_cache[f"existing-{i}"] = f"user{i}@example.com"

        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value={"mail": "new@example.com"})

        result = _run(_resolve_teams_user_email("guid-overflow", mock_gc))
        assert result == "new@example.com"
        # Cache was cleared then repopulated with new entry
        assert len(_teams_user_email_cache) == 1


class TestHandleTeamsNotification:
    """Tests for handle_teams_notification (lines 484-597)."""

    def _make_validated_notif(self, user, sub_id="teams-sub-ht"):
        return {
            "subscriptionId": sub_id,
            "clientState": "state",
            "changeType": "created",
            "resource": "chats/chat-id/messages/msg-1",
            "_user": user,
            "_subscription": MagicMock(subscription_id=sub_id),
        }

    def test_skips_non_created_change_type(self, db_session, test_user):
        """Notifications with changeType != 'created' are skipped."""
        from app.services.webhook_service import handle_teams_notification

        notif = self._make_validated_notif(test_user)
        notif["changeType"] = "updated"

        _run(handle_teams_notification({}, db_session, validated=[notif]))
        # No error raised, processed=0

    def test_skips_when_no_token(self, db_session, test_user):
        """Notifications skipped when no valid token available."""
        from app.services.webhook_service import handle_teams_notification

        notif = self._make_validated_notif(test_user)

        with patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value=None):
            _run(handle_teams_notification({}, db_session, validated=[notif]))

    def test_skips_when_graph_fetch_fails(self, db_session, test_user):
        """Notifications skipped when Graph message fetch raises."""
        from app.services.webhook_service import handle_teams_notification

        notif = self._make_validated_notif(test_user)
        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(side_effect=Exception("fetch error"))

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            _run(handle_teams_notification({}, db_session, validated=[notif]))

    def test_skips_message_without_id(self, db_session, test_user):
        """Messages without an 'id' field are skipped."""
        from app.services.webhook_service import handle_teams_notification

        notif = self._make_validated_notif(test_user)
        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value={})  # No 'id'

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            _run(handle_teams_notification({}, db_session, validated=[notif]))

    def test_skips_duplicate_message(self, db_session, test_user):
        """Duplicate messages (same external_id) are skipped."""
        from app.models import ActivityLog
        from app.services.webhook_service import handle_teams_notification

        # Pre-create an ActivityLog with the same external_id
        log = ActivityLog(
            user_id=test_user.id,
            activity_type="teams_message",
            channel="teams",
            event_type="message",
            direction="inbound",
            external_id="msg-dup-001",
            auto_logged=True,
        )
        db_session.add(log)
        db_session.commit()

        notif = self._make_validated_notif(test_user)
        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value={"id": "msg-dup-001"})

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            _run(handle_teams_notification({}, db_session, validated=[notif]))

    def test_skips_message_without_sender_id(self, db_session, test_user):
        """Messages without a sender user ID are skipped."""
        from app.services.webhook_service import handle_teams_notification

        notif = self._make_validated_notif(test_user)
        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value={"id": "msg-nosender", "from": {"user": {}}})

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            _run(handle_teams_notification({}, db_session, validated=[notif]))

    def test_skips_when_sender_email_unresolvable(self, db_session, test_user):
        """Messages skipped when sender email cannot be resolved."""
        from app.services.webhook_service import handle_teams_notification

        notif = self._make_validated_notif(test_user)
        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(side_effect=[
            {"id": "msg-noemail", "from": {"user": {"id": "guid-unknown", "displayName": "Unknown"}}},
            Exception("user lookup failed"),  # _resolve_teams_user_email call
        ])

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            _run(handle_teams_notification({}, db_session, validated=[notif]))

    def test_skips_own_messages(self, db_session, test_user):
        """Messages sent by the subscribed user are skipped."""
        from app.services.webhook_service import handle_teams_notification, _teams_user_email_cache

        _teams_user_email_cache["self-guid"] = test_user.email.lower()

        notif = self._make_validated_notif(test_user)
        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value={
            "id": "msg-own",
            "from": {"user": {"id": "self-guid", "displayName": "Self"}},
        })

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            _run(handle_teams_notification({}, db_session, validated=[notif]))

        _teams_user_email_cache.clear()

    def test_processes_inbound_message_with_company_match(self, db_session, test_user, test_company):
        """Inbound Teams message matched to a company creates an ActivityLog."""
        from app.models import ActivityLog
        from app.services.webhook_service import handle_teams_notification, _teams_user_email_cache

        sender_email = "vendor@external.com"
        _teams_user_email_cache["sender-guid"] = sender_email

        notif = self._make_validated_notif(test_user)
        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value={
            "id": "msg-company-match",
            "from": {"user": {"id": "sender-guid", "displayName": "External Vendor"}},
            "body": {"content": "Hello, interested in parts"},
        })

        match_result = {"type": "company", "id": test_company.id}
        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
            patch("app.services.activity_service.match_email_to_entity", return_value=match_result),
            patch("app.services.activity_service._update_last_activity"),
        ):
            _run(handle_teams_notification({}, db_session, validated=[notif]))

        log = db_session.query(ActivityLog).filter_by(external_id="msg-company-match").first()
        assert log is not None
        assert log.company_id == test_company.id
        _teams_user_email_cache.clear()

    def test_processes_inbound_message_with_vendor_match(self, db_session, test_user, test_vendor_card):
        """Inbound Teams message matched to a vendor card creates an ActivityLog."""
        from app.models import ActivityLog
        from app.services.webhook_service import handle_teams_notification, _teams_user_email_cache

        sender_email = "vendor@supplier.com"
        _teams_user_email_cache["vendor-guid"] = sender_email

        notif = self._make_validated_notif(test_user, sub_id="teams-sub-vc")
        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value={
            "id": "msg-vendor-match",
            "from": {"user": {"id": "vendor-guid", "displayName": "Vendor Rep"}},
            "body": {"content": "We have stock available"},
        })

        match_result = {"type": "vendor", "id": test_vendor_card.id}
        mock_update = MagicMock()
        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
            patch("app.services.activity_service.match_email_to_entity", return_value=match_result),
            patch("app.services.activity_service._update_last_activity", mock_update),
        ):
            _run(handle_teams_notification({}, db_session, validated=[notif]))

        log = db_session.query(ActivityLog).filter_by(external_id="msg-vendor-match").first()
        assert log is not None
        assert log.vendor_card_id == test_vendor_card.id
        _teams_user_email_cache.clear()
