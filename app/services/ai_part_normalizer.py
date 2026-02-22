"""AI Part Number Normalizer — Intelligently match variant part numbers to canonical form.

Purpose:
  Use LLM knowledge of electronic component naming conventions to normalize
  part numbers, infer manufacturers, extract package codes, and detect
  cross-references. Goes beyond regex by understanding JEDEC standards,
  manufacturer-specific suffixes, and industry conventions.

Business Rules:
  - Confidence ≥ 0.7 → use normalized result
  - Confidence < 0.7 → return original string unchanged (safety fallback)
  - Cache results in memory (same input always produces same output)
  - Batch processing in a single LLM call for efficiency
  - Never modify the original part number stored in the database

Called by: routers/ai.py (POST /api/ai/normalize-parts)
Depends on: services/gradient_service.py
"""

from __future__ import annotations

from loguru import logger

from app.services.gradient_service import gradient_json

CONFIDENCE_THRESHOLD = 0.7
MAX_BATCH_SIZE = 25  # LLM context limit per call

# In-memory cache: raw part number → normalized result dict
_cache: dict[str, dict] = {}

SYSTEM_PROMPT = """\
You are an expert in electronic component part numbering conventions.

Your knowledge includes:
- JEDEC standard part numbers (e.g., 2N2222, 1N4148)
- Manufacturer-specific naming: TI, STMicroelectronics, NXP, Microchip, \
Analog Devices, Infineon, ON Semi, Vishay, Murata, TDK, Samsung, Hynix
- Package suffixes: -DR (SOIC tape & reel), -DT (SOIC cut tape), \
VGT6 (LQFP), -ND (DigiKey suffix — NOT part of the MPN)
- Passive component codes: 0402, 0603, 0805 (imperial sizes), \
100nF, 10uF (values as part numbers)
- Cross-references: same die in different packages from same or \
different manufacturers

Rules:
- "normalized" should be the canonical MPN (uppercase, no extra whitespace, \
keep meaningful dashes)
- "manufacturer" should be the full company name, not abbreviation \
(e.g., "Texas Instruments" not "TI")
- "base_part" is the core part without package/suffix \
(e.g., "LM358" from "LM358DR")
- "package_code" is the suffix that denotes package/variant \
(e.g., "DR" from "LM358DR")
- "is_alias" = true only if the input appears to be a distributor SKU \
or cross-reference, not a real MPN
- Confidence reflects how certain you are (0.0-1.0)

Return ONLY valid JSON — an array with one object per input part number:
[
  {
    "original": "input string",
    "normalized": "CANONICAL-MPN",
    "manufacturer": "Full Company Name" or null,
    "base_part": "BASE" or null,
    "package_code": "SUFFIX" or null,
    "is_alias": false,
    "confidence": 0.95
  }
]"""


async def normalize_parts(raw_parts: list[str]) -> list[dict]:
    """Normalize a batch of part numbers using LLM intelligence.

    Args:
        raw_parts: List of raw part number strings.

    Returns:
        List of normalized result dicts (same order as input).
        Each dict has: original, normalized, manufacturer, base_part,
        package_code, is_alias, confidence.
    """
    if not raw_parts:
        return []

    results: list[dict] = []
    uncached: list[tuple[int, str]] = []  # (index, raw_part)

    # Check cache first
    for i, raw in enumerate(raw_parts):
        key = raw.strip()
        if key in _cache:
            results.append(_cache[key])
        else:
            results.append({})  # placeholder
            uncached.append((i, key))

    if not uncached:
        return results

    # Process uncached parts in batches
    for batch_start in range(0, len(uncached), MAX_BATCH_SIZE):
        batch = uncached[batch_start : batch_start + MAX_BATCH_SIZE]
        batch_parts = [part for _, part in batch]

        parsed = await _call_normalizer(batch_parts)

        for j, (idx, raw) in enumerate(batch):
            if parsed and j < len(parsed):
                result = _validate_result(raw, parsed[j])
            else:
                result = _fallback(raw)

            _cache[raw] = result
            results[idx] = result

    return results


async def _call_normalizer(parts: list[str]) -> list[dict] | None:
    """Call Gradient to normalize a batch of part numbers."""
    parts_list = "\n".join(f"{i+1}. {p}" for i, p in enumerate(parts))
    prompt = f"Normalize these {len(parts)} electronic component part numbers:\n\n{parts_list}"

    result = await gradient_json(
        prompt,
        system=SYSTEM_PROMPT,
        model_tier="default",
        max_tokens=256 * len(parts),
        temperature=0.1,
        timeout=30,
    )

    if not result:
        return None

    # Handle both list and dict-with-list responses
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in ("parts", "results", "normalized"):
            if isinstance(result.get(key), list):
                return result[key]

    logger.warning("Unexpected normalizer response format: {}", type(result).__name__)
    return None


def _validate_result(raw: str, parsed: dict) -> dict:
    """Validate and clean a single parsed result, with fallback."""
    if not isinstance(parsed, dict):
        return _fallback(raw)

    confidence = 0.5
    try:
        confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0.5))))
    except (ValueError, TypeError):
        pass

    # Low confidence → return original unchanged
    if confidence < CONFIDENCE_THRESHOLD:
        result = _fallback(raw)
        result["confidence"] = confidence
        return result

    return {
        "original": raw,
        "normalized": str(parsed.get("normalized") or raw).strip().upper(),
        "manufacturer": parsed.get("manufacturer") or None,
        "base_part": parsed.get("base_part") or None,
        "package_code": parsed.get("package_code") or None,
        "is_alias": bool(parsed.get("is_alias", False)),
        "confidence": confidence,
    }


def _fallback(raw: str) -> dict:
    """Return original part number unchanged (safe default)."""
    return {
        "original": raw,
        "normalized": raw.strip().upper(),
        "manufacturer": None,
        "base_part": None,
        "package_code": None,
        "is_alias": False,
        "confidence": 0.0,
    }


def clear_cache() -> int:
    """Clear the normalization cache. Returns number of entries cleared."""
    count = len(_cache)
    _cache.clear()
    return count
