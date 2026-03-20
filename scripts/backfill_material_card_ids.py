"""Backfill material_card_id on requirements, sightings, and offers.

Matches existing MPN strings to MaterialCard.normalized_mpn.
Creates new material cards for MPNs that don't have one yet.

Usage:
    python scripts/backfill_material_card_ids.py --dry-run   # Preview
    python scripts/backfill_material_card_ids.py              # Execute
"""

import argparse
import sys

from loguru import logger

sys.path.insert(0, "/root/availai")

from app.database import SessionLocal
from app.models import MaterialCard, Offer, Requirement, Sighting
from app.search_service import resolve_material_card
from app.utils.normalization import normalize_mpn, normalize_mpn_key

BATCH_SIZE = 1000


def _resolve_or_create(norm_key: str, display_mpn: str, db, card_cache: dict) -> int | None:
    """Find or create a material card, using a local cache to avoid repeated queries.

    On cache miss, delegates to resolve_material_card for atomic find-or-create.
    """
    if not norm_key:
        return None
    if norm_key in card_cache:
        return card_cache[norm_key]
    card = resolve_material_card(display_mpn, db)
    if card:
        card_cache[norm_key] = card.id
        return card.id
    return None


def backfill(dry_run: bool = False) -> dict:
    db = SessionLocal()
    stats = {
        "requirements": {"linked": 0, "created": 0, "skipped": 0},
        "sightings": {"linked": 0, "created": 0, "skipped": 0},
        "offers": {"linked": 0, "created": 0, "skipped": 0},
    }
    card_cache: dict[str, int] = {}

    try:
        # Pre-warm cache with all existing material cards (757K x 2 strings ~ 100MB, acceptable)
        logger.info("Loading material card lookup table...")
        for norm, card_id in db.query(MaterialCard.normalized_mpn, MaterialCard.id).all():
            card_cache[norm] = card_id
        logger.info(f"Loaded {len(card_cache)} material cards into cache")

        # --- Requirements ---
        logger.info("Backfilling requirements...")
        offset = 0
        while True:
            batch = (
                db.query(Requirement)
                .filter(Requirement.material_card_id.is_(None))
                .limit(BATCH_SIZE)
                .offset(offset)
                .all()
            )
            if not batch:
                break
            for r in batch:
                mpn = r.primary_mpn
                if not mpn:
                    stats["requirements"]["skipped"] += 1
                    continue
                norm_key = r.normalized_mpn or normalize_mpn_key(mpn)
                if not norm_key:
                    stats["requirements"]["skipped"] += 1
                    continue
                display = normalize_mpn(mpn) or mpn.strip()
                was_new = norm_key not in card_cache
                card_id = _resolve_or_create(norm_key, display, db, card_cache)
                if card_id:
                    if not dry_run:
                        r.material_card_id = card_id
                    stats["requirements"]["linked"] += 1
                    if was_new:
                        stats["requirements"]["created"] += 1
            if not dry_run:
                db.commit()
            offset += BATCH_SIZE
            logger.info(f"  Requirements: {offset} processed...")

        # --- Sightings ---
        logger.info("Backfilling sightings...")
        offset = 0
        while True:
            batch = (
                db.query(Sighting).filter(Sighting.material_card_id.is_(None)).limit(BATCH_SIZE).offset(offset).all()
            )
            if not batch:
                break
            for s in batch:
                mpn = s.mpn_matched
                if not mpn:
                    stats["sightings"]["skipped"] += 1
                    continue
                norm_key = normalize_mpn_key(mpn)
                if not norm_key:
                    stats["sightings"]["skipped"] += 1
                    continue
                display = normalize_mpn(mpn) or mpn.strip()
                was_new = norm_key not in card_cache
                card_id = _resolve_or_create(norm_key, display, db, card_cache)
                if card_id:
                    if not dry_run:
                        s.material_card_id = card_id
                    stats["sightings"]["linked"] += 1
                    if was_new:
                        stats["sightings"]["created"] += 1
            if not dry_run:
                db.commit()
            offset += BATCH_SIZE
            logger.info(f"  Sightings: {offset} processed...")

        # --- Offers (batched) ---
        logger.info("Backfilling offers...")
        offset = 0
        while True:
            batch = db.query(Offer).filter(Offer.material_card_id.is_(None)).limit(BATCH_SIZE).offset(offset).all()
            if not batch:
                break
            for o in batch:
                mpn = o.mpn
                if not mpn:
                    stats["offers"]["skipped"] += 1
                    continue
                norm_key = normalize_mpn_key(mpn)
                if not norm_key:
                    stats["offers"]["skipped"] += 1
                    continue
                display = normalize_mpn(mpn) or mpn.strip()
                was_new = norm_key not in card_cache
                card_id = _resolve_or_create(norm_key, display, db, card_cache)
                if card_id:
                    if not dry_run:
                        o.material_card_id = card_id
                    stats["offers"]["linked"] += 1
                    if was_new:
                        stats["offers"]["created"] += 1
            if not dry_run:
                db.commit()
            offset += BATCH_SIZE
            logger.info(f"  Offers: {offset} processed...")

        logger.info("Backfill complete!")
        for table, s in stats.items():
            logger.info(f"  {table}: linked={s['linked']}, new_cards={s['created']}, skipped={s['skipped']}")
        return stats

    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        logger.info("DRY RUN — no changes will be written")
    backfill(dry_run=args.dry_run)
