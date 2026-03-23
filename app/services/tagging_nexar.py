"""Nexar batch enrichment — classify untagged parts via Nexar/Octopart API.

Uses existing NexarConnector (aggregate query) to get manufacturer + category
for parts that weren't matched by prefix lookup. Rate-limited and batched.

Called by: app.routers.tagging_admin (manual trigger)
Depends on: app.connectors.sources (NexarConnector), app.services.tagging
"""

import asyncio

from loguru import logger
from sqlalchemy.orm import Session

from app.models.intelligence import MaterialCard
from app.models.tags import MaterialTag, Tag
from app.services.tagging import (
    classify_material_card,
    get_or_create_brand_tag,
    get_or_create_commodity_tag,
    tag_material_card,
)


async def _query_nexar_batch(mpns: list[str]) -> dict[str, dict]:
    """Query Nexar for a batch of MPNs.

    Returns {mpn: {manufacturer, category}}.
    """
    from app.services.credential_service import get_credential_cached

    client_id = get_credential_cached("nexar_api", "NEXAR_CLIENT_ID")
    client_secret = get_credential_cached("nexar_api", "NEXAR_CLIENT_SECRET")
    if not client_id or not client_secret:
        logger.warning("Nexar credentials not configured — skipping batch")
        return {}

    from app.connectors.sources import NexarConnector

    connector = NexarConnector(client_id, client_secret)
    results = {}

    for mpn in mpns:
        try:
            data = await connector._run_query(connector.AGGREGATE_QUERY, mpn)
            search_results = data.get("data", {}).get("supSearchMpn", {}).get("results", [])
            if search_results:
                part = search_results[0].get("part", {})
                manufacturer = (part.get("manufacturer") or {}).get("name")
                category = (part.get("category") or {}).get("name")
                if manufacturer or category:
                    results[mpn] = {"manufacturer": manufacturer, "category": category}
        except Exception:  # pragma: no cover
            logger.warning(f"Nexar query failed for {mpn}", exc_info=True)

    return results


async def run_nexar_backfill(db: Session, batch_size: int = 100, delay_seconds: float = 2.0) -> dict:
    """Classify untagged parts via Nexar API. Batched, rate-limited.

    Returns: {total_processed, total_matched, total_skipped}
    """
    # Find cards with NO brand tag
    tagged_brand_ids = (
        db.query(MaterialTag.material_card_id)
        .join(Tag, MaterialTag.tag_id == Tag.id)
        .filter(Tag.tag_type == "brand")
        .distinct()
        .subquery()
    )
    untagged = (
        db.query(MaterialCard.id, MaterialCard.normalized_mpn, MaterialCard.category)
        .filter(~MaterialCard.id.in_(db.query(tagged_brand_ids.c.material_card_id)))
        .order_by(MaterialCard.id)
        .all()
    )

    if not untagged:
        logger.info("No untagged cards for Nexar backfill")
        return {"total_processed": 0, "total_matched": 0, "total_skipped": 0}

    logger.info(f"Nexar backfill starting: {len(untagged)} untagged cards")
    total_processed = 0
    total_matched = 0
    total_skipped = 0

    for i in range(0, len(untagged), batch_size):
        batch = untagged[i : i + batch_size]
        mpns = [row.normalized_mpn for row in batch]

        nexar_results = await _query_nexar_batch(mpns)

        for card_id, normalized_mpn, existing_category in batch:
            total_processed += 1
            nexar_data = nexar_results.get(normalized_mpn)

            if not nexar_data:
                total_skipped += 1
                continue

            manufacturer = nexar_data.get("manufacturer")
            category = nexar_data.get("category") or existing_category

            # Update MaterialCard fields
            card = db.get(MaterialCard, card_id)
            if card and manufacturer and not card.manufacturer:
                card.manufacturer = manufacturer
            if card and category and not card.category:
                card.category = category

            result = classify_material_card(normalized_mpn, manufacturer, category)
            tags_to_apply = []

            if result.get("brand"):
                brand_tag = get_or_create_brand_tag(result["brand"]["name"], db)
                tags_to_apply.append(
                    {
                        "tag_id": brand_tag.id,
                        "source": "nexar",
                        "confidence": 0.95,
                    }
                )

            if result.get("commodity"):
                commodity_tag = get_or_create_commodity_tag(result["commodity"]["name"], db)
                if commodity_tag:  # pragma: no cover
                    tags_to_apply.append(
                        {
                            "tag_id": commodity_tag.id,
                            "source": "nexar",
                            "confidence": 0.9,
                        }
                    )

            if tags_to_apply:
                tag_material_card(card_id, tags_to_apply, db)
                total_matched += 1
            else:  # pragma: no cover
                total_skipped += 1

        db.commit()
        logger.info(f"Nexar backfill: {total_processed}/{len(untagged)} — {total_matched} matched")

        if i + batch_size < len(untagged):  # pragma: no cover
            await asyncio.sleep(delay_seconds)

    logger.info(f"Nexar backfill complete: {total_processed} processed, {total_matched} matched")
    return {"total_processed": total_processed, "total_matched": total_matched, "total_skipped": total_skipped}
