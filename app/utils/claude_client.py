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

import httpx

from app.config import settings

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
        "x-api-key": settings.anthropic_api_key,
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

    Returns:
        Parsed dict conforming to schema, or None on failure
    """
    if not settings.anthropic_api_key:
        return None

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
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                API_URL,
                headers=_headers(cache=cache_system),
                json=body,
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
    if not settings.anthropic_api_key:
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
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                API_URL,
                headers=_headers(cache=cache_system),
                json=body,
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
