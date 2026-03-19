"""Backfill structured specs from existing vendor API sightings.

What: Iterates material cards with vendor sightings, parses raw_data for specs.
Called by: manual one-time script
Depends on: vendor_spec_enrichment.enrich_card_from_sightings
"""

import argparse
import os
import sys

from loguru import logger

sys.path.insert(0, os.environ.get("APP_ROOT", "/app"))
from sqlalchemy import func

from app.database import SessionLocal
from app.models.intelligence import MaterialCard
from app.models.sourcing import Sighting


def backfill(category: str | None = None, limit: int = 0, dry_run: bool = True):
    """Backfill specs from vendor sightings."""
    from app.services.vendor_spec_enrichment import enrich_card_from_sightings

    db = SessionLocal()
    try:
        # Find cards that have vendor sightings with raw_data
        query = (
            db.query(MaterialCard.id)
            .join(Sighting, Sighting.material_card_id == MaterialCard.id)
            .filter(
                MaterialCard.deleted_at.is_(None),
                Sighting.raw_data.isnot(None),
                Sighting.source_type.in_(["digikey", "nexar", "mouser"]),
            )
            .group_by(MaterialCard.id)
        )

        if category:
            query = query.filter(func.lower(func.trim(MaterialCard.category)) == category.lower())

        if limit:
            query = query.limit(limit)

        card_ids = [r[0] for r in query.all()]
        logger.info(f"Found {len(card_ids)} cards with vendor sightings")

        stats = {"total": len(card_ids), "enriched": 0, "specs_added": 0, "skipped": 0}

        for i, card_id in enumerate(card_ids):
            if dry_run:
                stats["skipped"] += 1
                continue

            count = enrich_card_from_sightings(db, card_id)
            if count > 0:
                stats["enriched"] += 1
                stats["specs_added"] += count
            else:
                stats["skipped"] += 1

            if (i + 1) % 500 == 0:
                db.commit()
                logger.info(f"Progress: {i + 1}/{len(card_ids)} — {stats}")

        if not dry_run:
            db.commit()

        mode = "DRY RUN" if dry_run else "APPLIED"
        logger.info(f"[{mode}] Backfill complete: {stats}")

    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill specs from vendor sightings")
    parser.add_argument("--category", help="Specific category (e.g., capacitors)")
    parser.add_argument("--limit", type=int, default=0, help="Max cards (0 = all)")
    parser.add_argument("--apply", action="store_true", help="Actually write (default: dry run)")
    args = parser.parse_args()

    backfill(category=args.category, limit=args.limit, dry_run=not args.apply)
