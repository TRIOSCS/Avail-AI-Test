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
