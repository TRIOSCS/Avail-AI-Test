"""Tests for app/utils/llm_router.py — Direct Claude routing.

Covers:
  - routed_structured calls claude_structured with correct params
  - routed_text calls claude_text with correct params
  - routed_json calls claude_json with correct params
  - Default and custom arguments are passed through
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.utils.llm_router import (
    routed_json,
    routed_structured,
    routed_text,
)

# ── routed_structured ────────────────────────────────────────────────


class TestRoutedStructured:
    @pytest.mark.asyncio
    @patch("app.utils.llm_router.claude_structured", new_callable=AsyncMock)
    async def test_calls_claude_structured(self, mock_claude):
        schema = {"type": "object", "required": ["name"]}
        mock_claude.return_value = {"name": "x"}
        result = await routed_structured("test prompt", schema)
        assert result == {"name": "x"}
        mock_claude.assert_called_once_with(
            "test prompt",
            schema,
            system="",
            model_tier="fast",
            max_tokens=1024,
            timeout=30,
            thinking_budget=None,
        )

    @pytest.mark.asyncio
    @patch("app.utils.llm_router.claude_structured", new_callable=AsyncMock)
    async def test_passes_custom_params(self, mock_claude):
        schema = {"type": "object"}
        mock_claude.return_value = {"ok": True}
        result = await routed_structured(
            "test",
            schema,
            system="You are helpful.",
            model_tier="smart",
            max_tokens=2048,
            timeout=60,
            thinking_budget=4096,
        )
        assert result == {"ok": True}
        mock_claude.assert_called_once_with(
            "test",
            schema,
            system="You are helpful.",
            model_tier="smart",
            max_tokens=2048,
            timeout=60,
            thinking_budget=4096,
        )

    @pytest.mark.asyncio
    @patch("app.utils.llm_router.claude_structured", new_callable=AsyncMock)
    async def test_returns_none_on_failure(self, mock_claude):
        mock_claude.return_value = None
        result = await routed_structured("test", {"type": "object"})
        assert result is None


# ── routed_text ──────────────────────────────────────────────────────


class TestRoutedText:
    @pytest.mark.asyncio
    @patch("app.utils.llm_router.claude_text", new_callable=AsyncMock)
    async def test_calls_claude_text(self, mock_claude):
        mock_claude.return_value = "Hello from Claude"
        result = await routed_text("test prompt")
        assert result == "Hello from Claude"
        mock_claude.assert_called_once_with(
            "test prompt",
            system="",
            model_tier="smart",
            max_tokens=1500,
            tools=None,
            timeout=60,
        )

    @pytest.mark.asyncio
    @patch("app.utils.llm_router.claude_text", new_callable=AsyncMock)
    async def test_passes_tools(self, mock_claude):
        tools = [{"type": "web_search_20250305", "name": "web_search"}]
        mock_claude.return_value = "web result"
        result = await routed_text("test", tools=tools)
        assert result == "web result"
        assert mock_claude.call_args.kwargs["tools"] == tools

    @pytest.mark.asyncio
    @patch("app.utils.llm_router.claude_text", new_callable=AsyncMock)
    async def test_returns_none_on_failure(self, mock_claude):
        mock_claude.return_value = None
        result = await routed_text("test")
        assert result is None


# ── routed_json ──────────────────────────────────────────────────────


class TestRoutedJson:
    @pytest.mark.asyncio
    @patch("app.utils.llm_router.claude_json", new_callable=AsyncMock)
    async def test_calls_claude_json(self, mock_claude):
        mock_claude.return_value = {"ok": True}
        result = await routed_json("test prompt")
        assert result == {"ok": True}
        mock_claude.assert_called_once_with(
            "test prompt",
            system="",
            model_tier="fast",
            max_tokens=1024,
            tools=None,
            timeout=30,
        )

    @pytest.mark.asyncio
    @patch("app.utils.llm_router.claude_json", new_callable=AsyncMock)
    async def test_passes_custom_params(self, mock_claude):
        tools = [{"type": "web_search_20250305", "name": "web_search"}]
        mock_claude.return_value = {"data": "web"}
        result = await routed_json("test", tools=tools, system="sys", model_tier="smart")
        assert result == {"data": "web"}
        assert mock_claude.call_args.kwargs["tools"] == tools
        assert mock_claude.call_args.kwargs["system"] == "sys"
        assert mock_claude.call_args.kwargs["model_tier"] == "smart"

    @pytest.mark.asyncio
    @patch("app.utils.llm_router.claude_json", new_callable=AsyncMock)
    async def test_returns_none_on_failure(self, mock_claude):
        mock_claude.return_value = None
        result = await routed_json("test")
        assert result is None

    @pytest.mark.asyncio
    @patch("app.utils.llm_router.claude_json", new_callable=AsyncMock)
    async def test_returns_list(self, mock_claude):
        mock_claude.return_value = [{"item": 1}]
        result = await routed_json("test")
        assert result == [{"item": 1}]
