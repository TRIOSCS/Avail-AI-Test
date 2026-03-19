"""One-time migration: reclassify coarse legacy categories to granular 45-category taxonomy.

Maps old coarse categories (memory, processors, servers, storage) to their most common
granular equivalents. Cards that need AI-assisted reclassification (e.g., "storage" could
be ssd, hdd, or flash) are flagged for Phase 2 batch enrichment.

Called by: manual one-time script
Depends on: app.models.intelligence.MaterialCard, app.database.SessionLocal
"""

import os
import sys

from loguru import logger
from sqlalchemy import func

sys.path.insert(0, os.environ.get("APP_ROOT", "/app"))
from app.database import SessionLocal
from app.models.intelligence import MaterialCard

# ── Deterministic mappings (high confidence, no AI needed) ────────────
# These coarse categories map 1:1 to a granular equivalent.
DETERMINISTIC_MAP = {
    "processors": "cpu",
    "Microprocessors": "microprocessors",
}

# ── Ambiguous mappings (need AI or context to resolve) ────────────────
# These coarse categories could map to multiple granular ones.
# Set them to the most common granular category, and Phase 2 will
# re-evaluate with full AI context.
AMBIGUOUS_MAP = {
    "memory": "dram",       # Most "memory" cards are DRAM; flash gets its own
    "storage": "ssd",       # Default to SSD; Phase 2 will split HDD/Flash
    "servers": "server_chassis",  # Most "servers" are chassis/systems
}

# Combined mapping
COARSE_TO_GRANULAR = {**DETERMINISTIC_MAP, **AMBIGUOUS_MAP}


def main(dry_run: bool = True):
    db = SessionLocal()
    try:
        _run(db, dry_run=dry_run)
    finally:
        db.close()


def _run(db, dry_run: bool = True):
    total_updated = 0

    for old_cat, new_cat in COARSE_TO_GRANULAR.items():
        count = (
            db.query(func.count(MaterialCard.id))
            .filter(
                MaterialCard.deleted_at.is_(None),
                MaterialCard.category == old_cat,
            )
            .scalar()
        )

        if count == 0:
            continue

        logger.info(f"  {old_cat} → {new_cat}: {count} cards")

        if not dry_run:
            db.query(MaterialCard).filter(
                MaterialCard.deleted_at.is_(None),
                MaterialCard.category == old_cat,
            ).update({"category": new_cat}, synchronize_session=False)
            db.commit()

        total_updated += count

    # Also fix junk categories (very long strings used as category)
    junk_count = (
        db.query(func.count(MaterialCard.id))
        .filter(
            MaterialCard.deleted_at.is_(None),
            func.length(MaterialCard.category) > 25,
        )
        .scalar()
    )
    if junk_count > 0:
        logger.info(f"  junk (len>25) → other: {junk_count} cards")
        if not dry_run:
            db.query(MaterialCard).filter(
                MaterialCard.deleted_at.is_(None),
                func.length(MaterialCard.category) > 25,
            ).update({"category": "other"}, synchronize_session=False)
            db.commit()
        total_updated += junk_count

    mode = "DRY RUN" if dry_run else "APPLIED"
    logger.info(f"[{mode}] Total reclassified: {total_updated}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Reclassify coarse categories to granular taxonomy")
    parser.add_argument("--apply", action="store_true", help="Actually write changes (default: dry run)")
    args = parser.parse_args()
    main(dry_run=not args.apply)
