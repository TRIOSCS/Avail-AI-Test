"""AI-powered material card enrichment — description + commodity classification."""

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..models import MaterialCard
from ..services.specialty_detector import COMMODITY_MAP

log = logging.getLogger(__name__)

VALID_CATEGORIES = sorted(COMMODITY_MAP.keys()) + ["other"]

_SYSTEM_PROMPT = (
    "You are an expert electronic component engineer. "
    "Given a manufacturer part number (MPN) and optional manufacturer name, "
    "generate a concise technical description and classify the component into "
    "the correct commodity category.\n\n"
    "Rules:\n"
    "- description: 1-2 sentences describing what the part is, key specs if inferable from the MPN.\n"
    "- category: choose from the provided list. Use 'other' only if no category fits.\n"
    "- If you cannot identify the part at all, set description to null and category to 'other'.\n"
    "- Do NOT hallucinate specs — only include what you can confidently infer from the MPN."
)

_PART_SCHEMA = {
    "type": "object",
    "properties": {
        "parts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "mpn": {"type": "string"},
                    "description": {"type": ["string", "null"]},
                    "category": {"type": "string", "enum": VALID_CATEGORIES},
                },
                "required": ["mpn", "description", "category"],
            },
        }
    },
    "required": ["parts"],
}


async def enrich_material_cards(
    card_ids: list[int], db: Session, *, batch_size: int = 30
) -> dict:
    """Enrich material cards with AI-generated descriptions and categories.

    Returns {"enriched": int, "skipped": int, "errors": int}.
    """
    from ..utils.claude_client import claude_structured

    cards = (
        db.query(MaterialCard)
        .filter(
            MaterialCard.id.in_(card_ids),
            MaterialCard.deleted_at.is_(None),
        )
        .all()
    )

    stats = {"enriched": 0, "skipped": 0, "errors": 0}

    # Process in batches
    for i in range(0, len(cards), batch_size):
        chunk = cards[i : i + batch_size]
        await _enrich_batch(chunk, db, stats)

    return stats


async def _enrich_batch(
    cards: list[MaterialCard], db: Session, stats: dict
) -> None:
    """Enrich a single batch of cards via one Claude call."""
    from ..utils.claude_client import claude_structured

    parts_list = []
    for card in cards:
        entry = f"- MPN: {card.display_mpn}"
        if card.manufacturer:
            entry += f" | Manufacturer: {card.manufacturer}"
        parts_list.append(entry)

    parts_text = "\n".join(parts_list)
    cats_text = ", ".join(VALID_CATEGORIES)
    prompt = (
        f"Classify and describe each electronic component:\n\n"
        f"{parts_text}\n\n"
        f"Valid categories: {cats_text}\n\n"
        f"Return a JSON object with a 'parts' array, one entry per MPN above, "
        f"in the same order."
    )

    try:
        result = await claude_structured(
            prompt,
            _PART_SCHEMA,
            system=_SYSTEM_PROMPT,
            model_tier="fast",
            max_tokens=4096,
            timeout=60,
        )
    except Exception as e:
        log.error("Material enrichment Claude call failed: %s", e)
        stats["errors"] += len(cards)
        return

    if not result or "parts" not in result:
        log.warning("Material enrichment: empty or invalid response")
        stats["errors"] += len(cards)
        return

    ai_parts = result["parts"]
    now = datetime.now(timezone.utc)

    for card, ai in zip(cards, ai_parts):
        try:
            desc = ai.get("description")
            cat = ai.get("category", "other")

            if cat not in VALID_CATEGORIES:
                cat = "other"

            if desc:
                card.description = desc
            card.category = cat
            card.enrichment_source = "claude_haiku"
            card.enriched_at = now
            stats["enriched"] += 1
        except Exception as e:
            log.warning("Failed to apply enrichment for card %d: %s", card.id, e)
            stats["errors"] += 1

    try:
        db.commit()
    except Exception as e:
        log.error("Material enrichment commit failed: %s", e)
        db.rollback()
        stats["errors"] += len(cards)
        stats["enriched"] -= len(cards)


async def enrich_pending_cards(
    db: Session, *, limit: int = 300, batch_size: int = 30
) -> dict:
    """Find and enrich cards that need descriptions/categories.

    Priority order:
    1. Cards linked to active requirements (most visible)
    2. Cards with high search_count
    3. Any un-enriched cards
    """
    from ..models.sourcing import Requirement

    # Cards linked to active requirements that haven't been enriched
    active_req_cards = (
        db.query(MaterialCard.id)
        .join(Requirement, Requirement.material_card_id == MaterialCard.id)
        .filter(
            MaterialCard.enriched_at.is_(None),
            MaterialCard.deleted_at.is_(None),
        )
        .order_by(MaterialCard.search_count.desc())
        .limit(limit)
        .all()
    )
    card_ids = [r[0] for r in active_req_cards]

    # Fill remaining quota with any un-enriched cards
    if len(card_ids) < limit:
        remaining = limit - len(card_ids)
        existing = set(card_ids)
        more = (
            db.query(MaterialCard.id)
            .filter(
                MaterialCard.enriched_at.is_(None),
                MaterialCard.deleted_at.is_(None),
                ~MaterialCard.id.in_(existing) if existing else True,
            )
            .order_by(MaterialCard.search_count.desc())
            .limit(remaining)
            .all()
        )
        card_ids.extend(r[0] for r in more)

    if not card_ids:
        return {"enriched": 0, "skipped": 0, "errors": 0, "pending": 0}

    result = await enrich_material_cards(card_ids, db, batch_size=batch_size)
    result["pending"] = card_ids.__len__()
    return result
