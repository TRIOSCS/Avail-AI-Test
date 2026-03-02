"""Multi-source connector enrichment — query API connectors for manufacturer data.

Queries DigiKey, Mouser, Element14, OEMSecrets, BrokerBin, and Nexar to
enrich MaterialCards with manufacturer and category information. Used for
both live enrichment (new cards) and background backfill (low-confidence).

Called by: app.search_service (live hook), app.routers.tagging_admin, app.scheduler
Depends on: app.connectors.*, app.services.credential_service, app.services.tagging
"""

import asyncio
import importlib

from loguru import logger
from sqlalchemy.orm import Session

from app.models.intelligence import MaterialCard
from app.services.credential_service import get_credential_cached
from app.services.tagging import (
    classify_material_card,
    get_or_create_brand_tag,
    get_or_create_commodity_tag,
    tag_material_card,
)

# Connector configs: (source_name, connector_class_path, credential_keys, confidence)
# Ordered by priority — authoritative distributors first
_CONNECTOR_CONFIGS = [
    {
        "name": "digikey",
        "module": "app.connectors.digikey",
        "class": "DigiKeyConnector",
        "creds": [("digikey", "DIGIKEY_CLIENT_ID"), ("digikey", "DIGIKEY_CLIENT_SECRET")],
        "confidence": 0.95,
    },
    {
        "name": "mouser",
        "module": "app.connectors.mouser",
        "class": "MouserConnector",
        "creds": [("mouser", "MOUSER_API_KEY")],
        "confidence": 0.95,
    },
    {
        "name": "element14",
        "module": "app.connectors.element14",
        "class": "Element14Connector",
        "creds": [("element14", "ELEMENT14_API_KEY")],
        "confidence": 0.95,
    },
    {
        "name": "oemsecrets",
        "module": "app.connectors.oemsecrets",
        "class": "OEMSecretsConnector",
        "creds": [("oemsecrets", "OEMSECRETS_API_KEY")],
        "confidence": 0.95,
    },
    {
        "name": "brokerbin",
        "module": "app.connectors.sources",
        "class": "BrokerBinConnector",
        "creds": [("brokerbin", "BROKERBIN_API_KEY")],
        "confidence": 0.85,
    },
    {
        "name": "nexar",
        "module": "app.connectors.sources",
        "class": "NexarConnector",
        "creds": [("nexar", "NEXAR_CLIENT_ID"), ("nexar", "NEXAR_CLIENT_SECRET")],
        "confidence": 0.95,
    },
]

_IGNORED_MANUFACTURERS = {"", "unknown", "n/a", "various", "none", "other", "generic"}


async def enrich_material_card(mpn: str, db: Session) -> dict | None:
    """Query all available connectors for manufacturer data on a single MPN.

    Returns: {manufacturer, category, source, confidence} or None
    """
    for config in _CONNECTOR_CONFIGS:
        result = await _try_connector_config(config, mpn)
        if result:
            return result
    return None


async def _try_connector_config(config: dict, mpn: str) -> dict | None:
    """Try a single connector config. Returns enrichment data or None."""
    # Check credentials
    cred_values = []
    for source_name, env_var in config["creds"]:
        val = get_credential_cached(source_name, env_var)
        if not val:
            return None
        cred_values.append(val)

    try:
        module = importlib.import_module(config["module"])
        connector_class = getattr(module, config["class"])
        connector = connector_class(*cred_values)

        results = await asyncio.wait_for(connector.search(mpn), timeout=15)
        for r in results:
            mfr = (r.get("manufacturer") or "").strip()
            if mfr.lower() not in _IGNORED_MANUFACTURERS:
                return {
                    "manufacturer": mfr,
                    "category": (r.get("category") or r.get("description") or "").strip()[:200] or None,
                    "source": config["name"],
                    "confidence": config["confidence"],
                }
        return None
    except Exception:
        logger.debug("Connector %s failed for %s", config["name"], mpn, exc_info=True)
        return None


async def enrich_batch(mpns: list[str], db: Session, concurrency: int = 5) -> dict:
    """Enrich a batch of MPNs via connectors. Rate-limited.

    Returns: {total, matched, skipped, sources: {source_name: count}}
    """
    sem = asyncio.Semaphore(concurrency)
    total = len(mpns)
    matched = 0
    skipped = 0
    sources: dict[str, int] = {}

    async def _process_one(mpn: str):
        nonlocal matched, skipped
        async with sem:
            result = await enrich_material_card(mpn, db)
            if not result:
                skipped += 1
                return

            # Apply to card
            card = db.query(MaterialCard).filter_by(normalized_mpn=mpn).first()
            if not card:
                skipped += 1
                return

            _apply_enrichment_to_card(card, result, db)
            matched += 1
            sources[result["source"]] = sources.get(result["source"], 0) + 1

    # Process sequentially within the semaphore to avoid SQLAlchemy session issues
    for i, mpn in enumerate(mpns):
        await _process_one(mpn)
        if (i + 1) % 100 == 0:
            db.commit()
            db.expire_all()
            logger.info(f"Enrichment progress: {i + 1}/{total} ({matched} matched)")

    db.commit()
    logger.info(f"Enrichment complete: {total} total, {matched} matched, {skipped} skipped")
    return {"total": total, "matched": matched, "skipped": skipped, "sources": sources}


def _apply_enrichment_to_card(card: MaterialCard, enrichment: dict, db: Session) -> None:
    """Apply enrichment result to a material card and tag it."""
    manufacturer = enrichment["manufacturer"]
    confidence = enrichment["confidence"]
    source_name = enrichment["source"]

    # Update card fields
    if not card.manufacturer:
        card.manufacturer = manufacturer
    if enrichment.get("category") and not card.category:
        card.category = enrichment["category"]

    # Classify and tag
    result = classify_material_card(card.normalized_mpn, manufacturer, card.category)
    tags_to_apply = []

    if result.get("brand"):
        brand_tag = get_or_create_brand_tag(result["brand"]["name"], db)
        tags_to_apply.append({
            "tag_id": brand_tag.id,
            "source": f"connector_{source_name}",
            "confidence": confidence,
        })

    if result.get("commodity"):
        commodity_tag = get_or_create_commodity_tag(result["commodity"]["name"], db)
        if commodity_tag:
            tags_to_apply.append({
                "tag_id": commodity_tag.id,
                "source": f"connector_{source_name}",
                "confidence": min(confidence, 0.9),
            })

    if tags_to_apply:
        tag_material_card(card.id, tags_to_apply, db)


async def cross_validate_batch(db: Session, limit: int = 500, concurrency: int = 3) -> dict:
    """Cross-check low-confidence AI tags against connectors to upgrade confidence.

    Finds cards with ai_classified brand tags (confidence < 0.9), queries connectors
    for the same MPN, and if the connector confirms the same manufacturer, upgrades
    the tag confidence to 0.95.

    Returns: {total, confirmed, changed_manufacturer, no_result, sources: {}}
    """
    from app.models.tags import MaterialTag, Tag

    # Find cards with low-confidence AI brand tags
    low_conf = (
        db.query(
            MaterialCard.id,
            MaterialCard.normalized_mpn,
            MaterialCard.manufacturer,
            MaterialTag.id.label("mt_id"),
            MaterialTag.confidence,
            Tag.name.label("tag_name"),
        )
        .join(MaterialTag, MaterialCard.id == MaterialTag.material_card_id)
        .join(Tag, MaterialTag.tag_id == Tag.id)
        .filter(
            Tag.tag_type == "brand",
            MaterialTag.source == "ai_classified",
            MaterialTag.confidence < 0.9,
            MaterialTag.confidence > 0.3,  # Skip "Unknown" (0.3) — not worth validating
        )
        .order_by(MaterialTag.confidence.asc())
        .limit(limit)
        .all()
    )

    if not low_conf:
        logger.info("Cross-validate: no low-confidence AI tags to check")
        return {"total": 0, "confirmed": 0, "changed_manufacturer": 0, "no_result": 0, "sources": {}}

    logger.info(f"Cross-validate: checking {len(low_conf)} low-confidence AI tags")

    sem = asyncio.Semaphore(concurrency)
    confirmed = 0
    changed_mfr = 0
    no_result = 0
    sources: dict[str, int] = {}

    for i, row in enumerate(low_conf):
        async with sem:
            result = await enrich_material_card(row.normalized_mpn, db)

        if not result:
            no_result += 1
            continue

        connector_mfr = result["manufacturer"].lower().strip()
        ai_mfr = (row.tag_name or "").lower().strip()

        # Check if connector confirms the AI classification
        # Fuzzy match: either one contains the other, or exact match
        is_confirmed = (
            connector_mfr == ai_mfr
            or connector_mfr in ai_mfr
            or ai_mfr in connector_mfr
        )

        if is_confirmed:
            # Upgrade the existing tag confidence
            mt = db.get(MaterialTag, row.mt_id)
            if mt and mt.confidence < result["confidence"]:
                mt.confidence = result["confidence"]
                mt.source = f"ai_confirmed_{result['source']}"
                confirmed += 1
                sources[result["source"]] = sources.get(result["source"], 0) + 1
        else:
            # Connector says different manufacturer — apply the new one (higher confidence wins)
            card = db.get(MaterialCard, row.id)
            if card:
                _apply_enrichment_to_card(card, result, db)
                changed_mfr += 1
                sources[result["source"]] = sources.get(result["source"], 0) + 1

        if (i + 1) % 100 == 0:
            db.commit()
            db.expire_all()
            logger.info(
                f"Cross-validate progress: {i + 1}/{len(low_conf)} "
                f"({confirmed} confirmed, {changed_mfr} changed, {no_result} no result)"
            )

    db.commit()
    total = len(low_conf)
    logger.info(
        f"Cross-validate complete: {total} checked, {confirmed} confirmed, "
        f"{changed_mfr} changed, {no_result} no result"
    )
    return {
        "total": total,
        "confirmed": confirmed,
        "changed_manufacturer": changed_mfr,
        "no_result": no_result,
        "sources": sources,
    }
