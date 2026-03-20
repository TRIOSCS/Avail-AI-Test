"""LLM Router — Direct Claude routing.

Routes all LLM calls through Anthropic Claude API.

Called by: ai_gate (NC/ICS), response_parser, ai_service (draft_rfq, rephrase_rfq)
Depends on: claude_client
"""

from app.utils.claude_client import claude_json, claude_structured, claude_text


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
    """Structured output via Claude."""
    return await claude_structured(
        prompt,
        schema,
        system=system,
        model_tier=model_tier,
        max_tokens=max_tokens,
        timeout=timeout,
        thinking_budget=thinking_budget,
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
    """Text output via Claude."""
    return await claude_text(
        prompt,
        system=system,
        model_tier=model_tier,
        max_tokens=max_tokens,
        tools=tools,
        timeout=timeout,
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
    """JSON output via Claude."""
    return await claude_json(
        prompt,
        system=system,
        model_tier=model_tier,
        max_tokens=max_tokens,
        tools=tools,
        timeout=timeout,
    )
