"""Flagged best-effort inference for parts no authoritative source resolves.

Uses Claude Opus 4.8 (model_tier="opus") with a strict refusal rule. Produces ONLY
description + category — never structured specs (lifecycle/package/pins/rohs), because
guessing those is the dangerous kind of hallucination. A guess is only accepted at >=
0.95 confidence, and even then it is flagged "reconfirm needed" (status ai_inferred) so
it is never mistaken for verified data. Below 0.95 (or empty) => not_found.
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from app.utils.claude_client import claude_structured
from app.utils.claude_errors import ClaudeError

# A guess is only accepted at >= 95% confidence (and then flagged "reconfirm needed").
_MIN_CONFIDENCE = 0.95

_SYSTEM = (
    "You are an expert electronic-component engineer. You are given a single "
    "manufacturer or OEM part number with NO other context. Identify the part ONLY "
    "if you genuinely recognize it. It is correct and expected to decline for "
    "obscure OEM/FRU/service part numbers you do not actually know.\n"
    "Rules:\n"
    "- description: 1 concise sentence of what the part is. Empty string if not confident.\n"
    "- category: a short commodity category (e.g. 'Capacitor', 'Connector', 'Memory Module'). "
    "Empty string if not confident.\n"
    "- confidence: 0.0-1.0, your honest probability that this description is correct.\n"
    "- NEVER invent a plausible-sounding description. When unsure, return empty strings and low confidence."
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "category": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["description", "category", "confidence"],
}


@dataclass
class InferenceResult:
    status: str  # "ai_inferred" | "not_found"
    description: str | None
    category: str | None
    confidence: float


async def infer_part(display_mpn: str) -> InferenceResult:
    """Infer part description and category from a part number using Claude Opus.

    Args:
        display_mpn: The manufacturer part number to look up.

    Returns:
        InferenceResult with status "ai_inferred" if confident, "not_found" otherwise.
    """
    prompt = f"Part number: {display_mpn}"
    try:
        data = await claude_structured(
            prompt,
            _SCHEMA,
            system=_SYSTEM,
            model_tier="opus",
            max_tokens=300,
        )
    except ClaudeError:
        # Claude backend failure — surface it so the worker's circuit breaker can detect a
        # sustained outage instead of silently marking every part not_found. A confident
        # "I don't recognize it" reply is parsed data with low confidence, not an exception.
        raise
    except Exception as e:
        logger.warning("AI_INFER: unexpected error for {}: {}", display_mpn, type(e).__name__)
        data = None

    if not data:
        return InferenceResult("not_found", None, None, 0.0)

    desc = (data.get("description") or "").strip()
    cat = (data.get("category") or "").strip()
    conf = float(data.get("confidence") or 0.0)

    if not desc or conf < _MIN_CONFIDENCE:
        return InferenceResult("not_found", None, None, conf)
    return InferenceResult("ai_inferred", desc, cat or None, conf)
