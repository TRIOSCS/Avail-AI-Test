"""test_subscription_health.py — TDD tests for GraphSubscription health tracking.

RED phase: write tests before implementation to watch them fail.

Covers:
- Transient renewal failure increments renew_fail_count + sets last_error, keeps sub
- Crossing fail threshold (>=3) sets User.m365_error_reason
- Successful renewal resets counters, sets last_renewed_at, clears m365_error_reason
- 404/410 GONE still deletes (unchanged behavior)
- GET /api/admin/subscription-health returns per-user health data

Called by: pytest
Depends on: app/services/webhook_service.py, app/routers/admin/system.py,
            app/models/config.py (GraphSubscription)
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import GraphSubscription, User

_PATCH_GET_TOKEN = "app.scheduler.get_valid_token"
_PATCH_GRAPH_CLIENT = "app.utils.graph_client.GraphClient"

FAIL_THRESHOLD = 3  # matches implementation


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_user(db: Session, email: str = "sub-health@trioscs.com", m365_connected: bool = True) -> User:
    user = User(
        email=email,
        name="Sub Health Tester",
        role="buyer",
        azure_id=f"az-{email[:8]}",
        m365_connected=m365_connected,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_sub(
    db: Session,
    user: User,
    sub_id: str = "sub-health-001",
    fail_count: int = 0,
    last_error: str | None = None,
    expires_in_hours: int = 48,
) -> GraphSubscription:
    sub = GraphSubscription(
        user_id=user.id,
        subscription_id=sub_id,
        resource="/me/messages",
        change_type="created",
        expiration_dt=datetime.now(timezone.utc) + timedelta(hours=expires_in_hours),
        client_state="test-state",
        renew_fail_count=fail_count,
        last_error=last_error,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


# ══════════════════════════════════════════════════════════════════════
#  Transient failure tracking
# ══════════════════════════════════════════════════════════════════════


class TestTransientFailureTracking:
    def test_transient_exception_increments_fail_count(self, db_session):
        """A network/timeout exception increments renew_fail_count by 1."""
        from app.services.webhook_service import renew_subscription

        user = _make_user(db_session)
        sub = _make_sub(db_session, user, sub_id="sub-transient-1", fail_count=0)

        mock_gc = MagicMock()
        mock_gc.patch_json = AsyncMock(side_effect=Exception("network timeout"))

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            result = _run(renew_subscription(sub, db_session))

        assert result is False
        db_session.refresh(sub)
        assert sub.renew_fail_count == 1

    def test_transient_exception_sets_last_error(self, db_session):
        """A transient exception sets last_error with status + detail."""
        from app.services.webhook_service import renew_subscription

        user = _make_user(db_session, email="err-user@trioscs.com")
        sub = _make_sub(db_session, user, sub_id="sub-transient-err", fail_count=0)

        mock_gc = MagicMock()
        mock_gc.patch_json = AsyncMock(side_effect=Exception("connection refused"))

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            _run(renew_subscription(sub, db_session))

        db_session.refresh(sub)
        assert sub.last_error is not None
        assert len(sub.last_error) > 0

    def test_transient_exception_does_not_delete_subscription(self, db_session):
        """A transient exception does NOT delete the subscription record."""
        from app.services.webhook_service import renew_subscription

        user = _make_user(db_session, email="nodelete@trioscs.com")
        sub = _make_sub(db_session, user, sub_id="sub-transient-nodelete")
        sub_id_val = sub.id

        mock_gc = MagicMock()
        mock_gc.patch_json = AsyncMock(side_effect=Exception("timeout"))

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            _run(renew_subscription(sub, db_session))

        assert db_session.get(GraphSubscription, sub_id_val) is not None

    def test_graph_error_payload_non_gone_increments_fail_count(self, db_session):
        """A Graph error payload with non-404/410 code increments renew_fail_count."""
        from app.services.webhook_service import renew_subscription

        user = _make_user(db_session, email="err-payload@trioscs.com")
        sub = _make_sub(db_session, user, sub_id="sub-err-payload", fail_count=1)

        mock_gc = MagicMock()
        mock_gc.patch_json = AsyncMock(return_value={"error": 503, "detail": "Service Unavailable"})

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            _run(renew_subscription(sub, db_session))

        db_session.refresh(sub)
        assert sub.renew_fail_count == 2

    def test_fail_count_accumulates_across_calls(self, db_session):
        """Multiple failed renewals keep incrementing the counter."""
        from app.services.webhook_service import renew_subscription

        user = _make_user(db_session, email="accum@trioscs.com")
        sub = _make_sub(db_session, user, sub_id="sub-accumulate", fail_count=1)

        mock_gc = MagicMock()
        mock_gc.patch_json = AsyncMock(side_effect=Exception("timeout"))

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            _run(renew_subscription(sub, db_session))

        db_session.refresh(sub)
        assert sub.renew_fail_count == 2


# ══════════════════════════════════════════════════════════════════════
#  Threshold → m365_error_reason surfacing
# ══════════════════════════════════════════════════════════════════════


class TestThresholdSurfacing:
    def test_crossing_threshold_sets_m365_error_reason(self, db_session):
        """When fail_count reaches FAIL_THRESHOLD, User.m365_error_reason is set."""
        from app.services.webhook_service import renew_subscription

        user = _make_user(db_session, email="threshold@trioscs.com")
        # Sub already at threshold - 1 failures; next failure crosses it
        sub = _make_sub(db_session, user, sub_id="sub-threshold", fail_count=FAIL_THRESHOLD - 1)

        mock_gc = MagicMock()
        mock_gc.patch_json = AsyncMock(side_effect=Exception("timeout"))

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            _run(renew_subscription(sub, db_session))

        db_session.refresh(user)
        assert user.m365_error_reason is not None
        assert len(user.m365_error_reason) > 0
        assert "subscription" in user.m365_error_reason.lower() or "tracking" in user.m365_error_reason.lower()

    def test_below_threshold_does_not_set_m365_error_reason(self, db_session):
        """fail_count below threshold does NOT set m365_error_reason."""
        from app.services.webhook_service import renew_subscription

        user = _make_user(db_session, email="below-thresh@trioscs.com")
        # Sub at fail_count=0; one failure → count=1 (below threshold=3)
        sub = _make_sub(db_session, user, sub_id="sub-below-thresh", fail_count=0)

        mock_gc = MagicMock()
        mock_gc.patch_json = AsyncMock(side_effect=Exception("timeout"))

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            _run(renew_subscription(sub, db_session))

        db_session.refresh(user)
        assert user.m365_error_reason is None

    def test_above_threshold_still_sets_m365_error_reason(self, db_session):
        """fail_count already above threshold: m365_error_reason stays set on next fail."""
        from app.services.webhook_service import renew_subscription

        user = _make_user(db_session, email="above-thresh@trioscs.com")
        sub = _make_sub(db_session, user, sub_id="sub-above-thresh", fail_count=FAIL_THRESHOLD + 2)

        mock_gc = MagicMock()
        mock_gc.patch_json = AsyncMock(side_effect=Exception("timeout"))

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            _run(renew_subscription(sub, db_session))

        db_session.refresh(user)
        assert user.m365_error_reason is not None


# ══════════════════════════════════════════════════════════════════════
#  Successful renewal resets everything
# ══════════════════════════════════════════════════════════════════════


class TestSuccessfulRenewal:
    def test_success_resets_fail_count_to_zero(self, db_session):
        """A successful renewal resets renew_fail_count to 0."""
        from app.services.webhook_service import renew_subscription

        user = _make_user(db_session, email="success-reset@trioscs.com")
        sub = _make_sub(db_session, user, sub_id="sub-success-reset", fail_count=2)

        mock_gc = MagicMock()
        mock_gc.patch_json = AsyncMock(return_value={})

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            result = _run(renew_subscription(sub, db_session))

        assert result is True
        db_session.refresh(sub)
        assert sub.renew_fail_count == 0

    def test_success_clears_last_error(self, db_session):
        """A successful renewal clears last_error to None."""
        from app.services.webhook_service import renew_subscription

        user = _make_user(db_session, email="success-clearerr@trioscs.com")
        sub = _make_sub(db_session, user, sub_id="sub-success-clearerr", fail_count=1, last_error="503 unavail")

        mock_gc = MagicMock()
        mock_gc.patch_json = AsyncMock(return_value={})

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            result = _run(renew_subscription(sub, db_session))

        assert result is True
        db_session.refresh(sub)
        assert sub.last_error is None

    def test_success_sets_last_renewed_at(self, db_session):
        """A successful renewal sets last_renewed_at to a recent UTC timestamp."""
        from app.services.webhook_service import renew_subscription

        user = _make_user(db_session, email="success-ts@trioscs.com")
        sub = _make_sub(db_session, user, sub_id="sub-success-ts")

        before = datetime.now(timezone.utc)

        mock_gc = MagicMock()
        mock_gc.patch_json = AsyncMock(return_value={})

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            result = _run(renew_subscription(sub, db_session))

        assert result is True
        db_session.refresh(sub)
        # last_renewed_at should be set and recent
        assert sub.last_renewed_at is not None
        # Normalize for SQLite (strips tz)
        renewed_at = sub.last_renewed_at
        if renewed_at.tzinfo is None:
            renewed_at = renewed_at.replace(tzinfo=timezone.utc)
        before_naive = before.replace(tzinfo=None)
        renewed_naive = renewed_at.replace(tzinfo=None)
        assert renewed_naive >= before_naive - timedelta(seconds=5)

    def test_success_clears_m365_error_reason(self, db_session):
        """A successful renewal clears User.m365_error_reason."""
        from app.services.webhook_service import renew_subscription

        user = _make_user(db_session, email="success-clearerr-user@trioscs.com")
        user.m365_error_reason = "Email tracking degraded — Graph subscription renewal failing"
        db_session.commit()

        sub = _make_sub(db_session, user, sub_id="sub-success-clear-m365", fail_count=3)

        mock_gc = MagicMock()
        mock_gc.patch_json = AsyncMock(return_value={})

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            result = _run(renew_subscription(sub, db_session))

        assert result is True
        db_session.refresh(user)
        assert user.m365_error_reason is None


# ══════════════════════════════════════════════════════════════════════
#  Write-guard: don't clobber unrelated m365_error_reason
# ══════════════════════════════════════════════════════════════════════


class TestM365ErrorReasonGuard:
    def test_success_does_not_clear_unrelated_m365_error(self, db_session):
        """Successful renewal does NOT clear an unrelated m365_error_reason.

        The success path only clears the subscription-sentinel string; any other reason
        (e.g. "Token expired — re-authorize M365") must be left untouched so the user
        still sees that unrelated problem.
        """
        from app.services.webhook_service import renew_subscription

        unrelated_reason = "Token expired — re-authorize M365"
        user = _make_user(db_session, email="success-unrelated@trioscs.com")
        user.m365_error_reason = unrelated_reason
        db_session.commit()

        sub = _make_sub(db_session, user, sub_id="sub-success-unrelated", fail_count=0)

        mock_gc = MagicMock()
        mock_gc.patch_json = AsyncMock(return_value={})

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            result = _run(renew_subscription(sub, db_session))

        assert result is True
        db_session.refresh(user)
        assert user.m365_error_reason == unrelated_reason

    def test_failure_threshold_does_not_overwrite_unrelated_m365_error(self, db_session):
        """Crossing the fail threshold does NOT overwrite an unrelated
        m365_error_reason.

        The write-guard in _record_renewal_failure only sets the subscription- sentinel
        when m365_error_reason is None or already the sentinel.  A pre-existing
        unrelated error (e.g. "Token expired — re-authorize M365") must be preserved so
        the user sees the correct root cause.
        """
        from app.services.webhook_service import renew_subscription

        unrelated_reason = "Token expired — re-authorize M365"
        user = _make_user(db_session, email="threshold-unrelated@trioscs.com")
        user.m365_error_reason = unrelated_reason
        db_session.commit()

        # Already at threshold-1 so next failure crosses it
        sub = _make_sub(db_session, user, sub_id="sub-threshold-unrelated", fail_count=FAIL_THRESHOLD - 1)

        mock_gc = MagicMock()
        mock_gc.patch_json = AsyncMock(side_effect=Exception("timeout"))

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            _run(renew_subscription(sub, db_session))

        db_session.refresh(user)
        assert user.m365_error_reason == unrelated_reason


# ══════════════════════════════════════════════════════════════════════
#  404/410 GONE — unchanged behavior: delete subscription
# ══════════════════════════════════════════════════════════════════════


class TestGoneSubscriptionDeleted:
    def test_404_still_deletes_subscription(self, db_session):
        """404 GONE response still deletes the subscription record."""
        from app.services.webhook_service import renew_subscription

        user = _make_user(db_session, email="gone-404@trioscs.com")
        sub = _make_sub(db_session, user, sub_id="sub-gone-404")
        sub_id_val = sub.id

        mock_gc = MagicMock()
        mock_gc.patch_json = AsyncMock(return_value={"error": 404})

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            result = _run(renew_subscription(sub, db_session))

        assert result is False
        assert db_session.get(GraphSubscription, sub_id_val) is None

    def test_410_still_deletes_subscription(self, db_session):
        """410 GONE response still deletes the subscription record."""
        from app.services.webhook_service import renew_subscription

        user = _make_user(db_session, email="gone-410@trioscs.com")
        sub = _make_sub(db_session, user, sub_id="sub-gone-410")
        sub_id_val = sub.id

        mock_gc = MagicMock()
        mock_gc.patch_json = AsyncMock(return_value={"error": 410})

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            result = _run(renew_subscription(sub, db_session))

        assert result is False
        assert db_session.get(GraphSubscription, sub_id_val) is None

    def test_gone_does_not_increment_fail_count(self, db_session):
        """404/410 GONE deletes the row — no fail_count written (row gone)."""
        from app.services.webhook_service import renew_subscription

        user = _make_user(db_session, email="gone-nocount@trioscs.com")
        sub = _make_sub(db_session, user, sub_id="sub-gone-nocount")
        sub_id_val = sub.id

        mock_gc = MagicMock()
        mock_gc.patch_json = AsyncMock(return_value={"error": 404})

        with (
            patch(_PATCH_GET_TOKEN, new_callable=AsyncMock, return_value="token"),
            patch(_PATCH_GRAPH_CLIENT, return_value=mock_gc),
        ):
            _run(renew_subscription(sub, db_session))

        # Row deleted — no point checking fail_count
        assert db_session.get(GraphSubscription, sub_id_val) is None


# ══════════════════════════════════════════════════════════════════════
#  Health endpoint
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture()
def admin_client(db_session: Session):
    """TestClient with admin user pre-authenticated, DB wired to test session."""
    from app.database import get_db
    from app.dependencies import require_settings_access
    from app.main import app

    admin_user = User(
        email="admin@trioscs.com",
        name="Admin",
        role="admin",
        azure_id="az-admin-health",
        m365_connected=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(admin_user)
    db_session.commit()
    db_session.refresh(admin_user)

    def _override_db():
        yield db_session

    overridden = [get_db, require_settings_access]
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_settings_access] = lambda: admin_user

    try:
        with TestClient(app) as c:
            yield c, admin_user
    finally:
        for dep in overridden:
            app.dependency_overrides.pop(dep, None)


class TestSubscriptionHealthEndpoint:
    def test_health_endpoint_returns_200(self, db_session, admin_client):
        """GET /api/admin/subscription-health returns 200 for admin user."""
        client, admin_user = admin_client

        response = client.get("/api/admin/subscription-health")
        assert response.status_code == 200

    def test_health_endpoint_returns_subscriptions_key(self, db_session, admin_client):
        """Response includes a 'subscriptions' key."""
        client, admin_user = admin_client

        response = client.get("/api/admin/subscription-health")
        data = response.json()
        assert "subscriptions" in data

    def test_health_endpoint_includes_fail_count(self, db_session, admin_client):
        """Response includes renew_fail_count per subscription entry."""
        client, admin_user = admin_client

        # Create a sub with a fail count
        sub = GraphSubscription(
            user_id=admin_user.id,
            subscription_id="sub-health-endpoint-1",
            resource="/me/messages",
            change_type="created",
            expiration_dt=datetime.now(timezone.utc) + timedelta(hours=24),
            client_state="test",
            renew_fail_count=2,
            last_error="503 unavail",
        )
        db_session.add(sub)
        db_session.commit()

        response = client.get("/api/admin/subscription-health")
        data = response.json()
        subs = data["subscriptions"]
        assert len(subs) == 1
        assert subs[0]["renew_fail_count"] == 2
        assert subs[0]["last_error"] == "503 unavail"

    def test_health_endpoint_includes_last_renewed_at(self, db_session, admin_client):
        """Response includes last_renewed_at per subscription."""
        client, admin_user = admin_client

        now = datetime.now(timezone.utc)
        sub = GraphSubscription(
            user_id=admin_user.id,
            subscription_id="sub-health-endpoint-2",
            resource="/me/messages",
            change_type="created",
            expiration_dt=now + timedelta(hours=24),
            client_state="test",
            renew_fail_count=0,
            last_renewed_at=now,
        )
        db_session.add(sub)
        db_session.commit()

        response = client.get("/api/admin/subscription-health")
        data = response.json()
        subs = data["subscriptions"]
        assert len(subs) == 1
        assert subs[0]["last_renewed_at"] is not None

    def test_health_endpoint_includes_expiration_dt(self, db_session, admin_client):
        """Response includes expiration_dt per subscription."""
        client, admin_user = admin_client

        expiry = datetime.now(timezone.utc) + timedelta(hours=12)
        sub = GraphSubscription(
            user_id=admin_user.id,
            subscription_id="sub-health-endpoint-3",
            resource="/me/messages",
            change_type="created",
            expiration_dt=expiry,
            client_state="test",
            renew_fail_count=0,
        )
        db_session.add(sub)
        db_session.commit()

        response = client.get("/api/admin/subscription-health")
        data = response.json()
        subs = data["subscriptions"]
        assert len(subs) == 1
        assert subs[0]["expiration_dt"] is not None

    def test_health_endpoint_no_subscriptions(self, db_session, admin_client):
        """Returns empty list when user has no subscriptions."""
        client, admin_user = admin_client

        response = client.get("/api/admin/subscription-health")
        data = response.json()
        assert data["subscriptions"] == []

    def test_health_endpoint_requires_auth(self, db_session):
        """Endpoint returns 401/403 when not authenticated."""
        from app.main import app

        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/admin/subscription-health")
        assert response.status_code in (401, 403)
