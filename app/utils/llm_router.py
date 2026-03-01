"""LLM Router — Gradient-first, Claude fallback.

Routes LLM calls through DigitalOcean Gradient (free tier) first,
falling back to Anthropic Claude Direct only when Gradient fails or
the feature requires Claude-specific capabilities (web_search, extended thinking).

Called by: ai_gate (NC/ICS), response_parser, ai_service (draft_rfq, rephrase_rfq)
Depends on: gradient_service, claude_client
"""

import json

from loguru import logger

from app.services.gradient_service import gradient_json, gradient_text
from app.utils.claude_client import claude_json, claude_structured, claude_text

# Tier mapping: claude tiers → gradient tiers
_TIER_MAP = {"fast": "default", "smart": "strong"}


def _validate_required_fields(result: dict | list | None, schema: dict) -> bool:
    """Check that top-level required keys from schema exist in result."""
    if not isinstance(result, dict):
        return False
    for key in schema.get("required", []):
        if key not in result:
            return False
    return True


def _schema_to_instruction(schema: dict) -> str:
    """Convert a JSON schema into a compact instruction string for the system prompt."""
    return (
        "Return ONLY valid JSON matching this schema:\n"
        f"```json\n{json.dumps(schema, separators=(',', ':'))}\n```"
    )


async def routed_structured(
    prompt: str,
    schema: dict,
    *,
    system: str = "",
    model_tier: str = "fast",
    max_tokens: int = 1024,
    timeout: int = 30,
    thinking_budget: int | None = None,
) -> dict | None:
    """Structured output: Gradient first, Claude fallback.

    Skips Gradient when thinking_budget is set (Claude-only feature).
    Appends schema as JSON instruction to the system prompt for Gradient.
    Validates required fields before accepting Gradient result.
    """
    # Extended thinking is Claude-only
    if thinking_budget:
        return await claude_structured(
            prompt, schema,
            system=system, model_tier=model_tier,
            max_tokens=max_tokens, timeout=timeout,
            thinking_budget=thinking_budget,
        )

    # Try Gradient first
    gradient_system = f"{system}\n\n{_schema_to_instruction(schema)}" if system else _schema_to_instruction(schema)
    gradient_tier = _TIER_MAP.get(model_tier, "default")

    try:
        result = await gradient_json(
            prompt,
            system=gradient_system,
            model_tier=gradient_tier,
            max_tokens=max_tokens,
            timeout=timeout,
        )
        if result and _validate_required_fields(result, schema):
            logger.debug("routed_structured: Gradient success")
            return result
        if result:
            logger.info("routed_structured: Gradient returned incomplete result, falling back to Claude")
    except Exception as e:
        logger.info("routed_structured: Gradient failed ({}), falling back to Claude", e)

    # Fallback to Claude
    return await claude_structured(
        prompt, schema,
        system=system, model_tier=model_tier,
        max_tokens=max_tokens, timeout=timeout,
    )


async def routed_text(
    prompt: str,
    *,
    system: str = "",
    model_tier: str = "smart",
    max_tokens: int = 1500,
    tools: list[dict] | None = None,
    timeout: int = 60,
) -> str | None:
    """Text output: Gradient first, Claude fallback.

    Skips Gradient when tools are set (Claude-only feature like web_search).
    """
    # Tools (web_search etc.) are Claude-only
    if tools:
        return await claude_text(
            prompt,
            system=system, model_tier=model_tier,
            max_tokens=max_tokens, tools=tools, timeout=timeout,
        )

    # Try Gradient first
    gradient_tier = _TIER_MAP.get(model_tier, "default")

    try:
        result = await gradient_text(
            prompt,
            system=system,
            model_tier=gradient_tier,
            max_tokens=max_tokens,
            timeout=timeout,
        )
        if result:
            logger.debug("routed_text: Gradient success")
            return result
    except Exception as e:
        logger.info("routed_text: Gradient failed ({}), falling back to Claude", e)

    # Fallback to Claude
    return await claude_text(
        prompt,
        system=system, model_tier=model_tier,
        max_tokens=max_tokens, timeout=timeout,
    )


async def routed_json(
    prompt: str,
    *,
    system: str = "",
    model_tier: str = "fast",
    max_tokens: int = 1024,
    tools: list[dict] | None = None,
    timeout: int = 30,
) -> dict | list | None:
    """JSON output: Gradient first, Claude fallback.

    Skips Gradient when tools are set (Claude-only feature).
    """
    # Tools (web_search etc.) are Claude-only
    if tools:
        return await claude_json(
            prompt,
            system=system, model_tier=model_tier,
            max_tokens=max_tokens, tools=tools, timeout=timeout,
        )

    # Try Gradient first
    gradient_tier = _TIER_MAP.get(model_tier, "default")

    try:
        result = await gradient_json(
            prompt,
            system=system,
            model_tier=gradient_tier,
            max_tokens=max_tokens,
            timeout=timeout,
        )
        if result is not None:
            logger.debug("routed_json: Gradient success")
            return result
    except Exception as e:
        logger.info("routed_json: Gradient failed ({}), falling back to Claude", e)

    # Fallback to Claude
    return await claude_json(
        prompt,
        system=system, model_tier=model_tier,
        max_tokens=max_tokens, timeout=timeout,
    )
