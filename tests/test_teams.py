"""
test_teams.py — Teams notification service tests.

Tests rate limiting, card structure, deep links, graceful degradation,
and Teams disabled scenarios. All Graph API calls are mocked.

Called by: pytest
Depends on: app/services/teams.py, conftest.py
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.services.teams import (
    _build_deep_link,
    _get_teams_config,
    _is_rate_limited,
    _make_card,
    _mark_posted,
    clear_rate_limits,
    post_to_channel,
    send_competitive_quote_alert,
    send_hot_requirement_alert,
    send_ownership_warning,
    send_stock_match_alert,
)


@pytest.fixture(autouse=True)
def _clear_rate_limits():
    """Ensure clean rate limit state for each test."""
    clear_rate_limits()
    yield
    clear_rate_limits()


# ── Card builder ──────────────────────────────────────────────────────


class TestMakeCard:
    def test_card_structure(self):
        card = _make_card(
            title="TEST",
            subtitle="Test subtitle",
            facts=[{"title": "Key", "value": "Val"}],
            action_url="https://example.com",
        )
        assert card["type"] == "AdaptiveCard"
        assert card["version"] == "1.4"
        assert len(card["body"]) == 3
        assert card["body"][0]["text"] == "TEST"
        assert card["body"][1]["text"] == "Test subtitle"
        assert card["body"][2]["facts"] == [{"title": "Key", "value": "Val"}]
        assert card["actions"][0]["url"] == "https://example.com"

    def test_card_custom_accent(self):
        card = _make_card(
            title="T", subtitle="S", facts=[], action_url="",
            accent_color="good",
        )
        assert card["body"][0]["color"] == "good"

    def test_card_custom_action_title(self):
        card = _make_card(
            title="T", subtitle="S", facts=[], action_url="",
            action_title="Click Here",
        )
        assert card["actions"][0]["title"] == "Click Here"


# ── Deep links ─────────────────────────────────────────────────────────


class TestDeepLink:
    @patch("app.config.settings")
    def test_basic_link(self, mock_settings):
        mock_settings.app_url = "https://avail.trioscs.com"
        link = _build_deep_link("#requisition/42")
        assert link == "https://avail.trioscs.com/#requisition/42"

    @patch("app.config.settings")
    def test_trailing_slash(self, mock_settings):
        mock_settings.app_url = "https://avail.trioscs.com/"
        link = _build_deep_link("#company/5")
        assert link == "https://avail.trioscs.com/#company/5"


# ── Rate limiting ──────────────────────────────────────────────────────


class TestRateLimiting:
    def test_not_rate_limited_initially(self):
        assert not _is_rate_limited("hot_requirement", 1)

    def test_rate_limited_after_mark(self):
        _mark_posted("hot_requirement", 1)
        assert _is_rate_limited("hot_requirement", 1)

    def test_different_entity_not_limited(self):
        _mark_posted("hot_requirement", 1)
        assert not _is_rate_limited("hot_requirement", 2)

    def test_different_type_not_limited(self):
        _mark_posted("hot_requirement", 1)
        assert not _is_rate_limited("competitive_quote", 1)

    def test_clear_resets(self):
        _mark_posted("hot_requirement", 1)
        clear_rate_limits()
        assert not _is_rate_limited("hot_requirement", 1)


# ── post_to_channel ───────────────────────────────────────────────────


class TestPostToChannel:
    @pytest.mark.asyncio
    async def test_success(self):
        with patch("app.utils.graph_client.GraphClient") as MockGC:
            instance = AsyncMock()
            instance.post_json = AsyncMock(return_value={"id": "msg-1"})
            MockGC.return_value = instance

            result = await post_to_channel("team-1", "ch-1", {"type": "AdaptiveCard"}, "token-abc")
            assert result is True
            instance.post_json.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_api_error(self):
        with patch("app.utils.graph_client.GraphClient") as MockGC:
            instance = AsyncMock()
            instance.post_json = AsyncMock(return_value={"error": {"message": "Forbidden"}})
            MockGC.return_value = instance

            result = await post_to_channel("team-1", "ch-1", {}, "token-abc")
            assert result is False

    @pytest.mark.asyncio
    async def test_exception_returns_false(self):
        with patch("app.utils.graph_client.GraphClient") as MockGC:
            MockGC.side_effect = Exception("Network error")

            result = await post_to_channel("team-1", "ch-1", {}, "token-abc")
            assert result is False


# ── Hot requirement alert ──────────────────────────────────────────────


class TestHotRequirementAlert:
    @pytest.mark.asyncio
    async def test_sends_when_enabled(self):
        with patch("app.services.teams._get_teams_config", return_value=("ch-1", "team-1", True)), \
             patch("app.services.teams._get_system_token", new_callable=AsyncMock, return_value="tok"), \
             patch("app.services.teams.post_to_channel", new_callable=AsyncMock, return_value=True):
            result = await send_hot_requirement_alert(
                requirement_id=1, mpn="LM317T", target_qty=500,
                target_price=25.0, customer_name="Acme", requisition_id=10,
            )
            assert result is True

    @pytest.mark.asyncio
    async def test_skips_when_disabled(self):
        with patch("app.services.teams._get_teams_config", return_value=("", "", False)):
            result = await send_hot_requirement_alert(
                requirement_id=1, mpn="LM317T", target_qty=500,
                target_price=25.0, customer_name="Acme", requisition_id=10,
            )
            assert result is False

    @pytest.mark.asyncio
    async def test_rate_limited(self):
        _mark_posted("hot_requirement", 1)
        with patch("app.services.teams._get_teams_config", return_value=("ch-1", "team-1", True)):
            result = await send_hot_requirement_alert(
                requirement_id=1, mpn="LM317T", target_qty=500,
                target_price=25.0, customer_name="Acme", requisition_id=10,
            )
            assert result is False

    @pytest.mark.asyncio
    async def test_no_token(self):
        with patch("app.services.teams._get_teams_config", return_value=("ch-1", "team-1", True)), \
             patch("app.services.teams._get_system_token", new_callable=AsyncMock, return_value=None):
            result = await send_hot_requirement_alert(
                requirement_id=2, mpn="LM317T", target_qty=500,
                target_price=25.0, customer_name="Acme", requisition_id=10,
            )
            assert result is False


# ── Competitive quote alert ────────────────────────────────────────────


class TestCompetitiveQuoteAlert:
    @pytest.mark.asyncio
    async def test_sends_when_enabled(self):
        with patch("app.services.teams._get_teams_config", return_value=("ch-1", "team-1", True)), \
             patch("app.services.teams._get_system_token", new_callable=AsyncMock, return_value="tok"), \
             patch("app.services.teams.post_to_channel", new_callable=AsyncMock, return_value=True):
            result = await send_competitive_quote_alert(
                offer_id=1, mpn="LM317T", vendor_name="Arrow",
                offer_price=0.30, best_price=0.50, requisition_id=10,
            )
            assert result is True

    @pytest.mark.asyncio
    async def test_calculates_savings(self):
        """Verify the card contains correct savings percentage."""
        captured_card = {}

        async def _capture_post(team_id, channel_id, card, token):
            captured_card.update(card)
            return True

        with patch("app.services.teams._get_teams_config", return_value=("ch-1", "team-1", True)), \
             patch("app.services.teams._get_system_token", new_callable=AsyncMock, return_value="tok"), \
             patch("app.services.teams.post_to_channel", side_effect=_capture_post):
            await send_competitive_quote_alert(
                offer_id=2, mpn="LM317T", vendor_name="Arrow",
                offer_price=0.40, best_price=1.00, requisition_id=10,
            )
            # Savings = (1.00 - 0.40) / 1.00 * 100 = 60%
            facts = captured_card["body"][2]["facts"]
            savings_fact = next(f for f in facts if f["title"] == "Savings")
            assert "60" in savings_fact["value"]


# ── Ownership warning alert ────────────────────────────────────────────


class TestOwnershipWarning:
    @pytest.mark.asyncio
    async def test_sends_when_enabled(self):
        with patch("app.services.teams._get_teams_config", return_value=("ch-1", "team-1", True)), \
             patch("app.services.teams._get_system_token", new_callable=AsyncMock, return_value="tok"), \
             patch("app.services.teams.post_to_channel", new_callable=AsyncMock, return_value=True):
            result = await send_ownership_warning(
                company_id=1, company_name="Acme", owner_name="John", days_remaining=7,
            )
            assert result is True


# ── Stock match alert ──────────────────────────────────────────────────


class TestStockMatchAlert:
    @pytest.mark.asyncio
    async def test_sends_with_matches(self):
        with patch("app.services.teams._get_teams_config", return_value=("ch-1", "team-1", True)), \
             patch("app.services.teams._get_system_token", new_callable=AsyncMock, return_value="tok"), \
             patch("app.services.teams.post_to_channel", new_callable=AsyncMock, return_value=True):
            result = await send_stock_match_alert(
                matches=[
                    {"mpn": "LM317T", "requirement_id": 1, "requisition_id": 10},
                    {"mpn": "NE555P", "requirement_id": 2, "requisition_id": 10},
                ],
                filename="stock_jan.xlsx",
                vendor_name="Arrow Electronics",
            )
            assert result is True

    @pytest.mark.asyncio
    async def test_rate_limited_by_filename(self):
        _mark_posted("stock_match", "Arrow:stock.xlsx")
        with patch("app.services.teams._get_teams_config", return_value=("ch-1", "team-1", True)):
            result = await send_stock_match_alert(
                matches=[{"mpn": "X", "requirement_id": 1, "requisition_id": 1}],
                filename="stock.xlsx",
                vendor_name="Arrow",
            )
            assert result is False

    @pytest.mark.asyncio
    async def test_card_truncates_mpn_list(self):
        """When >5 matches, card shows first 5 + count."""
        captured_card = {}

        async def _capture_post(team_id, channel_id, card, token):
            captured_card.update(card)
            return True

        with patch("app.services.teams._get_teams_config", return_value=("ch-1", "team-1", True)), \
             patch("app.services.teams._get_system_token", new_callable=AsyncMock, return_value="tok"), \
             patch("app.services.teams.post_to_channel", side_effect=_capture_post):
            matches = [{"mpn": f"MPN-{i}", "requirement_id": i, "requisition_id": 1} for i in range(8)]
            await send_stock_match_alert(matches=matches, filename="big.xlsx", vendor_name="V")

            facts = captured_card["body"][2]["facts"]
            mpn_fact = next(f for f in facts if f["title"] == "MPNs")
            assert "+3 more" in mpn_fact["value"]


# ── Teams config helper ────────────────────────────────────────────────


class TestGetTeamsConfig:
    def test_returns_tuple(self, db_session):
        """Config helper returns (channel_id, team_id, enabled) tuple."""
        with patch("app.config.settings") as mock_settings:
            mock_settings.teams_channel_id = ""
            mock_settings.teams_team_id = ""
            ch_id, team_id, enabled = _get_teams_config()
            assert enabled is False

    def test_enabled_when_both_set(self, db_session):
        with patch("app.config.settings") as mock_settings:
            mock_settings.teams_channel_id = "ch-1"
            mock_settings.teams_team_id = "team-1"
            ch_id, team_id, enabled = _get_teams_config()
            assert ch_id == "ch-1"
            assert team_id == "team-1"
            assert enabled is True
