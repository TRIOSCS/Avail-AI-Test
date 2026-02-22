"""
test_claude_client.py — Tests for app/utils/claude_client.py

Mock HTTP calls and credential lookups to test Claude API wrappers.

Called by: pytest
Depends on: app/utils/claude_client.py
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


# ═══════════════════════════════════════════════════════════════════════
#  safe_json_parse — pure, no mock
# ═══════════════════════════════════════════════════════════════════════


class TestSafeJsonParse:
    def test_plain_json_object(self):
        assert safe_json_parse('{"key": "value"}') == {"key": "value"}

    def test_plain_json_array(self):
        assert safe_json_parse('[1, 2, 3]') == [1, 2, 3]

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


# ═══════════════════════════════════════════════════════════════════════
#  claude_structured — mock HTTP
# ═══════════════════════════════════════════════════════════════════════


class TestClaudeStructured:
    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_success_extracts_tool_input(self, mock_http, mock_cred):
        mock_http.post = AsyncMock(return_value=_mock_response(200, {
            "content": [
                {"type": "tool_use", "name": "structured_output", "input": {"parsed": True}}
            ]
        }))

        result = await claude_structured("test prompt", {"type": "object"})
        assert result == {"parsed": True}

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_api_error_returns_none(self, mock_http, mock_cred):
        mock_http.post = AsyncMock(return_value=_mock_response(500, text="Server Error"))

        result = await claude_structured("test", {"type": "object"})
        assert result is None

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", return_value=None)
    async def test_no_api_key_returns_none(self, mock_cred):
        result = await claude_structured("test", {"type": "object"})
        assert result is None

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_no_tool_use_block_returns_none(self, mock_http, mock_cred):
        mock_http.post = AsyncMock(return_value=_mock_response(200, {
            "content": [{"type": "text", "text": "No tool use here"}]
        }))

        result = await claude_structured("test", {"type": "object"})
        assert result is None

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_fast_tier_uses_haiku(self, mock_http, mock_cred):
        mock_http.post = AsyncMock(return_value=_mock_response(200, {
            "content": [{"type": "tool_use", "name": "structured_output", "input": {}}]
        }))

        await claude_structured("test", {"type": "object"}, model_tier="fast")
        body = mock_http.post.call_args.kwargs["json"]
        assert body["model"] == MODELS["fast"]

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_smart_tier_uses_sonnet(self, mock_http, mock_cred):
        mock_http.post = AsyncMock(return_value=_mock_response(200, {
            "content": [{"type": "tool_use", "name": "structured_output", "input": {}}]
        }))

        await claude_structured("test", {"type": "object"}, model_tier="smart")
        body = mock_http.post.call_args.kwargs["json"]
        assert body["model"] == MODELS["smart"]

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_connection_error_returns_none(self, mock_http, mock_cred):
        mock_http.post = AsyncMock(side_effect=ConnectionError("Connection refused"))

        result = await claude_structured("test", {"type": "object"})
        assert result is None


# ═══════════════════════════════════════════════════════════════════════
#  claude_text — mock HTTP
# ═══════════════════════════════════════════════════════════════════════


class TestClaudeText:
    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_joins_text_blocks(self, mock_http, mock_cred):
        mock_http.post = AsyncMock(return_value=_mock_response(200, {
            "content": [
                {"type": "text", "text": "Hello"},
                {"type": "text", "text": "World"},
            ]
        }))

        result = await claude_text("test")
        assert result == "Hello\nWorld"

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_no_text_blocks_returns_none(self, mock_http, mock_cred):
        mock_http.post = AsyncMock(return_value=_mock_response(200, {
            "content": [{"type": "tool_use", "name": "web_search", "input": {}}]
        }))

        result = await claude_text("test")
        assert result is None

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", return_value=None)
    async def test_no_api_key_returns_none(self, mock_cred):
        result = await claude_text("test")
        assert result is None


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

    def test_fast_tier_routing(self):
        req = _build_batch_request(
            custom_id="req-004",
            prompt="test",
            schema={"type": "object"},
            model_tier="fast",
        )
        assert req["params"]["model"] == MODELS["fast"]

    def test_smart_tier_routing(self):
        req = _build_batch_request(
            custom_id="req-005",
            prompt="test",
            schema={"type": "object"},
            model_tier="smart",
        )
        assert req["params"]["model"] == MODELS["smart"]

    def test_unknown_tier_defaults_to_fast(self):
        req = _build_batch_request(
            custom_id="req-006",
            prompt="test",
            schema={"type": "object"},
            model_tier="nonexistent",
        )
        assert req["params"]["model"] == MODELS["fast"]

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
        mock_http.post = AsyncMock(return_value=_mock_response(200, {
            "id": "batch_abc123",
            "request_counts": {"processing": 2},
        }))

        result = await claude_batch_submit([
            {"custom_id": "r1", "prompt": "p1", "schema": {"type": "object"}},
            {"custom_id": "r2", "prompt": "p2", "schema": {"type": "object"}},
        ])
        assert result == "batch_abc123"

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", return_value=None)
    async def test_no_api_key_returns_none(self, mock_cred):
        result = await claude_batch_submit([
            {"custom_id": "r1", "prompt": "p1", "schema": {"type": "object"}},
        ])
        assert result is None

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    async def test_empty_requests_returns_none(self, mock_cred):
        result = await claude_batch_submit([])
        assert result is None

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_http_500_returns_none(self, mock_http, mock_cred):
        mock_http.post = AsyncMock(return_value=_mock_response(500, text="Internal Server Error"))

        result = await claude_batch_submit([
            {"custom_id": "r1", "prompt": "p1", "schema": {"type": "object"}},
        ])
        assert result is None

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_connection_error_returns_none(self, mock_http, mock_cred):
        mock_http.post = AsyncMock(side_effect=ConnectionError("timeout"))

        result = await claude_batch_submit([
            {"custom_id": "r1", "prompt": "p1", "schema": {"type": "object"}},
        ])
        assert result is None


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
        jsonl = self._make_jsonl([
            ("r1", {
                "type": "succeeded",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "structured_output", "input": {"name": "Acme"}}
                    ]
                },
            }),
            ("r2", {
                "type": "succeeded",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "structured_output", "input": {"name": "Beta"}}
                    ]
                },
            }),
        ])

        # First call: status check
        status_resp = _mock_response(200, {
            "processing_status": "ended",
            "results_url": "https://api.anthropic.com/results/batch_abc123",
            "request_counts": {"succeeded": 2, "errored": 0},
        })
        # Second call: results fetch
        results_resp = _mock_response(200, text=jsonl)

        mock_http.get = AsyncMock(side_effect=[status_resp, results_resp])

        result = await claude_batch_results("batch_abc123")
        assert result == {"r1": {"name": "Acme"}, "r2": {"name": "Beta"}}

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_still_processing_returns_none(self, mock_http, mock_cred):
        mock_http.get = AsyncMock(return_value=_mock_response(200, {
            "processing_status": "in_progress",
        }))

        result = await claude_batch_results("batch_abc123")
        assert result is None

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_no_results_url_returns_none(self, mock_http, mock_cred):
        mock_http.get = AsyncMock(return_value=_mock_response(200, {
            "processing_status": "ended",
            # No results_url
        }))

        result = await claude_batch_results("batch_abc123")
        assert result is None

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_failed_entry_has_none_value(self, mock_http, mock_cred):
        jsonl = self._make_jsonl([
            ("r1", {
                "type": "errored",
                "error": {"type": "server_error", "message": "Internal error"},
            }),
        ])

        status_resp = _mock_response(200, {
            "processing_status": "ended",
            "results_url": "https://api.anthropic.com/results/batch_fail",
            "request_counts": {"succeeded": 0, "errored": 1},
        })
        results_resp = _mock_response(200, text=jsonl)
        mock_http.get = AsyncMock(side_effect=[status_resp, results_resp])

        result = await claude_batch_results("batch_fail")
        assert result == {"r1": None}

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", return_value=None)
    async def test_no_api_key_returns_none(self, mock_cred):
        result = await claude_batch_results("batch_abc123")
        assert result is None

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    async def test_empty_batch_id_returns_none(self, mock_cred):
        result = await claude_batch_results("")
        assert result is None

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.get_credential_cached", side_effect=_cred_side_effect)
    @patch("app.utils.claude_client.http")
    async def test_status_check_error_returns_none(self, mock_http, mock_cred):
        mock_http.get = AsyncMock(return_value=_mock_response(500, text="Server Error"))

        result = await claude_batch_results("batch_abc123")
        assert result is None
