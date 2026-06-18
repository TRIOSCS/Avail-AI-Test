"""test_claude_client.py — Tests for app/utils/claude_client.py.

Mock HTTP calls and credential lookups to test Claude API wrappers.

Called by: pytest
Depends on: app/utils/claude_client.py, app/utils/claude_errors.py
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.utils.claude_client import (
    MODELS,
    _build_batch_request,
    claude_batch_results,
    claude_batch_submit,
    claude_json,
    claude_structured,
    claude_text,
    safe_json_parse,
)
from app.utils.claude_errors import (
    ClaudeAuthError,
    ClaudeError,
    ClaudeRateLimitError,
    ClaudeServerError,
    ClaudeUnavailableError,
)

# ═══════════════════════════════════════════════════════════════════════
#  safe_json_parse — pure, no mock
# ═══════════════════════════════════════════════════════════════════════


class TestSafeJsonParse:
    def test_plain_json_object(self):
        assert safe_json_parse('{"key": "value"}') == {"key": "value"}

    def test_plain_json_array(self):
        assert safe_json_parse("[1, 2, 3]") == [1, 2, 3]

    def test_markdown_fenced_json(self):
        text = '```json\n{"a": 1}\n```'
        assert safe_json_parse(text) == {"a": 1}

    def test_markdown_fenced_no_lang(self):
        text = '```\n{"a": 1}\n```'
        assert safe_json_parse(text) == {"a": 1}

    def test_json_with_preamble(self):
        text = 'Here is the result:\n{"a": 1}'
        assert safe_json_parse(text) == {"a": 1}

    def test_json_with_trailing_text(self):
        text = '{"a": 1}\nThat is the answer.'
        assert safe_json_parse(text) == {"a": 1}

    def test_nested_json(self):
        text = '{"outer": {"inner": [1, 2]}}'
        result = safe_json_parse(text)
        assert result == {"outer": {"inner": [1, 2]}}

    def test_empty_string(self):
        assert safe_json_parse("") is None

    def test_none_input(self):
        assert safe_json_parse(None) is None

    def test_no_json_in_text(self):
        assert safe_json_parse("No JSON here at all") is None

    def test_invalid_json(self):
        assert safe_json_parse("{invalid: json}") is None


# ═══════════════════════════════════════════════════════════════════════
#  Shared mock helpers
# ═══════════════════════════════════════════════════════════════════════


def _mock_response(status_code=200, json_data=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.text = text
    return resp


def _cred_side_effect(service, key):
    """Return a fake API key for anthropic lookups."""
    if key == "ANTHROPIC_API_KEY":
        return "sk-test-key"
    return None


def _tool_use_response(tool_input=None):
    """200 response carrying a single structured_output tool_use block."""
    return _mock_response(
        200, {"content": [{"type": "tool_use", "name": "structured_output", "input": tool_input or {}}]}
    )


# ═══════════════════════════════════════════════════════════════════════
#  claude_structured — mock HTTP
# ═══════════════════════════════════════════════════════════════════════


class TestClaudeStructured:
    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_success_extracts_tool_input(self, mock_http, mock_cred):
        mock_http.post = AsyncMock(return_value=_tool_use_response({"parsed": True}))

        result = await claude_structured("test prompt", {"type": "object"})
        assert result == {"parsed": True}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status_code", "body_text", "expected_exc"),
        [
            (500, "Server Error", ClaudeServerError),
            (401, "Unauthorized", ClaudeAuthError),
            (403, "Forbidden", ClaudeAuthError),
            (429, "Rate limited", ClaudeRateLimitError),
        ],
        ids=["500_server", "401_auth", "403_auth", "429_rate_limit"],
    )
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_api_error_status_raises(self, mock_http, mock_cred, status_code, body_text, expected_exc):
        mock_http.post = AsyncMock(return_value=_mock_response(status_code, text=body_text))

        with pytest.raises(expected_exc):
            await claude_structured("test", {"type": "object"})

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", return_value=None)
    async def test_no_api_key_raises_unavailable(self, mock_cred):
        with pytest.raises(ClaudeUnavailableError):
            await claude_structured("test", {"type": "object"})

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_no_tool_use_block_returns_none(self, mock_http, mock_cred):
        mock_http.post = AsyncMock(
            return_value=_mock_response(200, {"content": [{"type": "text", "text": "No tool use here"}]})
        )

        result = await claude_structured("test", {"type": "object"})
        assert result is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model_tier", ["fast", "smart"], ids=["fast_haiku", "smart_sonnet"])
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_model_tier_selects_model(self, mock_http, mock_cred, model_tier):
        mock_http.post = AsyncMock(return_value=_tool_use_response())

        await claude_structured("test", {"type": "object"}, model_tier=model_tier)
        body = mock_http.post.call_args.kwargs["json"]
        assert body["model"] == MODELS[model_tier]

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_connection_error_raises_claude_error(self, mock_http, mock_cred):
        mock_http.post = AsyncMock(side_effect=ConnectionError("Connection refused"))

        with pytest.raises(ClaudeError, match="Connection refused"):
            await claude_structured("test", {"type": "object"})


# ═══════════════════════════════════════════════════════════════════════
#  claude_text — mock HTTP
# ═══════════════════════════════════════════════════════════════════════


class TestClaudeText:
    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_joins_text_blocks(self, mock_http, mock_cred):
        mock_http.post = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "content": [
                        {"type": "text", "text": "Hello"},
                        {"type": "text", "text": "World"},
                    ]
                },
            )
        )

        result = await claude_text("test")
        assert result == "Hello\nWorld"

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_no_text_blocks_returns_none(self, mock_http, mock_cred):
        mock_http.post = AsyncMock(
            return_value=_mock_response(200, {"content": [{"type": "tool_use", "name": "web_search", "input": {}}]})
        )

        result = await claude_text("test")
        assert result is None

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", return_value=None)
    async def test_no_api_key_raises_unavailable(self, mock_cred):
        with pytest.raises(ClaudeUnavailableError):
            await claude_text("test")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status_code", "body_text", "expected_exc"),
        [
            (500, "Error", ClaudeServerError),
            (401, "Unauthorized", ClaudeAuthError),
        ],
        ids=["500_server", "401_auth"],
    )
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_api_error_status_raises(self, mock_http, mock_cred, status_code, body_text, expected_exc):
        mock_http.post = AsyncMock(return_value=_mock_response(status_code, text=body_text))

        with pytest.raises(expected_exc):
            await claude_text("test")

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_connection_error_raises_claude_error(self, mock_http, mock_cred):
        mock_http.post = AsyncMock(side_effect=ConnectionError("timeout"))

        with pytest.raises(ClaudeError):
            await claude_text("test")


# ═══════════════════════════════════════════════════════════════════════
#  claude_json — mock claude_text
# ═══════════════════════════════════════════════════════════════════════


class TestClaudeJson:
    @pytest.mark.asyncio
    @patch("app.utils.claude_client.claude_text", new_callable=AsyncMock)
    async def test_parses_json_from_text(self, mock_text):
        mock_text.return_value = '```json\n{"key": "value"}\n```'

        result = await claude_json("test")
        assert result == {"key": "value"}

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.claude_text", new_callable=AsyncMock)
    async def test_no_text_returns_none(self, mock_text):
        mock_text.return_value = None

        result = await claude_json("test")
        assert result is None

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.claude_text", new_callable=AsyncMock)
    async def test_unavailable_propagates(self, mock_text):
        """ClaudeUnavailableError from claude_text propagates through claude_json."""
        mock_text.side_effect = ClaudeUnavailableError("not configured")

        with pytest.raises(ClaudeUnavailableError):
            await claude_json("test")


# ═══════════════════════════════════════════════════════════════════════
#  _build_batch_request — pure, no mock
# ═══════════════════════════════════════════════════════════════════════


class TestBuildBatchRequest:
    def test_output_structure(self):
        req = _build_batch_request(
            custom_id="req-001",
            prompt="Parse this",
            schema={"type": "object", "properties": {"name": {"type": "string"}}},
        )
        assert req["custom_id"] == "req-001"
        assert "params" in req
        params = req["params"]
        assert params["messages"][0]["content"] == "Parse this"
        assert len(params["tools"]) == 1
        assert params["tools"][0]["name"] == "structured_output"

    def test_system_prompt_included(self):
        req = _build_batch_request(
            custom_id="req-002",
            prompt="test",
            schema={"type": "object"},
            system="You are a parser.",
        )
        assert "system" in req["params"]
        assert req["params"]["system"][0]["text"] == "You are a parser."

    def test_no_system_prompt_excluded(self):
        req = _build_batch_request(
            custom_id="req-003",
            prompt="test",
            schema={"type": "object"},
            system="",
        )
        assert "system" not in req["params"]

    @pytest.mark.parametrize(
        ("custom_id", "model_tier", "expected_tier"),
        [
            ("req-004", "fast", "fast"),
            ("req-005", "smart", "smart"),
            ("req-006", "nonexistent", "fast"),
        ],
        ids=["fast", "smart", "unknown_defaults_to_fast"],
    )
    def test_tier_routing(self, custom_id, model_tier, expected_tier):
        req = _build_batch_request(
            custom_id=custom_id,
            prompt="test",
            schema={"type": "object"},
            model_tier=model_tier,
        )
        assert req["params"]["model"] == MODELS[expected_tier]

    def test_custom_max_tokens(self):
        req = _build_batch_request(
            custom_id="req-007",
            prompt="test",
            schema={"type": "object"},
            max_tokens=2048,
        )
        assert req["params"]["max_tokens"] == 2048


# ═══════════════════════════════════════════════════════════════════════
#  claude_batch_submit — mock HTTP
# ═══════════════════════════════════════════════════════════════════════


class TestClaudeBatchSubmit:
    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_success_returns_batch_id(self, mock_http, mock_cred):
        mock_http.post = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "id": "batch_abc123",
                    "request_counts": {"processing": 2},
                },
            )
        )

        result = await claude_batch_submit(
            [
                {"custom_id": "r1", "prompt": "p1", "schema": {"type": "object"}},
                {"custom_id": "r2", "prompt": "p2", "schema": {"type": "object"}},
            ]
        )
        assert result == "batch_abc123"

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", return_value=None)
    async def test_no_api_key_raises_unavailable(self, mock_cred):
        with pytest.raises(ClaudeUnavailableError):
            await claude_batch_submit(
                [
                    {"custom_id": "r1", "prompt": "p1", "schema": {"type": "object"}},
                ]
            )

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    async def test_empty_requests_returns_none(self, mock_cred):
        result = await claude_batch_submit([])
        assert result is None

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_http_500_raises_server_error(self, mock_http, mock_cred):
        mock_http.post = AsyncMock(return_value=_mock_response(500, text="Internal Server Error"))

        with pytest.raises(ClaudeServerError):
            await claude_batch_submit(
                [
                    {"custom_id": "r1", "prompt": "p1", "schema": {"type": "object"}},
                ]
            )

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_connection_error_raises_claude_error(self, mock_http, mock_cred):
        mock_http.post = AsyncMock(side_effect=ConnectionError("timeout"))

        with pytest.raises(ClaudeError):
            await claude_batch_submit(
                [
                    {"custom_id": "r1", "prompt": "p1", "schema": {"type": "object"}},
                ]
            )


# ═══════════════════════════════════════════════════════════════════════
#  claude_batch_results — mock HTTP
# ═══════════════════════════════════════════════════════════════════════


class TestClaudeBatchResults:
    def _make_jsonl(self, entries):
        """Build JSONL string from list of (custom_id, result_dict) tuples."""
        import json

        lines = []
        for cid, result in entries:
            lines.append(json.dumps({"custom_id": cid, "result": result}))
        return "\n".join(lines)

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_completed_batch_parses_jsonl(self, mock_http, mock_cred):
        jsonl = self._make_jsonl(
            [
                (
                    "r1",
                    {
                        "type": "succeeded",
                        "message": {
                            "content": [{"type": "tool_use", "name": "structured_output", "input": {"name": "Acme"}}]
                        },
                    },
                ),
                (
                    "r2",
                    {
                        "type": "succeeded",
                        "message": {
                            "content": [{"type": "tool_use", "name": "structured_output", "input": {"name": "Beta"}}]
                        },
                    },
                ),
            ]
        )

        # First call: status check
        status_resp = _mock_response(
            200,
            {
                "processing_status": "ended",
                "results_url": "https://api.anthropic.com/results/batch_abc123",
                "request_counts": {"succeeded": 2, "errored": 0},
            },
        )
        # Second call: results fetch
        results_resp = _mock_response(200, text=jsonl)

        mock_http.get = AsyncMock(side_effect=[status_resp, results_resp])

        result = await claude_batch_results("batch_abc123")
        assert result == {"r1": {"name": "Acme"}, "r2": {"name": "Beta"}}

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_still_processing_returns_none(self, mock_http, mock_cred):
        mock_http.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "processing_status": "in_progress",
                },
            )
        )

        result = await claude_batch_results("batch_abc123")
        assert result is None

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_no_results_url_returns_none(self, mock_http, mock_cred):
        mock_http.get = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "processing_status": "ended",
                    # No results_url
                },
            )
        )

        result = await claude_batch_results("batch_abc123")
        assert result is None

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_failed_entry_has_none_value(self, mock_http, mock_cred):
        jsonl = self._make_jsonl(
            [
                (
                    "r1",
                    {
                        "type": "errored",
                        "error": {"type": "server_error", "message": "Internal error"},
                    },
                ),
            ]
        )

        status_resp = _mock_response(
            200,
            {
                "processing_status": "ended",
                "results_url": "https://api.anthropic.com/results/batch_fail",
                "request_counts": {"succeeded": 0, "errored": 1},
            },
        )
        results_resp = _mock_response(200, text=jsonl)
        mock_http.get = AsyncMock(side_effect=[status_resp, results_resp])

        result = await claude_batch_results("batch_fail")
        assert result == {"r1": None}

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", return_value=None)
    async def test_no_api_key_raises_unavailable(self, mock_cred):
        with pytest.raises(ClaudeUnavailableError):
            await claude_batch_results("batch_abc123")

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    async def test_empty_batch_id_returns_none(self, mock_cred):
        result = await claude_batch_results("")
        assert result is None

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_status_check_500_raises_server_error(self, mock_http, mock_cred):
        mock_http.get = AsyncMock(return_value=_mock_response(500, text="Server Error"))

        with pytest.raises(ClaudeServerError):
            await claude_batch_results("batch_abc123")

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_results_fetch_error_returns_none(self, mock_http, mock_cred):
        """When results_url returns non-200, returns None."""
        status_resp = _mock_response(
            200,
            {
                "processing_status": "ended",
                "results_url": "https://api.anthropic.com/results/batch_err",
                "request_counts": {"succeeded": 0},
            },
        )
        results_resp = _mock_response(500, text="Error")
        mock_http.get = AsyncMock(side_effect=[status_resp, results_resp])

        result = await claude_batch_results("batch_err")
        assert result is None

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_connection_error_raises_claude_error(self, mock_http, mock_cred):
        """Connection error during batch results check raises ClaudeError."""
        mock_http.get = AsyncMock(side_effect=ConnectionError("timeout"))

        with pytest.raises(ClaudeError):
            await claude_batch_results("batch_abc123")

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_succeeded_no_tool_use_returns_none_value(self, mock_http, mock_cred):
        """Succeeded result without tool_use block has None value."""
        import json

        jsonl = json.dumps(
            {
                "custom_id": "r1",
                "result": {
                    "type": "succeeded",
                    "message": {"content": [{"type": "text", "text": "No structured output"}]},
                },
            }
        )
        status_resp = _mock_response(
            200,
            {
                "processing_status": "ended",
                "results_url": "https://api.anthropic.com/results/batch_no_tool",
                "request_counts": {"succeeded": 1},
            },
        )
        results_resp = _mock_response(200, text=jsonl)
        mock_http.get = AsyncMock(side_effect=[status_resp, results_resp])

        result = await claude_batch_results("batch_no_tool")
        assert result == {"r1": None}

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_jsonl_parse_error_skipped(self, mock_http, mock_cred):
        """Malformed JSONL lines are skipped."""
        jsonl = "not valid json\n{malformed"
        status_resp = _mock_response(
            200,
            {
                "processing_status": "ended",
                "results_url": "https://api.anthropic.com/results/batch_bad",
                "request_counts": {"succeeded": 0},
            },
        )
        results_resp = _mock_response(200, text=jsonl)
        mock_http.get = AsyncMock(side_effect=[status_resp, results_resp])

        result = await claude_batch_results("batch_bad")
        assert result == {}

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_empty_lines_in_jsonl_skipped(self, mock_http, mock_cred):
        """Empty lines in JSONL are skipped."""
        import json

        line = json.dumps(
            {
                "custom_id": "r1",
                "result": {
                    "type": "succeeded",
                    "message": {"content": [{"type": "tool_use", "name": "structured_output", "input": {"ok": True}}]},
                },
            }
        )
        jsonl = f"\n{line}\n\n"
        status_resp = _mock_response(
            200,
            {
                "processing_status": "ended",
                "results_url": "https://api.anthropic.com/results/batch_x",
                "request_counts": {"succeeded": 1},
            },
        )
        results_resp = _mock_response(200, text=jsonl)
        mock_http.get = AsyncMock(side_effect=[status_resp, results_resp])

        result = await claude_batch_results("batch_x")
        assert result == {"r1": {"ok": True}}


# ═══════════════════════════════════════════════════════════════════════
#  claude_structured — additional coverage (thinking_budget, system blocks)
# ═══════════════════════════════════════════════════════════════════════


class TestClaudeStructuredAdditional:
    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_thinking_budget_forces_smart_model(self, mock_http, mock_cred):
        """When thinking_budget is set, uses SMART model regardless of model_tier."""
        mock_http.post = AsyncMock(return_value=_tool_use_response({"x": 1}))

        await claude_structured("test", {"type": "object"}, model_tier="fast", thinking_budget=5000)
        body = mock_http.post.call_args.kwargs["json"]
        assert body["model"] == MODELS["smart"]
        assert "thinking" in body
        assert body["thinking"]["budget_tokens"] == 5000
        assert body["max_tokens"] >= 5000 + 1024

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_no_system_prompt_excluded(self, mock_http, mock_cred):
        """When system is empty, no system key in body."""
        mock_http.post = AsyncMock(return_value=_tool_use_response())

        await claude_structured("test", {"type": "object"}, system="")
        body = mock_http.post.call_args.kwargs["json"]
        assert "system" not in body

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_cache_system_false_no_cache_control(self, mock_http, mock_cred):
        """When cache_system=False, system block has no cache_control."""
        mock_http.post = AsyncMock(return_value=_tool_use_response())

        await claude_structured("test", {"type": "object"}, system="system prompt", cache_system=False)
        body = mock_http.post.call_args.kwargs["json"]
        assert "system" in body
        assert "cache_control" not in body["system"][0]

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_unknown_model_tier_defaults_fast(self, mock_http, mock_cred):
        """Unknown model_tier falls back to 'fast'."""
        mock_http.post = AsyncMock(return_value=_tool_use_response())

        await claude_structured("test", {"type": "object"}, model_tier="nonexistent")
        body = mock_http.post.call_args.kwargs["json"]
        assert body["model"] == MODELS["fast"]


# ═══════════════════════════════════════════════════════════════════════
#  claude_text — additional coverage (tools, system, model tiers)
# ═══════════════════════════════════════════════════════════════════════


class TestClaudeTextAdditional:
    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_tools_included_in_body(self, mock_http, mock_cred):
        """Tools parameter is passed to API body."""
        mock_http.post = AsyncMock(return_value=_mock_response(200, {"content": [{"type": "text", "text": "result"}]}))

        tools = [{"type": "web_search_20250305"}]
        await claude_text("test", tools=tools)
        body = mock_http.post.call_args.kwargs["json"]
        assert body["tools"] == tools


# ═══════════════════════════════════════════════════════════════════════
#  _headers — cache=True adds beta header
# ═══════════════════════════════════════════════════════════════════════


class TestHeaders:
    @patch("app.utils.claude_client.get_credential_cached", return_value="test-key")
    def test_cache_true_adds_beta_header(self, mock_cred):
        from app.utils.claude_client import _headers

        h = _headers(cache=True)
        assert "anthropic-beta" in h
        assert "prompt-caching" in h["anthropic-beta"]

    @patch("app.utils.claude_client.get_credential_cached", return_value="test-key")
    def test_cache_false_no_beta_header(self, mock_cred):
        from app.utils.claude_client import _headers

        h = _headers(cache=False)
        assert "anthropic-beta" not in h


# ═══════════════════════════════════════════════════════════════════════
#  Error hierarchy — verify inheritance
# ═══════════════════════════════════════════════════════════════════════


class TestErrorHierarchy:
    """Verify that all Claude error types inherit correctly."""

    def test_base_is_exception(self):
        assert issubclass(ClaudeError, Exception)

    @pytest.mark.parametrize(
        "exc_type",
        [ClaudeAuthError, ClaudeRateLimitError, ClaudeServerError, ClaudeUnavailableError],
        ids=["auth", "rate_limit", "server", "unavailable"],
    )
    def test_subtype_inherits_base(self, exc_type):
        assert issubclass(exc_type, ClaudeError)

    def test_catch_base_catches_all(self):
        """Catching ClaudeError catches all subtypes."""
        for exc_type in (ClaudeAuthError, ClaudeRateLimitError, ClaudeServerError, ClaudeUnavailableError):
            try:
                raise exc_type("test")
            except ClaudeError:
                pass  # Expected
            except Exception:
                pytest.fail(f"{exc_type.__name__} not caught by ClaudeError")

    def test_error_messages_preserved(self):
        """Error messages are accessible via str()."""
        err = ClaudeAuthError("bad key")
        assert "bad key" in str(err)

    def test_specific_catch_does_not_catch_siblings(self):
        """ClaudeAuthError catch does NOT catch ClaudeRateLimitError."""
        with pytest.raises(ClaudeRateLimitError):
            try:
                raise ClaudeRateLimitError("rate limited")
            except ClaudeAuthError:
                pytest.fail("Should not be caught")


# ═══════════════════════════════════════════════════════════════════════
#  Error differentiation — different status codes produce different errors
# ═══════════════════════════════════════════════════════════════════════


class TestErrorDifferentiation:
    """Verify that different HTTP status codes produce distinguishable exceptions."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status_code", "body_text", "expected_exc"),
        [
            (401, "Unauthorized", ClaudeAuthError),
            (429, "Rate limited", ClaudeRateLimitError),
            (500, "Server Error", ClaudeServerError),
            (503, "Overloaded", ClaudeServerError),
        ],
        ids=["401_auth", "429_rate_limit", "500_server", "503_server"],
    )
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_structured_status_maps_to_exc(self, mock_http, mock_cred, status_code, body_text, expected_exc):
        mock_http.post = AsyncMock(return_value=_mock_response(status_code, text=body_text))
        with pytest.raises(expected_exc):
            await claude_structured("test", {"type": "object"})

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status_code", "body_text", "expected_exc"),
        [
            (401, "Unauthorized", ClaudeAuthError),
            (429, "Rate limited", ClaudeRateLimitError),
            (500, "Server Error", ClaudeServerError),
        ],
        ids=["401_auth", "429_rate_limit", "500_server"],
    )
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_text_status_maps_to_exc(self, mock_http, mock_cred, status_code, body_text, expected_exc):
        mock_http.post = AsyncMock(return_value=_mock_response(status_code, text=body_text))
        with pytest.raises(expected_exc):
            await claude_text("test")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "call",
        [
            lambda: claude_structured("test", {"type": "object"}),
            lambda: claude_text("test"),
            lambda: claude_batch_submit([{"custom_id": "r1", "prompt": "p1", "schema": {"type": "object"}}]),
            lambda: claude_batch_results("batch_123"),
        ],
        ids=["structured", "text", "batch_submit", "batch_results"],
    )
    @patch("app.utils.claude_client.get_credential_cached", return_value=None)
    async def test_no_key_is_unavailable(self, mock_cred, call):
        with pytest.raises(ClaudeUnavailableError):
            await call()


# ═══════════════════════════════════════════════════════════════════════
#  Fix 2: batch metering via cost_bucket
# ═══════════════════════════════════════════════════════════════════════


class TestBatchMetering:
    """Verify that cost_bucket="email_mining" routes to _meter_usage."""

    def _make_jsonl(self, entries):
        import json

        return "\n".join(json.dumps({"custom_id": cid, "result": r}) for cid, r in entries)

    @pytest.mark.asyncio
    @patch("app.utils.claude_client._meter_usage")
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_batch_results_meters_with_bucket(self, mock_http, mock_cred, mock_meter):
        """cost_bucket triggers _meter_usage for each succeeded entry."""
        jsonl = self._make_jsonl(
            [
                (
                    "r1",
                    {
                        "type": "succeeded",
                        "message": {
                            "model": "claude-haiku-3",
                            "usage": {"input_tokens": 100, "output_tokens": 50},
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "structured_output",
                                    "input": {"parsed": True},
                                }
                            ],
                        },
                    },
                ),
            ]
        )
        status_resp = _mock_response(
            200,
            {
                "processing_status": "ended",
                "results_url": "https://api.anthropic.com/results/batch_m1",
                "request_counts": {"succeeded": 1, "errored": 0},
            },
        )
        results_resp = _mock_response(200, text=jsonl)
        mock_http.get = AsyncMock(side_effect=[status_resp, results_resp])

        result = await claude_batch_results("batch_m1", cost_bucket="email_mining")

        assert result == {"r1": {"parsed": True}}
        mock_meter.assert_called_once()
        call_args = mock_meter.call_args
        assert call_args[0][0] == "email_mining"  # bucket
        assert call_args[0][1] == "fast"  # haiku → fast

    @pytest.mark.asyncio
    @patch("app.utils.claude_client._meter_usage")
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_batch_results_no_bucket_skips_meter(self, mock_http, mock_cred, mock_meter):
        """Without cost_bucket, _meter_usage is NOT called."""
        jsonl = self._make_jsonl(
            [
                (
                    "r1",
                    {
                        "type": "succeeded",
                        "message": {
                            "model": "claude-haiku-3",
                            "usage": {"input_tokens": 100, "output_tokens": 50},
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "structured_output",
                                    "input": {"parsed": True},
                                }
                            ],
                        },
                    },
                ),
            ]
        )
        status_resp = _mock_response(
            200,
            {
                "processing_status": "ended",
                "results_url": "https://api.anthropic.com/results/batch_nm",
                "request_counts": {"succeeded": 1, "errored": 0},
            },
        )
        results_resp = _mock_response(200, text=jsonl)
        mock_http.get = AsyncMock(side_effect=[status_resp, results_resp])

        await claude_batch_results("batch_nm")  # no cost_bucket

        mock_meter.assert_not_called()
