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
from sqlalchemy import or_
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
    # BrokerBin removed — 0.85 confidence is below the 0.90 floor
    {
        "name": "nexar",
        "module": "app.connectors.sources",
        "class": "NexarConnector",
        "creds": [("nexar", "NEXAR_CLIENT_ID"), ("nexar", "NEXAR_CLIENT_SECRET")],
        "confidence": 0.95,
    },
]

_IGNORED_MANUFACTURERS = {"", "unknown", "n/a", "various", "none", "other", "generic"}


def _manufacturers_agree(a: str, b: str) -> bool:
    """Fuzzy manufacturer-name match: exact, or either name contains the other."""
    a = (a or "").lower().strip()
    b = (b or "").lower().strip()
    return a == b or a in b or b in a


def _nexar_manufacturer(data: dict) -> str | None:
    """Extract the first result's manufacturer name from a Nexar GraphQL payload.

    Returns the trimmed name, or None when there is no result or it is a junk/placeholder
    manufacturer (per ``_IGNORED_MANUFACTURERS``).
    """
    results = (data.get("data") or {}).get("supSearchMpn", {}).get("results", [])
    if not results:
        return None
    part = results[0].get("part", {})
    mfr = ((part.get("manufacturer") or {}).get("name") or "").strip()
    if not mfr or mfr.lower() in _IGNORED_MANUFACTURERS:
        return None
    return mfr


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
    """Try a single connector config.

    Returns enrichment data or None.
    """
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
                    # Harvest fields — previously discarded (see connector-desc-harvest spec).
                    "description": (r.get("description") or "").strip() or None,
                    "package_type": (r.get("package_type") or "").strip() or None,
                    "pin_count": r.get("pin_count"),
                    "rohs_status": (r.get("rohs_status") or "").strip() or None,
                    "datasheet_url": (r.get("datasheet_url") or "").strip() or None,
                }
        return None
    except asyncio.TimeoutError:
        logger.warning("Connector {} timed out for {}", config["name"], mpn)
        return None
    except Exception as exc:
        err_str = str(exc).lower()
        if "401" in err_str or "403" in err_str or "unauthorized" in err_str:
            logger.error("Connector {} auth failure for {} — check API credentials", config["name"], mpn)
        elif "429" in err_str or "rate" in err_str:
            logger.warning("Connector {} rate limited for {}", config["name"], mpn)
        else:
            logger.warning("Connector {} failed for {}", config["name"], mpn, exc_info=True)
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
    from app.services.spec_tiers import set_category, set_manufacturer

    manufacturer = enrichment["manufacturer"]
    confidence = enrichment["confidence"]
    source_name = enrichment["source"]

    # Connector name → its registered *_api ladder source (tier 90; brokerbin is
    # registered as-is at 65). The evidence here is a distributor-API part match, so
    # both writes below carry the connector's own SOURCE_TIER name.
    ladder_source = source_name if source_name == "brokerbin" else f"{source_name}_api"

    # Manufacturer through the F1 ladder (was a direct fill-when-NULL `card.manufacturer
    # = ...` — the last un-routed maker writer, which left NULL provenance ranking at
    # the legacy floor). The ladder fills an empty maker, displaces legacy/lower-tier
    # values (50 < 90), and never overwrites manual (100) or trio_source (95).
    set_manufacturer(card, manufacturer, ladder_source, confidence)
    if enrichment.get("category"):
        # Same ladder: fills an empty category and may correct a lower-tier one (decode
        # 85, AI guess 40), but never overwrites a higher-tier value and never persists
        # off-vocab junk.
        set_category(card, enrichment["category"], ladder_source, confidence)

    # Classify and tag
    result = classify_material_card(card.normalized_mpn, manufacturer, card.category)
    tags_to_apply = []

    if result.get("brand"):
        brand_tag = get_or_create_brand_tag(result["brand"]["name"], db)
        tags_to_apply.append(
            {
                "tag_id": brand_tag.id,
                "source": f"connector_{source_name}",
                "confidence": confidence,
            }
        )

    if result.get("commodity"):
        commodity_tag = get_or_create_commodity_tag(result["commodity"]["name"], db)
        if commodity_tag:
            tags_to_apply.append(
                {
                    "tag_id": commodity_tag.id,
                    "source": f"connector_{source_name}",
                    "confidence": min(confidence, 0.9),
                }
            )

    if tags_to_apply:
        tag_material_card(card.id, tags_to_apply, db)

    from app.config import settings

    if settings.connector_desc_harvest_enabled:
        # Best-effort: harvest is bonus (manufacturer/category/tags above are already
        # applied). A failure here — e.g. record_spec/categorize_and_record raising
        # IntegrityError/DataError, which re-raise out of their own per-card SAVEPOINTs
        # (leaving the outer txn usable) — must NOT propagate and abort the caller's
        # batch loop (enrich_batch._process_one is unguarded). Log this card and move on.
        try:
            _harvest_connector_enrichment(card, enrichment, ladder_source, db)
        except Exception:
            logger.exception("connector-desc harvest failed for card_id={}", card.id)


# Connector structured fields → seeded facet keys. lifecycle_status is intentionally
# omitted: no commodity schema defines a `lifecycle` facet (confirmed against
# commodity_seeds.json), so it would only ever no-op.
_CONNECTOR_FIELD_TO_FACET = {"package_type": "package", "pin_count": "pin_count", "rohs_status": "rohs"}


def _harvest_connector_enrichment(card: MaterialCard, enrichment: dict, ladder_source: str, db: Session) -> None:
    """Record the description + structured fields the connector returned (previously
    discarded).

    Description → categorize_and_record (categorizes an uncategorized card + fills
    facets) at connector_desc / tier 84. Structured fields → record_spec at the
    connector's vendor-API tier (90) — only stick where the commodity schema defines the
    key. datasheet_url → card column. All writes arbitrated by the F1 ladder; each in a
    per-card SAVEPOINT.
    """
    from app.services.desc_extractor._common import CONNECTOR_DESC_CONFIDENCE, CONNECTOR_DESC_SOURCE
    from app.services.desc_extractor.writer import categorize_and_record
    from app.services.spec_write_service import load_schema_cache, record_spec

    datasheet_url = (enrichment.get("datasheet_url") or "").strip()
    if datasheet_url and not card.datasheet_url:
        card.datasheet_url = datasheet_url[:1000]

    description = (enrichment.get("description") or "").strip()
    if description:
        # Fill-only: categorizes only if still uncategorized; always fills facets via the ladder.
        categorize_and_record(
            db, card, description=description, source=CONNECTOR_DESC_SOURCE, confidence=CONNECTOR_DESC_CONFIDENCE
        )

    category = (card.category or "").lower().strip()
    if not category:
        return  # no schema without a category — structured facets can't be validated
    schema_cache = load_schema_cache(db, category)
    with db.begin_nested():
        for field, facet_key in _CONNECTOR_FIELD_TO_FACET.items():
            value = enrichment.get(field)
            if value is None or value == "":
                continue
            record_spec(
                db, int(card.id), facet_key, value, source=ladder_source, confidence=0.95, schema_cache=schema_cache
            )


def _run_boost_loop(db: Session, base_query, confidence: float, source: str, label: str, batch_size: int) -> int:
    """Page through a tag-id query and bulk-upgrade each batch to (confidence, source).

    ``base_query`` selects ``MaterialTag.id`` with all phase-specific filters EXCEPT the
    ``MaterialTag.id > last_id`` keyset cursor, which this loop appends per batch. Returns
    the total number of tags upgraded.
    """
    from app.models.tags import MaterialTag

    boosted = 0
    last_id = 0

    while True:
        rows = base_query.filter(MaterialTag.id > last_id).order_by(MaterialTag.id).limit(batch_size).all()
        if not rows:
            break

        mt_ids = list({r.id for r in rows})
        last_id = max(mt_ids)

        boosted += (
            db.query(MaterialTag)
            .filter(MaterialTag.id.in_(mt_ids))
            .update({"confidence": confidence, "source": source}, synchronize_session="fetch")
        )
        db.commit()
        logger.info(f"{label}: {boosted} tags upgraded so far")

    return boosted


def boost_confidence_internal(db: Session, batch_size: int = 5000) -> dict:
    """Boost confidence for AI tags confirmed by internal data (no API calls).

    Phase 1: Cards where MaterialCard.manufacturer matches the AI-classified brand tag.
    If the card's manufacturer field (set from sightings/connectors) agrees with
    the AI tag, the AI was right — upgrade confidence from 0.7 to 0.90.

    Processes in batches to avoid locking the DB.

    Returns: {total_boosted, total_checked}
    """
    from sqlalchemy import func

    from app.models.sourcing import Sighting
    from app.models.tags import MaterialTag, Tag

    # A sighting manufacturer confirms a brand tag when the two names match exactly or
    # one contains the other (the SQL twin of ``_manufacturers_agree``).
    sighting_confirms_tag = or_(
        func.lower(Sighting.manufacturer) == func.lower(Tag.name),
        func.lower(Sighting.manufacturer).contains(func.lower(Tag.name)),
        func.lower(Tag.name).contains(func.lower(Sighting.manufacturer)),
    )

    # Phase 1: card manufacturer confirms an AI-classified brand tag.
    total_boosted = _run_boost_loop(
        db,
        db.query(MaterialTag.id)
        .join(Tag, MaterialTag.tag_id == Tag.id)
        .join(MaterialCard, MaterialCard.id == MaterialTag.material_card_id)
        .filter(
            Tag.tag_type == "brand",
            MaterialTag.source == "ai_classified",
            MaterialTag.confidence < 0.9,
            MaterialTag.confidence > 0.3,  # Skip "Unknown"
            MaterialCard.manufacturer.isnot(None),
            MaterialCard.manufacturer != "",
            func.lower(MaterialCard.manufacturer) == func.lower(Tag.name),
        ),
        confidence=0.90,
        source="ai_confirmed_internal",
        label="Confidence boost",
        batch_size=batch_size,
    )
    logger.info(f"Internal confidence boost complete: {total_boosted} tags upgraded to 0.90")

    # Phase 2 (fuzzy boost to 0.85) and Phase 3 (commodity boost to 0.85) removed —
    # both produce sub-0.90 confidence, below the minimum floor.
    fuzzy_boosted = 0
    commodity_boosted = 0

    # Phase 4: Sighting-confirmed — sighting manufacturer confirms existing brand tag (0.30-0.89).
    sighting_boosted = _run_boost_loop(
        db,
        db.query(MaterialTag.id)
        .join(Tag, MaterialTag.tag_id == Tag.id)
        .join(MaterialCard, MaterialCard.id == MaterialTag.material_card_id)
        .join(Sighting, Sighting.material_card_id == MaterialCard.id)
        .filter(
            Tag.tag_type == "brand",
            MaterialTag.confidence < 0.9,
            MaterialTag.confidence > 0.3,
            Sighting.manufacturer.isnot(None),
            Sighting.manufacturer != "",
            sighting_confirms_tag,
        ),
        confidence=0.90,
        source="sighting_confirmed",
        label="Sighting-confirmed boost",
        batch_size=batch_size,
    )
    if sighting_boosted:
        logger.info(f"Sighting-confirmed boost: {sighting_boosted} tags upgraded to 0.90")

    # Phase 5: Multi-source agreement — AI + sighting independently agree → 0.95.
    multi_boosted = _run_boost_loop(
        db,
        db.query(MaterialTag.id)
        .join(Tag, MaterialTag.tag_id == Tag.id)
        .join(MaterialCard, MaterialCard.id == MaterialTag.material_card_id)
        .join(Sighting, Sighting.material_card_id == MaterialCard.id)
        .filter(
            Tag.tag_type == "brand",
            MaterialTag.source.in_(
                ["ai_classified", "ai_confirmed_internal", "ai_confirmed_fuzzy", "sighting_confirmed"]
            ),
            MaterialTag.confidence < 0.95,
            MaterialTag.confidence >= 0.7,
            Sighting.manufacturer.isnot(None),
            Sighting.manufacturer != "",
            sighting_confirms_tag,
        ),
        confidence=0.95,
        source="multi_source_confirmed",
        label="Multi-source boost",
        batch_size=batch_size,
    )
    if multi_boosted:
        logger.info(f"Multi-source boost: {multi_boosted} tags upgraded to 0.95")

    return {
        "total_boosted": total_boosted,
        "fuzzy_boosted": fuzzy_boosted,
        "commodity_boosted": commodity_boosted,
        "sighting_boosted": sighting_boosted,
        "multi_source_boosted": multi_boosted,
    }


async def nexar_bulk_validate(db: Session, limit: int = 5000) -> dict:
    """Validate AI-classified tags via Nexar bulk GraphQL queries (fast, batch-
    friendly).

    Nexar's aggregate query returns manufacturer for 20 MPNs at once, much faster
    than individual connector searches. Tags confirmed get 0.95 confidence.

    Returns: {total_checked, confirmed, changed, no_result}
    """
    from app.models.tags import MaterialTag, Tag

    # Find AI-classified cards still at 0.7 confidence (not yet boosted)
    low_conf = (
        db.query(
            MaterialCard.id,
            MaterialCard.normalized_mpn,
            MaterialTag.id.label("mt_id"),
            Tag.name.label("tag_name"),
        )
        .join(MaterialTag, MaterialCard.id == MaterialTag.material_card_id)
        .join(Tag, MaterialTag.tag_id == Tag.id)
        .filter(
            Tag.tag_type == "brand",
            MaterialTag.source == "ai_classified",
            MaterialTag.confidence == 0.7,  # Only unconfirmed AI tags
        )
        .order_by(MaterialCard.id)
        .limit(limit)
        .all()
    )

    if not low_conf:
        return {"total_checked": 0, "confirmed": 0, "changed": 0, "no_result": 0}

    # Try to use Nexar (fastest bulk option)
    nexar_id = get_credential_cached("nexar", "NEXAR_CLIENT_ID")
    nexar_sec = get_credential_cached("nexar", "NEXAR_CLIENT_SECRET")

    if not nexar_id or not nexar_sec:
        logger.info("Nexar bulk validate: no credentials, skipping")
        return {"total_checked": 0, "confirmed": 0, "changed": 0, "no_result": 0, "error": "no_nexar_creds"}

    from app.connectors.sources import NexarConnector

    connector = NexarConnector(nexar_id, nexar_sec)

    confirmed = 0
    changed = 0
    no_result = 0

    # Process in batches of 20 (Nexar query limit)
    for i in range(0, len(low_conf), 20):
        batch = low_conf[i : i + 20]

        for row in batch:
            try:
                data = await connector._run_query(connector.AGGREGATE_QUERY, row.normalized_mpn)
                nexar_mfr = _nexar_manufacturer(data)

                if not nexar_mfr:
                    no_result += 1
                    continue

                mt = db.get(MaterialTag, row.mt_id)
                if not mt:
                    continue

                if _manufacturers_agree(nexar_mfr, row.tag_name):
                    mt.confidence = 0.95
                    mt.source = "ai_confirmed_nexar"
                    confirmed += 1
                else:
                    # Nexar disagrees — apply Nexar's manufacturer (higher confidence)
                    card = db.get(MaterialCard, row.id)
                    if card:
                        _apply_enrichment_to_card(
                            card,
                            {
                                "manufacturer": nexar_mfr.lower().title(),
                                "source": "nexar",
                                "confidence": 0.95,
                                "category": None,
                            },
                            db,
                        )
                        changed += 1
            except Exception:
                logger.warning("Nexar validate failed for {}", row.normalized_mpn, exc_info=True)
                no_result += 1

        db.commit()
        if (i + 20) % 200 == 0:
            logger.info(f"Nexar validate: {i + 20}/{len(low_conf)} — {confirmed} confirmed, {changed} changed")
            await asyncio.sleep(1)  # Rate limit courtesy

    db.commit()
    logger.info(
        f"Nexar bulk validate: {len(low_conf)} checked, {confirmed} confirmed, {changed} changed, {no_result} no result"
    )
    return {"total_checked": len(low_conf), "confirmed": confirmed, "changed": changed, "no_result": no_result}


async def nexar_backfill_untagged(db: Session, limit: int = 5000) -> dict:
    """Backfill completely untagged cards via Nexar queries.

    For cards with NO brand MaterialTag at all (after prefix/sighting passes),
    query Nexar for manufacturer data and apply tags at 0.95 confidence.

    Returns: {total_checked, tagged, no_result}
    """
    from app.models.tags import MaterialTag, Tag

    # Find cards with NO brand tag
    tagged_brand_ids = (
        db.query(MaterialTag.material_card_id)
        .join(Tag, MaterialTag.tag_id == Tag.id)
        .filter(Tag.tag_type == "brand")
        .distinct()
        .subquery()
    )
    untagged = (
        db.query(MaterialCard.id, MaterialCard.normalized_mpn)
        .filter(~MaterialCard.id.in_(db.query(tagged_brand_ids.c.material_card_id)))
        .order_by(MaterialCard.id)
        .limit(limit)
        .all()
    )

    if not untagged:
        return {"total_checked": 0, "tagged": 0, "no_result": 0}

    nexar_id = get_credential_cached("nexar", "NEXAR_CLIENT_ID")
    nexar_sec = get_credential_cached("nexar", "NEXAR_CLIENT_SECRET")

    if not nexar_id or not nexar_sec:
        return {"total_checked": 0, "tagged": 0, "no_result": 0, "error": "no_nexar_creds"}

    from app.connectors.sources import NexarConnector

    connector = NexarConnector(nexar_id, nexar_sec)

    tagged = 0
    no_result = 0

    for i, row in enumerate(untagged):
        try:
            data = await connector._run_query(connector.AGGREGATE_QUERY, row.normalized_mpn)
            nexar_mfr = _nexar_manufacturer(data)

            if not nexar_mfr:
                no_result += 1
                continue

            card = db.get(MaterialCard, row.id)
            if card:
                _apply_enrichment_to_card(
                    card,
                    {"manufacturer": nexar_mfr, "source": "nexar", "confidence": 0.95, "category": None},
                    db,
                )
                tagged += 1

        except Exception:
            logger.warning("Nexar backfill failed for {}", row.normalized_mpn, exc_info=True)
            no_result += 1

        if (i + 1) % 200 == 0:
            db.commit()
            logger.info(f"Nexar backfill: {i + 1}/{len(untagged)} — {tagged} tagged")
            await asyncio.sleep(1)

    db.commit()
    logger.info(f"Nexar backfill: {len(untagged)} checked, {tagged} tagged, {no_result} no result")
    return {"total_checked": len(untagged), "tagged": tagged, "no_result": no_result}


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

        # Check if connector confirms the AI classification (exact or substring match)
        if _manufacturers_agree(result["manufacturer"], row.tag_name):
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
        f"Cross-validate complete: {total} checked, {confirmed} confirmed, {changed_mfr} changed, {no_result} no result"
    )
    return {
        "total": total,
        "confirmed": confirmed,
        "changed_manufacturer": changed_mfr,
        "no_result": no_result,
        "sources": sources,
    }
