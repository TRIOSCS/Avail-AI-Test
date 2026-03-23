"""AI commodity gate for NetComponents search queue.

Uses Claude Haiku to classify electronic parts as worth searching on NC
(semiconductors, ICs, hard-to-find) vs. skip (standard passives, connectors).
Includes a classification cache to avoid re-classifying the same part.

Called by: worker loop (process_ai_gate)
Depends on: claude_client, nc_search_queue model
"""

import threading
import time
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from app.models import NcSearchQueue

# Cooldown after API failure to avoid hammering a broken endpoint
_GATE_COOLDOWN_SECONDS = 300  # 5 minutes
_last_api_failure: float = 0.0

_GATE_SYSTEM_PROMPT = """\
You classify electronic components for sourcing on NetComponents marketplace.

SEARCH on NetComponents (return search_nc=true):
- Semiconductors, ICs (microcontrollers, op-amps, voltage regulators, ADCs/DACs)
- FPGAs, CPLDs, ASICs
- Power management ICs (PMICs, DC-DC converters, LDOs)
- Memory chips (SRAM, DRAM, Flash, EEPROM)
- Obsolete/EOL/hard-to-find parts
- Military/aerospace parts (JAN, MIL-spec, QPL)
- RF/microwave components
- Specialty sensors (MEMS, pressure, accelerometer ICs)
- Any part that is not a commodity item

SKIP NetComponents (return search_nc=false):
- Standard chip resistors (RC/CR/ERJ/CRCW series)
- MLCC capacitors (GRM/CL/C0G/X5R/X7R series in standard packages)
- Standard inductors (commodity wound inductors)
- Commodity connectors (JST headers, Molex headers, USB-A/B/C standard)
- Standard LEDs (through-hole, common SMD 0402-1206)
- Standard diodes (1N4148, 1N5819, BAT54, common Schottky/switching)
- Standard crystals and oscillators
- Cable assemblies
- Mechanical/hardware items (screws, standoffs, enclosures)
- Fuses, ferrite beads (standard values)

Return a JSON array. Each element: {"mpn": str, "search_nc": bool, "commodity": str, "reason": str}
The commodity field should be one of: semiconductor, passive, connector, discrete, memory, power, rf, sensor, mechanical, other.
The reason should be a brief explanation (under 50 words).
"""

_GATE_SCHEMA = {
    "type": "object",
    "properties": {
        "classifications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "mpn": {"type": "string"},
                    "search_nc": {"type": "boolean"},
                    "commodity": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["mpn", "search_nc", "commodity", "reason"],
            },
        }
    },
    "required": ["classifications"],
}

# In-memory classification cache: (normalized_mpn, manufacturer) -> (commodity, decision, reason)
_classification_cache: dict[tuple[str, str], tuple[str, str, str]] = {}
_cache_lock = threading.Lock()


async def classify_parts_batch(parts: list[dict]) -> list[dict] | None:
    """Classify up to 30 parts using Claude Haiku.

    Args:
        parts: List of {"mpn": str, "manufacturer": str, "description": str}

    Returns:
        List of {"mpn": str, "search_nc": bool, "commodity": str, "reason": str}
        or None on API failure.
    """
    from app.utils.llm_router import routed_structured

    if not parts:
        return []

    prompt_lines = ["Classify these electronic parts:\n"]
    for p in parts:
        mfr = p.get("manufacturer") or "unknown"
        desc = p.get("description") or ""
        prompt_lines.append(f"- MPN: {p['mpn']}, Manufacturer: {mfr}, Description: {desc}")

    prompt = "\n".join(prompt_lines)

    try:
        result = await routed_structured(
            prompt=prompt,
            schema=_GATE_SCHEMA,
            system=_GATE_SYSTEM_PROMPT,
            model_tier="fast",
            max_tokens=2048,
            timeout=30,
        )
        if result and "classifications" in result:
            return result["classifications"]
        logger.warning("AI gate: unexpected response format: {}", result)
        return None
    except Exception as e:
        logger.error("AI gate: Claude API call failed: {}", e)
        return None


async def process_ai_gate(db: Session):
    """Process pending queue items through the AI classification gate.

    Checks classification cache first, then batch-classifies uncached items. Updates
    queue status to 'queued' (search) or 'gated_out' (skip). Respects a 5-minute
    cooldown after API failures.
    """
    global _last_api_failure
    if _last_api_failure and (time.monotonic() - _last_api_failure) < _GATE_COOLDOWN_SECONDS:
        remaining = int(_GATE_COOLDOWN_SECONDS - (time.monotonic() - _last_api_failure))
        logger.debug("AI gate: in cooldown after API failure ({}s remaining)", remaining)
        return

    pending = (
        db.query(NcSearchQueue)
        .filter(NcSearchQueue.status == "pending")
        .order_by(NcSearchQueue.priority.asc(), NcSearchQueue.created_at.desc())
        .limit(30)
        .all()
    )

    if not pending:
        return

    uncached = []
    for item in pending:
        cache_key = (item.normalized_mpn, (item.manufacturer or "").lower())
        with _cache_lock:
            cached = _classification_cache.get(cache_key)
        if cached:
            commodity, decision, reason = cached
            item.commodity_class = commodity
            item.gate_decision = decision
            item.gate_reason = f"[cached] {reason}"
            item.status = "queued" if decision == "search" else "gated_out"
            item.updated_at = datetime.now(timezone.utc)
            logger.debug("AI gate cache hit: {} -> {}", item.mpn, decision)
        else:
            uncached.append(item)

    if uncached:
        parts = [
            {"mpn": item.mpn, "manufacturer": item.manufacturer or "", "description": item.description or ""}
            for item in uncached
        ]

        # Split into batches of 30
        for batch_start in range(0, len(parts), 30):
            batch = parts[batch_start : batch_start + 30]
            batch_items = uncached[batch_start : batch_start + 30]

            results = await classify_parts_batch(batch)
            if results is None:
                # API failure — fail-open: default to search so items aren't stuck
                _last_api_failure = time.monotonic()
                logger.warning("AI gate: API failure, defaulting {} items to 'queued' (fail-open)", len(batch_items))
                for item in batch_items:
                    item.commodity_class = "unknown"
                    item.gate_decision = "search"
                    item.gate_reason = "AI gate unavailable — defaulting to search"
                    item.status = "queued"
                    item.updated_at = datetime.now(timezone.utc)
                break  # Stop processing further batches during this cycle

            # Build a lookup by MPN
            result_map = {r["mpn"]: r for r in results}

            for item in batch_items:
                classification = result_map.get(item.mpn)
                if not classification:
                    # Model didn't return this MPN — leave pending
                    logger.warning("AI gate: no classification returned for {}", item.mpn)
                    continue

                decision = "search" if classification["search_nc"] else "skip"
                commodity = classification["commodity"]
                reason = classification["reason"]

                item.commodity_class = commodity
                item.gate_decision = decision
                item.gate_reason = reason
                item.status = "queued" if decision == "search" else "gated_out"
                item.updated_at = datetime.now(timezone.utc)

                # Cache the classification
                cache_key = (item.normalized_mpn, (item.manufacturer or "").lower())
                with _cache_lock:
                    _classification_cache[cache_key] = (commodity, decision, reason)

                logger.info("AI gate: {} ({}) -> {} ({})", item.mpn, commodity, decision, reason)

    db.commit()
    logger.info(
        "AI gate processed {} items: {} from cache, {} classified",
        len(pending),
        len(pending) - len(uncached),
        len(uncached),
    )


def clear_classification_cache():
    """Clear the in-memory classification cache (for testing)."""
    with _cache_lock:
        _classification_cache.clear()
