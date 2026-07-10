"""tests/test_renew_subscription_resilience.py — TDD tests for resilient
renew_subscription.

Contract being implemented:
- 404 or 410 from Graph → subscription is genuinely gone → delete it, return False
- Any other error code (401, 503, etc.) or "max_retries" → transient → keep sub, return False
- Raised exception (network/timeout) → transient → keep sub, return False
- Success (no "error" key) → advance expiration_dt, return True

These tests were written BEFORE the fix so they initially FAIL against the old code
(which deletes on ANY failure). After the fix they go GREEN.

Called by: pytest
Depends on: app/services/webhook_service.py
"""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from app.models import GraphSubscription

_PATCH_GET_TOKEN = "app.scheduler.get_valid_token"
_PATCH_GRAPH_CLIENT = "app.utils.graph_client.GraphClient"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_subscription(db, user, sub_id="sub-resilience-001", client_state="state-test"):
    sub = GraphSubscription(
        user_id=user.id,
        subscription_id=sub_id,
        resource="/me/messages",
        change_type="created",
        expiration_dt=datetime.now(UTC) + timedelta(hours=48),
        client_state=client_state,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


# ══════════════════════════════════════════════════════════════════════
#  Transient errors — subscription MUST be kept
# ══════════════════════════════════════════════════════════════════════


class TestRenewTransientErrors:
    """Graph returns an error code that is NOT 404/410 → keep the subscription."""

    def test_503_keeps_subscription(self, db_session, test_user):
        """HTTP 503 from Graph is transient — subscription row must NOT be deleted."""
        from app.services.webhook_service import renew_subscription

        sub = _make_subscription(db_session, test_user, sub_id="sub-503-keep")
        sub_pk = sub.id

        mock_gc = MagicMock()
        mock_gc.patch_json = AsyncMock(return_value={"error": 503, "detail": "Service Unavailable"})

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            result = _run(renew_subscription(sub, db_session))

        assert result is False
        # Subscription MUST still exist
        assert db_session.get(GraphSubscription, sub_pk) is not None

    def test_401_keeps_subscription(self, db_session, test_user):
        """HTTP 401 (token expired) is transient/auth — keep subscription."""
        from app.services.webhook_service import renew_subscription

        sub = _make_subscription(db_session, test_user, sub_id="sub-401-keep")
        sub_pk = sub.id

        mock_gc = MagicMock()
        mock_gc.patch_json = AsyncMock(return_value={"error": 401, "detail": "Unauthorized"})

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            result = _run(renew_subscription(sub, db_session))

        assert result is False
        assert db_session.get(GraphSubscription, sub_pk) is not None

    def test_max_retries_keeps_subscription(self, db_session, test_user):
        """'max_retries' error string means Graph kept 429/5xx-ing — transient, keep
        sub."""
        from app.services.webhook_service import renew_subscription

        sub = _make_subscription(db_session, test_user, sub_id="sub-maxretry-keep")
        sub_pk = sub.id

        mock_gc = MagicMock()
        mock_gc.patch_json = AsyncMock(return_value={"error": "max_retries", "detail": "Too many retries"})

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            result = _run(renew_subscription(sub, db_session))

        assert result is False
        assert db_session.get(GraphSubscription, sub_pk) is not None

    def test_unknown_error_code_keeps_subscription(self, db_session, test_user):
        """An unrecognised numeric error code keeps the subscription (safe default)."""
        from app.services.webhook_service import renew_subscription

        sub = _make_subscription(db_session, test_user, sub_id="sub-unknown-keep")
        sub_pk = sub.id

        mock_gc = MagicMock()
        mock_gc.patch_json = AsyncMock(return_value={"error": 500, "detail": "Internal Server Error"})

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            result = _run(renew_subscription(sub, db_session))

        assert result is False
        assert db_session.get(GraphSubscription, sub_pk) is not None


# ══════════════════════════════════════════════════════════════════════
#  Network / raised exceptions — subscription MUST be kept
# ══════════════════════════════════════════════════════════════════════


class TestRenewNetworkExceptions:
    """patch_json raises an exception (timeout, DNS, etc.) → keep the subscription."""

    def test_timeout_exception_keeps_subscription(self, db_session, test_user):
        """A network timeout should NOT delete the subscription."""
        from app.services.webhook_service import renew_subscription

        sub = _make_subscription(db_session, test_user, sub_id="sub-timeout-keep")
        sub_pk = sub.id

        mock_gc = MagicMock()
        mock_gc.patch_json = AsyncMock(side_effect=TimeoutError("connection timed out"))

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            result = _run(renew_subscription(sub, db_session))

        assert result is False
        assert db_session.get(GraphSubscription, sub_pk) is not None

    def test_runtime_exception_keeps_subscription(self, db_session, test_user):
        """A generic RuntimeError from patch_json keeps the subscription."""
        from app.services.webhook_service import renew_subscription

        sub = _make_subscription(db_session, test_user, sub_id="sub-rterr-keep")
        sub_pk = sub.id

        mock_gc = MagicMock()
        mock_gc.patch_json = AsyncMock(side_effect=RuntimeError("unexpected error"))

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            result = _run(renew_subscription(sub, db_session))

        assert result is False
        assert db_session.get(GraphSubscription, sub_pk) is not None


# ══════════════════════════════════════════════════════════════════════
#  Gone statuses — subscription MUST be deleted
# ══════════════════════════════════════════════════════════════════════


class TestRenewGoneStatuses:
    """Graph returns 404 or 410 → subscription is confirmed gone → delete it."""

    def test_410_deletes_subscription(self, db_session, test_user):
        """HTTP 410 Gone means Graph has deleted the subscription — delete our
        record."""
        from app.services.webhook_service import renew_subscription

        sub = _make_subscription(db_session, test_user, sub_id="sub-410-delete")
        sub_pk = sub.id

        mock_gc = MagicMock()
        mock_gc.patch_json = AsyncMock(return_value={"error": 410, "detail": "Gone"})

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            result = _run(renew_subscription(sub, db_session))

        assert result is False
        assert db_session.get(GraphSubscription, sub_pk) is None

    def test_404_deletes_subscription(self, db_session, test_user):
        """HTTP 404 Not Found means the subscription no longer exists on Graph."""
        from app.services.webhook_service import renew_subscription

        sub = _make_subscription(db_session, test_user, sub_id="sub-404-delete")
        sub_pk = sub.id

        mock_gc = MagicMock()
        mock_gc.patch_json = AsyncMock(return_value={"error": 404, "detail": "Not Found"})

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            result = _run(renew_subscription(sub, db_session))

        assert result is False
        assert db_session.get(GraphSubscription, sub_pk) is None


# ══════════════════════════════════════════════════════════════════════
#  Success path — expiration advances
# ══════════════════════════════════════════════════════════════════════


class TestRenewSuccess:
    def test_success_returns_true_and_advances_expiration(self, db_session, test_user):
        """Successful renewal sets a future expiration_dt and returns True."""
        from app.services.webhook_service import renew_subscription

        sub = _make_subscription(db_session, test_user, sub_id="sub-success-adv")
        old_exp = sub.expiration_dt

        mock_gc = MagicMock()
        mock_gc.patch_json = AsyncMock(return_value={})  # no "error" key → success

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            result = _run(renew_subscription(sub, db_session))

        assert result is True
        new_exp = sub.expiration_dt
        # Strip tz for comparison (SQLite round-trips lose tz)
        old_naive = old_exp.replace(tzinfo=None) if old_exp.tzinfo else old_exp
        new_naive = new_exp.replace(tzinfo=None) if new_exp.tzinfo else new_exp
        assert new_naive > old_naive
