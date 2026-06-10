"""SP-Ingest AI-correction — standardize descriptions + extract deep specs, NEVER
inventing.

What: ``ai_correct`` runs each ConsolidatedPart through Claude (smart tier) to (a) standardize
      the description, (b) infer the canonical category from the 48 app keys ONLY when one is
      missing, and (c) extract deep-filter specs FROM the real description text — each spec with
      a confidence. The HARD GUARDRAIL (the SP1 lesson): transform only what the source text
      supports and return null for anything not present — never fabricate. Outputs are tagged
      source="trio_source_ai" downstream. Batched, with per-batch failure isolation.
Called by: app/management/ingest_source_data.py — ONLY when the CLI --ai-correct flag is set.
Depends on: app.utils.claude_client.claude_structured (model_tier="smart"), the
      ConsolidatedPart dataclass, and app.services.commodity_registry.get_all_commodities for
      the canonical category vocabulary.
"""

from __future__ import annotations

from loguru import logger

from app.services.commodity_registry import get_all_commodities
from app.services.source_ingest.models import ConsolidatedPart
from app.utils.claude_client import claude_structured

# The provenance tag every AI-corrected field carries through the ladder.
AI_SOURCE = "trio_source_ai"

_SYSTEM = (
    "You normalize electronic-component catalog data for a parts-sourcing database. You are "
    "given REAL source rows (MPN, manufacturer, raw description, optional category). Your job "
    "is to clean and structure ONLY what the source text already states.\n\n"
    "HARD RULES — these are non-negotiable:\n"
    "1. NEVER invent, guess, or infer facts that are not present in the provided source text. "
    "If the description does not state a value, return null for it. Do NOT use outside "
    "knowledge of the part number to fill gaps.\n"
    "2. The standardized description must contain only facts already in the raw description "
    "(you may reword/clean, not add).\n"
    "3. Set category ONLY when the source category is missing AND the description unambiguously "
    "identifies the commodity; otherwise return null. The category MUST be exactly one of the "
    "provided canonical keys.\n"
    "4. For each spec, provide a confidence in [0,1] reflecting how clearly the source text "
    "states it. Omit (null) any spec not stated.\n"
    "Returning null is always correct when the source is silent — fabrication is the worst error."
)


def _schema(canonical_keys: list[str]) -> dict:
    """JSON schema for one part's AI correction (description + category + specs)."""
    return {
        "type": "object",
        "properties": {
            "normalized_mpn": {"type": "string"},
            "standardized_description": {"type": ["string", "null"]},
            "category": {"type": ["string", "null"], "enum": [*canonical_keys, None]},
            "category_confidence": {"type": ["number", "null"]},
            "specs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "value": {"type": ["string", "number", "null"]},
                        "confidence": {"type": "number"},
                    },
                    "required": ["key", "value", "confidence"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["normalized_mpn", "standardized_description", "category", "specs"],
        "additionalProperties": False,
    }


def _part_payload(part: ConsolidatedPart) -> dict:
    """The minimal, source-only payload handed to the model for one part."""
    return {
        "normalized_mpn": part.normalized_mpn,
        "mpn": part.raw_mpn,
        "manufacturer": part.manufacturer or "",
        "raw_description": part.description or "",
        "source_category": part.category or "",
    }


def _apply_result(part: ConsolidatedPart, result: dict) -> None:
    """Annotate *part* in place with a validated AI result (silently ignores junk)."""
    desc = result.get("standardized_description")
    if isinstance(desc, str) and desc.strip():
        part.ai_description = desc.strip()

    # Only accept an AI category when the source had none (don't override real category).
    cat = result.get("category")
    if not part.category and isinstance(cat, str) and cat.strip():
        part.ai_category = cat.strip()
        conf = result.get("category_confidence")
        part.ai_category_confidence = float(conf) if isinstance(conf, (int, float)) else 0.5

    for spec in result.get("specs") or []:
        if not isinstance(spec, dict):
            continue
        key = spec.get("key")
        value = spec.get("value")
        # The guardrail's contract: a null value means "not stated" — never persist it.
        if not key or value is None or (isinstance(value, str) and not value.strip()):
            continue
        conf = spec.get("confidence")
        part.ai_specs[str(key)] = {
            "value": value,
            "confidence": float(conf) if isinstance(conf, (int, float)) else 0.5,
        }


async def ai_correct(parts: list[ConsolidatedPart], *, batch_size: int = 25) -> list[ConsolidatedPart]:
    """AI-correct *parts* in place (and return them). One Claude call per part, batched.

    Each part is standardized/categorized/spec-extracted under the no-fabrication guardrail and
    annotated with ``ai_*`` fields tagged ``trio_source_ai`` downstream. A failure on one batch
    is isolated (logged) — its parts simply keep their non-AI values. Confidence-bearing specs
    flow to ingest.py via ``part.ai_specs``.
    """
    canonical_keys = sorted(get_all_commodities())
    schema = _schema(canonical_keys)
    vocab_line = "Canonical category keys (choose exactly one or null): " + ", ".join(canonical_keys)

    for start in range(0, len(parts), batch_size):
        batch = parts[start : start + batch_size]
        # Per-part call (the spec mandates PER PART), but failures are isolated per batch.
        for part in batch:
            try:
                payload = _part_payload(part)
                prompt = f"{vocab_line}\n\nSource row (clean ONLY what this text states, null otherwise):\n{payload}"
                result = await claude_structured(
                    prompt=prompt,
                    schema=schema,
                    system=_SYSTEM,
                    model_tier="smart",
                    max_tokens=1024,
                )
                if result:
                    _apply_result(part, result)
            except Exception:
                logger.exception(
                    "ai_correct: failed on mpn={} (batch {}–{}) — keeping non-AI values",
                    part.normalized_mpn,
                    start,
                    start + len(batch),
                )
    return parts
