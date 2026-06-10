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
        logger.info("Re-enriching {} cards (limit={}, batch_size={})", len(card_ids), limit, batch_size)

        stats = await enrich_material_cards(card_ids, db, batch_size=batch_size)
        logger.info("Re-enrichment complete: {}", stats)

        # Backfill MaterialSpecFacet rows from updated specs_structured. Each entry is
        # re-recorded with ITS OWN provenance (source/confidence from the JSONB entry,
        # falling back to spec_extraction) — never an arbitrary tag like
        # card.enrichment_source, which is not a spec_tiers.SOURCE_TIER key and would
        # rank the re-record at tier 0 (losing to every ranked source). A same-source
        # re-record at equal (tier, confidence) wins on the newer timestamp, which is
        # exactly what refreshes the facet projection.
        from app.services.spec_write_service import record_spec

        enriched_cards = db.query(MaterialCard).filter(MaterialCard.id.in_(card_ids)).all()
        facet_count = 0
        for card in enriched_cards:
            if not card.specs_structured or not card.category:
                continue
            for spec_key, spec_data in card.specs_structured.items():
                if isinstance(spec_data, dict):
                    value = spec_data.get("value")
                    entry_source = str(spec_data.get("source") or "spec_extraction")
                    # Explicit-None check, NOT `or`: a stored confidence of 0.0 is a
                    # legitimate value — `or` would inflate it to 0.85, and the same-source
                    # equal-tier re-record would then PERSIST that manufactured confidence
                    # (0.85 > 0.0 wins the ladder), letting the entry beat other same-tier
                    # sources it never legitimately outranked. The 0.85 default applies
                    # only to entries with NO stored confidence.
                    conf = spec_data.get("confidence")
                    entry_confidence = float(conf) if conf is not None else 0.85
                else:
                    value = spec_data
                    entry_source = "spec_extraction"
                    entry_confidence = 0.85
                if value is not None:
                    record_spec(
                        db,
                        int(card.id),
                        spec_key,
                        value,
                        source=entry_source,
                        confidence=entry_confidence,
                    )
                    facet_count += 1
        db.commit()
        logger.info("Backfilled {} facet rows", facet_count)
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bulk re-enrich material cards")
    parser.add_argument("--limit", type=int, default=500, help="Max cards to re-enrich")
    parser.add_argument("--batch-size", type=int, default=30, help="Cards per AI batch call")
    args = parser.parse_args()

    asyncio.run(main(limit=args.limit, batch_size=args.batch_size))
