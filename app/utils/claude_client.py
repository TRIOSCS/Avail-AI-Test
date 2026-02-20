"""Claude API client — Structured Outputs, prompt caching, model routing.

Hardening: H9 (Structured Outputs), H10 (Prompt Caching).

Two model tiers:
  - FAST: claude-haiku-4-5 for high-volume parsing (responses, column mapping)
  - SMART: claude-sonnet-4-5 for intelligence (enrichment, intel, RFQ drafts)

Usage:
    from app.utils.claude_client import claude_structured, claude_text
    result = await claude_structured(
        prompt="Parse this vendor reply...",
        schema=RESPONSE_SCHEMA,
        system="You parse electronic component vendor emails.",
        model_tier="fast",
    )
"""

import json
import logging
from typing import Any

from app.http_client import http
from app.services.credential_service import get_credential_cached

log = logging.getLogger("avail.claude")

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

# Model tiers
MODELS = {
    "fast": "claude-haiku-4-5-20251001",
    "smart": "claude-sonnet-4-5-20250929",
}


def _headers(*, cache: bool = False) -> dict:
    """Build API headers. Enable prompt caching when static prompts are reused."""
    h = {
        "x-api-key": get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY"),
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
    }
    if cache:
        h["anthropic-beta"] = "prompt-caching-2024-07-31"
    return h


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
) -> dict | None:
    """Call Claude with guaranteed-valid JSON output (Structured Outputs).

    Args:
        prompt: User message content
        schema: JSON Schema that the model MUST conform to
        system: System prompt (cached if cache_system=True)
        model_tier: "fast" (Haiku) or "smart" (Sonnet)
        max_tokens: Max output tokens
        cache_system: Whether to mark the system prompt as cacheable (H10)
        timeout: Request timeout seconds
        thinking_budget: If set, enable extended thinking with this token budget.
            Requires SMART tier (Sonnet). Increases max_tokens automatically.

    Returns:
        Parsed dict conforming to schema, or None on failure
    """
    if not get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY"):
        return None

    # Extended thinking requires Sonnet
    if thinking_budget:
        model = MODELS["smart"]
    else:
        model = MODELS.get(model_tier, MODELS["fast"])

    # Build system with optional cache control (H10)
    system_blocks = []
    if system:
        block = {"type": "text", "text": system}
        if cache_system:
            block["cache_control"] = {"type": "ephemeral"}
        system_blocks.append(block)

    body: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }

    if system_blocks:
        body["system"] = system_blocks

    # Extended thinking
    if thinking_budget:
        body["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}
        # Ensure max_tokens covers both thinking and output
        body["max_tokens"] = max(max_tokens, thinking_budget + 1024)

    # H9: Structured Outputs — guaranteed valid JSON
    # Use tool-based approach for schema enforcement
    body["tools"] = [
        {
            "name": "structured_output",
            "description": "Return structured data matching the required schema.",
            "input_schema": schema,
        }
    ]
    body["tool_choice"] = {"type": "tool", "name": "structured_output"}

    try:
        resp = await http.post(
            API_URL,
            headers=_headers(cache=cache_system),
            json=body,
            timeout=timeout,
        )

        if resp.status_code != 200:
            log.warning(f"Claude API {resp.status_code}: {resp.text[:200]}")
            return None

        data = resp.json()
        # Tool use response — extract the tool input (guaranteed valid JSON)
        for block in data.get("content", []):
            if (
                block.get("type") == "tool_use"
                and block.get("name") == "structured_output"
            ):
                return block.get("input")

        log.warning("Claude structured output: no tool_use block in response")
        return None

    except Exception as e:
        log.warning(f"Claude structured call failed: {e}")
        return None


async def claude_text(
    prompt: str,
    *,
    system: str = "",
    model_tier: str = "smart",
    max_tokens: int = 1500,
    tools: list[dict] | None = None,
    cache_system: bool = True,
    timeout: int = 60,
) -> str | None:
    """Call Claude for free-form text response.

    Used for: RFQ drafts, company intel, contact enrichment (with web search).

    Args:
        prompt: User message
        system: System prompt
        model_tier: "fast" or "smart"
        max_tokens: Max output tokens
        tools: Optional tools (e.g., web_search)
        cache_system: Whether to cache the system prompt
        timeout: Request timeout

    Returns:
        Text response or None on failure
    """
    if not get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY"):
        return None

    model = MODELS.get(model_tier, MODELS["fast"])

    system_blocks = []
    if system:
        block = {"type": "text", "text": system}
        if cache_system:
            block["cache_control"] = {"type": "ephemeral"}
        system_blocks.append(block)

    body: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }

    if system_blocks:
        body["system"] = system_blocks
    if tools:
        body["tools"] = tools

    try:
        resp = await http.post(
            API_URL,
            headers=_headers(cache=cache_system),
            json=body,
            timeout=timeout,
        )

        if resp.status_code != 200:
            log.warning(f"Claude API {resp.status_code}: {resp.text[:200]}")
            return None

        data = resp.json()
        # Extract text from response (may be interleaved with tool use)
        texts = [
            b["text"] for b in data.get("content", []) if b.get("type") == "text"
        ]
        return "\n".join(texts) if texts else None

    except Exception as e:
        log.warning(f"Claude text call failed: {e}")
        return None


async def claude_json(
    prompt: str,
    *,
    system: str = "",
    model_tier: str = "fast",
    max_tokens: int = 1024,
    tools: list[dict] | None = None,
    timeout: int = 30,
) -> dict | list | None:
    """Call Claude expecting JSON in free-form text. Parses response.

    Fallback for cases where structured outputs aren't suitable
    (e.g., when using web_search tool alongside JSON extraction).
    """
    text = await claude_text(
        prompt,
        system=system,
        model_tier=model_tier,
        max_tokens=max_tokens,
        tools=tools,
        timeout=timeout,
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

    log.debug(f"JSON parse failed: {text[:100]}...")
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

    system_blocks = []
    if system:
        system_blocks.append({"type": "text", "text": system})

    params: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
        "tools": [
            {
                "name": "structured_output",
                "description": "Return structured data matching the required schema.",
                "input_schema": schema,
            }
        ],
        "tool_choice": {"type": "tool", "name": "structured_output"},
    }

    if system_blocks:
        params["system"] = system_blocks

    return {"custom_id": custom_id, "params": params}


async def claude_batch_submit(
    requests: list[dict],
) -> str | None:
    """Submit a batch of structured output requests to the Anthropic Batch API.

    Args:
        requests: List of dicts with keys:
            custom_id, prompt, schema, system, model_tier, max_tokens

    Returns:
        Batch ID string for polling, or None on failure.
        50% cost reduction vs individual calls; up to 24h processing.
    """
    if not get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY") or not requests:
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
            log.warning(f"Batch API submit {resp.status_code}: {resp.text[:300]}")
            return None

        data = resp.json()
        batch_id = data.get("id")
        count = data.get("request_counts", {}).get("processing", len(requests))
        log.info(f"Batch submitted: {batch_id} ({count} requests)")
        return batch_id

    except Exception as e:
        log.warning(f"Batch API submit failed: {e}")
        return None


async def claude_batch_results(
    batch_id: str,
) -> dict | None:
    """Check batch status and retrieve results if complete.

    Returns:
        None if still processing or on error.
        Dict of {custom_id: parsed_dict} when batch is complete.
        Entries with errors will have value None.
    """
    if not get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY") or not batch_id:
        return None

    try:
        # Check batch status
        resp = await http.get(
            f"{BATCH_API_URL}/{batch_id}",
            headers=_headers(),
            timeout=30,
        )

        if resp.status_code != 200:
            log.warning(f"Batch status check {resp.status_code}: {resp.text[:200]}")
            return None

        data = resp.json()
        status = data.get("processing_status")

        if status != "ended":
            log.debug(f"Batch {batch_id} status: {status}")
            return None  # Still processing

        # Fetch results JSONL
        results_url = data.get("results_url")
        if not results_url:
            log.warning(f"Batch {batch_id} ended but no results_url")
            return None

        results_resp = await http.get(
            results_url,
            headers=_headers(),
            timeout=30,
        )

        if results_resp.status_code != 200:
            log.warning(f"Batch results fetch {results_resp.status_code}")
            return None

        # Parse JSONL results
        parsed = {}
        for line in results_resp.text.strip().split("\n"):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                cid = entry.get("custom_id", "")
                result = entry.get("result", {})

                if result.get("type") == "succeeded":
                    message = result.get("message", {})
                    # Extract tool_use input (same as claude_structured)
                    for block in message.get("content", []):
                        if (
                            block.get("type") == "tool_use"
                            and block.get("name") == "structured_output"
                        ):
                            parsed[cid] = block.get("input")
                            break
                    else:
                        parsed[cid] = None
                else:
                    error = result.get("error", {})
                    log.warning(
                        f"Batch item {cid} failed: {error.get('type', 'unknown')}"
                    )
                    parsed[cid] = None

            except json.JSONDecodeError:
                log.debug(f"Batch JSONL parse error: {line[:100]}")
                continue

        counts = data.get("request_counts", {})
        log.info(
            f"Batch {batch_id} complete: "
            f"{counts.get('succeeded', 0)} succeeded, "
            f"{counts.get('errored', 0)} errored"
        )
        return parsed

    except Exception as e:
        log.warning(f"Batch results check failed: {e}")
        return None
