"""Phase 1: Extract descriptions, manufacturers, and datasheet URLs from existing sighting data.

Sighting raw_data contains the full connector response dict (DigiKey DetailedDescription,
Mouser Description, etc.) that was fetched during searches but never pulled back onto
MaterialCards. This script mines that data at zero API cost.

Called by: manual script (one-time or periodic re-run)
Depends on: app.models.intelligence.MaterialCard, app.models.sourcing.Sighting
"""

import argparse
import os
import sys

from loguru import logger
from sqlalchemy import func

sys.path.insert(0, os.environ.get("APP_ROOT", "/app"))
from app.database import SessionLocal
from app.models.intelligence import MaterialCard
from app.models.sourcing import Sighting

# ── Source priority for descriptions (highest = most authoritative) ────
# Authorized distributors provide manufacturer-vetted descriptions.
SOURCE_PRIORITY = {
    "digikey": 10,
    "mouser": 9,
    "element14": 8,
    "octopart": 7,
    "oemsecrets": 6,
    "sourcengine": 5,
    "netcomponents": 4,
    "icsource": 4,
    "brokerbin": 3,
    "ebay": 2,
    "ai_live_web": 1,
}


def _extract_description(raw_data: dict, source_type: str) -> str | None:
    """Pull the best description from a sighting's raw_data dict.

    Each connector stores data differently — this handles the variations.
    """
    if not raw_data or not isinstance(raw_data, dict):
        return None

    # Most connectors store description as a top-level key
    desc = raw_data.get("description")

    # eBay uses ebay_title instead
    if not desc and source_type == "ebay":
        desc = raw_data.get("ebay_title")

    if not desc or not isinstance(desc, str):
        return None

    desc = desc.strip()
    # Skip very short descriptions (noise)
    if len(desc) < 10:
        return None

    return desc[:1000]  # MaterialCard.description is String(1000)


def _extract_datasheet_url(raw_data: dict) -> str | None:
    """Pull datasheet URL from raw_data. OEMSecrets is the primary source."""
    if not raw_data or not isinstance(raw_data, dict):
        return None

    url = raw_data.get("datasheet_url")
    if url and isinstance(url, str) and url.startswith("http"):
        return url[:1000]  # MaterialCard.datasheet_url is String(1000)
    return None


def enrich_card_from_sightings(
    card: MaterialCard,
    sightings: list[tuple],
    dry_run: bool = True,
) -> dict:
    """Enrich a single MaterialCard from its sightings.

    sightings: list of (source_type, manufacturer, is_authorized, raw_data) tuples,
    pre-sorted by source priority.

    Returns dict of fields that were updated.
    """
    updates = {}

    # ── Description: pick best from highest-priority authorized source ──
    best_desc = None
    best_desc_priority = -1
    best_desc_authorized = False

    for source_type, mfg, is_auth, raw_data in sightings:
        desc = _extract_description(raw_data, source_type)
        if not desc:
            continue

        priority = SOURCE_PRIORITY.get(source_type, 0)
        # Prefer authorized sources, then highest priority, then longest
        is_better = (
            (is_auth and not best_desc_authorized)
            or (is_auth == best_desc_authorized and priority > best_desc_priority)
            or (is_auth == best_desc_authorized and priority == best_desc_priority and len(desc) > len(best_desc or ""))
        )
        if is_better:
            best_desc = desc
            best_desc_priority = priority
            best_desc_authorized = bool(is_auth)

    if best_desc:
        should_overwrite = (
            not card.description
            or card.description.strip() == ""
            # Overwrite old Gradient AI descriptions if we have authorized distributor data
            or (best_desc_authorized and card.enrichment_source == "gradient_ai")
        )
        if should_overwrite:
            updates["description"] = best_desc

    # ── Manufacturer: pick from highest-priority authorized source ──────
    if not card.manufacturer or card.manufacturer.strip() == "":
        for source_type, mfg, is_auth, raw_data in sightings:
            if mfg and isinstance(mfg, str) and mfg.strip() and is_auth:
                updates["manufacturer"] = mfg.strip()[:255]
                break
        # Fallback: any source with a manufacturer
        if "manufacturer" not in updates:
            for source_type, mfg, is_auth, raw_data in sightings:
                if mfg and isinstance(mfg, str) and mfg.strip():
                    updates["manufacturer"] = mfg.strip()[:255]
                    break

    # ── Datasheet URL: OEMSecrets is the primary source ─────────────────
    if not card.datasheet_url:
        for source_type, mfg, is_auth, raw_data in sightings:
            url = _extract_datasheet_url(raw_data)
            if url:
                updates["datasheet_url"] = url
                break

    if not dry_run and updates:
        for field, value in updates.items():
            setattr(card, field, value)
        card.enrichment_source = "sighting_extraction"

    return updates


def main(dry_run: bool = True, limit: int = 0):
    db = SessionLocal()
    try:
        _run(db, dry_run=dry_run, limit=limit)
    finally:
        db.close()


def _run(db, dry_run: bool = True, limit: int = 0):
    # Count cards with sightings
    total_cards_with_sightings = (
        db.query(func.count(func.distinct(Sighting.material_card_id)))
        .filter(Sighting.material_card_id.isnot(None))
        .scalar()
    )
    logger.info(f"MaterialCards with sightings: {total_cards_with_sightings}")

    # Process in chunks of 500 cards
    chunk_size = 500
    offset = 0
    stats = {"processed": 0, "updated": 0, "skipped": 0, "desc_updated": 0, "mfg_updated": 0, "ds_updated": 0}

    while True:
        # Get a chunk of material_card_ids that have sightings
        card_ids_q = (
            db.query(func.distinct(Sighting.material_card_id))
            .filter(Sighting.material_card_id.isnot(None))
            .order_by(Sighting.material_card_id)
            .offset(offset)
            .limit(chunk_size)
            .all()
        )
        card_ids = [r[0] for r in card_ids_q]
        if not card_ids:
            break

        # Load the cards
        cards = (
            db.query(MaterialCard)
            .filter(MaterialCard.id.in_(card_ids), MaterialCard.deleted_at.is_(None))
            .all()
        )
        card_map = {c.id: c for c in cards}

        # Load all sightings for these cards, ordered by source priority
        sightings = (
            db.query(
                Sighting.material_card_id,
                Sighting.source_type,
                Sighting.manufacturer,
                Sighting.is_authorized,
                Sighting.raw_data,
            )
            .filter(Sighting.material_card_id.in_(card_ids))
            .all()
        )

        # Group sightings by card_id, sorted by priority
        card_sightings: dict[int, list] = {}
        for s in sightings:
            cid = s.material_card_id
            if cid not in card_sightings:
                card_sightings[cid] = []
            card_sightings[cid].append((s.source_type, s.manufacturer, s.is_authorized, s.raw_data))

        # Sort each card's sightings by source priority (highest first)
        for cid in card_sightings:
            card_sightings[cid].sort(
                key=lambda x: SOURCE_PRIORITY.get(x[0] or "", 0), reverse=True
            )

        # Enrich each card
        for cid, sight_list in card_sightings.items():
            card = card_map.get(cid)
            if not card:
                continue

            updates = enrich_card_from_sightings(card, sight_list, dry_run=dry_run)
            stats["processed"] += 1

            if updates:
                stats["updated"] += 1
                if "description" in updates:
                    stats["desc_updated"] += 1
                if "manufacturer" in updates:
                    stats["mfg_updated"] += 1
                if "datasheet_url" in updates:
                    stats["ds_updated"] += 1
            else:
                stats["skipped"] += 1

        if not dry_run:
            db.commit()

        offset += chunk_size

        if stats["processed"] % 5000 == 0:
            logger.info(f"Progress: {stats}")

        if limit and stats["processed"] >= limit:
            break

    mode = "DRY RUN" if dry_run else "APPLIED"
    logger.info(f"[{mode}] Final stats: {stats}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mine sighting data for material card enrichment")
    parser.add_argument("--apply", action="store_true", help="Actually write changes (default: dry run)")
    parser.add_argument("--limit", type=int, default=0, help="Max cards to process (0 = all)")
    args = parser.parse_args()
    main(dry_run=not args.apply, limit=args.limit)
