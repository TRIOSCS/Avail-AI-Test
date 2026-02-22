"""
test_http_client.py — Tests for app/http_client.py

Covers uncovered lines:
- close_clients when RuntimeError occurs (lines 40-41, 44-45)
- Client configuration verification
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.http_client import close_clients, http, http_redirect


# ── Client configuration ──────────────────────────────────────────────


class TestClientConfig:
    def test_http_client_exists(self):
        """http client is an AsyncClient instance."""
        import httpx
        assert isinstance(http, httpx.AsyncClient)

    def test_http_redirect_client_exists(self):
        """http_redirect client is an AsyncClient instance."""
        import httpx
        assert isinstance(http_redirect, httpx.AsyncClient)

    def test_http_no_redirects(self):
        """Default client does NOT follow redirects."""
        assert http.follow_redirects is False

    def test_http_redirect_follows_redirects(self):
        """Redirect client follows redirects."""
        assert http_redirect.follow_redirects is True


# ── close_clients ─────────────────────────────────────────────────────


class TestCloseClients:
    @pytest.mark.asyncio
    async def test_close_clients_success(self):
        """close_clients calls aclose on both clients."""
        with patch.object(http, "aclose", new_callable=AsyncMock) as mock_http_close, \
             patch.object(http_redirect, "aclose", new_callable=AsyncMock) as mock_redir_close:
            await close_clients()
            mock_http_close.assert_called_once()
            mock_redir_close.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_clients_runtime_error_http(self):
        """RuntimeError from http.aclose is caught (lines 40-41)."""
        with patch.object(http, "aclose", new_callable=AsyncMock, side_effect=RuntimeError("already closed")), \
             patch.object(http_redirect, "aclose", new_callable=AsyncMock) as mock_redir_close:
            await close_clients()  # Should not raise
            mock_redir_close.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_clients_runtime_error_redirect(self):
        """RuntimeError from http_redirect.aclose is caught (lines 44-45)."""
        with patch.object(http, "aclose", new_callable=AsyncMock) as mock_http_close, \
             patch.object(http_redirect, "aclose", new_callable=AsyncMock, side_effect=RuntimeError("already closed")):
            await close_clients()  # Should not raise
            mock_http_close.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_clients_both_runtime_error(self):
        """Both clients raising RuntimeError is handled."""
        with patch.object(http, "aclose", new_callable=AsyncMock, side_effect=RuntimeError), \
             patch.object(http_redirect, "aclose", new_callable=AsyncMock, side_effect=RuntimeError):
            await close_clients()  # Should not raise
