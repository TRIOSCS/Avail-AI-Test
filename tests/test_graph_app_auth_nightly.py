"""test_graph_app_auth_nightly.py — Coverage boost for app/services/graph_app_auth.py.

Targets missing lines: cached-token early return, missing-settings None return, HTTP
error/bad-status/no-token None returns, token + cache update on success, cache expiry
refetch.

Called by: pytest
Depends on: tests/conftest.py
"""

from __future__ import annotations

import os
import time

os.environ["TESTING"] = "1"
os.environ["RATE_LIMIT_ENABLED"] = "false"

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.services.graph_app_auth as gaa


@pytest.fixture(autouse=True)
def clear_token_cache():
    """Clear the module-level token cache before and after every test."""
    gaa._TOKEN_CACHE.clear()
    yield
    gaa._TOKEN_CACHE.clear()


# ── cached token ──────────────────────────────────────────────────────


async def test_returns_cached_token():
    """When a fresh token exists in the cache, http.post is NOT called."""
    gaa._TOKEN_CACHE["token"] = "cached-token-abc"
    gaa._TOKEN_CACHE["expires_at"] = time.monotonic() + 3600  # far future

    with patch("app.services.graph_app_auth.http") as mock_http:
        result = await gaa.get_app_graph_token()

    assert result == "cached-token-abc"
    mock_http.post.assert_not_called()


# ── missing settings ──────────────────────────────────────────────────


async def test_returns_none_missing_settings():
    """No azure_client_id configured → returns None immediately."""
    with (
        patch.object(gaa.settings, "azure_client_id", ""),
        patch.object(gaa.settings, "azure_client_secret", "sec"),
        patch.object(gaa.settings, "azure_tenant_id", "tid"),
    ):
        result = await gaa.get_app_graph_token()
    assert result is None


async def test_returns_none_all_settings_missing():
    """No Azure settings at all → returns None."""
    with (
        patch.object(gaa.settings, "azure_client_id", ""),
        patch.object(gaa.settings, "azure_client_secret", ""),
        patch.object(gaa.settings, "azure_tenant_id", ""),
    ):
        result = await gaa.get_app_graph_token()
    assert result is None


# ── HTTP errors ───────────────────────────────────────────────────────


async def test_returns_none_on_http_exception():
    """http.post raises an exception → returns None (no crash)."""
    with (
        patch.object(gaa.settings, "azure_client_id", "cid"),
        patch.object(gaa.settings, "azure_client_secret", "sec"),
        patch.object(gaa.settings, "azure_tenant_id", "tid"),
        patch("app.services.graph_app_auth.http") as mock_http,
    ):
        mock_http.post = AsyncMock(side_effect=ConnectionError("network down"))
        result = await gaa.get_app_graph_token()
    assert result is None


async def test_returns_none_on_bad_status():
    """http.post returns status 400 → returns None."""
    resp = MagicMock(status_code=400, text="Bad Request")
    with (
        patch.object(gaa.settings, "azure_client_id", "cid"),
        patch.object(gaa.settings, "azure_client_secret", "sec"),
        patch.object(gaa.settings, "azure_tenant_id", "tid"),
        patch("app.services.graph_app_auth.http") as mock_http,
    ):
        mock_http.post = AsyncMock(return_value=resp)
        result = await gaa.get_app_graph_token()
    assert result is None


async def test_returns_none_when_no_access_token_in_response():
    """Status 200 but body has no 'access_token' key → returns None."""
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"token_type": "Bearer"}  # no access_token
    with (
        patch.object(gaa.settings, "azure_client_id", "cid"),
        patch.object(gaa.settings, "azure_client_secret", "sec"),
        patch.object(gaa.settings, "azure_tenant_id", "tid"),
        patch("app.services.graph_app_auth.http") as mock_http,
    ):
        mock_http.post = AsyncMock(return_value=resp)
        result = await gaa.get_app_graph_token()
    assert result is None
    assert "token" not in gaa._TOKEN_CACHE


# ── success path + cache update ───────────────────────────────────────


async def test_returns_token_and_populates_cache():
    """Successful token fetch → token returned and _TOKEN_CACHE updated."""
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"access_token": "FRESHTOKEN", "expires_in": 3600}
    with (
        patch.object(gaa.settings, "azure_client_id", "cid"),
        patch.object(gaa.settings, "azure_client_secret", "sec"),
        patch.object(gaa.settings, "azure_tenant_id", "tid"),
        patch("app.services.graph_app_auth.http") as mock_http,
    ):
        mock_http.post = AsyncMock(return_value=resp)
        result = await gaa.get_app_graph_token()

    assert result == "FRESHTOKEN"
    assert gaa._TOKEN_CACHE["token"] == "FRESHTOKEN"
    assert float(gaa._TOKEN_CACHE["expires_at"]) > time.monotonic()


# ── expired cache refetch ─────────────────────────────────────────────


async def test_cache_expired_refetches_token():
    """Expired token in cache → new HTTP request is made."""
    # Seed with expired token (expires_at in the past)
    gaa._TOKEN_CACHE["token"] = "old-token"
    gaa._TOKEN_CACHE["expires_at"] = time.monotonic() - 1  # already expired

    resp = MagicMock(status_code=200)
    resp.json.return_value = {"access_token": "NEW-TOKEN", "expires_in": 3600}
    with (
        patch.object(gaa.settings, "azure_client_id", "cid"),
        patch.object(gaa.settings, "azure_client_secret", "sec"),
        patch.object(gaa.settings, "azure_tenant_id", "tid"),
        patch("app.services.graph_app_auth.http") as mock_http,
    ):
        mock_http.post = AsyncMock(return_value=resp)
        result = await gaa.get_app_graph_token()

    assert result == "NEW-TOKEN"
    assert mock_http.post.call_count == 1
