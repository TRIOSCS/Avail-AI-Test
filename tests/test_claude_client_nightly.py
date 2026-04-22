"""test_claude_client_nightly.py — Coverage gap tests for app/utils/claude_client.py.

Targets specific missing lines: 111, 176, 187, 200-207, 213, 256-259, 268,
308, 319, 328-335, 340, 485, 487, 490, 531, 534, 563, 577-580.

Called by: pytest
Depends on: app/utils/claude_client.py, app/utils/claude_errors.py
"""

import json
import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.utils.claude_client import (
    claude_batch_results,
    claude_batch_submit,
    claude_structured,
    claude_text,
)
from app.utils.claude_errors import (
    ClaudeAuthError,
    ClaudeError,
    ClaudeRateLimitError,
    ClaudeServerError,
)

# ─────────────────────────────────────────────────────────────────────────────
#  Shared helpers (mirror conftest pattern from existing tests)
# ─────────────────────────────────────────────────────────────────────────────


def _mock_response(status_code=200, json_data=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.text = text
    return resp


def _cred_side_effect(service, key):
    if key == "ANTHROPIC_API_KEY":
        return "sk-test-key"
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  claude_structured — gap coverage
# ─────────────────────────────────────────────────────────────────────────────


class TestClaudeStructuredGaps:
    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_cache_control_set_when_system_and_cache_system_true(self, mock_http, mock_cred):
        """Line 111: block['cache_control'] is set when system provided and cache_system=True."""
        mock_http.post = AsyncMock(
            return_value=_mock_response(
                200,
                {"content": [{"type": "tool_use", "name": "structured_output", "input": {"ok": True}}]},
            )
        )

        await claude_structured(
            "test prompt",
            {"type": "object"},
            system="You are a helpful assistant",
            cache_system=True,
        )

        call_kwargs = mock_http.post.call_args.kwargs
        body = call_kwargs["json"]
        # system key present and block has cache_control
        assert "system" in body
        system_block = body["system"][0]
        assert system_block["type"] == "text"
        assert system_block["text"] == "You are a helpful assistant"
        assert system_block["cache_control"] == {"type": "ephemeral"}

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_400_raises_claude_error(self, mock_http, mock_cred):
        """Line 176: non-401/403/429/5xx status raises generic ClaudeError."""
        mock_http.post = AsyncMock(return_value=_mock_response(400, text="Bad Request"))

        with pytest.raises(ClaudeError) as exc_info:
            await claude_structured("test", {"type": "object"})

        assert "400" in str(exc_info.value)
        # Must not be a subclass like AuthError/RateLimitError/ServerError
        assert not isinstance(exc_info.value, ClaudeAuthError)
        assert not isinstance(exc_info.value, ClaudeRateLimitError)
        assert not isinstance(exc_info.value, ClaudeServerError)

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_cache_read_tokens_recorded_in_span(self, mock_http, mock_cred):
        """Line 187: cache_read_input_tokens in usage triggers span.set_data."""
        mock_http.post = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "content": [{"type": "tool_use", "name": "structured_output", "input": {"x": 1}}],
                    "usage": {
                        "input_tokens": 50,
                        "output_tokens": 20,
                        "cache_read_input_tokens": 100,
                    },
                },
            )
        )

        result = await claude_structured("test", {"type": "object"})
        # Main assertion: call succeeded and returned structured result
        assert result == {"x": 1}

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.asyncio.sleep", new_callable=AsyncMock)
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_connect_error_retries_then_succeeds(self, mock_http, mock_cred, mock_sleep):
        """Lines 200-207: ConnectError on first attempt → retry → success."""
        success_resp = _mock_response(
            200,
            {"content": [{"type": "tool_use", "name": "structured_output", "input": {"retried": True}}]},
        )
        mock_http.post = AsyncMock(side_effect=[httpx.ConnectError("Connection refused"), success_resp])

        result = await claude_structured("test", {"type": "object"})
        assert result == {"retried": True}
        # sleep was called once between attempts
        mock_sleep.assert_called_once()

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.asyncio.sleep", new_callable=AsyncMock)
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_connect_error_exhausts_retries_raises_claude_error(self, mock_http, mock_cred, mock_sleep):
        """Line 207 + 213: ConnectError on all 3 attempts raises ClaudeError."""
        mock_http.post = AsyncMock(side_effect=httpx.ConnectError("unreachable"))

        with pytest.raises(ClaudeError, match="unreachable"):
            await claude_structured("test", {"type": "object"})

        assert mock_sleep.call_count == 2  # slept before attempt 2 and 3

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.asyncio.sleep", new_callable=AsyncMock)
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_all_retries_exhausted_via_429_raises_rate_limit(self, mock_http, mock_cred, mock_sleep):
        """Line 213: all retry attempts used (via 429 retries + final 429) raises error."""
        # First two attempts get 429 (triggers retry), third gets 429 without retry budget
        mock_http.post = AsyncMock(return_value=_mock_response(429, text="Rate Limited"))

        with pytest.raises(ClaudeRateLimitError):
            await claude_structured("test", {"type": "object"})


# ─────────────────────────────────────────────────────────────────────────────
#  claude_text — gap coverage
# ─────────────────────────────────────────────────────────────────────────────


class TestClaudeTextGaps:
    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_system_with_cache_system_true_sets_cache_control(self, mock_http, mock_cred):
        """Lines 256-259, 268: system block with cache_control when
        cache_system=True."""
        mock_http.post = AsyncMock(
            return_value=_mock_response(
                200,
                {"content": [{"type": "text", "text": "Hello"}]},
            )
        )

        result = await claude_text(
            "test prompt",
            system="You are a sourcing assistant",
            cache_system=True,
        )

        assert result == "Hello"
        body = mock_http.post.call_args.kwargs["json"]
        assert "system" in body
        block = body["system"][0]
        assert block["type"] == "text"
        assert block["text"] == "You are a sourcing assistant"
        assert block["cache_control"] == {"type": "ephemeral"}

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_system_with_cache_system_false_no_cache_control(self, mock_http, mock_cred):
        """Lines 256-259 branch: no cache_control when cache_system=False."""
        mock_http.post = AsyncMock(
            return_value=_mock_response(
                200,
                {"content": [{"type": "text", "text": "OK"}]},
            )
        )

        await claude_text("test", system="System prompt", cache_system=False)

        body = mock_http.post.call_args.kwargs["json"]
        block = body["system"][0]
        assert "cache_control" not in block

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_400_raises_claude_error(self, mock_http, mock_cred):
        """Line 308: non-401/403/429/5xx raises generic ClaudeError."""
        mock_http.post = AsyncMock(return_value=_mock_response(400, text="Bad Request"))

        with pytest.raises(ClaudeError) as exc_info:
            await claude_text("test")

        assert "400" in str(exc_info.value)
        assert not isinstance(exc_info.value, ClaudeAuthError)
        assert not isinstance(exc_info.value, ClaudeRateLimitError)
        assert not isinstance(exc_info.value, ClaudeServerError)

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_cache_read_tokens_in_usage(self, mock_http, mock_cred):
        """Line 319: cache_read_input_tokens present in usage triggers span.set_data."""
        mock_http.post = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "content": [{"type": "text", "text": "cached response"}],
                    "usage": {
                        "input_tokens": 30,
                        "output_tokens": 10,
                        "cache_read_input_tokens": 50,
                    },
                },
            )
        )

        result = await claude_text("test")
        assert result == "cached response"

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.asyncio.sleep", new_callable=AsyncMock)
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_connect_error_retries_then_succeeds(self, mock_http, mock_cred, mock_sleep):
        """Lines 328-335: ConnectError on first attempt → retry → success."""
        success_resp = _mock_response(
            200,
            {"content": [{"type": "text", "text": "recovered"}]},
        )
        mock_http.post = AsyncMock(side_effect=[httpx.ConnectError("Connection refused"), success_resp])

        result = await claude_text("test")
        assert result == "recovered"
        mock_sleep.assert_called_once()

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.asyncio.sleep", new_callable=AsyncMock)
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_connect_error_exhausts_retries_raises_claude_error(self, mock_http, mock_cred, mock_sleep):
        """Lines 335 + 340: ConnectError on all attempts raises ClaudeError."""
        mock_http.post = AsyncMock(side_effect=httpx.ConnectError("host unreachable"))

        with pytest.raises(ClaudeError, match="unreachable"):
            await claude_text("test")

        assert mock_sleep.call_count == 2

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.asyncio.sleep", new_callable=AsyncMock)
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_all_retries_exhausted_via_429_raises_rate_limit(self, mock_http, mock_cred, mock_sleep):
        """Line 340: all retries exhausted via repeated 429."""
        mock_http.post = AsyncMock(return_value=_mock_response(429, text="Rate Limited"))

        with pytest.raises(ClaudeRateLimitError):
            await claude_text("test")


# ─────────────────────────────────────────────────────────────────────────────
#  claude_batch_submit — gap coverage
# ─────────────────────────────────────────────────────────────────────────────


class TestClaudeBatchSubmitGaps:
    _base_request = [{"custom_id": "r1", "prompt": "parse this", "schema": {"type": "object"}}]

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_401_raises_auth_error(self, mock_http, mock_cred):
        """Line 485: 401 response raises ClaudeAuthError."""
        mock_http.post = AsyncMock(return_value=_mock_response(401, text="Unauthorized"))

        with pytest.raises(ClaudeAuthError):
            await claude_batch_submit(self._base_request)

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_403_raises_auth_error(self, mock_http, mock_cred):
        """Line 485: 403 response raises ClaudeAuthError."""
        mock_http.post = AsyncMock(return_value=_mock_response(403, text="Forbidden"))

        with pytest.raises(ClaudeAuthError):
            await claude_batch_submit(self._base_request)

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_429_raises_rate_limit_error(self, mock_http, mock_cred):
        """Line 487: 429 response raises ClaudeRateLimitError."""
        mock_http.post = AsyncMock(return_value=_mock_response(429, text="Rate Limited"))

        with pytest.raises(ClaudeRateLimitError):
            await claude_batch_submit(self._base_request)

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_400_raises_generic_claude_error(self, mock_http, mock_cred):
        """Line 490: non-401/403/429/5xx raises generic ClaudeError."""
        mock_http.post = AsyncMock(return_value=_mock_response(400, text="Bad Request"))

        with pytest.raises(ClaudeError) as exc_info:
            await claude_batch_submit(self._base_request)

        assert "400" in str(exc_info.value)
        assert not isinstance(exc_info.value, ClaudeAuthError)
        assert not isinstance(exc_info.value, ClaudeRateLimitError)
        assert not isinstance(exc_info.value, ClaudeServerError)


# ─────────────────────────────────────────────────────────────────────────────
#  claude_batch_results — gap coverage
# ─────────────────────────────────────────────────────────────────────────────


class TestClaudeBatchResultsGaps:
    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_status_check_401_raises_auth_error(self, mock_http, mock_cred):
        """Line 531: 401 on status check raises ClaudeAuthError."""
        mock_http.get = AsyncMock(return_value=_mock_response(401, text="Unauthorized"))

        with pytest.raises(ClaudeAuthError):
            await claude_batch_results("batch_abc")

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_status_check_500_raises_server_error(self, mock_http, mock_cred):
        """Line 533: 500 on status check raises ClaudeServerError."""
        mock_http.get = AsyncMock(return_value=_mock_response(500, text="Server Error"))

        with pytest.raises(ClaudeServerError):
            await claude_batch_results("batch_abc")

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_status_check_400_raises_generic_claude_error(self, mock_http, mock_cred):
        """Line 534: non-401/403/5xx raises generic ClaudeError."""
        mock_http.get = AsyncMock(return_value=_mock_response(400, text="Bad Request"))

        with pytest.raises(ClaudeError) as exc_info:
            await claude_batch_results("batch_abc")

        assert "400" in str(exc_info.value)
        assert not isinstance(exc_info.value, ClaudeAuthError)
        assert not isinstance(exc_info.value, ClaudeServerError)

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_ended_status_without_results_url_returns_none(self, mock_http, mock_cred):
        """Line 563: status='ended' but no results_url → returns None."""
        mock_http.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "processing_status": "ended",
                    # intentionally omit results_url
                },
            )
        )

        result = await claude_batch_results("batch_abc")
        assert result is None

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_tool_input_as_json_string_is_parsed(self, mock_http, mock_cred):
        """Lines 577-580: tool input returned as JSON string is parsed into dict."""
        inner_data = {"price": 1.50, "qty": 100}
        jsonl_line = json.dumps(
            {
                "custom_id": "r1",
                "result": {
                    "type": "succeeded",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "structured_output",
                                # API returns tool input as a JSON string instead of a dict
                                "input": json.dumps(inner_data),
                            }
                        ]
                    },
                },
            }
        )

        status_resp = _mock_response(
            200,
            {
                "processing_status": "ended",
                "results_url": "https://api.anthropic.com/results/batch_str",
                "request_counts": {"succeeded": 1, "errored": 0},
            },
        )
        results_resp = _mock_response(200, text=jsonl_line)
        mock_http.get = AsyncMock(side_effect=[status_resp, results_resp])

        result = await claude_batch_results("batch_str")
        assert result == {"r1": {"price": 1.50, "qty": 100}}

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_tool_input_as_invalid_json_string_returns_none(self, mock_http, mock_cred):
        """Lines 578-580: tool input as invalid JSON string → None."""
        jsonl_line = json.dumps(
            {
                "custom_id": "r1",
                "result": {
                    "type": "succeeded",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "structured_output",
                                "input": "not valid json {",
                            }
                        ]
                    },
                },
            }
        )

        status_resp = _mock_response(
            200,
            {
                "processing_status": "ended",
                "results_url": "https://api.anthropic.com/results/batch_bad_str",
                "request_counts": {"succeeded": 1, "errored": 0},
            },
        )
        results_resp = _mock_response(200, text=jsonl_line)
        mock_http.get = AsyncMock(side_effect=[status_resp, results_resp])

        result = await claude_batch_results("batch_bad_str")
        assert result == {"r1": None}
