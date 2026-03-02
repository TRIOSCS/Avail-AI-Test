"""AI fallback classification — classify remaining untagged parts via Claude Haiku.

Last resort in the classification waterfall. Batches 20-50 MPNs per API call,
parses structured JSON response, creates MaterialTags with source='ai_classified'.

Called by: app.routers.tagging_admin (manual trigger)
Depends on: app.utils.claude_client, app.services.tagging
"""

import asyncio

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


async def classify_parts_with_ai(part_numbers: list[str]) -> list[dict]:  # pragma: no cover
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
        if not isinstance(item, dict):  # pragma: no cover
            continue
        classified.append({
            "mpn": (item.get("mpn") or "").strip(),
            "manufacturer": (item.get("manufacturer") or "Unknown").strip(),
            "category": (item.get("category") or "Miscellaneous").strip(),
        })

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
        confidence = 0.3 if is_unknown else 0.7

        tags_to_apply = []

        brand_tag = get_or_create_brand_tag(manufacturer, db)
        tags_to_apply.append({
            "tag_id": brand_tag.id,
            "source": "ai_classified",
            "confidence": confidence,
        })

        commodity_tag = get_or_create_commodity_tag(category, db)
        if commodity_tag:  # pragma: no cover
            tags_to_apply.append({
                "tag_id": commodity_tag.id,
                "source": "ai_classified",
                "confidence": confidence,
            })

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


async def run_ai_backfill(db: Session, batch_size: int = 50, concurrency: int = 10) -> dict:  # pragma: no cover
    """Classify remaining untagged parts after Nexar. Concurrent API calls.

    Args:
        batch_size: MPNs per Claude API call (default 50)
        concurrency: Parallel API calls (default 10, so 500 MPNs per round)

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

    logger.info(f"AI backfill starting: {len(untagged)} untagged cards (batch={batch_size}, concurrency={concurrency})")
    total_processed = 0
    total_matched = 0
    total_unknown = 0

    # Semaphore limits concurrent API calls; delay paces token usage
    sem = asyncio.Semaphore(concurrency)

    async def _classify_batch(batch):
        mpns = [row.normalized_mpn for row in batch]
        async with sem:
            for attempt in range(3):
                try:
                    return await classify_parts_with_ai(mpns)
                except Exception as e:
                    if "rate_limit" in str(e).lower() or "429" in str(e):
                        wait = 10 * (attempt + 1)
                        logger.warning(f"Rate limited, waiting {wait}s (attempt {attempt + 1}/3)")
                        await asyncio.sleep(wait)
                    else:
                        logger.exception("AI classification batch failed")
                        break
            return [{"mpn": mpn, "manufacturer": "Unknown", "category": "Miscellaneous"} for mpn in mpns]

    # Split all untagged into sub-batches
    all_batches = [
        untagged[j : j + batch_size]
        for j in range(0, len(untagged), batch_size)
    ]

    # Process in rounds of (concurrency) batches with a delay between rounds
    for round_start in range(0, len(all_batches), concurrency):
        round_batches = all_batches[round_start : round_start + concurrency]

        results = await asyncio.gather(*[_classify_batch(b) for b in round_batches])

        for batch, classified in zip(round_batches, results):
            matched, unknown = _apply_ai_results(classified, batch, db)
            total_matched += matched
            total_unknown += unknown
            total_processed += len(batch)

        db.commit()
        logger.info(f"AI backfill: {total_processed}/{len(untagged)} — {total_matched} matched, {total_unknown} unknown")

        # Pace to stay under 90K output tokens/min (~3K tokens per batch)
        # concurrency batches * 3K tokens = tokens_per_round
        # Target: stay under 80K/min to leave headroom
        if round_start + concurrency < len(all_batches):
            tokens_per_round = concurrency * 3000
            delay = max(1.0, (tokens_per_round / 80000) * 60)
            await asyncio.sleep(delay)

    logger.info(f"AI backfill complete: {total_processed} processed, {total_matched} matched, {total_unknown} unknown")
    return {"total_processed": total_processed, "total_matched": total_matched, "total_unknown": total_unknown}
