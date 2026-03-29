"""AI commodity gate for search worker queues.

Uses Claude Haiku to classify electronic parts as worth searching on a
given marketplace (semiconductors, ICs, hard-to-find) vs. skip (standard
passives, connectors). Includes a classification cache to avoid
re-classifying the same part.

Called by: worker loop (process_ai_gate)
Depends on: llm_router, queue model
"""

import threading
import time
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

# Cooldown after API failure to avoid hammering a broken endpoint
_GATE_COOLDOWN_SECONDS = 300  # 5 minutes


def _build_system_prompt(marketplace_name: str, search_field: str) -> str:
    """Build the classification system prompt for a given marketplace."""
    return f"""\
You classify electronic components for sourcing on {marketplace_name} marketplace.

SEARCH on {marketplace_name} (return {search_field}=true):
- Semiconductors, ICs (microcontrollers, op-amps, voltage regulators, ADCs/DACs)
- FPGAs, CPLDs, ASICs
- Power management ICs (PMICs, DC-DC converters, LDOs)
- Memory chips (SRAM, DRAM, Flash, EEPROM)
- Obsolete/EOL/hard-to-find parts
- Military/aerospace parts (JAN, MIL-spec, QPL)
- RF/microwave components
- Specialty sensors (MEMS, pressure, accelerometer ICs)
- Any part that is not a commodity item

SKIP {marketplace_name} (return {search_field}=false):
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

Return a JSON array. Each element: {{"mpn": str, "{search_field}": bool, "commodity": str, "reason": str}}
The commodity field should be one of: semiconductor, passive, connector, discrete, memory, power, rf, sensor, mechanical, other.
The reason should be a brief explanation (under 50 words).
"""


def _build_schema(search_field: str) -> dict:
    """Build the JSON schema for classification results."""
    return {
        "type": "object",
        "properties": {
            "classifications": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "mpn": {"type": "string"},
                        search_field: {"type": "boolean"},
                        "commodity": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["mpn", search_field, "commodity", "reason"],
                },
            }
        },
        "required": ["classifications"],
    }


class AIGate:
    """Parameterized AI commodity gate for a search worker queue.

    Args:
        queue_model: The SQLAlchemy model class for the queue table.
        marketplace_name: Human-readable marketplace name (e.g. "ICsource", "NetComponents").
        search_field: The boolean field name in the classification result
            (e.g. "search_ics", "search_nc").
        log_prefix: Short prefix for log messages (e.g. "ICS", "NC").
    """

    def __init__(
        self,
        queue_model: type,
        marketplace_name: str,
        search_field: str,
        log_prefix: str = "WORKER",
    ):
        self.queue_model = queue_model
        self.marketplace_name = marketplace_name
        self.search_field = search_field
        self.log_prefix = log_prefix
        self._system_prompt = _build_system_prompt(marketplace_name, search_field)
        self._schema = _build_schema(search_field)
        self._last_api_failure: float = 0.0
        # In-memory classification cache: (normalized_mpn, manufacturer) -> (commodity, decision, reason)
        self._classification_cache: dict[tuple[str, str], tuple[str, str, str]] = {}
        self._cache_lock = threading.Lock()

    async def classify_parts_batch(self, parts: list[dict]) -> list[dict] | None:
        """Classify up to 30 parts using Claude Haiku.

        Args:
            parts: List of {"mpn": str, "manufacturer": str, "description": str}

        Returns:
            List of {"mpn": str, search_field: bool, "commodity": str, "reason": str}
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
                schema=self._schema,
                system=self._system_prompt,
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

    async def process_ai_gate(self, db: Session):
        """Process pending queue items through the AI classification gate.

        Checks classification cache first, then batch-classifies uncached items. Updates
        queue status to 'queued' (search) or 'gated_out' (skip). Respects a 5-minute
        cooldown after API failures.
        """
        if self._last_api_failure and (time.monotonic() - self._last_api_failure) < _GATE_COOLDOWN_SECONDS:
            remaining = int(_GATE_COOLDOWN_SECONDS - (time.monotonic() - self._last_api_failure))
            logger.debug("AI gate: in cooldown after API failure ({}s remaining)", remaining)
            return

        model = self.queue_model
        pending = db.query(model).filter(model.status == "pending").order_by(model.created_at.asc()).limit(30).all()

        if not pending:
            return

        uncached = []
        for item in pending:
            cache_key = (item.normalized_mpn, (item.manufacturer or "").lower())
            with self._cache_lock:
                cached = self._classification_cache.get(cache_key)
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

                results = await self.classify_parts_batch(batch)
                if results is None:
                    # API failure — fail-open: default to search so items aren't stuck
                    self._last_api_failure = time.monotonic()
                    logger.warning(
                        "AI gate: API failure, defaulting {} items to 'queued' (fail-open)", len(batch_items)
                    )
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

                    decision = "search" if classification[self.search_field] else "skip"
                    commodity = classification["commodity"]
                    reason = classification["reason"]

                    item.commodity_class = commodity
                    item.gate_decision = decision
                    item.gate_reason = reason
                    item.status = "queued" if decision == "search" else "gated_out"
                    item.updated_at = datetime.now(timezone.utc)

                    # Cache the classification
                    cache_key = (item.normalized_mpn, (item.manufacturer or "").lower())
                    with self._cache_lock:
                        self._classification_cache[cache_key] = (commodity, decision, reason)

                    logger.info("AI gate: {} ({}) -> {} ({})", item.mpn, commodity, decision, reason)

        db.commit()
        logger.info(
            "AI gate processed {} items: {} from cache, {} classified",
            len(pending),
            len(pending) - len(uncached),
            len(uncached),
        )
