"""Merge MaterialCard pairs where one card has a packaging suffix variant.

Finds cards whose normalized_mpn ends with PBF/NOPB/TR/CT suffixes and merges
them into the base card (without the suffix). All sightings, offers, and
requirements are re-pointed to the base card, and the suffix card is soft-deleted.

Usage:
    python scripts/merge_suffix_material_cards.py --dry-run   # Preview
    python scripts/merge_suffix_material_cards.py              # Execute

Called by: manual invocation
Depends on: app.database.SessionLocal, app.models (MaterialCard, Sighting, Offer, Requirement)
"""

import argparse
import sys
from datetime import datetime, timezone

from loguru import logger

sys.path.insert(0, "/root/availai")

from sqlalchemy import update

from app.database import SessionLocal
from app.models import MaterialCard, Offer, Requirement, Sighting

# Suffixes to strip, ordered longest-first so "trpbf" matches before "pbf"
SUFFIXES = ["ctpbf", "trpbf", "nopb", "pbf"]

BATCH_SIZE = 100


def find_merge_candidates(db) -> list[tuple[int, str, int, str]]:
    """Find (suffix_card_id, suffix_mpn, base_card_id, base_mpn) pairs.

    Loads all active MaterialCards, builds a lookup by normalized_mpn, then finds suffix
    variants whose base MPN also exists.
    """
    logger.info("Loading all active MaterialCards...")
    rows = db.query(MaterialCard.id, MaterialCard.normalized_mpn).filter(MaterialCard.deleted_at.is_(None)).all()
    if not rows:
        logger.info("No active MaterialCards found — nothing to do.")
        return []

    logger.info(f"Loaded {len(rows)} active MaterialCards")

    # Build lookup: normalized_mpn -> card id
    mpn_to_id: dict[str, int] = {}
    for card_id, norm_mpn in rows:
        if norm_mpn:
            mpn_to_id[norm_mpn] = card_id

    candidates: list[tuple[int, str, int, str]] = []
    for norm_mpn, suffix_card_id in mpn_to_id.items():
        for suffix in SUFFIXES:
            if norm_mpn.endswith(suffix) and len(norm_mpn) > len(suffix):
                base_mpn = norm_mpn[: -len(suffix)]
                if base_mpn in mpn_to_id:
                    base_card_id = mpn_to_id[base_mpn]
                    if base_card_id != suffix_card_id:
                        candidates.append((suffix_card_id, norm_mpn, base_card_id, base_mpn))
                break  # Only match the first (longest) suffix

    logger.info(f"Found {len(candidates)} merge candidates")
    return candidates


def merge(dry_run: bool = False) -> dict:
    """Execute the merge of suffix MaterialCards into their base cards."""
    db = SessionLocal()
    stats = {
        "merged": 0,
        "sightings_moved": 0,
        "offers_moved": 0,
        "requirements_moved": 0,
    }

    try:
        candidates = find_merge_candidates(db)
        if not candidates:
            logger.info("No merge candidates — done.")
            return stats

        for i, (suffix_id, suffix_mpn, base_id, base_mpn) in enumerate(candidates, 1):
            logger.info(
                f"[{i}/{len(candidates)}] Merging '{suffix_mpn}' (id={suffix_id}) → '{base_mpn}' (id={base_id})"
            )

            if not dry_run:
                # Move sightings
                result = db.execute(
                    update(Sighting).where(Sighting.material_card_id == suffix_id).values(material_card_id=base_id)
                )
                sightings_count = result.rowcount
            else:
                sightings_count = db.query(Sighting).filter(Sighting.material_card_id == suffix_id).count()

            if not dry_run:
                # Move offers
                result = db.execute(
                    update(Offer).where(Offer.material_card_id == suffix_id).values(material_card_id=base_id)
                )
                offers_count = result.rowcount
            else:
                offers_count = db.query(Offer).filter(Offer.material_card_id == suffix_id).count()

            if not dry_run:
                # Move requirements
                result = db.execute(
                    update(Requirement)
                    .where(Requirement.material_card_id == suffix_id)
                    .values(material_card_id=base_id)
                )
                requirements_count = result.rowcount
            else:
                requirements_count = db.query(Requirement).filter(Requirement.material_card_id == suffix_id).count()

            if not dry_run:
                # Soft-delete the suffix card
                db.execute(
                    update(MaterialCard)
                    .where(MaterialCard.id == suffix_id)
                    .values(deleted_at=datetime.now(timezone.utc))
                )

            stats["merged"] += 1
            stats["sightings_moved"] += sightings_count
            stats["offers_moved"] += offers_count
            stats["requirements_moved"] += requirements_count

            logger.info(f"  → sightings={sightings_count}, offers={offers_count}, requirements={requirements_count}")

            # Batch commit every BATCH_SIZE merges
            if not dry_run and i % BATCH_SIZE == 0:
                db.commit()
                logger.info(f"  Committed batch at {i} merges")

        # Final commit for remaining
        if not dry_run:
            db.commit()

        prefix = "DRY RUN — " if dry_run else ""
        logger.info(f"{prefix}Merge complete!")
        logger.info(f"  Cards merged: {stats['merged']}")
        logger.info(f"  Sightings moved: {stats['sightings_moved']}")
        logger.info(f"  Offers moved: {stats['offers_moved']}")
        logger.info(f"  Requirements moved: {stats['requirements_moved']}")
        return stats

    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Merge MaterialCard suffix variants (PBF/NOPB/TR/CT) into base cards")
    parser.add_argument("--dry-run", action="store_true", help="Preview without making changes")
    args = parser.parse_args()
    if args.dry_run:
        logger.info("DRY RUN — no changes will be written")
    merge(dry_run=args.dry_run)
