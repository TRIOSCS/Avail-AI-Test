"""Phase 3: Per-commodity structured spec extraction via Anthropic Batch API.

Groups MaterialCards by category, then uses commodity-specific prompts to extract
the exact specs needed for faceted search sub-filters (DDR type, capacitance,
voltage rating, form factor, etc.).

Writes to specs_summary (Text) in parseable "Key: Value | Key: Value" format
as an interim step until the specs_structured JSONB column exists.

Called by: manual script
Depends on: app.utils.claude_client, app.models.intelligence.MaterialCard
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
from app.services.commodity_registry import get_batch_spec_schema
from app.services.spec_write_service import record_spec

BATCH_SIZE = 50  # MPNs per request

# ── Commodity spec schemas — single source of truth from commodity_registry ──
COMMODITY_SPECS = get_batch_spec_schema()


def _build_spec_prompt(category: str, cards: list[dict]) -> str:
    """Build a commodity-specific spec extraction prompt."""
    schema = COMMODITY_SPECS[category]
    spec_instructions = []
    for spec in schema["specs"]:
        line = f"- {spec['key']}: {spec['label']}"
        if spec["type"] == "enum":
            line += f" (one of: {spec.get('values', 'see common values')})"
        elif spec["type"] == "numeric":
            unit = spec.get("unit", "")
            line += f" (number{', unit: ' + unit if unit else ''})"
        elif spec["type"] == "boolean":
            line += " (true/false)"
        spec_instructions.append(line)

    spec_text = "\n".join(spec_instructions)

    card_lines = []
    for c in cards:
        entry = f"- MPN: {c['display_mpn']}"
        if c.get("manufacturer"):
            entry += f" | Mfg: {c['manufacturer']}"
        if c.get("description"):
            entry += f" | Desc: {c['description'][:200]}"
        card_lines.append(entry)

    cards_text = "\n".join(card_lines)

    return (
        f"Extract technical specifications for these {category} components.\n\n"
        f"Specs to extract:\n{spec_text}\n\n"
        f"Components:\n{cards_text}\n\n"
        f"For each component, return its specs. Set null for specs you cannot determine. "
        f"Include a 'confidence' (0.0-1.0) for each spec value."
    )


def _build_spec_schema(category: str) -> dict:
    """Build JSON schema for spec extraction output."""
    schema = COMMODITY_SPECS[category]
    spec_props = {}
    for spec in schema["specs"]:
        spec_props[spec["key"]] = {"type": ["string", "number", "boolean", "null"]}
        spec_props[f"{spec['key']}_confidence"] = {"type": "number"}

    return {
        "type": "object",
        "properties": {
            "parts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "mpn": {"type": "string"},
                        **spec_props,
                    },
                    "required": ["mpn"],
                },
            }
        },
        "required": ["parts"],
    }


def _specs_to_summary(category: str, ai_part: dict) -> str | None:
    """Convert AI-extracted specs to parseable specs_summary string.

    Format: "Key: Value | Key: Value | ..."
    Only includes specs with confidence >= 0.85.
    """
    schema = COMMODITY_SPECS[category]
    parts = []
    for spec in schema["specs"]:
        value = ai_part.get(spec["key"])
        conf = ai_part.get(f"{spec['key']}_confidence", 0.0)
        if value is not None and conf >= 0.85:
            parts.append(f"{spec['label']}: {value}")

    return " | ".join(parts) if parts else None


async def submit_spec_extraction(db, category: str, limit: int = 0) -> dict:
    """Submit cards of a specific category for spec extraction."""
    from app.utils.claude_client import claude_batch_submit

    if category not in COMMODITY_SPECS:
        return {"error": f"No spec schema for category '{category}'"}

    query = (
        db.query(
            MaterialCard.id,
            MaterialCard.display_mpn,
            MaterialCard.manufacturer,
            MaterialCard.description,
        )
        .filter(
            MaterialCard.deleted_at.is_(None),
            MaterialCard.category == category,
            # Only cards with a description (needed for spec extraction)
            MaterialCard.description.isnot(None),
            MaterialCard.description != "",
        )
        .order_by(MaterialCard.search_count.desc().nullslast())
    )

    if limit:
        query = query.limit(limit)

    rows = query.yield_per(5000).all()
    logger.info(f"[{category}] Cards with description: {len(rows)}")

    if not rows:
        return {"error": f"No cards found for category '{category}'"}

    all_cards = [
        {"id": r.id, "display_mpn": r.display_mpn, "manufacturer": r.manufacturer, "description": r.description}
        for r in rows
    ]

    system = (
        "You are an expert electronic component engineer. Extract structured specifications "
        "from part numbers and descriptions. Only include specs you are confident about. "
        "Set null for anything uncertain."
    )

    schema = _build_spec_schema(category)
    requests = []
    meta_map = {}

    for i in range(0, len(all_cards), BATCH_SIZE):
        chunk = all_cards[i : i + BATCH_SIZE]
        custom_id = f"specs_{category}_{i}"
        prompt = _build_spec_prompt(category, chunk)

        requests.append(
            {
                "custom_id": custom_id,
                "prompt": prompt,
                "schema": schema,
                "system": system,
                "model_tier": "smart",
                "max_tokens": 8192,
            }
        )
        meta_map[custom_id] = [{"id": c["id"], "mpn": c["display_mpn"]} for c in chunk]

    logger.info(f"[{category}] Built {len(requests)} batch requests")

    batch_id = await claude_batch_submit(requests)
    if not batch_id:
        return {"error": "Batch submission failed"}

    meta = {
        "batch_id": batch_id,
        "category": category,
        "request_map": meta_map,
        "total_cards": len(all_cards),
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }
    meta_path = f"/tmp/specs_batch_{category}_{batch_id}.json"
    with open(meta_path, "w") as f:
        json.dump(meta, f)

    return {"batch_id": batch_id, "total_submitted": len(all_cards), "meta_path": meta_path}


async def apply_spec_results(meta_path: str, db, dry_run: bool = True) -> dict:
    """Apply completed spec extraction results."""
    from app.utils.claude_client import claude_batch_results

    with open(meta_path) as f:
        meta = json.load(f)

    category = meta["category"]
    batch_id = meta["batch_id"]
    stats = {"processed": 0, "updated": 0, "skipped": 0, "errors": 0}

    results = await claude_batch_results(batch_id)
    if results is None:
        logger.info(f"Batch {batch_id} not ready yet")
        return {"status": "processing"}

    for custom_id, result_data in results.items():
        if result_data is None:
            stats["errors"] += 1
            continue

        card_meta_list = meta["request_map"].get(custom_id, [])
        parts = result_data.get("parts", [])

        if len(parts) != len(card_meta_list):
            logger.warning(
                "AI returned %d parts but expected %d for %s",
                len(parts),
                len(card_meta_list),
                custom_id,
            )

        for card_info, ai_part in zip(card_meta_list, parts):
            card_id = card_info["id"]
            stats["processed"] += 1

            summary = _specs_to_summary(category, ai_part)
            if not summary:
                stats["skipped"] += 1
                continue

            if not dry_run:
                db.query(MaterialCard).filter(MaterialCard.id == card_id).update(
                    {"specs_summary": summary},
                    synchronize_session=False,
                )
                schema = COMMODITY_SPECS.get(category, {})
                for spec in schema.get("specs", []):
                    value = ai_part.get(spec["key"])
                    conf = ai_part.get(f"{spec['key']}_confidence", 0.0)
                    if value is not None and conf >= 0.70:
                        record_spec(
                            db,
                            card_id,
                            spec["key"],
                            value,
                            source="haiku_extraction",
                            confidence=conf,
                            # AI prompt already instructs extraction in canonical units,
                            # so no conversion is needed—pass unit directly from registry.
                            unit=spec.get("canonical_unit"),
                        )

            stats["updated"] += 1

        if not dry_run:
            db.commit()

    mode = "DRY RUN" if dry_run else "APPLIED"
    logger.info(f"[{mode}] [{category}] Spec results: {stats}")
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 3: Structured spec extraction")
    sub = parser.add_subparsers(dest="command", required=True)

    submit_p = sub.add_parser("submit", help="Submit cards for spec extraction")
    submit_p.add_argument("category", help="Commodity category (e.g., dram, capacitors)")
    submit_p.add_argument("--limit", type=int, default=0, help="Max cards (0 = all)")
    submit_p.add_argument("--all", action="store_true", help="Submit all 15 commodity categories")

    apply_p = sub.add_parser("apply", help="Apply spec results")
    apply_p.add_argument("meta_path", help="Path to metadata JSON")
    apply_p.add_argument("--apply", action="store_true", help="Actually write (default: dry run)")

    args = parser.parse_args()

    if args.command == "submit":
        if args.all:

            async def submit_all():
                db = SessionLocal()
                try:
                    for cat in COMMODITY_SPECS:
                        result = await submit_spec_extraction(db, cat, limit=args.limit)
                        logger.info(f"[{cat}] {result}")
                finally:
                    db.close()

            asyncio.run(submit_all())
        else:

            async def submit_one():
                db = SessionLocal()
                try:
                    result = await submit_spec_extraction(db, args.category, limit=args.limit)
                    logger.info(result)
                finally:
                    db.close()

            asyncio.run(submit_one())
    elif args.command == "apply":

        async def apply():
            db = SessionLocal()
            try:
                result = await apply_spec_results(args.meta_path, db, dry_run=not args.apply)
                logger.info(result)
            finally:
                db.close()

        asyncio.run(apply())
