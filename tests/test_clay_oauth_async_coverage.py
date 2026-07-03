"""tests/test_clay_oauth_async_coverage.py — Async-function coverage for clay_oauth service.

Covers: register_client, _persist_tokens, exchange_code, refresh, get_access_token.

Called by: pytest (asyncio_mode=auto — no @pytest.mark.asyncio needed)
Depends on: conftest.py, app.services.clay_oauth
"""

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("TESTING", "1")


class TestRegisterClient:
    async def test_returns_existing_client_id(self):
        """register_client() returns stored client_id without HTTP call."""
        from app.services.clay_oauth import register_client

        with patch("app.services.clay_oauth._load", return_value="cid-existing") as mock_load:
            result = await register_client()

        assert result == "cid-existing"
        mock_load.assert_called_once_with("CLAY_OAUTH_CLIENT_ID")

    async def test_registers_new_client_via_http(self):
        """register_client() POSTs to CLAY_REGISTER_URL and stores returned client_id."""
        from app.services.clay_oauth import register_client

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"client_id": "cid-new"}

        with (
            patch("app.services.clay_oauth._load", return_value=None),
            patch("app.services.clay_oauth.http") as mock_http,
            patch("app.services.clay_oauth._store") as mock_store,
        ):
            mock_http.post = AsyncMock(return_value=mock_resp)
            result = await register_client()

        assert result == "cid-new"
        mock_store.assert_called_once_with({"CLAY_OAUTH_CLIENT_ID": "cid-new"})

    async def test_raises_on_http_error(self):
        """register_client() raises RuntimeError when registration HTTP call fails."""
        from app.services.clay_oauth import register_client

        mock_resp = MagicMock()
        mock_resp.status_code = 400

        with (
            patch("app.services.clay_oauth._load", return_value=None),
            patch("app.services.clay_oauth.http") as mock_http,
        ):
            mock_http.post = AsyncMock(return_value=mock_resp)
            with pytest.raises(RuntimeError, match="Clay client registration failed"):
                await register_client()

    async def test_raises_when_response_missing_client_id(self):
        """register_client() raises RuntimeError when DCR response omits client_id."""
        from app.services.clay_oauth import register_client

        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {}

        with (
            patch("app.services.clay_oauth._load", return_value=None),
            patch("app.services.clay_oauth.http") as mock_http,
        ):
            mock_http.post = AsyncMock(return_value=mock_resp)
            with pytest.raises(RuntimeError, match="missing client_id"):
                await register_client()


class TestPersistTokens:
    def test_skips_when_no_access_token(self):
        """_persist_tokens() does nothing when access_token is absent from dict."""
        from app.services.clay_oauth import _persist_tokens

        with patch("app.services.clay_oauth._store") as mock_store:
            _persist_tokens({})

        mock_store.assert_not_called()

    def test_stores_tokens_with_refresh_token(self):
        """_persist_tokens() includes CLAY_OAUTH_REFRESH_TOKEN when provided."""
        from app.services.clay_oauth import _persist_tokens

        tok = {"access_token": "at-abc", "refresh_token": "rt-xyz", "expires_in": 3600}

        with patch("app.services.clay_oauth._store") as mock_store:
            _persist_tokens(tok)

        mock_store.assert_called_once()
        updates = mock_store.call_args[0][0]
        assert updates["CLAY_OAUTH_ACCESS_TOKEN"] == "at-abc"
        assert updates["CLAY_OAUTH_REFRESH_TOKEN"] == "rt-xyz"
        assert updates["CLAY_OAUTH_NEEDS_RECONNECT"] is None
        assert "CLAY_OAUTH_EXPIRES_AT" in updates

    def test_stores_tokens_without_refresh_token(self):
        """_persist_tokens() omits CLAY_OAUTH_REFRESH_TOKEN when not in response (rotation-aware)."""
        from app.services.clay_oauth import _persist_tokens

        tok = {"access_token": "at-abc", "expires_in": 3600}

        with patch("app.services.clay_oauth._store") as mock_store:
            _persist_tokens(tok)

        updates = mock_store.call_args[0][0]
        assert updates["CLAY_OAUTH_ACCESS_TOKEN"] == "at-abc"
        assert "CLAY_OAUTH_REFRESH_TOKEN" not in updates
        assert "CLAY_OAUTH_EXPIRES_AT" in updates


class TestExchangeCode:
    async def test_returns_true_on_success(self):
        """exchange_code() persists tokens and returns True on 200 response."""
        from app.services.clay_oauth import exchange_code

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "at-xyz"}

        with (
            patch("app.services.clay_oauth.http") as mock_http,
            patch("app.services.clay_oauth._persist_tokens") as mock_persist,
        ):
            mock_http.post = AsyncMock(return_value=mock_resp)
            result = await exchange_code("code-abc", "verifier-def", "cid-123")

        assert result is True
        mock_persist.assert_called_once_with({"access_token": "at-xyz"})

    async def test_returns_false_on_http_error(self):
        """exchange_code() returns False when HTTP call fails."""
        from app.services.clay_oauth import exchange_code

        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.json.return_value = {}

        with patch("app.services.clay_oauth.http") as mock_http:
            mock_http.post = AsyncMock(return_value=mock_resp)
            result = await exchange_code("code-bad", "verifier-bad", "cid-123")

        assert result is False


class TestRefresh:
    async def test_returns_none_when_no_refresh_token(self):
        """refresh() returns None immediately when CLAY_OAUTH_REFRESH_TOKEN is absent."""
        from app.services.clay_oauth import refresh

        with patch("app.services.clay_oauth._load", return_value=None):
            result = await refresh()

        assert result is None

    async def test_returns_none_and_sets_needs_reconnect_on_http_failure(self):
        """refresh() marks needs_reconnect and returns None on HTTP failure."""
        from app.services.clay_oauth import refresh

        def _load_side(key):
            return {"CLAY_OAUTH_REFRESH_TOKEN": "rt-token", "CLAY_OAUTH_CLIENT_ID": "cid-123"}.get(key)

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.json.return_value = {}

        with (
            patch("app.services.clay_oauth._load", side_effect=_load_side),
            patch("app.services.clay_oauth.http") as mock_http,
            patch("app.services.clay_oauth._store") as mock_store,
        ):
            mock_http.post = AsyncMock(return_value=mock_resp)
            result = await refresh()

        assert result is None
        mock_store.assert_called_once()
        updates = mock_store.call_args[0][0]
        assert updates["CLAY_OAUTH_NEEDS_RECONNECT"] == "1"
        assert updates["CLAY_OAUTH_ACCESS_TOKEN"] is None

    async def test_returns_access_token_on_success(self):
        """refresh() persists new tokens and returns the new access token."""
        from app.services.clay_oauth import refresh

        def _load_side(key):
            return {
                "CLAY_OAUTH_REFRESH_TOKEN": "rt-token",
                "CLAY_OAUTH_CLIENT_ID": "cid-123",
                "CLAY_OAUTH_ACCESS_TOKEN": "new-at",
            }.get(key)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "new-at"}

        with (
            patch("app.services.clay_oauth._load", side_effect=_load_side),
            patch("app.services.clay_oauth.http") as mock_http,
            patch("app.services.clay_oauth._persist_tokens"),
        ):
            mock_http.post = AsyncMock(return_value=mock_resp)
            result = await refresh()

        assert result == "new-at"


class TestGetAccessToken:
    async def test_returns_none_when_no_token_and_no_refresh_token(self):
        """get_access_token() returns None when both access token and refresh token are absent."""
        from app.services.clay_oauth import get_access_token

        with patch("app.services.clay_oauth._load", return_value=None):
            result = await get_access_token()

        assert result is None

    async def test_refreshes_when_token_is_expired(self):
        """get_access_token() calls refresh() when stored token is past expiry buffer."""
        from app.services.clay_oauth import get_access_token

        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

        def _load_side(key):
            return {"CLAY_OAUTH_ACCESS_TOKEN": "old-at", "CLAY_OAUTH_EXPIRES_AT": past}.get(key)

        with (
            patch("app.services.clay_oauth._load", side_effect=_load_side),
            patch("app.services.clay_oauth.refresh", new_callable=AsyncMock, return_value="new-at") as mock_refresh,
        ):
            result = await get_access_token()

        assert result == "new-at"
        mock_refresh.assert_called_once()

    async def test_returns_token_when_still_valid(self):
        """get_access_token() returns stored token directly when expiry is in the future."""
        from app.services.clay_oauth import get_access_token

        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

        def _load_side(key):
            return {"CLAY_OAUTH_ACCESS_TOKEN": "valid-at", "CLAY_OAUTH_EXPIRES_AT": future}.get(key)

        with patch("app.services.clay_oauth._load", side_effect=_load_side):
            result = await get_access_token()

        assert result == "valid-at"
