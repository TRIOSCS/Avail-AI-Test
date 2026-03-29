"""Tests for M9 (LIKE escape) and M10 (enrichment error classification).

M9: Verify that sightings and tags routers escape LIKE wildcards in user input.
M10: Verify that enrichment connector errors are classified by type.

Called by: pytest
Depends on: app.utils.sql_helpers, app.services.enrichment
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.utils.sql_helpers import escape_like

# ── M9: LIKE Pattern Injection Escape ──────────────────────────────────


class TestLikeEscapeIntegration:
    """Verify escape_like is applied to user-facing search inputs."""

    def test_percent_in_sales_person_escaped(self):
        """Percent wildcards in sales_person filter must be escaped."""
        raw = "100%match"
        escaped = escape_like(raw)
        assert "%" not in escaped or r"\%" in escaped
        assert escaped == r"100\%match"

    def test_underscore_in_search_query_escaped(self):
        """Underscore wildcards in q filter must be escaped."""
        raw = "LM_358"
        escaped = escape_like(raw)
        assert escaped == r"LM\_358"

    def test_combined_wildcards_in_tag_search(self):
        """Combined wildcards in tag search must all be escaped."""
        raw = "100%_test"
        escaped = escape_like(raw)
        assert escaped == r"100\%\_test"

    def test_sightings_router_imports_escape_like(self):
        """sightings.py must import escape_like."""
        from app.routers import sightings

        assert hasattr(sightings, "escape_like")

    def test_tags_router_imports_escape_like(self):
        """tags.py must import escape_like."""
        from app.routers import tags

        assert hasattr(tags, "escape_like")


# ── M10: Enrichment Connector Error Classification ─────────────────────


def _make_config():
    """Minimal connector config for testing."""
    return {
        "name": "TestConnector",
        "module": "app.connectors.test_fake",
        "class": "FakeConnector",
        "creds": [("test_source", "TEST_API_KEY")],
        "confidence": 0.8,
    }


class TestEnrichmentErrorClassification:
    """Verify different error types produce different log levels/messages."""

    @pytest.mark.asyncio()
    async def test_timeout_logs_warning(self):
        """TimeoutError should log a 'timed out' warning."""
        import app.services.enrichment as mod

        config = _make_config()
        mock_logger = MagicMock()
        orig_logger = mod.logger

        with (
            patch.object(mod, "get_credential_cached", return_value="fake-key"),
            patch.object(mod, "importlib") as mock_importlib,
            patch.object(mod, "logger", mock_logger),
        ):
            mock_module = MagicMock()
            mock_connector = MagicMock()
            mock_connector.search = AsyncMock(side_effect=asyncio.TimeoutError())
            mock_module.FakeConnector.return_value = mock_connector
            mock_importlib.import_module.return_value = mock_module

            # asyncio.wait_for will raise TimeoutError from the connector
            result = await mod._try_connector_config(config, "LM358N")

        assert result is None
        mock_logger.warning.assert_called_once()
        assert "timed out" in mock_logger.warning.call_args[0][0]

    @pytest.mark.asyncio()
    async def test_auth_401_logs_error(self):
        """401 errors should log at ERROR level with auth failure message."""
        import app.services.enrichment as mod

        config = _make_config()
        mock_logger = MagicMock()

        with (
            patch.object(mod, "get_credential_cached", return_value="fake-key"),
            patch.object(mod, "importlib") as mock_importlib,
            patch.object(mod, "logger", mock_logger),
            patch.object(mod.asyncio, "wait_for", side_effect=Exception("HTTP 401 Unauthorized")),
        ):
            mock_module = MagicMock()
            mock_importlib.import_module.return_value = mock_module

            result = await mod._try_connector_config(config, "LM358N")

        assert result is None
        mock_logger.error.assert_called_once()
        assert "auth failure" in mock_logger.error.call_args[0][0]

    @pytest.mark.asyncio()
    async def test_auth_403_logs_error(self):
        """403 Forbidden should also be classified as auth failure."""
        import app.services.enrichment as mod

        config = _make_config()
        mock_logger = MagicMock()

        with (
            patch.object(mod, "get_credential_cached", return_value="fake-key"),
            patch.object(mod, "importlib") as mock_importlib,
            patch.object(mod, "logger", mock_logger),
            patch.object(mod.asyncio, "wait_for", side_effect=Exception("HTTP 403 Forbidden")),
        ):
            mock_module = MagicMock()
            mock_importlib.import_module.return_value = mock_module

            result = await mod._try_connector_config(config, "LM358N")

        assert result is None
        mock_logger.error.assert_called_once()
        assert "auth failure" in mock_logger.error.call_args[0][0]

    @pytest.mark.asyncio()
    async def test_rate_limit_logs_warning(self):
        """429 rate-limit errors should log a warning with rate-limited message."""
        import app.services.enrichment as mod

        config = _make_config()
        mock_logger = MagicMock()

        with (
            patch.object(mod, "get_credential_cached", return_value="fake-key"),
            patch.object(mod, "importlib") as mock_importlib,
            patch.object(mod, "logger", mock_logger),
            patch.object(mod.asyncio, "wait_for", side_effect=Exception("HTTP 429 Too Many Requests")),
        ):
            mock_module = MagicMock()
            mock_importlib.import_module.return_value = mock_module

            result = await mod._try_connector_config(config, "LM358N")

        assert result is None
        mock_logger.warning.assert_called_once()
        assert "rate limited" in mock_logger.warning.call_args[0][0]

    @pytest.mark.asyncio()
    async def test_generic_error_logs_warning_with_exc_info(self):
        """Generic errors should log a warning with exc_info=True."""
        import app.services.enrichment as mod

        config = _make_config()
        mock_logger = MagicMock()

        with (
            patch.object(mod, "get_credential_cached", return_value="fake-key"),
            patch.object(mod, "importlib") as mock_importlib,
            patch.object(mod, "logger", mock_logger),
            patch.object(mod.asyncio, "wait_for", side_effect=Exception("Connection refused")),
        ):
            mock_module = MagicMock()
            mock_importlib.import_module.return_value = mock_module

            result = await mod._try_connector_config(config, "LM358N")

        assert result is None
        mock_logger.warning.assert_called_once()
        call_kwargs = mock_logger.warning.call_args
        assert call_kwargs[1].get("exc_info") is True
