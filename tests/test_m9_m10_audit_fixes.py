"""Tests for M9 (LIKE escape) and M10 (enrichment error classification).

M9: Verify that the sightings router escapes LIKE wildcards in user input.
M10: Verify that enrichment connector errors are classified by type.

Called by: pytest
Depends on: app.utils.sql_helpers, app.services.enrichment
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.utils.sql_helpers import escape_like

# ── M9: LIKE Pattern Injection Escape ──────────────────────────────────


class TestLikeEscapeIntegration:
    """Verify escape_like is applied to user-facing search inputs."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("100%match", r"100\%match"),
            ("LM_358", r"LM\_358"),
            ("100%_test", r"100\%\_test"),
        ],
        ids=[
            "percent_in_sales_person",
            "underscore_in_search_query",
            "combined_wildcards_in_tag_search",
        ],
    )
    def test_wildcards_escaped(self, raw, expected):
        """User-facing search inputs must have LIKE wildcards escaped."""
        assert escape_like(raw) == expected

    @pytest.mark.parametrize("module_name", ["sightings"])
    def test_router_imports_escape_like(self, module_name):
        """sightings.py must import escape_like."""
        import importlib

        router_module = importlib.import_module(f"app.routers.{module_name}")
        assert hasattr(router_module, "escape_like")


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
            mock_connector.search = AsyncMock(side_effect=TimeoutError())
            mock_module.FakeConnector.return_value = mock_connector
            mock_importlib.import_module.return_value = mock_module

            # asyncio.wait_for will raise TimeoutError from the connector
            result = await mod._try_connector_config(config, "LM358N")

        assert result is None
        mock_logger.warning.assert_called_once()
        assert "timed out" in mock_logger.warning.call_args[0][0]

    @pytest.mark.parametrize(
        ("exc_message", "log_level", "expected_substr"),
        [
            ("HTTP 401 Unauthorized", "error", "auth failure"),
            ("HTTP 403 Forbidden", "error", "auth failure"),
            ("HTTP 429 Too Many Requests", "warning", "rate limited"),
        ],
        ids=["auth_401", "auth_403", "rate_limit_429"],
    )
    @pytest.mark.asyncio()
    async def test_classified_error_log_level_and_message(self, exc_message, log_level, expected_substr):
        """401/403 log at ERROR (auth failure); 429 logs a rate-limited WARNING."""
        import app.services.enrichment as mod

        config = _make_config()
        mock_logger = MagicMock()

        with (
            patch.object(mod, "get_credential_cached", return_value="fake-key"),
            patch.object(mod, "importlib") as mock_importlib,
            patch.object(mod, "logger", mock_logger),
            patch.object(mod.asyncio, "wait_for", side_effect=Exception(exc_message)),
        ):
            mock_importlib.import_module.return_value = MagicMock()

            result = await mod._try_connector_config(config, "LM358N")

        assert result is None
        log_method = getattr(mock_logger, log_level)
        log_method.assert_called_once()
        assert expected_substr in log_method.call_args[0][0]

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
            mock_importlib.import_module.return_value = MagicMock()

            result = await mod._try_connector_config(config, "LM358N")

        assert result is None
        mock_logger.warning.assert_called_once()
        assert mock_logger.warning.call_args[1].get("exc_info") is True
