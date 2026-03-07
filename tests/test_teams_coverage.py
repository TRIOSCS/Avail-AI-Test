"""
test_teams_coverage.py -- Additional coverage tests for teams.py

Targets missing lines: 58-70, 227, 229, 234, 268, 270, 275, 309, 319, 358-377
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Pre-import to ensure modules are in sys.modules before patching
import app.database  # noqa: F401
import app.services.admin_service  # noqa: F401
from app.services.teams import (
    _get_system_token,
    _get_teams_config,
    _mark_posted,
    clear_rate_limits,
    send_competitive_quote_alert,
    send_ownership_warning,
    send_stock_match_alert,
)  # _get_teams_config still tested directly in TestGetTeamsConfigDB


@pytest.fixture(autouse=True)
def _clear():
    clear_rate_limits()
    yield
    clear_rate_limits()


# =====================================================================
#  _get_teams_config -- DB override path (lines 58-70)
# =====================================================================


class TestGetTeamsConfigDB:
    def test_db_overrides_channel_and_team(self):
        """Lines 58-70: DB config overrides env settings."""
        mock_db = MagicMock()
        mock_cfg = {
            "teams_channel_id": "db-ch-1",
            "teams_team_id": "db-team-1",
            "teams_enabled": "true",
        }
        mock_settings = MagicMock()
        mock_settings.teams_channel_id = "env-ch"
        mock_settings.teams_team_id = "env-team"

        with (
            patch("app.config.settings", mock_settings),
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.services.admin_service.get_config_values", return_value=mock_cfg),
        ):
            ch, team, enabled = _get_teams_config()
        assert ch == "db-ch-1"
        assert team == "db-team-1"
        assert enabled is True
        mock_db.close.assert_called_once()

    def test_db_enabled_false(self):
        """Line 68: teams_enabled = false."""
        mock_db = MagicMock()
        mock_cfg = {
            "teams_channel_id": "ch-1",
            "teams_team_id": "team-1",
            "teams_enabled": "false",
        }
        mock_settings = MagicMock()
        mock_settings.teams_channel_id = ""
        mock_settings.teams_team_id = ""

        with (
            patch("app.config.settings", mock_settings),
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.services.admin_service.get_config_values", return_value=mock_cfg),
        ):
            ch, team, enabled = _get_teams_config()
        assert enabled is False

    def test_db_exception_falls_back_to_env(self):
        """Line 70: DB exception falls back to env settings."""
        mock_settings = MagicMock()
        mock_settings.teams_channel_id = "env-ch"
        mock_settings.teams_team_id = "env-team"

        with (
            patch("app.config.settings", mock_settings),
            patch("app.database.SessionLocal", side_effect=Exception("DB down")),
        ):
            ch, team, enabled = _get_teams_config()
        assert ch == "env-ch"
        assert team == "env-team"
        assert enabled is True

    def test_db_partial_config(self):
        """Lines 64-66: only some DB values present."""
        mock_db = MagicMock()
        mock_cfg = {"teams_channel_id": "db-ch-2"}
        mock_settings = MagicMock()
        mock_settings.teams_channel_id = "env-ch"
        mock_settings.teams_team_id = "env-team"

        with (
            patch("app.config.settings", mock_settings),
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.services.admin_service.get_config_values", return_value=mock_cfg),
        ):
            ch, team, enabled = _get_teams_config()
        assert ch == "db-ch-2"
        assert team == "env-team"


# =====================================================================
#  send_competitive_quote_alert -- Lines 227, 229, 234
# =====================================================================


class TestCompetitiveQuoteAlertCoverage:
    @pytest.mark.asyncio
    async def test_disabled_returns_false(self):
        with patch("app.services.teams._get_channel_for_event", return_value=("", "", False)):
            result = await send_competitive_quote_alert(
                offer_id=1,
                mpn="LM317T",
                vendor_name="Arrow",
                offer_price=0.30,
                best_price=0.50,
                requisition_id=10,
            )
        assert result is False

    @pytest.mark.asyncio
    async def test_rate_limited_returns_false(self):
        _mark_posted("competitive_quote", 99)
        with patch("app.services.teams._get_channel_for_event", return_value=("ch", "team", True)):
            result = await send_competitive_quote_alert(
                offer_id=99,
                mpn="LM317T",
                vendor_name="Arrow",
                offer_price=0.30,
                best_price=0.50,
                requisition_id=10,
            )
        assert result is False

    @pytest.mark.asyncio
    async def test_no_token_returns_false(self):
        with (
            patch("app.services.teams._get_channel_for_event", return_value=("ch", "team", True)),
            patch("app.services.teams._get_system_token", new_callable=AsyncMock, return_value=None),
        ):
            result = await send_competitive_quote_alert(
                offer_id=50,
                mpn="LM317T",
                vendor_name="Arrow",
                offer_price=0.30,
                best_price=0.50,
                requisition_id=10,
            )
        assert result is False

    @pytest.mark.asyncio
    async def test_zero_best_price(self):
        with (
            patch("app.services.teams._get_channel_for_event", return_value=("ch", "team", True)),
            patch("app.services.teams._get_system_token", new_callable=AsyncMock, return_value="tok"),
            patch("app.services.teams.post_to_channel", new_callable=AsyncMock, return_value=True),
        ):
            result = await send_competitive_quote_alert(
                offer_id=51,
                mpn="LM317T",
                vendor_name="Arrow",
                offer_price=0.30,
                best_price=0.0,
                requisition_id=10,
            )
        assert result is True


# =====================================================================
#  send_ownership_warning -- Lines 268, 270, 275
# =====================================================================


class TestOwnershipWarningCoverage:
    @pytest.mark.asyncio
    async def test_disabled_returns_false(self):
        with patch("app.services.teams._get_channel_for_event", return_value=("", "", False)):
            result = await send_ownership_warning(
                company_id=1,
                company_name="Acme",
                owner_name="John",
                days_remaining=7,
            )
        assert result is False

    @pytest.mark.asyncio
    async def test_rate_limited_returns_false(self):
        _mark_posted("ownership_expiring", 88)
        with patch("app.services.teams._get_channel_for_event", return_value=("ch", "team", True)):
            result = await send_ownership_warning(
                company_id=88,
                company_name="Acme",
                owner_name="John",
                days_remaining=7,
            )
        assert result is False

    @pytest.mark.asyncio
    async def test_no_token_returns_false(self):
        with (
            patch("app.services.teams._get_channel_for_event", return_value=("ch", "team", True)),
            patch("app.services.teams._get_system_token", new_callable=AsyncMock, return_value=None),
        ):
            result = await send_ownership_warning(
                company_id=77,
                company_name="Acme",
                owner_name="John",
                days_remaining=7,
            )
        assert result is False


# =====================================================================
#  send_stock_match_alert -- Lines 309, 319
# =====================================================================


class TestStockMatchAlertCoverage:
    @pytest.mark.asyncio
    async def test_disabled_returns_false(self):
        with patch("app.services.teams._get_channel_for_event", return_value=("", "", False)):
            result = await send_stock_match_alert(
                matches=[{"mpn": "X", "requirement_id": 1, "requisition_id": 1}],
                filename="stock.xlsx",
                vendor_name="Arrow",
            )
        assert result is False

    @pytest.mark.asyncio
    async def test_no_token_returns_false(self):
        with (
            patch("app.services.teams._get_channel_for_event", return_value=("ch", "team", True)),
            patch("app.services.teams._get_system_token", new_callable=AsyncMock, return_value=None),
        ):
            result = await send_stock_match_alert(
                matches=[{"mpn": "X", "requirement_id": 1, "requisition_id": 1}],
                filename="unique.xlsx",
                vendor_name="UniqueVendor",
            )
        assert result is False

    @pytest.mark.asyncio
    async def test_empty_matches(self):
        with (
            patch("app.services.teams._get_channel_for_event", return_value=("ch", "team", True)),
            patch("app.services.teams._get_system_token", new_callable=AsyncMock, return_value="tok"),
            patch("app.services.teams.post_to_channel", new_callable=AsyncMock, return_value=True),
        ):
            result = await send_stock_match_alert(
                matches=[],
                filename="empty.xlsx",
                vendor_name="V",
            )
        assert result is True


# =====================================================================
#  _get_system_token -- Lines 358-377
# =====================================================================


class TestGetSystemToken:
    @pytest.mark.asyncio
    async def test_returns_token_from_admin(self):
        mock_admin = MagicMock()
        mock_admin.access_token = "old-token"
        mock_admin.m365_connected = True

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = mock_admin

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fresh-token"),
        ):
            token = await _get_system_token()
        assert token == "fresh-token"
        mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_admin_returns_none(self):
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None

        with patch("app.database.SessionLocal", return_value=mock_db):
            token = await _get_system_token()
        assert token is None
        mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_exception_returns_none(self):
        with patch("app.database.SessionLocal", side_effect=Exception("DB down")):
            token = await _get_system_token()
        assert token is None
