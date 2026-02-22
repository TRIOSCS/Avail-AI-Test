"""Tests for Gradient AI Service — mocked HTTP calls, no real API needed.

Covers: gradient_text, gradient_json, gradient_batch_json, retry logic,
        JSON parsing edge cases, token logging, error handling.
"""

import json
import os

os.environ["TESTING"] = "1"
os.environ["DO_GRADIENT_API_KEY"] = "test-key-for-testing"

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.gradient_service import (
    gradient_text,
    gradient_json,
    gradient_batch_json,
    _safe_json_parse,
    _call_llm,
    MODELS,
    API_URL,
)


# ── Fixtures ──────────────────────────────────────────────────────────


def _mock_response(status_code=200, json_data=None, text=""):
    """Create a mock httpx response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text or json.dumps(json_data or {})
    resp.json.return_value = json_data or {}
    return resp


def _chat_response(content, model="anthropic-claude-4.5-sonnet", prompt_tokens=50, completion_tokens=20):
    """Build a standard chat completion response dict."""
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
        "model": model,
    }


# ── Model tier tests ─────────────────────────────────────────────────


def test_model_tiers():
    """Verify model tier mapping."""
    assert MODELS["default"] == "anthropic-claude-4.5-sonnet"
    assert MODELS["strong"] == "anthropic-claude-opus-4.6"


# ── gradient_text tests ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gradient_text_basic():
    """Basic text generation returns the model's content."""
    mock_data = _chat_response("Hello, I can help with sourcing.")
    with patch("app.services.gradient_service.http") as mock_http:
        mock_http.post = AsyncMock(return_value=_mock_response(200, mock_data))
        result = await gradient_text("Say hello")

    assert result == "Hello, I can help with sourcing."
    mock_http.post.assert_called_once()


@pytest.mark.asyncio
async def test_gradient_text_with_system_prompt():
    """System prompt is included in the messages."""
    mock_data = _chat_response("Professional response")
    with patch("app.services.gradient_service.http") as mock_http:
        mock_http.post = AsyncMock(return_value=_mock_response(200, mock_data))
        result = await gradient_text(
            "Draft an RFQ",
            system="You are a sourcing assistant.",
            temperature=0.6,
        )

    assert result == "Professional response"
    call_args = mock_http.post.call_args
    body = call_args.kwargs.get("json") or call_args[1].get("json")
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][0]["content"] == "You are a sourcing assistant."
    assert body["messages"][1]["role"] == "user"
    assert body["temperature"] == 0.6


@pytest.mark.asyncio
async def test_gradient_text_default_model():
    """Default model tier uses anthropic-claude-4.5-sonnet."""
    mock_data = _chat_response("result")
    with patch("app.services.gradient_service.http") as mock_http:
        mock_http.post = AsyncMock(return_value=_mock_response(200, mock_data))
        await gradient_text("Test")

    call_args = mock_http.post.call_args
    body = call_args.kwargs.get("json") or call_args[1].get("json")
    assert body["model"] == "anthropic-claude-4.5-sonnet"


@pytest.mark.asyncio
async def test_gradient_text_model_tier_strong():
    """model_tier='strong' uses the larger model."""
    mock_data = _chat_response("analysis", model="anthropic-claude-opus-4.6")
    with patch("app.services.gradient_service.http") as mock_http:
        mock_http.post = AsyncMock(return_value=_mock_response(200, mock_data))
        await gradient_text("Analyze this", model_tier="strong")

    call_args = mock_http.post.call_args
    body = call_args.kwargs.get("json") or call_args[1].get("json")
    assert body["model"] == MODELS["strong"]


@pytest.mark.asyncio
async def test_gradient_text_explicit_model():
    """Explicit model overrides model_tier."""
    mock_data = _chat_response("result")
    with patch("app.services.gradient_service.http") as mock_http:
        mock_http.post = AsyncMock(return_value=_mock_response(200, mock_data))
        await gradient_text("Test", model="openai-gpt-5-2")

    call_args = mock_http.post.call_args
    body = call_args.kwargs.get("json") or call_args[1].get("json")
    assert body["model"] == "openai-gpt-5-2"


@pytest.mark.asyncio
async def test_gradient_text_no_api_key():
    """Returns None when API key is not configured."""
    with patch("app.services.gradient_service.settings") as mock_settings:
        mock_settings.do_gradient_api_key = ""
        result = await gradient_text("Hello")

    assert result is None


@pytest.mark.asyncio
async def test_gradient_text_api_error():
    """Returns None on non-retryable API error (e.g. 401)."""
    with patch("app.services.gradient_service.http") as mock_http:
        mock_http.post = AsyncMock(
            return_value=_mock_response(401, text="Unauthorized")
        )
        result = await gradient_text("Hello")

    assert result is None


@pytest.mark.asyncio
async def test_gradient_text_empty_choices():
    """Returns None when response has no choices."""
    mock_data = {"choices": [], "usage": {}}
    with patch("app.services.gradient_service.http") as mock_http:
        mock_http.post = AsyncMock(return_value=_mock_response(200, mock_data))
        result = await gradient_text("Hello")

    assert result is None


@pytest.mark.asyncio
async def test_gradient_text_custom_timeout():
    """Custom timeout is passed through to HTTP call."""
    mock_data = _chat_response("result")
    with patch("app.services.gradient_service.http") as mock_http:
        mock_http.post = AsyncMock(return_value=_mock_response(200, mock_data))
        await gradient_text("Test", timeout=60)

    call_args = mock_http.post.call_args
    assert call_args.kwargs.get("timeout") == 60


# ── gradient_json tests ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gradient_json_clean():
    """Parses clean JSON response."""
    json_str = json.dumps({"part_number": "LM358DR", "manufacturer": "TI"})
    mock_data = _chat_response(json_str)
    with patch("app.services.gradient_service.http") as mock_http:
        mock_http.post = AsyncMock(return_value=_mock_response(200, mock_data))
        result = await gradient_json("Parse this part number: LM358DR")

    assert result == {"part_number": "LM358DR", "manufacturer": "TI"}


@pytest.mark.asyncio
async def test_gradient_json_markdown_fences():
    """Parses JSON wrapped in markdown code fences."""
    content = '```json\n{"status": "active", "price": 1.25}\n```'
    mock_data = _chat_response(content)
    with patch("app.services.gradient_service.http") as mock_http:
        mock_http.post = AsyncMock(return_value=_mock_response(200, mock_data))
        result = await gradient_json("Get status")

    assert result == {"status": "active", "price": 1.25}


@pytest.mark.asyncio
async def test_gradient_json_with_preamble():
    """Extracts JSON even when the model adds text before/after."""
    content = 'Here is the result:\n{"vendor": "Mouser", "score": 95}\nHope this helps!'
    mock_data = _chat_response(content)
    with patch("app.services.gradient_service.http") as mock_http:
        mock_http.post = AsyncMock(return_value=_mock_response(200, mock_data))
        result = await gradient_json("Vendor info")

    assert result == {"vendor": "Mouser", "score": 95}


@pytest.mark.asyncio
async def test_gradient_json_array_response():
    """Parses JSON array response."""
    content = json.dumps([{"mpn": "STM32F4"}, {"mpn": "LM358"}])
    mock_data = _chat_response(content)
    with patch("app.services.gradient_service.http") as mock_http:
        mock_http.post = AsyncMock(return_value=_mock_response(200, mock_data))
        result = await gradient_json("List parts")

    assert isinstance(result, list)
    assert len(result) == 2


@pytest.mark.asyncio
async def test_gradient_json_malformed():
    """Returns None on completely unparseable response."""
    mock_data = _chat_response("I don't have that information, sorry.")
    with patch("app.services.gradient_service.http") as mock_http:
        mock_http.post = AsyncMock(return_value=_mock_response(200, mock_data))
        result = await gradient_json("Parse this")

    assert result is None


@pytest.mark.asyncio
async def test_gradient_json_appends_json_instruction():
    """Appends JSON instruction to system prompt if not present."""
    mock_data = _chat_response('{"ok": true}')
    with patch("app.services.gradient_service.http") as mock_http:
        mock_http.post = AsyncMock(return_value=_mock_response(200, mock_data))
        await gradient_json("Test", system="You are a parser.")

    call_args = mock_http.post.call_args
    body = call_args.kwargs.get("json") or call_args[1].get("json")
    system_msg = body["messages"][0]["content"]
    assert "Return ONLY valid JSON" in system_msg


@pytest.mark.asyncio
async def test_gradient_json_skips_instruction_if_json_in_system():
    """Does not append redundant instruction when system already mentions JSON."""
    mock_data = _chat_response('{"ok": true}')
    with patch("app.services.gradient_service.http") as mock_http:
        mock_http.post = AsyncMock(return_value=_mock_response(200, mock_data))
        await gradient_json("Test", system="Return valid JSON only.")

    call_args = mock_http.post.call_args
    body = call_args.kwargs.get("json") or call_args[1].get("json")
    system_msg = body["messages"][0]["content"]
    assert system_msg == "Return valid JSON only."


# ── Retry logic tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retry_on_500():
    """Retries on 500 and succeeds on second attempt."""
    mock_data = _chat_response("recovered")
    with patch("app.services.gradient_service.http") as mock_http:
        mock_http.post = AsyncMock(
            side_effect=[
                _mock_response(500, text="Internal Server Error"),
                _mock_response(200, mock_data),
            ]
        )
        with patch("app.services.gradient_service.asyncio.sleep", new_callable=AsyncMock):
            result = await gradient_text("Test retry")

    assert result == "recovered"
    assert mock_http.post.call_count == 2


@pytest.mark.asyncio
async def test_retry_on_429():
    """Retries on rate limit (429)."""
    mock_data = _chat_response("success after rate limit")
    with patch("app.services.gradient_service.http") as mock_http:
        mock_http.post = AsyncMock(
            side_effect=[
                _mock_response(429, text="Rate limited"),
                _mock_response(200, mock_data),
            ]
        )
        with patch("app.services.gradient_service.asyncio.sleep", new_callable=AsyncMock):
            result = await gradient_text("Test rate limit")

    assert result == "success after rate limit"


@pytest.mark.asyncio
async def test_retry_on_502():
    """Retries on 502 Bad Gateway."""
    mock_data = _chat_response("recovered from gateway")
    with patch("app.services.gradient_service.http") as mock_http:
        mock_http.post = AsyncMock(
            side_effect=[
                _mock_response(502, text="Bad Gateway"),
                _mock_response(200, mock_data),
            ]
        )
        with patch("app.services.gradient_service.asyncio.sleep", new_callable=AsyncMock):
            result = await gradient_text("Test 502")

    assert result == "recovered from gateway"


@pytest.mark.asyncio
async def test_retry_on_exception():
    """Retries on network exceptions."""
    mock_data = _chat_response("recovered from error")
    with patch("app.services.gradient_service.http") as mock_http:
        mock_http.post = AsyncMock(
            side_effect=[
                TimeoutError("Connection timed out"),
                _mock_response(200, mock_data),
            ]
        )
        with patch("app.services.gradient_service.asyncio.sleep", new_callable=AsyncMock):
            result = await gradient_text("Test exception retry")

    assert result == "recovered from error"


@pytest.mark.asyncio
async def test_no_retry_on_401():
    """Does NOT retry on 401 (auth error)."""
    with patch("app.services.gradient_service.http") as mock_http:
        mock_http.post = AsyncMock(
            return_value=_mock_response(401, text="Unauthorized")
        )
        result = await gradient_text("Test no retry")

    assert result is None
    assert mock_http.post.call_count == 1


@pytest.mark.asyncio
async def test_no_retry_on_403():
    """Does NOT retry on 403 (forbidden)."""
    with patch("app.services.gradient_service.http") as mock_http:
        mock_http.post = AsyncMock(
            return_value=_mock_response(403, text="Forbidden")
        )
        result = await gradient_text("Test no retry 403")

    assert result is None
    assert mock_http.post.call_count == 1


@pytest.mark.asyncio
async def test_max_retries_exhausted():
    """Returns None after all retries are exhausted."""
    with patch("app.services.gradient_service.http") as mock_http:
        mock_http.post = AsyncMock(
            return_value=_mock_response(500, text="Server error")
        )
        with patch("app.services.gradient_service.asyncio.sleep", new_callable=AsyncMock):
            result = await gradient_text("Test exhaustion")

    assert result is None
    assert mock_http.post.call_count == 3  # MAX_RETRIES


@pytest.mark.asyncio
async def test_exponential_backoff_delays():
    """Verify exponential backoff: 1s, 2s."""
    sleep_calls = []

    async def mock_sleep(delay):
        sleep_calls.append(delay)

    with patch("app.services.gradient_service.http") as mock_http:
        mock_http.post = AsyncMock(
            return_value=_mock_response(500, text="Server error")
        )
        with patch("app.services.gradient_service.asyncio.sleep", side_effect=mock_sleep):
            await gradient_text("Test backoff")

    assert sleep_calls == [1.0, 2.0]


# ── Batch tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gradient_batch_json():
    """Batch processes multiple prompts concurrently."""
    responses = [
        _chat_response(json.dumps({"normalized": "LM358DR"})),
        _chat_response(json.dumps({"normalized": "STM32F407VGT6"})),
        _chat_response(json.dumps({"normalized": "NE555P"})),
    ]
    call_count = 0

    async def mock_post(*args, **kwargs):
        nonlocal call_count
        idx = call_count
        call_count += 1
        return _mock_response(200, responses[idx % len(responses)])

    with patch("app.services.gradient_service.http") as mock_http:
        mock_http.post = mock_post
        results = await gradient_batch_json(
            ["Normalize LM358DR", "Normalize STM32F407VGT6", "Normalize NE555P"],
            system="Normalize part numbers. Return JSON.",
        )

    assert len(results) == 3
    assert all(r is not None for r in results)


@pytest.mark.asyncio
async def test_gradient_batch_json_partial_failure():
    """Batch returns None for failed items, valid results for others."""
    call_count = 0

    async def mock_post(*args, **kwargs):
        nonlocal call_count
        idx = call_count
        call_count += 1
        if idx == 1:
            return _mock_response(500, text="Server error")
        return _mock_response(200, _chat_response(json.dumps({"ok": True})))

    with patch("app.services.gradient_service.http") as mock_http:
        mock_http.post = mock_post
        with patch("app.services.gradient_service.asyncio.sleep", new_callable=AsyncMock):
            results = await gradient_batch_json(["a", "b", "c"])

    assert len(results) == 3
    assert results[0] is not None
    assert results[2] is not None


# ── _safe_json_parse unit tests ───────────────────────────────────────


def test_parse_clean_json():
    assert _safe_json_parse('{"a": 1}') == {"a": 1}


def test_parse_markdown_fences():
    assert _safe_json_parse('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_markdown_fences_no_language():
    assert _safe_json_parse('```\n{"a": 1}\n```') == {"a": 1}


def test_parse_with_preamble():
    assert _safe_json_parse('Result:\n{"a": 1}\nDone.') == {"a": 1}


def test_parse_array():
    assert _safe_json_parse("[1, 2, 3]") == [1, 2, 3]


def test_parse_nested_json():
    data = '{"parts": [{"mpn": "LM358", "qty": 100}]}'
    assert _safe_json_parse(data) == {"parts": [{"mpn": "LM358", "qty": 100}]}


def test_parse_empty():
    assert _safe_json_parse("") is None


def test_parse_none():
    assert _safe_json_parse(None) is None


def test_parse_garbage():
    assert _safe_json_parse("no json here at all") is None


def test_parse_whitespace_only():
    assert _safe_json_parse("   \n  ") is None
