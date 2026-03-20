"""Phase 2: AI category + description + package enrichment via Anthropic Batch API.

Uses Sonnet via Batch API (50% cheaper, no rate limits) for maximum quality.
Combines category classification, description generation, and package extraction
in a single pass per batch of 50 MPNs.

Called by: manual script
Depends on: app.utils.claude_client (claude_batch_submit, claude_batch_results),
            app.models.intelligence.MaterialCard, app.services.specialty_detector
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

from loguru import logger

sys.path.insert(0, os.environ.get("APP_ROOT", "/app"))
from app.database import SessionLocal
from app.models.intelligence import MaterialCard
from app.services.specialty_detector import COMMODITY_MAP

VALID_CATEGORIES = sorted(COMMODITY_MAP.keys())

BATCH_SIZE = 50  # MPNs per batch request
MAX_REQUESTS_PER_BATCH = 50000  # Anthropic limit is 100K, stay under

_SYSTEM = (
    "You are an expert electronic component engineer with encyclopedic knowledge of "
    "manufacturer part numbering schemes. Given a list of manufacturer part numbers (MPNs) "
    "with optional context (manufacturer, existing description), classify each part and "
    "generate accurate technical descriptions.\n\n"
    "Rules:\n"
    "- category: choose exactly one from the provided list. Use 'other' only if nothing fits.\n"
    "- description: 2-3 sentences. Include key specs inferable from the MPN. "
    "Do NOT hallucinate specs you aren't confident about.\n"
    "- manufacturer: the component manufacturer (not the distributor).\n"
    '- package_type: physical package if inferable (e.g., LQFP-100, BGA-256, 0603, DIMM, 2.5").\n'
    "- lifecycle_status: one of 'active', 'nrfnd', 'eol', 'obsolete', 'ltb'. "
    "Infer from MPN suffixes (-ND, -NRFND, -OBSOLETE) or known manufacturer EOL patterns. "
    "Set null if unknown.\n"
    "- rohs_status: one of 'compliant', 'non-compliant', 'exempt'. "
    "Infer from suffixes like -PBF (lead-free), /NOPB, -E3 (RoHS). Set null if unknown.\n"
    "- pin_count: integer number of pins/terminals if inferable from package or MPN "
    "(e.g., LQFP-100 = 100, SO-8 = 8). Set null if unknown.\n"
    "- confidence: your confidence (0.0-1.0) that the classification and description are correct.\n"
    "- If you cannot identify the part at all, set category to 'other' and confidence to 0.0."
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "parts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "mpn": {"type": "string"},
                    "category": {"type": "string", "enum": VALID_CATEGORIES},
                    "category_confidence": {"type": "number"},
                    "description": {"type": ["string", "null"]},
                    "description_confidence": {"type": "number"},
                    "manufacturer": {"type": ["string", "null"]},
                    "package_type": {"type": ["string", "null"]},
                    "lifecycle_status": {
                        "type": ["string", "null"],
                        "enum": ["active", "nrfnd", "eol", "obsolete", "ltb", None],
                    },
                    "rohs_status": {"type": ["string", "null"], "enum": ["compliant", "non-compliant", "exempt", None]},
                    "pin_count": {"type": ["integer", "null"]},
                },
                "required": ["mpn", "category", "category_confidence", "description", "description_confidence"],
            },
        }
    },
    "required": ["parts"],
}


def _build_prompt(cards: list[dict]) -> str:
    """Build the user prompt for a batch of cards."""
    lines = []
    for c in cards:
        entry = f"- MPN: {c['display_mpn']}"
        if c.get("manufacturer"):
            entry += f" | Manufacturer: {c['manufacturer']}"
        if c.get("description"):
            entry += f" | Context: {c['description'][:150]}"
        lines.append(entry)

    parts_text = "\n".join(lines)
    cats_text = ", ".join(VALID_CATEGORIES)

    return (
        f"Classify and describe each electronic component.\n\n"
        f"Valid categories: {cats_text}\n\n"
        f"Components:\n{parts_text}\n\n"
        f"Return a JSON object with a 'parts' array, one entry per MPN above, in the same order."
    )


def _build_batch_requests(all_cards: list[dict]) -> list[dict]:
    """Build Batch API request entries from card dicts."""
    requests = []
    for i in range(0, len(all_cards), BATCH_SIZE):
        chunk = all_cards[i : i + BATCH_SIZE]
        custom_id = f"enrich_{i}_{i + len(chunk)}"
        prompt = _build_prompt(chunk)

        requests.append(
            {
                "custom_id": custom_id,
                "prompt": prompt,
                "schema": _SCHEMA,
                "system": _SYSTEM,
                "model_tier": "smart",  # Sonnet for quality
                "max_tokens": 8192,
            }
        )

    return requests


async def submit_enrichment_batch(db, limit: int = 0) -> dict:
    """Submit material cards for batch enrichment.

    Returns {batch_id, total_submitted, meta_path}.
    """
    from app.utils.claude_client import claude_batch_submit

    # Query cards that need enrichment — prioritize by search_count
    query = (
        db.query(
            MaterialCard.id,
            MaterialCard.display_mpn,
            MaterialCard.manufacturer,
            MaterialCard.description,
            MaterialCard.category,
            MaterialCard.enrichment_source,
        )
        .filter(MaterialCard.deleted_at.is_(None))
        .order_by(MaterialCard.search_count.desc().nullslast())
    )

    if limit:
        query = query.limit(limit)

    rows = query.all()
    logger.info(f"Cards to enrich: {len(rows)}")

    if not rows:
        return {"error": "No cards to enrich"}

    # Build card dicts with metadata for the batch
    all_cards = []
    card_meta = {}  # custom_id → [(card_id, mpn), ...]

    for r in rows:
        all_cards.append(
            {
                "id": r.id,
                "display_mpn": r.display_mpn,
                "manufacturer": r.manufacturer,
                "description": r.description,
            }
        )

    # Build batch requests (50 MPNs per request)
    requests = _build_batch_requests(all_cards)
    logger.info(f"Built {len(requests)} batch requests ({len(all_cards)} cards)")

    # Store metadata for applying results later
    meta = {"cards": [], "request_map": {}}
    idx = 0
    for req in requests:
        chunk = all_cards[idx : idx + BATCH_SIZE]
        meta["request_map"][req["custom_id"]] = [{"id": c["id"], "mpn": c["display_mpn"]} for c in chunk]
        idx += BATCH_SIZE
    meta["cards"] = [{"id": c["id"], "mpn": c["display_mpn"]} for c in all_cards]

    # Submit in chunks of MAX_REQUESTS_PER_BATCH
    batch_ids = []
    for chunk_start in range(0, len(requests), MAX_REQUESTS_PER_BATCH):
        chunk = requests[chunk_start : chunk_start + MAX_REQUESTS_PER_BATCH]
        batch_id = await claude_batch_submit(chunk)
        if batch_id:
            batch_ids.append(batch_id)
            logger.info(f"Submitted batch chunk: {batch_id} ({len(chunk)} requests)")
        else:
            logger.error(f"Failed to submit batch chunk starting at {chunk_start}")

    if not batch_ids:
        return {"error": "All batch submissions failed"}

    # Persist metadata
    meta["batch_ids"] = batch_ids
    meta["submitted_at"] = datetime.now(timezone.utc).isoformat()
    meta_path = f"/tmp/enrich_batch_meta_{batch_ids[0]}.json"
    with open(meta_path, "w") as f:
        json.dump(meta, f)

    return {
        "batch_ids": batch_ids,
        "total_submitted": len(all_cards),
        "total_requests": len(requests),
        "meta_path": meta_path,
    }


async def apply_batch_results(meta_path: str, db, dry_run: bool = True) -> dict:
    """Apply completed batch results to MaterialCards.

    Returns stats dict.
    """
    from app.utils.claude_client import claude_batch_results

    with open(meta_path) as f:
        meta = json.load(f)

    stats = {"processed": 0, "updated": 0, "skipped": 0, "errors": 0}

    for batch_id in meta["batch_ids"]:
        results = await claude_batch_results(batch_id)
        if results is None:
            logger.info(f"Batch {batch_id} not ready yet")
            stats["errors"] += 1
            continue

        for custom_id, result_data in results.items():
            if result_data is None:
                stats["errors"] += 1
                continue

            card_meta_list = meta["request_map"].get(custom_id, [])
            parts = result_data.get("parts", [])

            for card_info, ai_part in zip(card_meta_list, parts):
                card_id = card_info["id"]
                stats["processed"] += 1

                cat = ai_part.get("category", "other")
                cat_conf = ai_part.get("category_confidence", 0.0)
                desc = ai_part.get("description")
                desc_conf = ai_part.get("description_confidence", 0.0)
                mfg = ai_part.get("manufacturer")
                pkg = ai_part.get("package_type")

                # Validate category
                if cat not in VALID_CATEGORIES:
                    cat = "other"
                    cat_conf = 0.0

                # Apply confidence gates
                updates = {}
                if cat_conf >= 0.90 and cat != "other":
                    updates["category"] = cat
                if desc and desc_conf >= 0.90:
                    updates["description"] = desc[:1000]
                if mfg and isinstance(mfg, str) and mfg.strip():
                    updates["manufacturer"] = mfg.strip()[:255]
                if pkg and isinstance(pkg, str) and pkg.strip():
                    updates["package_type"] = pkg.strip()[:100]

                if not updates:
                    stats["skipped"] += 1
                    continue

                if not dry_run:
                    db.query(MaterialCard).filter(MaterialCard.id == card_id).update(
                        {
                            **updates,
                            "enrichment_source": "sonnet_batch_v2",
                            "enriched_at": datetime.now(timezone.utc),
                        },
                        synchronize_session=False,
                    )

                stats["updated"] += 1

            if not dry_run:
                db.commit()

    mode = "DRY RUN" if dry_run else "APPLIED"
    logger.info(f"[{mode}] Batch results: {stats}")
    return stats


async def main_submit(limit: int = 0):
    db = SessionLocal()
    result = await submit_enrichment_batch(db, limit=limit)
    logger.info(f"Submit result: {result}")
    db.close()
    return result


async def main_apply(meta_path: str, dry_run: bool = True):
    db = SessionLocal()
    result = await apply_batch_results(meta_path, db, dry_run=dry_run)
    logger.info(f"Apply result: {result}")
    db.close()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 2: AI batch enrichment for material cards")
    sub = parser.add_subparsers(dest="command", required=True)

    submit_parser = sub.add_parser("submit", help="Submit cards to Batch API")
    submit_parser.add_argument("--limit", type=int, default=0, help="Max cards to submit (0 = all)")

    apply_parser = sub.add_parser("apply", help="Apply batch results to DB")
    apply_parser.add_argument("meta_path", help="Path to metadata JSON from submit step")
    apply_parser.add_argument("--apply", action="store_true", help="Actually write changes (default: dry run)")

    check_parser = sub.add_parser("check", help="Check batch status")
    check_parser.add_argument("meta_path", help="Path to metadata JSON")

    args = parser.parse_args()

    if args.command == "submit":
        asyncio.run(main_submit(limit=args.limit))
    elif args.command == "apply":
        asyncio.run(main_apply(args.meta_path, dry_run=not args.apply))
    elif args.command == "check":
        # Quick status check
        with open(args.meta_path) as f:
            meta = json.load(f)
        logger.info(f"Batch IDs: {meta.get('batch_ids', [])}")
        logger.info(f"Total cards: {len(meta.get('cards', []))}")
        logger.info(f"Submitted at: {meta.get('submitted_at', 'unknown')}")
