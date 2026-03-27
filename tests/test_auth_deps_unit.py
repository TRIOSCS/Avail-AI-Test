"""test_auth_deps_unit.py — Direct unit tests for auth dependency functions.

Tests the core auth dependencies in app/dependencies.py by calling them
directly with mock request objects and real User rows in the test DB.

Called by: pytest
Depends on: app.dependencies, app.models.User, tests.conftest (db_session)
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.dependencies import (
    get_user,
    require_admin,
    require_buyer,
    require_fresh_token,
    require_user,
)
from app.models import User

# ── Helpers ──────────────────────────────────────────────────────────


def _make_request(session_data: dict | None = None, headers: dict | None = None) -> MagicMock:
    """Build a mock Request with configurable session and headers."""
    req = MagicMock()
    req.session = session_data or {}
    req.headers = headers or {}
    req.method = "GET"
    req.url = MagicMock()
    req.url.path = "/test"
    return req


def _create_user(db: Session, *, email: str = "unit@test.com", role: str = "buyer", is_active: bool = True) -> User:
    """Insert and return a User row."""
    user = User(
        email=email,
        name=f"Unit {role.title()}",
        role=role,
        azure_id=f"azure-{email}",
        is_active=is_active,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# ── get_user ─────────────────────────────────────────────────────────


class TestGetUser:
    """Tests for get_user(request, db)."""

    def test_valid_session_returns_user(self, db_session: Session):
        user = _create_user(db_session)
        request = _make_request(session_data={"user_id": user.id})

        result = get_user(request, db_session)

        assert result is not None
        assert result.id == user.id
        assert result.email == user.email

    def test_no_session_returns_none(self, db_session: Session):
        request = _make_request(session_data={})

        result = get_user(request, db_session)

        assert result is None

    def test_invalid_user_id_returns_none(self, db_session: Session):
        request = _make_request(session_data={"user_id": 999999})

        result = get_user(request, db_session)

        assert result is None

    def test_db_error_clears_session_returns_none(self, db_session: Session):
        """If db.get() raises, session is cleared and None returned."""
        request = _make_request(session_data={"user_id": 1})

        with patch.object(db_session, "get", side_effect=Exception("db boom")):
            result = get_user(request, db_session)

        assert result is None
        # Session should have been cleared
        assert request.session == {}


# ── require_user ─────────────────────────────────────────────────────


class TestRequireUser:
    """Tests for require_user(request, db)."""

    def test_valid_session_returns_user(self, db_session: Session):
        user = _create_user(db_session)
        request = _make_request(session_data={"user_id": user.id})

        result = require_user(request, db_session)

        assert result.id == user.id

    def test_no_session_raises_401(self, db_session: Session):
        request = _make_request()

        with pytest.raises(HTTPException) as exc_info:
            require_user(request, db_session)

        assert exc_info.value.status_code == 401

    def test_deactivated_user_raises_403(self, db_session: Session):
        user = _create_user(db_session, is_active=False)
        request = _make_request(session_data={"user_id": user.id})

        with pytest.raises(HTTPException) as exc_info:
            require_user(request, db_session)

        assert exc_info.value.status_code == 403
        assert "deactivated" in str(exc_info.value.detail).lower()

    def test_valid_agent_api_key_returns_agent_user(self, db_session: Session):
        """Agent API key auth creates a service-to-service session."""
        agent_user = _create_user(db_session, email="agent@availai.local", role="admin")
        request = _make_request(
            session_data={},
            headers={"x-agent-key": "test-agent-key-secret"},
        )

        result = require_user(request, db_session)

        assert result.id == agent_user.id
        assert result.email == "agent@availai.local"

    def test_wrong_agent_api_key_raises_401(self, db_session: Session):
        """Invalid agent API key should not authenticate."""
        _create_user(db_session, email="agent@availai.local", role="admin")
        request = _make_request(
            session_data={},
            headers={"x-agent-key": "wrong-key"},
        )

        with pytest.raises(HTTPException) as exc_info:
            require_user(request, db_session)

        assert exc_info.value.status_code == 401

    def test_agent_key_without_agent_user_raises_503(self, db_session: Session):
        """Valid key but no agent user in DB should raise 503."""
        request = _make_request(
            session_data={},
            headers={"x-agent-key": "test-agent-key-secret"},
        )

        with pytest.raises(HTTPException) as exc_info:
            require_user(request, db_session)

        assert exc_info.value.status_code == 503


# ── require_admin ────────────────────────────────────────────────────


class TestRequireAdmin:
    """Tests for require_admin(request, db)."""

    def test_admin_user_returns_user(self, db_session: Session):
        user = _create_user(db_session, role="admin")
        request = _make_request(session_data={"user_id": user.id})

        result = require_admin(request, db_session)

        assert result.id == user.id

    def test_non_admin_raises_403(self, db_session: Session):
        user = _create_user(db_session, role="buyer")
        request = _make_request(session_data={"user_id": user.id})

        with pytest.raises(HTTPException) as exc_info:
            require_admin(request, db_session)

        assert exc_info.value.status_code == 403
        assert "admin" in str(exc_info.value.detail).lower()

    def test_agent_service_account_blocked(self, db_session: Session):
        """Agent service account should be blocked from admin endpoints."""
        _create_user(db_session, email="agent@availai.local", role="admin")
        request = _make_request(
            session_data={},
            headers={"x-agent-key": "test-agent-key-secret"},
        )

        with pytest.raises(HTTPException) as exc_info:
            require_admin(request, db_session)

        assert exc_info.value.status_code == 403
        assert "agent" in str(exc_info.value.detail).lower()

    def test_sales_user_raises_403(self, db_session: Session):
        user = _create_user(db_session, role="sales")
        request = _make_request(session_data={"user_id": user.id})

        with pytest.raises(HTTPException) as exc_info:
            require_admin(request, db_session)

        assert exc_info.value.status_code == 403


# ── require_buyer ────────────────────────────────────────────────────


class TestRequireBuyer:
    """Tests for require_buyer(request, db)."""

    def test_buyer_role_allowed(self, db_session: Session):
        user = _create_user(db_session, role="buyer")
        request = _make_request(session_data={"user_id": user.id})

        result = require_buyer(request, db_session)

        assert result.id == user.id

    def test_sales_role_allowed(self, db_session: Session):
        user = _create_user(db_session, email="sales@test.com", role="sales")
        request = _make_request(session_data={"user_id": user.id})

        result = require_buyer(request, db_session)

        assert result.id == user.id

    def test_trader_role_allowed(self, db_session: Session):
        user = _create_user(db_session, email="trader@test.com", role="trader")
        request = _make_request(session_data={"user_id": user.id})

        result = require_buyer(request, db_session)

        assert result.id == user.id

    def test_manager_role_allowed(self, db_session: Session):
        user = _create_user(db_session, email="mgr@test.com", role="manager")
        request = _make_request(session_data={"user_id": user.id})

        result = require_buyer(request, db_session)

        assert result.id == user.id

    def test_admin_role_allowed(self, db_session: Session):
        user = _create_user(db_session, email="adm@test.com", role="admin")
        request = _make_request(session_data={"user_id": user.id})

        result = require_buyer(request, db_session)

        assert result.id == user.id

    def test_unauthorized_role_raises_403(self, db_session: Session):
        """A user with no recognized buyer-tier role should be rejected.

        Since all UserRole values are allowed by require_buyer, we test by directly
        setting a role value not in the allowed set. In practice this can't happen with
        the StrEnum, but it validates the guard.
        """
        # Create user then manually set role to something outside the allowed set
        user = _create_user(db_session, role="buyer")
        # Temporarily override role attribute to simulate an unrecognized role
        request = _make_request(session_data={"user_id": user.id})

        # Patch the user's role after retrieval to test the guard
        with patch.object(User, "role", new_callable=lambda: property(lambda self: "viewer")):
            with pytest.raises(HTTPException) as exc_info:
                require_buyer(request, db_session)

        assert exc_info.value.status_code == 403


# ── require_fresh_token ──────────────────────────────────────────────


class TestRequireFreshToken:
    """Tests for require_fresh_token(request, db) — async."""

    @pytest.mark.asyncio
    async def test_no_user_raises_401(self, db_session: Session):
        request = _make_request()

        with pytest.raises(HTTPException) as exc_info:
            await require_fresh_token(request, db_session)

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_no_access_token_raises_401(self, db_session: Session):
        user = _create_user(db_session)
        user.access_token = None
        db_session.commit()
        request = _make_request(session_data={"user_id": user.id})

        with pytest.raises(HTTPException) as exc_info:
            await require_fresh_token(request, db_session)

        assert exc_info.value.status_code == 401
        assert "no access token" in str(exc_info.value.detail).lower()

    @pytest.mark.asyncio
    async def test_valid_fresh_token_returned(self, db_session: Session):
        user = _create_user(db_session)
        user.access_token = "valid-token-abc"
        user.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        db_session.commit()
        request = _make_request(session_data={"user_id": user.id})

        result = await require_fresh_token(request, db_session)

        assert result == "valid-token-abc"

    @pytest.mark.asyncio
    async def test_expired_token_no_refresh_raises_401(self, db_session: Session):
        user = _create_user(db_session)
        user.access_token = "expired-token"
        user.token_expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        user.refresh_token = None
        db_session.commit()
        request = _make_request(session_data={"user_id": user.id})

        with pytest.raises(HTTPException) as exc_info:
            await require_fresh_token(request, db_session)

        assert exc_info.value.status_code == 401
        assert "expired" in str(exc_info.value.detail).lower()

    @pytest.mark.asyncio
    async def test_near_expiry_triggers_refresh(self, db_session: Session):
        """Token within 15-min buffer should attempt refresh."""
        user = _create_user(db_session)
        user.access_token = "old-token"
        user.token_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        user.refresh_token = "refresh-token-xyz"
        db_session.commit()
        request = _make_request(session_data={"user_id": user.id})

        with patch(
            "app.scheduler.refresh_user_token", new_callable=AsyncMock, return_value="new-token"
        ) as mock_refresh:
            result = await require_fresh_token(request, db_session)

        assert result == "new-token"
        mock_refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_refresh_failure_raises_401(self, db_session: Session):
        """If refresh returns None, should raise 401."""
        user = _create_user(db_session)
        user.access_token = "old-token"
        user.token_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        user.refresh_token = "refresh-token-xyz"
        db_session.commit()
        request = _make_request(session_data={"user_id": user.id})

        with patch("app.scheduler.refresh_user_token", new_callable=AsyncMock, return_value=None):
            with pytest.raises(HTTPException) as exc_info:
                await require_fresh_token(request, db_session)

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_token_without_expiry_returned_as_is(self, db_session: Session):
        """Token with no expiry timestamp should be returned without refresh."""
        user = _create_user(db_session)
        user.access_token = "no-expiry-token"
        user.token_expires_at = None
        db_session.commit()
        request = _make_request(session_data={"user_id": user.id})

        result = await require_fresh_token(request, db_session)

        assert result == "no-expiry-token"
