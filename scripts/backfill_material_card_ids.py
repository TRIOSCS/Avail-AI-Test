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


def _backfill_table(db, card_cache: dict, *, label: str, model, mpn_of, norm_key_of, dry_run: bool) -> dict:
    """Link one table's rows to material cards via offset-paginated batches.

    ``mpn_of(row)`` returns the raw MPN string; ``norm_key_of(row, mpn)`` returns the
    normalization key (Requirements reuse their precomputed ``normalized_mpn`` column).
    """
    counts = {"linked": 0, "created": 0, "skipped": 0}
    logger.info(f"Backfilling {label}...")
    offset = 0
    while True:
        batch = db.query(model).filter(model.material_card_id.is_(None)).limit(BATCH_SIZE).offset(offset).all()
        if not batch:
            break
        for row in batch:
            mpn = mpn_of(row)
            norm_key = norm_key_of(row, mpn) if mpn else None
            if not norm_key:
                counts["skipped"] += 1
                continue
            display = normalize_mpn(mpn) or mpn.strip()
            was_new = norm_key not in card_cache
            card_id = _resolve_or_create(norm_key, display, db, card_cache)
            if card_id:
                if not dry_run:
                    row.material_card_id = card_id
                counts["linked"] += 1
                if was_new:
                    counts["created"] += 1
        if not dry_run:
            db.commit()
        offset += BATCH_SIZE
        logger.info(f"  {label.capitalize()}: {offset} processed...")
    return counts


def backfill(dry_run: bool = False) -> dict:
    db = SessionLocal()
    card_cache: dict[str, int] = {}

    try:
        # Pre-warm cache with all existing material cards (757K x 2 strings ~ 100MB, acceptable)
        logger.info("Loading material card lookup table...")
        for norm, card_id in db.query(MaterialCard.normalized_mpn, MaterialCard.id).all():
            card_cache[norm] = card_id
        logger.info(f"Loaded {len(card_cache)} material cards into cache")

        stats = {
            "requirements": _backfill_table(
                db,
                card_cache,
                label="requirements",
                model=Requirement,
                mpn_of=lambda r: r.primary_mpn,
                norm_key_of=lambda r, mpn: r.normalized_mpn or normalize_mpn_key(mpn),
                dry_run=dry_run,
            ),
            "sightings": _backfill_table(
                db,
                card_cache,
                label="sightings",
                model=Sighting,
                mpn_of=lambda s: s.mpn_matched,
                norm_key_of=lambda s, mpn: normalize_mpn_key(mpn),
                dry_run=dry_run,
            ),
            "offers": _backfill_table(
                db,
                card_cache,
                label="offers",
                model=Offer,
                mpn_of=lambda o: o.mpn,
                norm_key_of=lambda o, mpn: normalize_mpn_key(mpn),
                dry_run=dry_run,
            ),
        }

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
