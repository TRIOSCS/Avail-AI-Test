"""Part discovery service — grows the material card library continuously.

Four strategies for discovering new parts:
A) Cross-reference expansion (from enriched cross_references JSONB)
B) Family/series expansion (AI identifies same-family parts)
C) Commodity gap fill (AI lists top MPNs per category)
D) Manufacturer catalog crawl (web search for product catalogs)

New cards go through the standard enrichment pipeline automatically.

Called by: app.jobs.part_discovery_jobs (scheduler)
Depends on: app.models.intelligence.MaterialCard, app.utils.claude_client,
            app.search_service.resolve_material_card
"""

import asyncio
import json
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.intelligence import MaterialCard
from app.services.specialty_detector import COMMODITY_MAP


async def expand_cross_references(db: Session, limit: int = 500) -> dict:
    """Strategy A: Create MaterialCards for cross-referenced MPNs that don't exist.

    For every card with populated cross_references, create new cards for
    the referenced MPNs. Each new card enters the standard enrichment pipeline.
    """
    from app.search_service import resolve_material_card

    stats = {"checked": 0, "created": 0, "already_exists": 0, "errors": 0}

    cards = (
        db.query(MaterialCard.id, MaterialCard.cross_references, MaterialCard.manufacturer)
        .filter(
            MaterialCard.deleted_at.is_(None),
            MaterialCard.cross_references.isnot(None),
        )
        .limit(limit)
        .all()
    )

    for card in cards:
        refs = card.cross_references
        if not isinstance(refs, list):
            continue

        for ref in refs:
            if not isinstance(ref, dict) or not ref.get("mpn"):
                continue

            stats["checked"] += 1
            mpn = ref["mpn"].strip().upper()

            # Check if already exists
            existing = (
                db.query(MaterialCard.id)
                .filter(MaterialCard.normalized_mpn == mpn, MaterialCard.deleted_at.is_(None))
                .first()
            )

            if existing:
                stats["already_exists"] += 1
                continue

            try:
                new_card = resolve_material_card(mpn, db)
                if new_card:
                    # Set manufacturer from cross-ref if available
                    mfg = ref.get("manufacturer") or card.manufacturer
                    if mfg and not new_card.manufacturer:
                        new_card.manufacturer = mfg
                    new_card.enrichment_source = "discovery_crossref"
                    db.commit()
                    stats["created"] += 1
            except Exception as e:
                logger.warning(f"Failed to create card for cross-ref {mpn}: {e}")
                db.rollback()
                stats["errors"] += 1

    logger.info(f"Cross-ref expansion: {stats}")
    return stats


async def expand_families(db: Session, batch_size: int = 100) -> dict:
    """Strategy B: Ask AI to list other parts in the same family/series.

    For popular MPNs, discover all siblings in the product family.
    E.g., STM32F103C8T6 → STM32F103CBT6, STM32F103RBT6, etc.
    """
    from app.search_service import resolve_material_card
    from app.utils.claude_client import claude_json

    stats = {"seed_cards": 0, "discovered": 0, "created": 0, "already_exists": 0, "errors": 0}

    # Get popular MPNs as seeds
    seeds = (
        db.query(MaterialCard.id, MaterialCard.display_mpn, MaterialCard.manufacturer, MaterialCard.category)
        .filter(
            MaterialCard.deleted_at.is_(None),
            MaterialCard.search_count >= 3,
            MaterialCard.category.isnot(None),
            MaterialCard.category != "other",
        )
        .order_by(MaterialCard.search_count.desc())
        .limit(batch_size)
        .all()
    )

    stats["seed_cards"] = len(seeds)
    logger.info(f"Family expansion: {len(seeds)} seed MPNs")

    # Batch seeds into groups of 20 for efficiency
    for i in range(0, len(seeds), 20):
        chunk = seeds[i: i + 20]
        mpn_list = "\n".join(
            f"- {s.display_mpn} ({s.manufacturer or 'unknown'}, {s.category})" for s in chunk
        )

        try:
            result = await claude_json(
                f"For each part below, list up to 5 other parts in the same product family/series "
                f"by the same manufacturer. Only include REAL part numbers you are confident exist.\n\n"
                f"{mpn_list}",
                schema={
                    "type": "object",
                    "properties": {
                        "families": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "seed_mpn": {"type": "string"},
                                    "family_members": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                },
                                "required": ["seed_mpn", "family_members"],
                            },
                        }
                    },
                    "required": ["families"],
                },
                system="You are an expert electronic component engineer. List real part numbers only.",
                model_tier="smart",
                max_tokens=4096,
            )

            if not result or "families" not in result:
                continue

            for family in result["families"]:
                for member_mpn in family.get("family_members", []):
                    if not member_mpn or not isinstance(member_mpn, str):
                        continue

                    stats["discovered"] += 1
                    mpn_norm = member_mpn.strip().upper()

                    existing = (
                        db.query(MaterialCard.id)
                        .filter(MaterialCard.normalized_mpn == mpn_norm, MaterialCard.deleted_at.is_(None))
                        .first()
                    )

                    if existing:
                        stats["already_exists"] += 1
                        continue

                    try:
                        new_card = resolve_material_card(member_mpn.strip(), db)
                        if new_card:
                            new_card.enrichment_source = "discovery_family"
                            db.commit()
                            stats["created"] += 1
                    except Exception as e:
                        db.rollback()
                        stats["errors"] += 1

        except Exception as e:
            logger.warning(f"Family expansion batch failed: {e}")
            stats["errors"] += 1

    logger.info(f"Family expansion: {stats}")
    return stats


async def fill_commodity_gaps(db: Session) -> dict:
    """Strategy C: Ask AI for top MPNs per underrepresented category.

    For categories with few cards, discover the most commonly sourced parts.
    """
    from app.search_service import resolve_material_card
    from app.utils.claude_client import claude_json

    stats = {"categories_checked": 0, "discovered": 0, "created": 0, "already_exists": 0}

    # Find categories with few cards
    category_counts = (
        db.query(MaterialCard.category, func.count(MaterialCard.id))
        .filter(MaterialCard.deleted_at.is_(None), MaterialCard.category.isnot(None))
        .group_by(MaterialCard.category)
        .all()
    )
    count_map = {cat: cnt for cat, cnt in category_counts}

    # Target categories under 1000 cards
    small_categories = [
        cat for cat in COMMODITY_MAP
        if cat != "other" and count_map.get(cat, 0) < 1000
    ]

    if not small_categories:
        return stats

    logger.info(f"Gap fill: {len(small_categories)} small categories")

    for category in small_categories[:10]:  # Process 10 per run
        stats["categories_checked"] += 1

        try:
            result = await claude_json(
                f"List the 30 most commonly sourced '{category}' electronic component MPNs "
                f"that a global distributor would stock. Include manufacturer for each.",
                schema={
                    "type": "object",
                    "properties": {
                        "parts": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "mpn": {"type": "string"},
                                    "manufacturer": {"type": "string"},
                                },
                                "required": ["mpn"],
                            },
                        }
                    },
                    "required": ["parts"],
                },
                system="You are an expert in electronic component supply chains. List real, common MPNs only.",
                model_tier="smart",
                max_tokens=4096,
            )

            if not result or "parts" not in result:
                continue

            for part in result["parts"]:
                mpn = part.get("mpn", "").strip()
                if not mpn:
                    continue

                stats["discovered"] += 1
                mpn_norm = mpn.upper()

                existing = (
                    db.query(MaterialCard.id)
                    .filter(MaterialCard.normalized_mpn == mpn_norm, MaterialCard.deleted_at.is_(None))
                    .first()
                )

                if existing:
                    stats["already_exists"] += 1
                    continue

                try:
                    new_card = resolve_material_card(mpn, db)
                    if new_card:
                        new_card.enrichment_source = "discovery_gap_fill"
                        if part.get("manufacturer"):
                            new_card.manufacturer = part["manufacturer"][:255]
                        new_card.category = category
                        db.commit()
                        stats["created"] += 1
                except Exception as e:
                    db.rollback()

        except Exception as e:
            logger.warning(f"Gap fill for {category} failed: {e}")

    logger.info(f"Gap fill: {stats}")
    return stats
