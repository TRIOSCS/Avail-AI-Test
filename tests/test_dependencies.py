"""test_dependencies.py — Tests for shared FastAPI dependencies.

Tests auth functions, role-based access, query helpers, and token management.
Uses in-memory SQLite via conftest fixtures.

Called by: pytest
Depends on: app/dependencies.py, conftest.py
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.dependencies import (
    get_req_for_user,
    get_user,
    is_admin,
    require_fresh_token,
)

# ── Helpers ─────────────────────────────────────────────────────────


def _mock_request(session_data=None):
    req = MagicMock()
    req.session = session_data or {}
    return req


# ── get_user ────────────────────────────────────────────────────────


class TestGetUser:
    def test_returns_user_when_session_has_id(self, db_session, test_user):
        request = _mock_request({"user_id": test_user.id})
        user = get_user(request, db_session)
        assert user is not None
        assert user.id == test_user.id

    def test_returns_none_when_no_session(self, db_session):
        request = _mock_request({})
        user = get_user(request, db_session)
        assert user is None

    def test_returns_none_when_user_not_found(self, db_session):
        request = _mock_request({"user_id": 99999})
        user = get_user(request, db_session)
        assert user is None


# ── is_admin ────────────────────────────────────────────────────────


class TestIsAdmin:
    def test_admin_role(self, admin_user):
        assert is_admin(admin_user) is True

    def test_buyer_role(self, test_user):
        assert is_admin(test_user) is False


class TestGetReqForUser:
    def test_buyer_can_get_any(self, db_session, test_user, test_requisition):
        req = get_req_for_user(db_session, test_user, test_requisition.id)
        assert req is not None
        assert req.id == test_requisition.id

    def test_nonexistent_req(self, db_session, test_user):
        with pytest.raises(HTTPException) as exc_info:
            get_req_for_user(db_session, test_user, 99999)
        assert exc_info.value.status_code == 404


# ── require_fresh_token ─────────────────────────────────────────────


class TestRequireFreshToken:
    """Tests for the non-blocking token validation dependency."""

    async def test_returns_token_when_not_near_expiry(self, db_session, test_user):
        """Token far from expiry is returned directly."""
        test_user.access_token = "valid-token"
        test_user.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        db_session.commit()

        request = _mock_request({"user_id": test_user.id})
        token = await require_fresh_token(request, db_session)
        assert token == "valid-token"

    async def test_returns_token_when_within_buffer_not_expired(self, db_session, test_user):
        """Token within 15-min buffer but NOT expired → return current token (no inline
        refresh)."""
        test_user.access_token = "buffer-token"
        # 10 minutes from now — within buffer but not expired
        test_user.token_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        db_session.commit()

        request = _mock_request({"user_id": test_user.id})
        token = await require_fresh_token(request, db_session)
        assert token == "buffer-token"

    async def test_raises_401_when_truly_expired(self, db_session, test_user):
        """Token past expiry → 401, m365_connected set to False."""
        from fastapi import HTTPException

        test_user.access_token = "expired-token"
        test_user.token_expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        test_user.m365_connected = True
        db_session.commit()

        request = _mock_request({"user_id": test_user.id})
        with pytest.raises(HTTPException) as exc_info:
            await require_fresh_token(request, db_session)
        assert exc_info.value.status_code == 401
        db_session.refresh(test_user)
        assert test_user.m365_connected is False

    async def test_raises_401_when_no_access_token(self, db_session, test_user):
        """User with no access_token → 401."""
        from fastapi import HTTPException

        test_user.access_token = None
        db_session.commit()

        request = _mock_request({"user_id": test_user.id})
        with pytest.raises(HTTPException) as exc_info:
            await require_fresh_token(request, db_session)
        assert exc_info.value.status_code == 401

    async def test_raises_401_when_no_session(self, db_session):
        """No session → 401."""
        from fastapi import HTTPException

        request = _mock_request({})
        with pytest.raises(HTTPException) as exc_info:
            await require_fresh_token(request, db_session)
        assert exc_info.value.status_code == 401

    async def test_returns_token_when_no_expiry_set(self, db_session, test_user):
        """Token with no token_expires_at → returned without refresh check."""
        test_user.access_token = "no-expiry-token"
        test_user.token_expires_at = None
        db_session.commit()

        request = _mock_request({"user_id": test_user.id})
        token = await require_fresh_token(request, db_session)
        assert token == "no-expiry-token"
