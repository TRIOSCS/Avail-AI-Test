"""AI classification — classify untagged parts via Claude Haiku.

Last resort in the classification waterfall. Two modes:
  - Gradient (primary, free): Submit MPNs via DigitalOcean Gradient
  - Anthropic (fallback): Direct API call

Called by: tagging_ai_batch (batch backfill), app.routers.tagging_admin
Depends on: app.utils.claude_client, app.services.tagging
"""

from loguru import logger
from sqlalchemy.orm import Session

from app.models.intelligence import MaterialCard
from app.services.tagging import (
    get_or_create_brand_tag,
    get_or_create_commodity_tag,
    tag_material_card,
)

_CLASSIFY_PROMPT = """Classify these electronic component part numbers. For each MPN, provide:
- manufacturer: The manufacturer (full company name), or null if you are not at least 90% confident
- category: The component category (e.g., Microcontrollers (MCU), Capacitors, Connectors, etc.), or null if unknown
- confidence: Your confidence in the manufacturer identification (0.0 to 1.0). Only return a manufacturer if confidence >= 0.90.

Common MPN patterns to help:
- STM32*, STM8* → STMicroelectronics; TPS*, LM*, SN* → Texas Instruments
- ATMEGA*, PIC* → Microchip Technology; LPC*, S32K* → NXP Semiconductors
- IRF*, BSC*, CY8C* → Infineon Technologies; AD*, LTC*, MAX* → Analog Devices
- GRM*, BLM* → Murata; ERJ*, EEE* → Panasonic; CRCW* → Vishay

If this looks like an internal/custom part number (company-specific prefixes, purchase
order numbers, or non-standard formats), return null for manufacturer.
If you are less than 90% confident in the manufacturer, return null — do NOT guess.

Return a JSON array with one object per part:
[{{"mpn": "...", "manufacturer": "..." or null, "category": "..." or null, "confidence": 0.0-1.0}}]

Part numbers to classify:
{mpns}"""

_SYSTEM = "You are an expert electronic component classifier. Return only valid JSON. Use null instead of 'Unknown' when unsure."


async def classify_parts_with_ai(part_numbers: list[str]) -> list[dict]:  # pragma: no cover
    """Batch MPNs via Gradient (free) with Anthropic fallback. Parse structured JSON response."""
    from app.services.gradient_service import gradient_json

    mpn_list = "\n".join(f"- {mpn}" for mpn in part_numbers)
    prompt = _CLASSIFY_PROMPT.format(mpns=mpn_list)

    # Primary: Gradient Claude Sonnet (free, unlimited, high accuracy for MPN classification)
    result = await gradient_json(
        prompt,
        system=_SYSTEM,
        model="anthropic-claude-sonnet-4-6",
        max_tokens=4096,
        temperature=0.1,
        timeout=60,
    )

    # Fallback: direct Anthropic API ($400 budget)
    if not result or not isinstance(result, list):
        from app.utils.claude_client import claude_json

        result = await claude_json(
            prompt,
            system=_SYSTEM,
            model_tier="fast",
            max_tokens=4096,
            timeout=60,
        )

    if not result or not isinstance(result, list):
        logger.warning("AI classification returned invalid response")
        return [{"mpn": mpn, "manufacturer": "Unknown", "category": "Miscellaneous"} for mpn in part_numbers]

    # Validate and normalize response
    classified = []
    for item in result:
        if not isinstance(item, dict):  # pragma: no cover
            continue
        classified.append(
            {
                "mpn": (item.get("mpn") or "").strip(),
                "manufacturer": (item.get("manufacturer") or "Unknown").strip(),
                "category": (item.get("category") or "Miscellaneous").strip(),
            }
        )

    return classified


def _apply_ai_results(classified: list[dict], batch: list, db: Session) -> tuple[int, int]:  # pragma: no cover
    """Apply classification results to DB. Returns (matched, unknown) counts."""
    matched = 0
    unknown = 0
    mpn_to_result = {c["mpn"].lower(): c for c in classified}

    for card_id, normalized_mpn in batch:
        result = mpn_to_result.get(normalized_mpn.lower(), {})
        manufacturer = result.get("manufacturer", "Unknown")
        category = result.get("category", "Miscellaneous")

        is_unknown = manufacturer == "Unknown"
        confidence = 0.3 if is_unknown else 0.92

        tags_to_apply = []

        brand_tag = get_or_create_brand_tag(manufacturer, db)
        tags_to_apply.append(
            {
                "tag_id": brand_tag.id,
                "source": "ai_classified",
                "confidence": confidence,
            }
        )

        commodity_tag = get_or_create_commodity_tag(category, db) if category and category != "Miscellaneous" else None
        if commodity_tag:  # pragma: no cover
            tags_to_apply.append(
                {
                    "tag_id": commodity_tag.id,
                    "source": "ai_classified",
                    "confidence": confidence,
                }
            )

        tag_material_card(card_id, tags_to_apply, db)

        if is_unknown:
            unknown += 1
        else:
            matched += 1

        if not is_unknown:
            card = db.get(MaterialCard, card_id)
            if card and not card.manufacturer:
                card.manufacturer = manufacturer

    return matched, unknown
