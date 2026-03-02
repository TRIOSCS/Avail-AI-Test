"""AI fallback classification — classify remaining untagged parts via Claude Haiku.

Last resort in the classification waterfall. Batches 20-50 MPNs per API call,
parses structured JSON response, creates MaterialTags with source='ai_classified'.

Called by: app.routers.tagging_admin (manual trigger)
Depends on: app.utils.claude_client, app.services.tagging
"""

from loguru import logger
from sqlalchemy.orm import Session

from app.models.intelligence import MaterialCard
from app.models.tags import MaterialTag, Tag
from app.services.tagging import (
    get_or_create_brand_tag,
    get_or_create_commodity_tag,
    tag_material_card,
)

_CLASSIFY_PROMPT = """Classify these electronic component part numbers. For each MPN, provide:
- manufacturer: The most likely manufacturer (full company name)
- category: The component category (e.g., Microcontrollers (MCU), Capacitors, Connectors, etc.)

Return a JSON array with one object per part:
[{{"mpn": "...", "manufacturer": "...", "category": "..."}}]

If you're unsure about a part, use "Unknown" for manufacturer and "Miscellaneous" for category.

Part numbers to classify:
{mpns}"""

_SYSTEM = "You are an expert electronic component classifier. Return only valid JSON."


async def classify_parts_with_ai(part_numbers: list[str]) -> list[dict]:
    """Batch MPNs per Claude Haiku call. Parse structured JSON response."""
    from app.utils.claude_client import claude_json

    mpn_list = "\n".join(f"- {mpn}" for mpn in part_numbers)
    prompt = _CLASSIFY_PROMPT.format(mpns=mpn_list)

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
        if not isinstance(item, dict):
            continue
        classified.append({
            "mpn": (item.get("mpn") or "").strip(),
            "manufacturer": (item.get("manufacturer") or "Unknown").strip(),
            "category": (item.get("category") or "Miscellaneous").strip(),
        })

    return classified


async def run_ai_backfill(db: Session, batch_size: int = 30) -> dict:
    """Classify remaining untagged parts after Nexar. Same batching patterns.

    Returns: {total_processed, total_matched, total_unknown}
    """
    # Find cards with NO brand tag
    tagged_brand_ids = (
        db.query(MaterialTag.material_card_id)
        .join(Tag, MaterialTag.tag_id == Tag.id)
        .filter(Tag.tag_type == "brand")
        .distinct()
        .subquery()
    )
    untagged = (
        db.query(MaterialCard.id, MaterialCard.normalized_mpn)
        .filter(~MaterialCard.id.in_(db.query(tagged_brand_ids.c.material_card_id)))
        .order_by(MaterialCard.id)
        .all()
    )

    if not untagged:
        logger.info("No untagged cards for AI backfill")
        return {"total_processed": 0, "total_matched": 0, "total_unknown": 0}

    logger.info(f"AI backfill starting: {len(untagged)} untagged cards")
    total_processed = 0
    total_matched = 0
    total_unknown = 0

    for i in range(0, len(untagged), batch_size):
        batch = untagged[i : i + batch_size]
        mpns = [row.normalized_mpn for row in batch]

        try:
            classified = await classify_parts_with_ai(mpns)
        except Exception:
            logger.exception("AI classification batch failed")
            total_unknown += len(batch)
            total_processed += len(batch)
            continue

        # Build lookup: mpn → classification result
        mpn_to_result = {c["mpn"].lower(): c for c in classified}

        for card_id, normalized_mpn in batch:
            total_processed += 1
            result = mpn_to_result.get(normalized_mpn.lower(), {})
            manufacturer = result.get("manufacturer", "Unknown")
            category = result.get("category", "Miscellaneous")

            is_unknown = manufacturer == "Unknown"
            confidence = 0.3 if is_unknown else 0.7

            tags_to_apply = []

            brand_tag = get_or_create_brand_tag(manufacturer, db)
            tags_to_apply.append({
                "tag_id": brand_tag.id,
                "source": "ai_classified",
                "confidence": confidence,
            })

            commodity_tag = get_or_create_commodity_tag(category, db)
            if commodity_tag:
                tags_to_apply.append({
                    "tag_id": commodity_tag.id,
                    "source": "ai_classified",
                    "confidence": confidence,
                })

            tag_material_card(card_id, tags_to_apply, db)

            if is_unknown:
                total_unknown += 1
            else:
                total_matched += 1

            # Update card fields if AI discovered manufacturer
            if not is_unknown:
                card = db.get(MaterialCard, card_id)
                if card and not card.manufacturer:
                    card.manufacturer = manufacturer

        db.commit()
        logger.info(f"AI backfill: {total_processed}/{len(untagged)} — {total_matched} matched, {total_unknown} unknown")

    logger.info(f"AI backfill complete: {total_processed} processed, {total_matched} matched, {total_unknown} unknown")
    return {"total_processed": total_processed, "total_matched": total_matched, "total_unknown": total_unknown}
