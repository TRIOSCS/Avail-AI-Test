"""Tests for app/utils/token_manager.py — Azure AD token lifecycle management.

Covers:
- get_valid_token: valid token returns immediately, near-expiry triggers refresh,
  refresh failure returns None
- refresh_user_token: successful refresh, no refresh_token, refresh failure
- _refresh_access_token: success, HTTP error, network exception
- _utc: None, naive datetime, aware datetime
- Backward compat: scheduler.py still exports the functions
"""

import asyncio
import os

os.environ["TESTING"] = "1"

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.utils.token_manager import (
    _refresh_access_token,
    _utc,
    get_valid_token,
    refresh_user_token,
)


class TestUtc:
    def test_none(self):
        assert _utc(None) is None

    def test_naive_datetime(self):
        dt = datetime(2026, 1, 1, 12, 0, 0)
        result = _utc(dt)
        assert result.tzinfo == timezone.utc

    def test_aware_datetime(self):
        dt = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = _utc(dt)
        assert result is dt  # unchanged


class TestGetValidToken:
    @pytest.mark.asyncio
    async def test_valid_token_returns_immediately(self):
        user = MagicMock()
        user.access_token = "valid-token"
        user.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        db = MagicMock()

        result = await get_valid_token(user, db)
        assert result == "valid-token"
        db.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_near_expiry_triggers_refresh(self):
        user = MagicMock()
        user.access_token = "old-token"
        user.token_expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)
        user.refresh_token = "refresh-tok"
        user.email = "test@test.com"
        db = MagicMock()

        with patch(
            "app.utils.token_manager._refresh_access_token",
            new_callable=AsyncMock,
            return_value=("new-token", None),
        ):
            result = await refresh_user_token(user, db)

        assert result == "new-token"
        assert user.access_token == "new-token"

    @pytest.mark.asyncio
    async def test_refresh_failure_returns_none(self):
        user = MagicMock()
        user.access_token = "expired"
        user.token_expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        user.refresh_token = "refresh-tok"
        user.email = "test@test.com"
        db = MagicMock()

        with patch(
            "app.utils.token_manager._refresh_access_token",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await get_valid_token(user, db)

        assert result is None
        assert user.m365_error_reason == "Token refresh failed"


class TestRefreshUserToken:
    @pytest.mark.asyncio
    async def test_no_refresh_token(self):
        user = MagicMock()
        user.refresh_token = None
        db = MagicMock()

        result = await refresh_user_token(user, db)
        assert result is None

    @pytest.mark.asyncio
    async def test_refresh_success_with_new_refresh(self):
        user = MagicMock()
        user.refresh_token = "old-refresh"
        user.email = "test@test.com"
        db = MagicMock()

        with patch(
            "app.utils.token_manager._refresh_access_token",
            new_callable=AsyncMock,
            return_value=("new-access", "new-refresh"),
        ):
            result = await refresh_user_token(user, db)

        assert result == "new-access"
        assert user.access_token == "new-access"
        assert user.refresh_token == "new-refresh"
        assert user.m365_connected is True

    @pytest.mark.asyncio
    async def test_refresh_failure_disconnects(self):
        user = MagicMock()
        user.refresh_token = "bad-refresh"
        user.email = "test@test.com"
        db = MagicMock()

        with patch(
            "app.utils.token_manager._refresh_access_token",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await refresh_user_token(user, db)

        assert result is None
        assert user.m365_connected is False


class TestRefreshAccessToken:
    @pytest.mark.asyncio
    async def test_success(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "new-token",
            "refresh_token": "new-refresh",
        }

        with patch("app.utils.token_manager.http.post", new_callable=AsyncMock, return_value=mock_response):
            result = await _refresh_access_token("rt", "cid", "cs", "tid")

        assert result == ("new-token", "new-refresh")

    @pytest.mark.asyncio
    async def test_http_error(self):
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "Bad request"

        with patch("app.utils.token_manager.http.post", new_callable=AsyncMock, return_value=mock_response):
            result = await _refresh_access_token("rt", "cid", "cs", "tid")

        assert result is None

    @pytest.mark.asyncio
    async def test_network_exception(self):
        with patch("app.utils.token_manager.http.post", new_callable=AsyncMock, side_effect=Exception("timeout")):
            result = await _refresh_access_token("rt", "cid", "cs", "tid")

        assert result is None


class TestBackwardCompat:
    def test_scheduler_reexports(self):
        """scheduler.py still exports token functions for backward compatibility."""
        from app.scheduler import get_valid_token as gvt
        from app.scheduler import refresh_user_token as rut

        assert gvt is get_valid_token
        assert rut is refresh_user_token
