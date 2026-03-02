"""Tagging backfill — bulk-classify existing MaterialCards.

Two-step process:
1. seed_from_existing_manufacturers: Harvest cards with manufacturer already set
2. run_prefix_backfill: Classify remaining via prefix lookup table

Both are idempotent (skip already-tagged cards) and batched for memory safety.

Called by: app.scheduler, app.routers.tagging_admin
Depends on: app.models.tags, app.models.intelligence, app.services.tagging
"""

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.intelligence import MaterialCard
from app.models.tags import MaterialTag
from app.services.tagging import (
    classify_material_card,
    get_or_create_brand_tag,
    get_or_create_commodity_tag,
    tag_material_card,
)


def seed_from_existing_manufacturers(db: Session) -> dict:
    """Harvest MaterialCards with manufacturer already populated.

    Creates brand tags + MaterialTags from existing data.
    Returns: {total_seeded, unique_brands_created}
    """
    # Cards with manufacturer set but no brand MaterialTag yet
    already_tagged = (
        db.query(MaterialTag.material_card_id)
        .join(MaterialTag.tag)
        .filter(MaterialTag.tag.has(tag_type="brand"))
        .subquery()
    )
    cards = (
        db.query(MaterialCard)
        .filter(
            MaterialCard.manufacturer.isnot(None),
            MaterialCard.manufacturer != "",
            ~MaterialCard.id.in_(db.query(already_tagged.c.material_card_id)),
        )
        .all()
    )

    total_seeded = 0
    brands_created = set()

    for card in cards:
        result = classify_material_card(card.normalized_mpn, card.manufacturer, card.category)

        tags_to_apply = []
        if result.get("brand"):
            brand_tag = get_or_create_brand_tag(result["brand"]["name"], db)
            if brand_tag.id is None:
                db.flush()
            brands_created.add(brand_tag.name)
            tags_to_apply.append({
                "tag_id": brand_tag.id,
                "source": result["brand"]["source"],
                "confidence": result["brand"]["confidence"],
            })

        if result.get("commodity"):
            commodity_tag = get_or_create_commodity_tag(result["commodity"]["name"], db)
            if commodity_tag:
                tags_to_apply.append({
                    "tag_id": commodity_tag.id,
                    "source": result["commodity"]["source"],
                    "confidence": result["commodity"]["confidence"],
                })

        if tags_to_apply:
            tag_material_card(card.id, tags_to_apply, db)
            total_seeded += 1

    db.commit()
    logger.info(f"Seeded {total_seeded} cards from existing manufacturers, {len(brands_created)} unique brands")

    return {"total_seeded": total_seeded, "unique_brands_created": len(brands_created)}


def run_prefix_backfill(db: Session, batch_size: int = 1000) -> dict:
    """Classify remaining untagged MaterialCards via prefix lookup table.

    Batched and idempotent — skips already-tagged cards.
    Returns: {total_processed, total_matched, total_unmatched, new_brands_discovered}
    """
    # Cards with NO MaterialTag records at all
    tagged_ids = db.query(MaterialTag.material_card_id).distinct().subquery()
    total_untagged = (
        db.query(func.count(MaterialCard.id))
        .filter(~MaterialCard.id.in_(db.query(tagged_ids.c.material_card_id)))
        .scalar()
    )

    if not total_untagged:
        logger.info("No untagged cards — prefix backfill skipped")
        return {"total_processed": 0, "total_matched": 0, "total_unmatched": 0, "new_brands_discovered": 0}

    logger.info(f"Prefix backfill starting: {total_untagged} untagged cards")

    total_processed = 0
    total_matched = 0
    total_unmatched = 0
    new_brands = set()
    last_id = 0

    while True:
        # Use ID-based pagination (not offset) since tagging changes the result set
        batch = (
            db.query(MaterialCard)
            .filter(
                ~MaterialCard.id.in_(db.query(tagged_ids.c.material_card_id)),
                MaterialCard.id > last_id,
            )
            .order_by(MaterialCard.id)
            .limit(batch_size)
            .all()
        )
        if not batch:
            break

        for card in batch:
            last_id = card.id
            result = classify_material_card(card.normalized_mpn, None, card.category)
            tags_to_apply = []

            if result.get("brand"):
                brand_tag = get_or_create_brand_tag(result["brand"]["name"], db)
                if brand_tag.id is None:
                    db.flush()
                new_brands.add(brand_tag.name)
                tags_to_apply.append({
                    "tag_id": brand_tag.id,
                    "source": result["brand"]["source"],
                    "confidence": result["brand"]["confidence"],
                })

            if result.get("commodity"):
                commodity_tag = get_or_create_commodity_tag(result["commodity"]["name"], db)
                if commodity_tag:
                    tags_to_apply.append({
                        "tag_id": commodity_tag.id,
                        "source": result["commodity"]["source"],
                        "confidence": result["commodity"]["confidence"],
                    })

            if tags_to_apply:
                tag_material_card(card.id, tags_to_apply, db)
                total_matched += 1
            else:
                total_unmatched += 1

            total_processed += 1

        db.commit()
        logger.info(f"Prefix backfill: {total_processed}/{total_untagged} — {total_matched} matched")

    logger.info(
        f"Prefix backfill complete: {total_processed} processed, "
        f"{total_matched} matched, {total_unmatched} unmatched, "
        f"{len(new_brands)} brands discovered"
    )

    return {
        "total_processed": total_processed,
        "total_matched": total_matched,
        "total_unmatched": total_unmatched,
        "new_brands_discovered": len(new_brands),
    }
