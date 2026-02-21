"""Gradient AI Service — Reusable LLM client for DigitalOcean Gradient inference.

Purpose:
  Foundation layer for all Gradient-powered AI features. Wraps the OpenAI-compatible
  inference endpoint at inference.do-ai.run with retry logic, structured JSON parsing,
  token usage logging, and model routing.

Design rules:
  - Every call returns a result or None (callers handle gracefully)
  - All failures are logged but never crash the app
  - Token usage logged for cost tracking
  - Retries with exponential backoff on transient errors (429, 500, 502, 503, 504)

Available model tiers:
  - DEFAULT: anthropic-claude-sonnet-4-5 (structured extraction, parsing, drafting)
  - STRONG: anthropic-claude-opus-4-6 (complex reasoning, analysis)

Called by: ai_email_parser, ai_part_normalizer, ai_email_drafter, ai_quote_analyzer,
           ai_pricing_analyzer, ai_sourcing_assistant
Depends on: app.http_client, app.config
"""

import asyncio
import json
import time
from typing import Any

from loguru import logger

from app.http_client import http
from app.config import settings

API_URL = "https://inference.do-ai.run/v1/chat/completions"

# Model tiers — Anthropic models on DigitalOcean Gradient
MODELS = {
    "default": "anthropic-claude-sonnet-4-5",
    "strong": "anthropic-claude-opus-4-6",
}

# HTTP status codes worth retrying
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
BASE_DELAY = 1.0  # seconds


def _headers() -> dict:
    """Build auth headers for Gradient inference API."""
    return {
        "Authorization": f"Bearer {settings.do_gradient_api_key}",
        "Content-Type": "application/json",
    }


async def _call_llm(
    messages: list[dict],
    *,
    model: str | None = None,
    model_tier: str = "default",
    max_tokens: int = 1024,
    temperature: float = 0.2,
    timeout: int = 30,
) -> dict | None:
    """Low-level call to Gradient inference with retries and token logging.

    Returns the full API response dict, or None on failure.
    """
    if not settings.do_gradient_api_key:
        logger.warning("DO_GRADIENT_API_KEY not set — skipping Gradient call")
        return None

    resolved_model = model or MODELS.get(model_tier, MODELS["default"])

    body = {
        "model": resolved_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    for attempt in range(MAX_RETRIES):
        try:
            start = time.monotonic()
            resp = await http.post(
                API_URL,
                headers=_headers(),
                json=body,
                timeout=timeout,
            )
            elapsed = time.monotonic() - start

            if resp.status_code == 200:
                data = resp.json()
                usage = data.get("usage", {})
                logger.info(
                    "Gradient OK | model={} | in={} | out={} | {:.1f}s",
                    resolved_model,
                    usage.get("prompt_tokens", "?"),
                    usage.get("completion_tokens", "?"),
                    elapsed,
                )
                return data

            if resp.status_code in RETRYABLE_STATUSES and attempt < MAX_RETRIES - 1:
                delay = BASE_DELAY * (2 ** attempt)
                logger.warning(
                    "Gradient {} (attempt {}/{}), retry in {:.1f}s: {}",
                    resp.status_code, attempt + 1, MAX_RETRIES, delay,
                    resp.text[:200],
                )
                await asyncio.sleep(delay)
                continue

            logger.warning("Gradient API {}: {}", resp.status_code, resp.text[:200])
            return None

        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                delay = BASE_DELAY * (2 ** attempt)
                logger.warning(
                    "Gradient call failed (attempt {}/{}), retry in {:.1f}s: {}",
                    attempt + 1, MAX_RETRIES, delay, e,
                )
                await asyncio.sleep(delay)
                continue
            logger.warning("Gradient call failed after {} attempts: {}", MAX_RETRIES, e)
            return None

    return None


def _extract_text(data: dict) -> str | None:
    """Extract text content from a chat completion response."""
    choices = data.get("choices", [])
    if not choices:
        return None
    return choices[0].get("message", {}).get("content")


async def gradient_text(
    prompt: str,
    *,
    system: str = "",
    model: str | None = None,
    model_tier: str = "default",
    max_tokens: int = 1500,
    temperature: float = 0.5,
    timeout: int = 30,
) -> str | None:
    """Call Gradient for a free-form text response.

    Use for: email drafts, summaries, comparisons, recommendations.

    Args:
        prompt: User message content.
        system: System prompt (domain context, persona).
        model: Explicit model ID (overrides model_tier).
        model_tier: "default" or "strong".
        max_tokens: Max output tokens.
        temperature: 0.1-0.3 for extraction, 0.5-0.7 for generation.
        timeout: Request timeout seconds.

    Returns:
        Text response or None on failure.
    """
    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    data = await _call_llm(
        messages,
        model=model,
        model_tier=model_tier,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
    )
    if not data:
        return None

    return _extract_text(data)


async def gradient_json(
    prompt: str,
    *,
    system: str = "",
    model: str | None = None,
    model_tier: str = "default",
    max_tokens: int = 1024,
    temperature: float = 0.2,
    timeout: int = 30,
) -> dict | list | None:
    """Call Gradient expecting JSON output. Parses response with fallback extraction.

    Use for: structured extraction (email parsing, part normalization, quote analysis).

    The system prompt should instruct the model to return only valid JSON.

    Args:
        prompt: User message (should describe the desired JSON structure).
        system: System prompt (include "Return ONLY valid JSON" instruction).
        model: Explicit model ID (overrides model_tier).
        model_tier: "default" or "strong".
        max_tokens: Max output tokens.
        temperature: Keep low (0.1-0.3) for reliable structured output.
        timeout: Request timeout seconds.

    Returns:
        Parsed dict/list or None on failure.
    """
    json_system = system
    if system and "json" not in system.lower():
        json_system = system + " Return ONLY valid JSON, no markdown or explanation."

    text = await gradient_text(
        prompt,
        system=json_system,
        model=model,
        model_tier=model_tier,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
    )
    if not text:
        return None

    return _safe_json_parse(text)


def _safe_json_parse(text: str) -> dict | list | None:
    """Parse JSON from LLM output that may contain markdown fences or preamble."""
    if not text:
        return None

    cleaned = text.strip()

    # Strip markdown code fences
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()

    # Direct parse
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Find JSON object or array in text
    for start_char, end_char in [("{", "}"), ("[", "]")]:
        start = cleaned.find(start_char)
        end = cleaned.rfind(end_char)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                continue

    logger.debug("JSON parse failed: {}...", text[:100])
    return None


async def gradient_batch_json(
    prompts: list[str],
    *,
    system: str = "",
    model: str | None = None,
    model_tier: str = "default",
    max_tokens: int = 1024,
    temperature: float = 0.2,
    timeout: int = 30,
) -> list[dict | list | None]:
    """Process multiple prompts concurrently. Returns results in same order.

    Use for: batch part normalization, bulk enrichment.
    Runs prompts concurrently with asyncio.gather for throughput.
    """
    tasks = [
        gradient_json(
            prompt,
            system=system,
            model=model,
            model_tier=model_tier,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
        )
        for prompt in prompts
    ]
    return list(await asyncio.gather(*tasks))
