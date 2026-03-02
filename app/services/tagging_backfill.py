"""Tagging backfill — bulk-classify existing MaterialCards.

Two-step process:
1. seed_from_existing_manufacturers: Harvest cards with manufacturer already set
2. run_prefix_backfill: Classify remaining via prefix lookup table

Both are idempotent (skip already-tagged cards) and batched for memory safety.

Called by: app.scheduler, app.routers.tagging_admin
Depends on: app.models.tags, app.models.intelligence, app.services.tagging
"""

from collections import Counter

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.intelligence import MaterialCard
from app.models.sourcing import Sighting
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
            if brand_tag.id is None:  # pragma: no cover
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
                if brand_tag.id is None:  # pragma: no cover
                    db.flush()
                new_brands.add(brand_tag.name)
                tags_to_apply.append({
                    "tag_id": brand_tag.id,
                    "source": result["brand"]["source"],
                    "confidence": result["brand"]["confidence"],
                })

            if result.get("commodity"):  # pragma: no cover
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


_JUNK_MANUFACTURERS = {"unknown", "n/a", "various", "", "none", "other", "generic", "-", "na"}


def backfill_manufacturer_from_sightings(db: Session, batch_size: int = 500) -> dict:
    """Mine sighting manufacturer data for untagged material cards.

    For each untagged card, find the most common non-empty manufacturer
    across its sightings (majority vote). Apply as brand tag with
    confidence based on agreement level:
    - 3+ sources agree: 0.95 (source='sighting_consensus')
    - 2 sources agree: 0.90 (source='sighting_consensus')
    - 1 source only: 0.85 (source='sighting_single')

    Called by: app.routers.tagging_admin, app.scheduler
    """
    tagged_ids = db.query(MaterialTag.material_card_id).distinct().subquery()
    total_untagged = (
        db.query(func.count(MaterialCard.id))
        .filter(~MaterialCard.id.in_(db.query(tagged_ids.c.material_card_id)))
        .scalar()
    )

    if not total_untagged:
        logger.info("Sighting mining: no untagged cards")
        return {"total_processed": 0, "total_tagged": 0, "total_skipped": 0}

    logger.info(f"Sighting mining: {total_untagged} untagged cards to process")

    total_processed = 0
    total_tagged = 0
    total_skipped = 0
    last_id = 0

    while True:
        cards = (
            db.query(MaterialCard)
            .filter(
                ~MaterialCard.id.in_(db.query(tagged_ids.c.material_card_id)),
                MaterialCard.id > last_id,
            )
            .order_by(MaterialCard.id)
            .limit(batch_size)
            .all()
        )
        if not cards:
            break

        for card in cards:
            last_id = card.id
            total_processed += 1

            # Get distinct manufacturer values from sightings for this card
            sighting_mfrs = (
                db.query(Sighting.manufacturer)
                .filter(
                    Sighting.material_card_id == card.id,
                    Sighting.manufacturer.isnot(None),
                    Sighting.manufacturer != "",
                )
                .all()
            )

            # Filter junk and count occurrences
            mfr_counts: Counter = Counter()
            for (mfr,) in sighting_mfrs:
                cleaned = mfr.strip()
                if cleaned.lower() not in _JUNK_MANUFACTURERS:
                    mfr_counts[cleaned] += 1

            if not mfr_counts:
                total_skipped += 1
                continue

            # Majority vote: pick the most common manufacturer
            winner, count = mfr_counts.most_common(1)[0]
            distinct_sources = len(mfr_counts)

            # Confidence based on agreement level
            if count >= 3:
                confidence = 0.95
                source = "sighting_consensus"
            elif count >= 2 or distinct_sources >= 2:
                confidence = 0.90
                source = "sighting_consensus"
            else:
                confidence = 0.85
                source = "sighting_single"

            # Update card manufacturer if not set
            if not card.manufacturer:
                card.manufacturer = winner

            # Create brand tag
            brand_tag = get_or_create_brand_tag(winner, db)
            if brand_tag.id is None:
                db.flush()
            tag_material_card(
                card.id,
                [{"tag_id": brand_tag.id, "source": source, "confidence": confidence}],
                db,
            )
            total_tagged += 1

        db.commit()
        logger.info(f"Sighting mining: {total_processed}/{total_untagged} — {total_tagged} tagged")

    logger.info(
        f"Sighting mining complete: {total_processed} processed, "
        f"{total_tagged} tagged, {total_skipped} skipped"
    )
    return {"total_processed": total_processed, "total_tagged": total_tagged, "total_skipped": total_skipped}


def repair_entity_tag_visibility(db: Session) -> dict:
    """Recalculate is_visible for ALL entity tags using corrected thresholds.

    Needed after fixing the entity_type mismatch in TagThresholdConfig (migration 043).
    Processes all distinct (entity_type, entity_id) pairs and recalculates visibility.

    Called by: app.routers.tagging_admin
    Returns: {total_entities, total_tags_updated, now_visible, now_hidden}
    """
    from app.models.tags import EntityTag
    from app.services.tagging import recalculate_entity_tag_visibility

    # Get all distinct entity (type, id) pairs
    entities = (
        db.query(EntityTag.entity_type, EntityTag.entity_id)
        .distinct()
        .all()
    )

    if not entities:
        logger.info("Visibility repair: no entity tags found")
        return {"total_entities": 0, "total_tags_updated": 0, "now_visible": 0, "now_hidden": 0}

    logger.info(f"Visibility repair: recalculating {len(entities)} entities")

    total_entities = 0
    for entity_type, entity_id in entities:
        recalculate_entity_tag_visibility(entity_type, entity_id, db)
        total_entities += 1

        if total_entities % 100 == 0:
            db.commit()
            logger.info(f"Visibility repair: {total_entities}/{len(entities)} entities processed")

    db.commit()

    # Count results
    now_visible = db.query(func.count(EntityTag.id)).filter(EntityTag.is_visible.is_(True)).scalar() or 0
    now_hidden = db.query(func.count(EntityTag.id)).filter(EntityTag.is_visible.is_(False)).scalar() or 0

    logger.info(
        f"Visibility repair complete: {total_entities} entities, "
        f"{now_visible} visible, {now_hidden} hidden"
    )
    return {
        "total_entities": total_entities,
        "total_tags_updated": now_visible + now_hidden,
        "now_visible": now_visible,
        "now_hidden": now_hidden,
    }
