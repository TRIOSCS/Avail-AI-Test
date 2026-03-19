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

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.intelligence import MaterialCard
from app.services.specialty_detector import COMMODITY_MAP
from app.utils.normalization import normalize_mpn_key


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

    # Collect all candidate MPNs first for batch existence check
    candidate_mpns = []
    for card in cards:
        refs = card.cross_references
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if isinstance(ref, dict) and ref.get("mpn"):
                candidate_mpns.append(normalize_mpn_key(ref["mpn"]))

    # Batch existence check — single query instead of N+1
    existing_mpns = set()
    for i in range(0, len(candidate_mpns), 500):
        chunk = candidate_mpns[i : i + 500]
        rows = (
            db.query(MaterialCard.normalized_mpn)
            .filter(MaterialCard.normalized_mpn.in_(chunk), MaterialCard.deleted_at.is_(None))
            .all()
        )
        existing_mpns.update(r.normalized_mpn for r in rows)

    for card in cards:
        refs = card.cross_references
        if not isinstance(refs, list):
            continue

        for ref in refs:
            if not isinstance(ref, dict) or not ref.get("mpn"):
                continue

            stats["checked"] += 1
            mpn = normalize_mpn_key(ref["mpn"])

            if mpn in existing_mpns:
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
    """Strategy B: Ask AI for crosses, substitutes, and pin-compatible alternatives.

    For popular MPNs, discover direct replacements and cross-manufacturer equivalents.
    E.g., Samsung M393A2K43DB3-CWE → Micron MTA18ASF2G72PDZ-3G2R (cross-manufacturer sub).
    E.g., STM32F103C8T6 → GD32F103C8T6 (pin-compatible clone).
    """
    from app.search_service import resolve_material_card
    from app.utils.claude_client import claude_json

    stats = {"seed_cards": 0, "discovered": 0, "created": 0, "already_exists": 0, "errors": 0}

    # Get popular MPNs as seeds — prioritize searched parts
    seeds = (
        db.query(MaterialCard.id, MaterialCard.display_mpn, MaterialCard.manufacturer, MaterialCard.category)
        .filter(
            MaterialCard.deleted_at.is_(None),
            MaterialCard.search_count >= 1,
            MaterialCard.category.isnot(None),
            MaterialCard.category != "other",
        )
        .order_by(MaterialCard.search_count.desc())
        .limit(batch_size)
        .all()
    )

    stats["seed_cards"] = len(seeds)
    logger.info(f"Cross/substitute discovery: {len(seeds)} seed MPNs")

    # Batch seeds into groups of 20 for efficiency
    for i in range(0, len(seeds), 20):
        chunk = seeds[i : i + 20]
        mpn_list = "\n".join(f"- {s.display_mpn} ({s.manufacturer or 'unknown'}, {s.category})" for s in chunk)

        try:
            result = await claude_json(
                f"For each electronic component below, list its CROSSES and SUBSTITUTES — parts from "
                f"ANY manufacturer that are direct replacements, pin-compatible alternatives, or "
                f"functional equivalents a buyer could use instead. Include:\n"
                f"1. Cross-manufacturer equivalents (e.g., Samsung DRAM → Micron/SK Hynix equivalent)\n"
                f"2. Pin-compatible clones (e.g., STM32 → GD32/APM32)\n"
                f"3. Same-family variants with different specs (e.g., DDR4-3200 → DDR4-2933 version)\n"
                f"4. Second-source parts from alternative manufacturers\n\n"
                f"Only include REAL part numbers you are confident exist. Up to 8 per part.\n\n"
                f"{mpn_list}\n\n"
                f'Respond with JSON: {{"families": [{{"seed_mpn": "...", "family_members": ["..."]}}]}}',
                system=(
                    "You are an expert electronic component sourcing engineer specializing in "
                    "cross-references and substitute parts. Your job is to help buyers find "
                    "alternative sources when their first-choice part is unavailable. "
                    "List real, verified part numbers only — no guessing."
                ),
                model_tier="smart",
                max_tokens=4096,
            )

            if not result or "families" not in result:
                continue

            # Batch existence check for all discovered MPNs in this chunk
            all_discovered = []
            for family in result["families"]:
                for m in family.get("family_members", []):
                    if m and isinstance(m, str):
                        all_discovered.append(normalize_mpn_key(m))
            existing_set = set()
            if all_discovered:
                rows = (
                    db.query(MaterialCard.normalized_mpn)
                    .filter(MaterialCard.normalized_mpn.in_(all_discovered), MaterialCard.deleted_at.is_(None))
                    .all()
                )
                existing_set = {r.normalized_mpn for r in rows}

            for family in result["families"]:
                for member_mpn in family.get("family_members", []):
                    if not member_mpn or not isinstance(member_mpn, str):
                        continue

                    stats["discovered"] += 1
                    mpn_norm = normalize_mpn_key(member_mpn)

                    if mpn_norm in existing_set:
                        stats["already_exists"] += 1
                        continue

                    try:
                        new_card = resolve_material_card(member_mpn.strip(), db)
                        if new_card:
                            new_card.enrichment_source = "discovery_cross_sub"
                            db.commit()
                            stats["created"] += 1
                    except Exception as e:
                        logger.warning(f"Failed to create card for cross/sub {member_mpn}: {e}")
                        db.rollback()
                        stats["errors"] += 1

        except Exception as e:
            logger.warning(f"Cross/substitute discovery batch failed: {e}")
            stats["errors"] += 1

    logger.info(f"Cross/substitute discovery: {stats}")
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
    small_categories = [cat for cat in COMMODITY_MAP if cat != "other" and count_map.get(cat, 0) < 1000]

    if not small_categories:
        return stats

    logger.info(f"Gap fill: {len(small_categories)} small categories")

    for category in small_categories[:10]:  # Process 10 per run
        stats["categories_checked"] += 1

        try:
            result = await claude_json(
                f"List the 30 most commonly sourced '{category}' electronic component MPNs "
                f"that a global distributor would stock. Include manufacturer for each.\n\n"
                f'Respond with JSON: {{"parts": [{{"mpn": "...", "manufacturer": "..."}}]}}',
                system="You are an expert in electronic component supply chains. List real, common MPNs only.",
                model_tier="smart",
                max_tokens=4096,
            )

            if not result or "parts" not in result:
                continue

            # Batch existence check for all MPNs in this AI response
            candidate_norms = [normalize_mpn_key(p["mpn"]) for p in result["parts"] if p.get("mpn", "").strip()]
            existing_set = set()
            if candidate_norms:
                rows = (
                    db.query(MaterialCard.normalized_mpn)
                    .filter(MaterialCard.normalized_mpn.in_(candidate_norms), MaterialCard.deleted_at.is_(None))
                    .all()
                )
                existing_set = {r.normalized_mpn for r in rows}

            for part in result["parts"]:
                mpn = part.get("mpn", "").strip()
                if not mpn:
                    continue

                stats["discovered"] += 1
                mpn_norm = normalize_mpn_key(mpn)

                if mpn_norm in existing_set:
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
                    logger.warning(f"Failed to create card for gap fill {mpn}: {e}")
                    db.rollback()

        except Exception as e:
            logger.warning(f"Gap fill for {category} failed: {e}")

    logger.info(f"Gap fill: {stats}")
    return stats
