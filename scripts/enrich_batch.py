"""Batch AI enrichment for material cards — category classification.

Builds structured prompts for Claude to classify material cards into
granular component categories, then submits as batch requests for
50% cost reduction.

Called by: jobs/enrichment_jobs.py, tests/test_enrich_batch.py
Depends on: app.utils.claude_client, app.models.materials
"""

# 45+ granular categories for faceted search
VALID_CATEGORIES = {
    # Memory
    "dram",
    "sram",
    "flash_memory",
    "eeprom",
    "nvram",
    "memory_modules",
    # Processors
    "cpu",
    "gpu",
    "fpga",
    "microcontrollers",
    "dsp",
    "asic",
    # Passive components
    "capacitors",
    "resistors",
    "inductors",
    "transformers",
    "filters",
    "crystals_oscillators",
    # Active components
    "diodes",
    "transistors",
    "mosfets",
    "igbt",
    "thyristors",
    "voltage_regulators",
    "op_amps",
    "comparators",
    "adc_dac",
    # Connectors & electromechanical
    "connectors",
    "relays",
    "switches",
    "fuses",
    "circuit_breakers",
    # ICs
    "logic_ics",
    "interface_ics",
    "power_management",
    "sensor_ics",
    "rf_ics",
    "driver_ics",
    # Board-level
    "motherboards",
    "network_cards",
    "raid_controllers",
    "power_supplies",
    "fans_cooling",
    "cables",
    # Other
    "other",
}

BATCH_SIZE = 50


def _build_prompt(cards: list[dict]) -> str:
    """Build a classification prompt for a batch of material cards.

    Args:
        cards: List of dicts with display_mpn, manufacturer, description.

    Returns:
        Prompt string for Claude.
    """
    lines = ["Classify each part number into exactly one category.\n"]
    lines.append("Valid categories: " + ", ".join(sorted(VALID_CATEGORIES)) + "\n")

    for i, card in enumerate(cards, 1):
        mpn = card.get("display_mpn", "UNKNOWN")
        entry = f"{i}. {mpn}"

        mfr = card.get("manufacturer")
        if mfr:
            entry += f" (Manufacturer: {mfr})"

        desc = card.get("description")
        if desc:
            entry += f" — Context: {desc}"

        lines.append(entry)

    lines.append('\nReturn a JSON array where each element has: {"mpn": "...", "category": "..."}')
    return "\n".join(lines)


def _build_batch_requests(cards: list[dict]) -> list[dict]:
    """Split cards into batches and build request dicts for Claude batch API.

    Args:
        cards: List of dicts with id, display_mpn, manufacturer, description.

    Returns:
        List of request dicts with custom_id, prompt, schema, system, model_tier, max_tokens.
    """
    requests = []

    for batch_idx in range(0, len(cards), BATCH_SIZE):
        batch = cards[batch_idx : batch_idx + BATCH_SIZE]
        prompt = _build_prompt(batch)
        card_ids = [c["id"] for c in batch]

        requests.append(
            {
                "custom_id": f"enrich_batch_{batch_idx}_{card_ids[0]}_{card_ids[-1]}",
                "prompt": prompt,
                "system": "You are an electronic component classification expert. "
                "Classify each part number into exactly one category from the provided list.",
                "schema": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "mpn": {"type": "string"},
                            "category": {"type": "string"},
                        },
                        "required": ["mpn", "category"],
                    },
                },
                "model_tier": "smart",
                "max_tokens": 4096,
            }
        )

    return requests
