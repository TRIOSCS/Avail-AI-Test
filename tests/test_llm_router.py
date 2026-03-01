"""Tests for app/utils/llm_router.py — Gradient-first, Claude fallback routing.

Covers:
  - Gradient success → Claude NOT called
  - Gradient None → Claude fallback called
  - Schema validation failure → Claude fallback
  - thinking_budget → skips Gradient entirely
  - tools → skips Gradient entirely
  - Both fail → returns None
  - Tier mapping (fast→default, smart→strong)
  - Schema prompt building
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.utils.llm_router import (
    _schema_to_instruction,
    _validate_required_fields,
    routed_json,
    routed_structured,
    routed_text,
)

SAMPLE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "value": {"type": "number"},
    },
    "required": ["name", "value"],
}


# ── _validate_required_fields ────────────────────────────────────────


class TestValidateRequiredFields:
    def test_valid_result(self):
        assert _validate_required_fields({"name": "x", "value": 1}, SAMPLE_SCHEMA) is True

    def test_missing_key(self):
        assert _validate_required_fields({"name": "x"}, SAMPLE_SCHEMA) is False

    def test_none_result(self):
        assert _validate_required_fields(None, SAMPLE_SCHEMA) is False

    def test_list_result(self):
        assert _validate_required_fields([{"name": "x"}], SAMPLE_SCHEMA) is False

    def test_no_required_in_schema(self):
        assert _validate_required_fields({"anything": 1}, {"type": "object"}) is True


# ── _schema_to_instruction ───────────────────────────────────────────


class TestSchemaToInstruction:
    def test_contains_schema(self):
        result = _schema_to_instruction(SAMPLE_SCHEMA)
        assert '"required"' in result
        assert "Return ONLY valid JSON" in result

    def test_compact_json(self):
        result = _schema_to_instruction({"type": "object", "required": ["a"]})
        assert "  " not in result.split("```json\n")[1]  # no pretty-print spaces


# ── routed_structured ────────────────────────────────────────────────


class TestRoutedStructured:
    @pytest.mark.asyncio
    @patch("app.utils.llm_router.claude_structured", new_callable=AsyncMock)
    @patch("app.utils.llm_router.gradient_json", new_callable=AsyncMock)
    async def test_gradient_success_skips_claude(self, mock_gradient, mock_claude):
        mock_gradient.return_value = {"name": "x", "value": 1}
        result = await routed_structured("test", SAMPLE_SCHEMA)
        assert result == {"name": "x", "value": 1}
        mock_gradient.assert_called_once()
        mock_claude.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.utils.llm_router.claude_structured", new_callable=AsyncMock)
    @patch("app.utils.llm_router.gradient_json", new_callable=AsyncMock)
    async def test_gradient_none_falls_back_to_claude(self, mock_gradient, mock_claude):
        mock_gradient.return_value = None
        mock_claude.return_value = {"name": "y", "value": 2}
        result = await routed_structured("test", SAMPLE_SCHEMA)
        assert result == {"name": "y", "value": 2}
        mock_claude.assert_called_once()

    @pytest.mark.asyncio
    @patch("app.utils.llm_router.claude_structured", new_callable=AsyncMock)
    @patch("app.utils.llm_router.gradient_json", new_callable=AsyncMock)
    async def test_gradient_incomplete_falls_back(self, mock_gradient, mock_claude):
        mock_gradient.return_value = {"name": "x"}  # missing "value"
        mock_claude.return_value = {"name": "x", "value": 1}
        result = await routed_structured("test", SAMPLE_SCHEMA)
        assert result == {"name": "x", "value": 1}
        mock_claude.assert_called_once()

    @pytest.mark.asyncio
    @patch("app.utils.llm_router.claude_structured", new_callable=AsyncMock)
    @patch("app.utils.llm_router.gradient_json", new_callable=AsyncMock)
    async def test_gradient_exception_falls_back(self, mock_gradient, mock_claude):
        mock_gradient.side_effect = Exception("timeout")
        mock_claude.return_value = {"name": "z", "value": 3}
        result = await routed_structured("test", SAMPLE_SCHEMA)
        assert result == {"name": "z", "value": 3}

    @pytest.mark.asyncio
    @patch("app.utils.llm_router.claude_structured", new_callable=AsyncMock)
    @patch("app.utils.llm_router.gradient_json", new_callable=AsyncMock)
    async def test_thinking_budget_skips_gradient(self, mock_gradient, mock_claude):
        mock_claude.return_value = {"name": "t", "value": 9}
        result = await routed_structured("test", SAMPLE_SCHEMA, thinking_budget=2048)
        assert result == {"name": "t", "value": 9}
        mock_gradient.assert_not_called()
        mock_claude.assert_called_once()
        # Verify thinking_budget passed through
        assert mock_claude.call_args.kwargs["thinking_budget"] == 2048

    @pytest.mark.asyncio
    @patch("app.utils.llm_router.claude_structured", new_callable=AsyncMock)
    @patch("app.utils.llm_router.gradient_json", new_callable=AsyncMock)
    async def test_both_fail_returns_none(self, mock_gradient, mock_claude):
        mock_gradient.return_value = None
        mock_claude.return_value = None
        result = await routed_structured("test", SAMPLE_SCHEMA)
        assert result is None

    @pytest.mark.asyncio
    @patch("app.utils.llm_router.claude_structured", new_callable=AsyncMock)
    @patch("app.utils.llm_router.gradient_json", new_callable=AsyncMock)
    async def test_tier_mapping_fast(self, mock_gradient, mock_claude):
        mock_gradient.return_value = {"name": "a", "value": 1}
        await routed_structured("test", SAMPLE_SCHEMA, model_tier="fast")
        assert mock_gradient.call_args.kwargs["model_tier"] == "default"

    @pytest.mark.asyncio
    @patch("app.utils.llm_router.claude_structured", new_callable=AsyncMock)
    @patch("app.utils.llm_router.gradient_json", new_callable=AsyncMock)
    async def test_tier_mapping_smart(self, mock_gradient, mock_claude):
        mock_gradient.return_value = {"name": "a", "value": 1}
        await routed_structured("test", SAMPLE_SCHEMA, model_tier="smart")
        assert mock_gradient.call_args.kwargs["model_tier"] == "strong"

    @pytest.mark.asyncio
    @patch("app.utils.llm_router.claude_structured", new_callable=AsyncMock)
    @patch("app.utils.llm_router.gradient_json", new_callable=AsyncMock)
    async def test_schema_appended_to_system(self, mock_gradient, mock_claude):
        mock_gradient.return_value = {"name": "a", "value": 1}
        await routed_structured("test", SAMPLE_SCHEMA, system="You are helpful.")
        system_arg = mock_gradient.call_args.kwargs["system"]
        assert "You are helpful." in system_arg
        assert '"required"' in system_arg


# ── routed_text ──────────────────────────────────────────────────────


class TestRoutedText:
    @pytest.mark.asyncio
    @patch("app.utils.llm_router.claude_text", new_callable=AsyncMock)
    @patch("app.utils.llm_router.gradient_text", new_callable=AsyncMock)
    async def test_gradient_success_skips_claude(self, mock_gradient, mock_claude):
        mock_gradient.return_value = "Hello from Gradient"
        result = await routed_text("test")
        assert result == "Hello from Gradient"
        mock_claude.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.utils.llm_router.claude_text", new_callable=AsyncMock)
    @patch("app.utils.llm_router.gradient_text", new_callable=AsyncMock)
    async def test_gradient_none_falls_back(self, mock_gradient, mock_claude):
        mock_gradient.return_value = None
        mock_claude.return_value = "Hello from Claude"
        result = await routed_text("test")
        assert result == "Hello from Claude"

    @pytest.mark.asyncio
    @patch("app.utils.llm_router.claude_text", new_callable=AsyncMock)
    @patch("app.utils.llm_router.gradient_text", new_callable=AsyncMock)
    async def test_tools_skips_gradient(self, mock_gradient, mock_claude):
        mock_claude.return_value = "web search result"
        tools = [{"type": "web_search_20250305", "name": "web_search"}]
        result = await routed_text("test", tools=tools)
        assert result == "web search result"
        mock_gradient.assert_not_called()
        assert mock_claude.call_args.kwargs["tools"] == tools

    @pytest.mark.asyncio
    @patch("app.utils.llm_router.claude_text", new_callable=AsyncMock)
    @patch("app.utils.llm_router.gradient_text", new_callable=AsyncMock)
    async def test_gradient_exception_falls_back(self, mock_gradient, mock_claude):
        mock_gradient.side_effect = Exception("boom")
        mock_claude.return_value = "fallback"
        result = await routed_text("test")
        assert result == "fallback"

    @pytest.mark.asyncio
    @patch("app.utils.llm_router.claude_text", new_callable=AsyncMock)
    @patch("app.utils.llm_router.gradient_text", new_callable=AsyncMock)
    async def test_both_fail_returns_none(self, mock_gradient, mock_claude):
        mock_gradient.return_value = None
        mock_claude.return_value = None
        result = await routed_text("test")
        assert result is None


# ── routed_json ──────────────────────────────────────────────────────


class TestRoutedJson:
    @pytest.mark.asyncio
    @patch("app.utils.llm_router.claude_json", new_callable=AsyncMock)
    @patch("app.utils.llm_router.gradient_json", new_callable=AsyncMock)
    async def test_gradient_success_skips_claude(self, mock_gradient, mock_claude):
        mock_gradient.return_value = {"ok": True}
        result = await routed_json("test")
        assert result == {"ok": True}
        mock_claude.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.utils.llm_router.claude_json", new_callable=AsyncMock)
    @patch("app.utils.llm_router.gradient_json", new_callable=AsyncMock)
    async def test_gradient_none_falls_back(self, mock_gradient, mock_claude):
        mock_gradient.return_value = None
        mock_claude.return_value = {"ok": True}
        result = await routed_json("test")
        assert result == {"ok": True}

    @pytest.mark.asyncio
    @patch("app.utils.llm_router.claude_json", new_callable=AsyncMock)
    @patch("app.utils.llm_router.gradient_json", new_callable=AsyncMock)
    async def test_tools_skips_gradient(self, mock_gradient, mock_claude):
        mock_claude.return_value = {"data": "web"}
        tools = [{"type": "web_search_20250305", "name": "web_search"}]
        result = await routed_json("test", tools=tools)
        assert result == {"data": "web"}
        mock_gradient.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.utils.llm_router.claude_json", new_callable=AsyncMock)
    @patch("app.utils.llm_router.gradient_json", new_callable=AsyncMock)
    async def test_both_fail_returns_none(self, mock_gradient, mock_claude):
        mock_gradient.return_value = None
        mock_claude.return_value = None
        result = await routed_json("test")
        assert result is None

    @pytest.mark.asyncio
    @patch("app.utils.llm_router.claude_json", new_callable=AsyncMock)
    @patch("app.utils.llm_router.gradient_json", new_callable=AsyncMock)
    async def test_gradient_exception_falls_back_to_claude(self, mock_gradient, mock_claude):
        """Lines 174-175: Gradient exception triggers Claude fallback."""
        mock_gradient.side_effect = Exception("Gradient timeout")
        mock_claude.return_value = {"fallback": True}
        result = await routed_json("test")
        assert result == {"fallback": True}
        mock_claude.assert_called_once()
