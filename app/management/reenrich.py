"""Bulk re-enrichment command — re-enriches all material cards to populate new specs.

Usage: python -m app.management.reenrich [--limit N] [--batch-size N]

Called by: admin manually after deploying new commodity specs
Depends on: material_enrichment_service.enrich_material_cards, spec_write_service.record_spec
"""

import argparse
import asyncio

from loguru import logger


async def main(limit: int = 500, batch_size: int = 30):
    """Re-enrich material cards in batches."""
    from app.database import SessionLocal
    from app.models import MaterialCard
    from app.services.material_enrichment_service import enrich_material_cards

    db = SessionLocal()
    try:
        cards = (
            db.query(MaterialCard.id)
            .filter(MaterialCard.deleted_at.is_(None))
            .order_by(MaterialCard.enriched_at.asc().nullsfirst())
            .limit(limit)
            .all()
        )
        card_ids = [c[0] for c in cards]
        logger.info("Re-enriching %d cards (limit=%d, batch_size=%d)", len(card_ids), limit, batch_size)

        stats = await enrich_material_cards(card_ids, db, batch_size=batch_size)
        logger.info("Re-enrichment complete: %s", stats)

        # Backfill MaterialSpecFacet rows from updated specs_structured
        from app.services.spec_write_service import record_spec

        enriched_cards = db.query(MaterialCard).filter(MaterialCard.id.in_(card_ids)).all()
        facet_count = 0
        for card in enriched_cards:
            if not card.specs_structured or not card.category:
                continue
            for spec_key, spec_data in card.specs_structured.items():
                value = spec_data.get("value") if isinstance(spec_data, dict) else spec_data
                if value is not None:
                    record_spec(
                        db,
                        card.id,
                        spec_key,
                        value,
                        source=card.enrichment_source or "reenrich",
                        confidence=0.85,
                    )
                    facet_count += 1
        db.commit()
        logger.info("Backfilled %d facet rows", facet_count)
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bulk re-enrich material cards")
    parser.add_argument("--limit", type=int, default=500, help="Max cards to re-enrich")
    parser.add_argument("--batch-size", type=int, default=30, help="Cards per AI batch call")
    args = parser.parse_args()

    asyncio.run(main(limit=args.limit, batch_size=args.batch_size))
