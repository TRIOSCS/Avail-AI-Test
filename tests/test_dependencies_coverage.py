"""
test_dependencies_coverage.py — Additional coverage for app/dependencies.py

Covers uncovered lines:
- get_user exception path (lines 44-46)
- require_user: agent key auth, no user 401, deactivated 403 (lines 57, 60-63)
- require_admin 403 (lines 74-76)
- require_settings_access 403 (lines 82-84)
- require_buyer 403 (lines 89-92)
- get_req_for_user sales path (line 114)
- require_fresh_token full flow (lines 127-152)
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.dependencies import (
    get_req_for_user,
    get_user,
    require_admin,
    require_buyer,
    require_fresh_token,
    require_settings_access,
    require_user,
)
from app.models import Requisition, User


# ── Helpers ──────────────────────────────────────────────────────────


def _mock_request(session_data=None, headers=None):
    req = MagicMock()
    req.session = session_data or {}
    req.headers = headers or {}
    return req


# ── get_user exception handling (lines 44-46) ─────────────────────


class TestGetUserException:
    def test_db_exception_clears_session_returns_none(self, db_session, test_user):
        """When db.get raises an exception, session is cleared, returns None."""
        # Use a MagicMock for session so we can assert .clear() was called
        mock_session = MagicMock()
        mock_session.get.return_value = test_user.id
        request = MagicMock()
        request.session = mock_session
        request.headers = {}

        # Make db.get raise an exception
        mock_db = MagicMock()
        mock_db.get.side_effect = Exception("DB connection lost")

        user = get_user(request, mock_db)
        assert user is None
        mock_session.clear.assert_called_once()


# ── require_user (lines 57-63) ──────────────────────────────────────


class TestRequireUser:
    def test_no_user_no_agent_key_raises_401(self, db_session):
        """No session user and no agent key -> 401."""
        request = _mock_request({})
        with pytest.raises(HTTPException) as exc_info:
            require_user(request, db_session)
        assert exc_info.value.status_code == 401

    def test_agent_key_matches_returns_agent_user(self, db_session):
        """When agent key matches settings, returns the agent@availai.local user."""
        # Create agent user
        agent = User(
            email="agent@availai.local",
            name="Agent",
            role="admin",
            azure_id="agent-az",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(agent)
        db_session.commit()

        request = _mock_request({}, headers={"x-agent-key": "test-agent-key-123"})

        with patch("app.config.settings") as mock_settings:
            mock_settings.agent_api_key = "test-agent-key-123"
            user = require_user(request, db_session)
            assert user.email == "agent@availai.local"

    def test_agent_key_no_match_raises_401(self, db_session):
        """When agent key doesn't match, raises 401."""
        request = _mock_request({}, headers={"x-agent-key": "wrong-key"})

        with patch("app.config.settings") as mock_settings:
            mock_settings.agent_api_key = "correct-key"
            with pytest.raises(HTTPException) as exc_info:
                require_user(request, db_session)
            assert exc_info.value.status_code == 401

    def test_deactivated_user_raises_403(self, db_session):
        """Deactivated user raises 403, session is cleared."""
        user = User(
            email="deactivated@test.com",
            name="Deactivated",
            role="buyer",
            azure_id="az-deact",
            is_active=False,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.commit()

        request = _mock_request({"user_id": user.id})

        with pytest.raises(HTTPException) as exc_info:
            require_user(request, db_session)
        assert exc_info.value.status_code == 403
        assert "deactivated" in str(exc_info.value.detail).lower()


# ── require_admin (lines 74-76) ──────────────────────────────────────


class TestRequireAdmin:
    def test_non_admin_raises_403(self, db_session, test_user):
        """Non-admin user raises 403."""
        request = _mock_request({"user_id": test_user.id})
        with pytest.raises(HTTPException) as exc_info:
            require_admin(request, db_session)
        assert exc_info.value.status_code == 403
        assert "Admin" in str(exc_info.value.detail)

    def test_admin_user_passes(self, db_session, admin_user):
        """Admin user passes through."""
        request = _mock_request({"user_id": admin_user.id})
        user = require_admin(request, db_session)
        assert user.role == "admin"


# ── require_settings_access (lines 82-84) ──────────────────────────


class TestRequireSettingsAccess:
    def test_buyer_raises_403(self, db_session, test_user):
        """Buyer role should not have settings access."""
        request = _mock_request({"user_id": test_user.id})
        with pytest.raises(HTTPException) as exc_info:
            require_settings_access(request, db_session)
        assert exc_info.value.status_code == 403
        assert "Settings" in str(exc_info.value.detail)

    def test_admin_allowed(self, db_session, admin_user):
        """Admin has settings access."""
        request = _mock_request({"user_id": admin_user.id})
        user = require_settings_access(request, db_session)
        assert user.role == "admin"

    def test_dev_assistant_allowed(self, db_session):
        """dev_assistant has settings access."""
        dev = User(
            email="dev@trioscs.com",
            name="Dev",
            role="dev_assistant",
            azure_id="az-dev",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(dev)
        db_session.commit()

        request = _mock_request({"user_id": dev.id})
        user = require_settings_access(request, db_session)
        assert user.role == "dev_assistant"


# ── require_buyer (lines 89-92) ──────────────────────────────────────


class TestRequireBuyer:
    def test_sales_raises_403(self, db_session, sales_user):
        """Sales role is not allowed for buyer actions."""
        request = _mock_request({"user_id": sales_user.id})
        with pytest.raises(HTTPException) as exc_info:
            require_buyer(request, db_session)
        assert exc_info.value.status_code == 403
        assert "Buyer" in str(exc_info.value.detail)

    def test_buyer_allowed(self, db_session, test_user):
        """Buyer role is allowed."""
        request = _mock_request({"user_id": test_user.id})
        user = require_buyer(request, db_session)
        assert user.role == "buyer"

    def test_trader_allowed(self, db_session, trader_user):
        """Trader role is allowed for buyer actions."""
        request = _mock_request({"user_id": trader_user.id})
        user = require_buyer(request, db_session)
        assert user.role == "trader"

    def test_admin_allowed(self, db_session, admin_user):
        """Admin role is allowed for buyer actions."""
        request = _mock_request({"user_id": admin_user.id})
        user = require_buyer(request, db_session)
        assert user.role == "admin"

    def test_manager_allowed(self, db_session, manager_user):
        """Manager role is allowed for buyer actions."""
        request = _mock_request({"user_id": manager_user.id})
        user = require_buyer(request, db_session)
        assert user.role == "manager"

    def test_dev_assistant_raises_403(self, db_session):
        """dev_assistant is not allowed for buyer actions."""
        dev = User(
            email="dev3@test.com",
            name="Dev",
            role="dev_assistant",
            azure_id="az-dev3",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(dev)
        db_session.commit()

        request = _mock_request({"user_id": dev.id})
        with pytest.raises(HTTPException) as exc_info:
            require_buyer(request, db_session)
        assert exc_info.value.status_code == 403


# ── get_req_for_user — sales path (line 114) ────────────────────────


class TestGetReqForUserSales:
    def test_sales_sees_own_req(self, db_session, sales_user):
        """Sales user can see their own requisition."""
        req = Requisition(
            name="Sales REQ",
            status="open",
            created_by=sales_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()

        result = get_req_for_user(db_session, sales_user, req.id)
        assert result is not None
        assert result.id == req.id

    def test_sales_cannot_see_others_req(self, db_session, sales_user, test_requisition):
        """Sales user cannot see another user's requisition."""
        result = get_req_for_user(db_session, sales_user, test_requisition.id)
        assert result is None


# ── require_fresh_token (lines 127-152) ─────────────────────────────


class TestRequireFreshToken:
    @pytest.mark.asyncio
    async def test_no_user_raises_401(self, db_session):
        """No authenticated user raises 401."""
        request = _mock_request({})
        with pytest.raises(HTTPException) as exc_info:
            await require_fresh_token(request, db_session)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_no_token_raises_401(self, db_session, test_user):
        """User with no access_token raises 401."""
        test_user.access_token = None
        db_session.commit()

        request = _mock_request({"user_id": test_user.id})
        with pytest.raises(HTTPException) as exc_info:
            await require_fresh_token(request, db_session)
        assert exc_info.value.status_code == 401
        assert "No access token" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_valid_token_returned(self, db_session, test_user):
        """Valid, non-expired token is returned directly."""
        test_user.access_token = "valid-token-123"
        test_user.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        db_session.commit()

        request = _mock_request({"user_id": test_user.id})
        token = await require_fresh_token(request, db_session)
        assert token == "valid-token-123"

    @pytest.mark.asyncio
    async def test_near_expiry_with_refresh_token_refreshes(self, db_session, test_user):
        """Token near expiry with refresh_token triggers refresh."""
        test_user.access_token = "old-token"
        test_user.refresh_token = "refresh-token"
        test_user.token_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        db_session.commit()

        request = _mock_request({"user_id": test_user.id})

        with patch("app.scheduler.refresh_user_token", new_callable=AsyncMock) as mock_refresh:
            mock_refresh.return_value = "new-token"
            token = await require_fresh_token(request, db_session)
            assert token == "new-token"
            mock_refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_near_expiry_refresh_fails_raises_401(self, db_session, test_user):
        """When token refresh fails and no refresh token, raises 401."""
        test_user.access_token = "old-token"
        test_user.refresh_token = "refresh-token"
        test_user.token_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        db_session.commit()

        request = _mock_request({"user_id": test_user.id})

        with patch("app.scheduler.refresh_user_token", new_callable=AsyncMock) as mock_refresh:
            mock_refresh.return_value = None  # Refresh failed
            with pytest.raises(HTTPException) as exc_info:
                await require_fresh_token(request, db_session)
            assert exc_info.value.status_code == 401
            assert "expired" in str(exc_info.value.detail).lower()

    @pytest.mark.asyncio
    async def test_near_expiry_no_refresh_token_raises_401(self, db_session, test_user):
        """Near expiry with no refresh_token disconnects and raises 401."""
        test_user.access_token = "old-token"
        test_user.refresh_token = None
        test_user.token_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        db_session.commit()

        request = _mock_request({"user_id": test_user.id})

        with pytest.raises(HTTPException) as exc_info:
            await require_fresh_token(request, db_session)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_no_expiry_time_returns_token(self, db_session, test_user):
        """Token without expiry time is returned without refresh."""
        test_user.access_token = "token-no-expiry"
        test_user.token_expires_at = None
        db_session.commit()

        request = _mock_request({"user_id": test_user.id})
        token = await require_fresh_token(request, db_session)
        assert token == "token-no-expiry"
