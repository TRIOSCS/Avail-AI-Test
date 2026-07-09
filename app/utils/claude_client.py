"""Claude API client — Structured Outputs, prompt caching, model routing.

Hardening: H9 (Structured Outputs), H10 (Prompt Caching).

Two model tiers:
  - FAST: claude-haiku-4-5 for high-volume parsing (responses, column mapping)
  - SMART: claude-sonnet-4-6 for intelligence (enrichment, intel, RFQ drafts)

Usage:
    from app.utils.claude_client import claude_structured, claude_text
    result = await claude_structured(
        prompt="Parse this vendor reply...",
        schema=RESPONSE_SCHEMA,
        system="You parse electronic component vendor emails.",
        model_tier="fast",
    )
"""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import httpx
import sentry_sdk
from loguru import logger

from app.config import settings
from app.http_client import http
from app.services.credential_service import get_credential_cached
from app.utils.claude_errors import (
    ClaudeAuthError,
    ClaudeError,
    ClaudeRateLimitError,
    ClaudeServerError,
    ClaudeUnavailableError,
)

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

# Model tiers — read from settings, fallback to sensible defaults
MODELS = {
    "fast": "claude-haiku-4-5-20251001",
    "smart": settings.anthropic_model or "claude-sonnet-4-6",
    "opus": "claude-opus-4-8",
}


def _headers(*, cache: bool = False) -> dict:
    """Build API headers.

    Enable prompt caching when static prompts are reused.
    """
    h = {
        "x-api-key": get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY"),
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
    }
    if cache:
        h["anthropic-beta"] = "prompt-caching-2024-07-31"
    return h


# Tool definition that pins the model to a JSON Schema (H9: Structured Outputs).
_STRUCTURED_OUTPUT_TOOL_NAME = "structured_output"


def _structured_output_tool(schema: dict) -> dict:
    """Tool definition that forces schema-conforming JSON output."""
    return {
        "name": _STRUCTURED_OUTPUT_TOOL_NAME,
        "description": "Return structured data matching the required schema.",
        "input_schema": schema,
    }


def _system_blocks(system: str, *, cache: bool) -> list[dict]:
    """Build the `system` field, optionally marking it cacheable (H10)."""
    if not system:
        return []
    block: dict[str, Any] = {"type": "text", "text": system}
    if cache:
        block["cache_control"] = {"type": "ephemeral"}
    return [block]


_MISSING = object()


def _extract_tool_input(content: list[dict]) -> Any:
    """Return the structured_output tool input, or `_MISSING` if no such block."""
    for block in content:
        if block.get("type") == "tool_use" and block.get("name") == _STRUCTURED_OUTPUT_TOOL_NAME:
            return block.get("input")
    return _MISSING


def _raise_for_status(resp: httpx.Response, *, context: str) -> None:
    """Map a non-200 Claude API response to the matching ClaudeError subclass."""
    if resp.status_code in (401, 403):
        raise ClaudeAuthError(f"Claude API auth failed: {resp.status_code}")
    if resp.status_code == 429:
        raise ClaudeRateLimitError("Rate limit exceeded after retries")
    if resp.status_code >= 500:
        raise ClaudeServerError(f"Claude API error: {resp.status_code}")
    raise ClaudeError(f"{context}: {resp.status_code}")


def _record_usage(span: Any, usage: dict) -> None:
    """Copy token-usage counters from a response into the Sentry span."""
    span.set_data("ai.prompt_tokens.used", usage.get("input_tokens", 0))
    span.set_data("ai.completion_tokens.used", usage.get("output_tokens", 0))
    span.set_data(
        "ai.total_tokens.used",
        usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
    )
    if usage.get("cache_read_input_tokens"):
        span.set_data("ai.cache_read_tokens", usage["cache_read_input_tokens"])


def _meter_usage(bucket: str, model_tier: str, usage: dict) -> None:
    """Aggregate one call's token usage into Redis date-counters for a cost bucket.

    Opt-in — runs only when a caller passes ``cost_bucket`` (e.g. the enrichment
    worker), so app/search/RFQ/email traffic is unaffected. Keyed by
    ``claude_usage:{bucket}:{model_tier}:{metric}:{UTC-date}`` so a readout can price
    each tier (fast/smart/opus) separately. Mirrors the
    ``enrichment_worker:web_calls:{date}`` pattern (atomic ``intel_cache.incr_count``).
    Records ``server_tool_use.web_search_requests`` so the $0.01/search surcharge is
    measured, not estimated. NEVER raises — metering must not break a real Claude call.
    """
    try:
        from app.cache import intel_cache

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        prefix = f"claude_usage:{bucket}:{model_tier}"
        server_tool = usage.get("server_tool_use") or {}
        counters = {
            "calls": 1,
            "input_tokens": int(usage.get("input_tokens", 0) or 0),
            "output_tokens": int(usage.get("output_tokens", 0) or 0),
            "cache_read_tokens": int(usage.get("cache_read_input_tokens", 0) or 0),
            "cache_write_tokens": int(usage.get("cache_creation_input_tokens", 0) or 0),
            "web_searches": int(server_tool.get("web_search_requests", 0) or 0),
        }
        # 35-day TTL keeps a month+ of per-day history for weekly/monthly readouts.
        for metric, amount in counters.items():
            if amount:
                intel_cache.incr_count(f"{prefix}:{metric}:{today}", amount, ttl_days=35.0)
    except Exception as e:  # metering is best-effort; never break the caller
        logger.debug("claude usage metering skipped ({}): {}", bucket, e)


async def claude_structured(
    prompt: str,
    schema: dict,
    *,
    system: str = "",
    model_tier: str = "fast",
    max_tokens: int = 1024,
    cache_system: bool = True,
    timeout: int = 30,
    thinking_budget: int | None = None,
    cost_bucket: str | None = None,
    max_attempts: int = 3,
) -> dict | None:
    """Call Claude with guaranteed-valid JSON output (Structured Outputs).

    Args:
        prompt: User message content
        schema: JSON Schema that the model MUST conform to
        system: System prompt (cached if cache_system=True)
        model_tier: "fast" (Haiku), "smart" (Sonnet), or "opus" (Opus)
        max_tokens: Max output tokens
        cache_system: Whether to mark the system prompt as cacheable (H10)
        timeout: Request timeout seconds
        thinking_budget: If set, enable extended thinking with this token budget.
            Requires SMART tier (Sonnet). Increases max_tokens automatically.
        max_attempts: Retry budget for transient errors (429/503/connect/timeout).
            Defaults to 3 (unchanged behavior for existing callers). Interactive
            HTTP-request-scoped callers (e.g. the AI-insights refresh endpoints,
            P2.8) pass a tightened ``timeout`` + ``max_attempts=1`` so a slow
            Claude call can't hold an HTMX request open for the full
            timeout × retries worst case.

    Returns:
        Parsed dict conforming to schema, or None on failure

    Raises:
        ClaudeUnavailableError: API key not configured
        ClaudeAuthError: API key invalid (401/403)
        ClaudeRateLimitError: Rate limited after retries (429)
        ClaudeServerError: API returned 5xx
        ClaudeError: Network/timeout or all retries exhausted
    """
    result, _usage = await claude_structured_with_usage(
        prompt,
        schema,
        system=system,
        model_tier=model_tier,
        max_tokens=max_tokens,
        cache_system=cache_system,
        timeout=timeout,
        thinking_budget=thinking_budget,
        cost_bucket=cost_bucket,
        max_attempts=max_attempts,
    )
    return result


async def claude_structured_with_usage(
    prompt: str,
    schema: dict,
    *,
    system: str = "",
    model_tier: str = "fast",
    max_tokens: int = 1024,
    cache_system: bool = True,
    timeout: int = 30,
    thinking_budget: int | None = None,
    cost_bucket: str | None = None,
    max_attempts: int = 3,
) -> tuple[dict | None, dict]:
    """Like :func:`claude_structured`, but also returns the raw token-usage dict.

    Returns ``(tool_input_or_None, usage)`` where ``usage`` carries
    ``input_tokens``/``output_tokens`` (empty dict if the response omitted it).
    Callers that need to record spend (e.g. trouble-ticket diagnosis) use this;
    the plain :func:`claude_structured` wrapper preserves the original return
    contract for the 30+ existing callers.

    ``max_attempts`` defaults to 3 (unchanged behavior). See
    :func:`claude_structured`'s docstring for the interactive-caller use case.
    """
    if not get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY"):
        raise ClaudeUnavailableError("ANTHROPIC_API_KEY not configured")

    # Extended thinking requires Sonnet
    if thinking_budget:
        model = MODELS["smart"]
    else:
        model = MODELS.get(model_tier, MODELS["fast"])

    body: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }

    system_blocks = _system_blocks(system, cache=cache_system)
    if system_blocks:
        body["system"] = system_blocks

    # Extended thinking
    if thinking_budget:
        body["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}
        # Ensure max_tokens covers both thinking and output
        body["max_tokens"] = max(max_tokens, thinking_budget + 1024)

    # H9: Structured Outputs — guaranteed valid JSON via tool-based schema enforcement
    body["tools"] = [_structured_output_tool(schema)]
    body["tool_choice"] = {"type": "tool", "name": _STRUCTURED_OUTPUT_TOOL_NAME}

    max_attempts = max(1, max_attempts)
    for attempt in range(1, max_attempts + 1):
        try:
            with sentry_sdk.start_span(
                op="ai.chat_completions.create",
                description=f"claude_structured ({model_tier})",
            ) as span:
                span.set_data("ai.model_id", model)
                span.set_data("ai.streaming", False)
                span.set_data("ai.pipeline.name", "claude_structured")

                resp = await http.post(
                    API_URL,
                    headers=_headers(cache=cache_system),
                    json=body,
                    timeout=timeout,
                )

                # Retry on transient errors (429 rate limit, 503 overloaded)
                if resp.status_code in (429, 503) and attempt < max_attempts:
                    delay = 2**attempt
                    logger.warning(
                        f"Claude API {resp.status_code} (attempt {attempt}/{max_attempts}), retrying in {delay}s"
                    )
                    await asyncio.sleep(delay)
                    continue

                if resp.status_code != 200:
                    span.set_data("ai.response.status_code", resp.status_code)
                    logger.warning(f"Claude API {resp.status_code}: {resp.text[:200]}")
                    _raise_for_status(resp, context="Claude API error")

                data = resp.json()
                usage = data.get("usage", {})
                _record_usage(span, usage)
                if cost_bucket:
                    _meter_usage(cost_bucket, model_tier, usage)

                # Tool use response — extract the tool input (guaranteed valid JSON)
                tool_input = _extract_tool_input(data.get("content", []))
                if tool_input is _MISSING:
                    logger.warning("Claude structured output: no tool_use block in response")
                    return None, usage
                return tool_input, usage

        except (ClaudeError,):
            raise
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            if attempt < max_attempts:
                delay = 2**attempt
                logger.warning(
                    f"Claude structured {type(e).__name__} (attempt {attempt}/{max_attempts}), retrying in {delay}s"
                )
                await asyncio.sleep(delay)
                continue
            raise ClaudeError(f"Claude API unreachable: {e}") from e
        except Exception as e:
            logger.warning("Claude structured call failed: {} ({})", type(e).__name__, e)
            logger.debug("Claude structured call traceback:", exc_info=True)
            raise ClaudeError(f"Claude structured call failed: {e}") from e

    raise ClaudeError("All retry attempts failed")


async def claude_text(
    prompt: str,
    *,
    system: str = "",
    model_tier: str = "smart",
    max_tokens: int = 1500,
    tools: list[dict] | None = None,
    cache_system: bool = True,
    timeout: int = 60,
    cost_bucket: str | None = None,
) -> str | None:
    """Call Claude for free-form text response.

    Used for: RFQ drafts, company intel, contact enrichment (with web search).

    Args:
        prompt: User message
        system: System prompt
        model_tier: "fast", "smart", or "opus"
        max_tokens: Max output tokens
        tools: Optional tools (e.g., web_search)
        cache_system: Whether to cache the system prompt
        timeout: Request timeout

    Returns:
        Text response or None on failure

    Raises:
        ClaudeUnavailableError: API key not configured
        ClaudeAuthError: API key invalid (401/403)
        ClaudeRateLimitError: Rate limited after retries (429)
        ClaudeServerError: API returned 5xx
        ClaudeError: Network/timeout or all retries exhausted
    """
    if not get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY"):
        raise ClaudeUnavailableError("ANTHROPIC_API_KEY not configured")

    model = MODELS.get(model_tier, MODELS["fast"])

    body: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }

    system_blocks = _system_blocks(system, cache=cache_system)
    if system_blocks:
        body["system"] = system_blocks
    if tools:
        body["tools"] = tools

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            with sentry_sdk.start_span(
                op="ai.chat_completions.create",
                description=f"claude_text ({model_tier})",
            ) as span:
                span.set_data("ai.model_id", model)
                span.set_data("ai.streaming", False)
                span.set_data("ai.pipeline.name", "claude_text")

                resp = await http.post(
                    API_URL,
                    headers=_headers(cache=cache_system),
                    json=body,
                    timeout=timeout,
                )

                # Retry on transient errors (429 rate limit, 503 overloaded)
                if resp.status_code in (429, 503) and attempt < max_attempts:
                    delay = 2**attempt
                    logger.warning(
                        f"Claude API {resp.status_code} (attempt {attempt}/{max_attempts}), retrying in {delay}s"
                    )
                    await asyncio.sleep(delay)
                    continue

                if resp.status_code != 200:
                    span.set_data("ai.response.status_code", resp.status_code)
                    logger.warning(f"Claude API {resp.status_code}: {resp.text[:200]}")
                    _raise_for_status(resp, context="Claude API error")

                data = resp.json()
                _record_usage(span, data.get("usage", {}))
                if cost_bucket:
                    _meter_usage(cost_bucket, model_tier, data.get("usage", {}))

                # Extract text from response (may be interleaved with tool use)
                texts = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
                return "\n".join(texts) if texts else None

        except (ClaudeError,):
            raise
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            if attempt < max_attempts:
                delay = 2**attempt
                logger.warning(
                    f"Claude text {type(e).__name__} (attempt {attempt}/{max_attempts}), retrying in {delay}s"
                )
                await asyncio.sleep(delay)
                continue
            raise ClaudeError(f"Claude API unreachable: {e}") from e
        except Exception as e:
            logger.warning(f"Claude text call failed: {e}")
            raise ClaudeError(f"Claude text call failed: {e}") from e

    raise ClaudeError("All retry attempts failed")


async def claude_json(
    prompt: str,
    *,
    system: str = "",
    model_tier: str = "fast",
    max_tokens: int = 1024,
    tools: list[dict] | None = None,
    timeout: int = 30,
    cost_bucket: str | None = None,
) -> dict | list | None:
    """Call Claude expecting JSON in free-form text. Parses response.

    Fallback for cases where structured outputs aren't suitable (e.g., when using
    web_search tool alongside JSON extraction).
    """
    text = await claude_text(
        prompt,
        system=system,
        model_tier=model_tier,
        max_tokens=max_tokens,
        tools=tools,
        timeout=timeout,
        cost_bucket=cost_bucket,
    )
    if not text:
        return None

    return safe_json_parse(text)


def safe_json_parse(text: str) -> dict | list | None:
    """Parse JSON from text that may contain markdown fences or preamble."""
    if not text:
        return None

    # Strip markdown code fences
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        # Remove first and last ``` lines
        lines = [line for line in lines if not line.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()

    # Try direct parse
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Try to find JSON object or array in the text
    for start_char, end_char in [("{", "}"), ("[", "]")]:
        start = cleaned.find(start_char)
        end = cleaned.rfind(end_char)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                continue

    logger.debug(f"JSON parse failed: {text[:100]}...")
    return None


# ── Batch API ────────────────────────────────────────────────────────────

BATCH_API_URL = "https://api.anthropic.com/v1/messages/batches"


def _build_batch_request(
    custom_id: str,
    prompt: str,
    schema: dict,
    system: str = "",
    model_tier: str = "fast",
    max_tokens: int = 1024,
) -> dict:
    """Build a single request entry for the Batch API."""
    model = MODELS.get(model_tier, MODELS["fast"])

    params: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
        "tools": [_structured_output_tool(schema)],
        "tool_choice": {"type": "tool", "name": _STRUCTURED_OUTPUT_TOOL_NAME},
    }

    system_blocks = _system_blocks(system, cache=False)
    if system_blocks:
        params["system"] = system_blocks

    return {"custom_id": custom_id, "params": params}


async def claude_batch_submit(
    requests: list[dict],
    *,
    cost_bucket: str | None = None,
) -> str | None:
    """Submit a batch of structured output requests to the Anthropic Batch API.

    Args:
        requests: List of dicts with keys:
            custom_id, prompt, schema, system, model_tier, max_tokens
        cost_bucket: Optional Redis metering bucket (e.g. "email_mining").
            Passed through to ``claude_batch_results`` where per-entry usage
            is metered on poll completion; no metering occurs at submit time.

    Returns:
        Batch ID string for polling, or None on failure.
        50% cost reduction vs individual calls; up to 24h processing.
    """
    if not get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY"):
        raise ClaudeUnavailableError("ANTHROPIC_API_KEY not configured")
    if not requests:
        return None

    batch_requests = [
        _build_batch_request(
            custom_id=r["custom_id"],
            prompt=r["prompt"],
            schema=r["schema"],
            system=r.get("system", ""),
            model_tier=r.get("model_tier", "fast"),
            max_tokens=r.get("max_tokens", 1024),
        )
        for r in requests
    ]

    try:
        resp = await http.post(
            BATCH_API_URL,
            headers=_headers(),
            json={"requests": batch_requests},
            timeout=30,
        )

        if resp.status_code != 200:
            logger.warning(f"Batch API submit {resp.status_code}: {resp.text[:300]}")
            if resp.status_code in (401, 403):
                raise ClaudeAuthError(f"Claude API auth failed: {resp.status_code}")
            if resp.status_code == 429:
                raise ClaudeRateLimitError("Rate limit exceeded")
            if resp.status_code >= 500:
                raise ClaudeServerError(f"Claude API error: {resp.status_code}")
            raise ClaudeError(f"Batch API submit error: {resp.status_code}")

        data = resp.json()
        batch_id = data.get("id")
        count = data.get("request_counts", {}).get("processing", len(requests))
        logger.info(f"Batch submitted: {batch_id} ({count} requests)")
        return batch_id

    except ClaudeError:
        raise
    except Exception as e:
        logger.warning(f"Batch API submit failed: {e}")
        raise ClaudeError(f"Batch API submit failed: {e}") from e


async def claude_batch_results(
    batch_id: str,
    *,
    cost_bucket: str | None = None,
) -> dict | None:
    """Check batch status and retrieve results if complete.

    Args:
        batch_id: Batch ID returned by ``claude_batch_submit``.
        cost_bucket: Optional Redis metering bucket (e.g. "email_mining").
            When set, calls ``_meter_usage`` for each succeeded entry so
            per-entry token cost is visible in Redis usage counters.

    Returns:
        None if still processing or on error.
        Dict of {custom_id: parsed_dict} when batch is complete.
        Entries with errors will have value None.
    """
    if not get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY"):
        raise ClaudeUnavailableError("ANTHROPIC_API_KEY not configured")
    if not batch_id:
        return None

    try:
        # Check batch status
        resp = await http.get(
            f"{BATCH_API_URL}/{batch_id}",
            headers=_headers(),
            timeout=30,
        )

        if resp.status_code != 200:
            logger.warning(f"Batch status check {resp.status_code}: {resp.text[:200]}")
            if resp.status_code in (401, 403):
                raise ClaudeAuthError(f"Claude API auth failed: {resp.status_code}")
            if resp.status_code >= 500:
                raise ClaudeServerError(f"Claude API error: {resp.status_code}")
            raise ClaudeError(f"Batch status check error: {resp.status_code}")

        data = resp.json()
        status = data.get("processing_status")

        if status != "ended":
            logger.debug(f"Batch {batch_id} status: {status}")
            return None  # Still processing

        # Fetch results JSONL
        results_url = data.get("results_url")
        if not results_url:
            logger.warning(f"Batch {batch_id} ended but no results_url")
            return None

        results_resp = await http.get(
            results_url,
            headers=_headers(),
            timeout=30,
        )

        if results_resp.status_code != 200:
            logger.warning(f"Batch results fetch {results_resp.status_code}")
            return None

        # Parse JSONL results
        parsed: dict = {}
        for line in results_resp.text.strip().split("\n"):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                cid = entry.get("custom_id", "")
                result = entry.get("result", {})

                if result.get("type") == "succeeded":
                    message = result.get("message", {})
                    # Meter per-entry token usage when a cost_bucket is set.
                    if cost_bucket:
                        model_id = message.get("model", "")
                        # Derive tier from model id (mirrors streaming path heuristic)
                        if "haiku" in model_id:
                            entry_tier = "fast"
                        elif "opus" in model_id:
                            entry_tier = "opus"
                        else:
                            entry_tier = "smart"
                        _meter_usage(cost_bucket, entry_tier, message.get("usage", {}))
                    # Extract tool_use input (same as claude_structured)
                    raw_input = _extract_tool_input(message.get("content", []))
                    if raw_input is _MISSING:
                        parsed[cid] = None
                    else:
                        # API occasionally returns tool input as JSON string
                        if isinstance(raw_input, str):
                            try:
                                raw_input = json.loads(raw_input)
                            except (json.JSONDecodeError, TypeError):
                                raw_input = None
                        parsed[cid] = raw_input
                else:
                    error = result.get("error", {})
                    logger.warning(f"Batch item {cid} failed: {error.get('type', 'unknown')}")
                    parsed[cid] = None

            except json.JSONDecodeError:
                logger.warning(f"Batch JSONL parse error: {line[:100]}")
                continue

        counts = data.get("request_counts", {})
        logger.info(
            f"Batch {batch_id} complete: {counts.get('succeeded', 0)} succeeded, {counts.get('errored', 0)} errored"
        )
        return parsed

    except ClaudeError:
        raise
    except Exception as e:
        logger.warning(f"Batch results check failed: {e}")
        raise ClaudeError(f"Batch results check failed: {e}") from e
