"""AI-powered material card enrichment — description + commodity classification.

Includes real-time enrichment (enrich_material_cards, enrich_pending_cards)
and batch API enrichment (batch_enrich_materials, process_material_batch_results).

Called by: app/routers, app/jobs/core_jobs.py
Depends on: app.utils.claude_client, app.services.batch_queue, app.cache.intel_cache
"""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from ..cache.intel_cache import _get_redis
from ..models import MaterialCard
from ..services.batch_queue import BatchQueue
from ..services.specialty_detector import COMMODITY_MAP
from ..utils.claude_client import claude_batch_results, claude_batch_submit

# 45 granular categories from COMMODITY_MAP + "other" fallback.
# COMMODITY_MAP already includes "other" as a key, so no need to append.
VALID_CATEGORIES = sorted(COMMODITY_MAP.keys())

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


async def enrich_material_cards(card_ids: list[int], db: Session, *, batch_size: int = 30) -> dict:
    """Enrich material cards with AI-generated descriptions and categories.

    Returns {"enriched": int, "skipped": int, "errors": int}.
    """

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


async def _enrich_batch(cards: list[MaterialCard], db: Session, stats: dict) -> None:
    """Enrich a single batch of cards via Claude Haiku."""
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
        logger.error("Material enrichment AI call failed: %s", e)
        stats["errors"] += len(cards)
        return

    if not result or "parts" not in result:
        logger.warning("Material enrichment: empty or invalid response")
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
            logger.warning("Failed to apply enrichment for card %d: %s", card.id, e)
            stats["errors"] += 1

    try:
        db.commit()
    except Exception as e:
        logger.error("Material enrichment commit failed: %s", e)
        db.rollback()
        stats["errors"] += len(cards)
        stats["enriched"] -= len(cards)


async def enrich_pending_cards(db: Session, *, limit: int = 300, batch_size: int = 30) -> dict:
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


# ── Batch API enrichment ──────────────────────────────────────────────

_REDIS_KEY = "batch:material_enrich:current"
_BATCH_LIMIT = 200


def _build_enrich_prompt(cards: list[MaterialCard]) -> str:
    """Build the enrichment prompt for a list of material cards."""
    parts_list = []
    for card in cards:
        entry = f"- MPN: {card.display_mpn}"
        if card.manufacturer:
            entry += f" | Manufacturer: {card.manufacturer}"
        parts_list.append(entry)

    parts_text = "\n".join(parts_list)
    cats_text = ", ".join(VALID_CATEGORIES)
    return (
        f"Classify and describe each electronic component:\n\n"
        f"{parts_text}\n\n"
        f"Valid categories: {cats_text}\n\n"
        f"Return a JSON object with a 'parts' array, one entry per MPN above, "
        f"in the same order."
    )


async def batch_enrich_materials(db: Session) -> str | None:
    """Submit unenriched materials to Claude Batch API.

    Queries for up to 200 materials with enriched_at IS NULL, builds a batch request
    using the same prompt/schema as real-time enrichment, and submits via
    claude_batch_submit(). Stores the batch_id in Redis for later polling.

    Returns the batch_id or None if no materials to enrich or submit failed.
    """
    r = _get_redis()
    if r and r.get(_REDIS_KEY):
        logger.info("batch_enrich_materials: batch already pending, skipping submit")
        return None

    cards = (
        db.query(MaterialCard)
        .filter(
            MaterialCard.enriched_at.is_(None),
            MaterialCard.deleted_at.is_(None),
        )
        .order_by(MaterialCard.id)
        .limit(_BATCH_LIMIT)
        .all()
    )

    if not cards:
        logger.debug("batch_enrich_materials: no unenriched cards found")
        return None

    # Build batch — one request per card so results map 1:1
    bq = BatchQueue(prefix="mat_enrich")
    for card in cards:
        prompt = _build_enrich_prompt([card])
        bq.enqueue(
            str(card.id),
            {
                "prompt": prompt,
                "schema": _PART_SCHEMA,
                "system": _SYSTEM_PROMPT,
                "model_tier": "fast",
                "max_tokens": 1024,
            },
        )

    requests = bq.build_batch()
    if not requests:
        return None

    batch_id = await claude_batch_submit(requests)
    if not batch_id:
        logger.warning("batch_enrich_materials: claude_batch_submit returned None")
        return None

    if r:
        r.set(_REDIS_KEY, batch_id)

    logger.info("batch_enrich_materials: submitted %d cards, batch_id=%s", len(cards), batch_id)
    return batch_id


async def process_material_batch_results(db: Session) -> dict | None:
    """Poll and apply batch enrichment results.

    Loads the batch_id from Redis, checks for results via claude_batch_results(). If
    results are available, applies description/category to each material card, sets
    enriched_at, and clears the Redis key.

    Returns {"applied": int, "errors": int} when batch is complete, or None if no batch
    pending / still processing / Redis unavailable.
    """
    r = _get_redis()
    if not r:
        return None

    raw = r.get(_REDIS_KEY)
    if not raw:
        return None

    batch_id = raw.decode() if isinstance(raw, bytes) else raw

    results = await claude_batch_results(batch_id)
    if results is None:
        logger.debug("process_material_batch_results: batch %s still processing", batch_id)
        return None

    now = datetime.now(timezone.utc)
    stats = {"applied": 0, "errors": 0}

    for custom_id, parsed in results.items():
        if parsed is None:
            logger.debug("Batch result error for %s — skipping", custom_id)
            stats["errors"] += 1
            continue

        # custom_id format: "mat_enrich-<card_id>"
        id_parts = custom_id.split("-", 1)
        if len(id_parts) < 2:
            stats["errors"] += 1
            continue
        try:
            card_id = int(id_parts[1])
        except (ValueError, IndexError):
            stats["errors"] += 1
            continue

        card = db.get(MaterialCard, card_id)
        if not card:
            stats["errors"] += 1
            continue

        ai_parts = parsed.get("parts", [])
        if not ai_parts:
            stats["errors"] += 1
            continue

        ai = ai_parts[0]
        try:
            desc = ai.get("description")
            cat = ai.get("category", "other")
            if cat not in VALID_CATEGORIES:
                cat = "other"
            if desc:
                card.description = desc
            card.category = cat
            card.enrichment_source = "batch_api"
            card.enriched_at = now
            stats["applied"] += 1
        except Exception as e:
            logger.warning("Failed to apply batch enrichment for card %d: %s", card_id, e)
            stats["errors"] += 1

    try:
        db.commit()
    except Exception as e:
        logger.error("process_material_batch_results commit failed: %s", e)
        db.rollback()
        return stats

    r.delete(_REDIS_KEY)
    logger.info(
        "process_material_batch_results: %d applied, %d errors from batch %s",
        stats["applied"],
        stats["errors"],
        batch_id,
    )
    return stats
