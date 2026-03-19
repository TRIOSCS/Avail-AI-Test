"""Spec write service — single entry point for recording structured specs.

What: Normalizes, validates, conflict-resolves, and writes spec data to
      both the JSONB column (source of truth) and the facet table (indexed projection).
Called by: Data population jobs (SP2), vendor API enrichment, AI extraction.
Depends on: CommoditySpecSchema, MaterialSpecFacet, MaterialCard, unit_normalizer.
"""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from app.models import CommoditySpecSchema, MaterialCard, MaterialSpecFacet
from app.services.unit_normalizer import normalize_value

# Vendor API sources are authoritative — their values are never overwritten.
_VENDOR_API_SOURCES: set[str] = {"digikey_api", "nexar_api", "mouser_api"}


def load_schema_cache(db: Session, commodity: str) -> dict:
    """Pre-load all schemas for a commodity into a cache dict.

    Returns: {(commodity, spec_key): CommoditySpecSchema, ...}
    """
    schemas = db.query(CommoditySpecSchema).filter_by(commodity=commodity).all()
    return {(s.commodity, s.spec_key): s for s in schemas}


def record_spec(
    db: Session,
    card_id: int,
    spec_key: str,
    value: str | int | float | bool,
    *,
    source: str,
    confidence: float,
    unit: str | None = None,
    schema_cache: dict | None = None,
) -> None:
    """Record a structured spec value for a material card.

    Handles normalization, validation, conflict resolution, and facet sync. Does not
    commit — caller manages the transaction.
    """
    card = db.get(MaterialCard, card_id)
    if card is None:
        logger.warning("record_spec: card_id={} not found, skipping", card_id)
        return

    category = (card.category or "").lower().strip()
    if not category:
        logger.debug("record_spec: card {} has no category, skipping", card_id)
        return

    # Look up schema — use cache if provided
    if schema_cache is not None:
        schema = schema_cache.get((category, spec_key))
    else:
        schema = db.query(CommoditySpecSchema).filter_by(commodity=category, spec_key=spec_key).first()
    if schema is None:
        logger.debug(
            "record_spec: no schema for commodity={} spec_key={}, skipping",
            category,
            spec_key,
        )
        return

    # Validate enum
    if schema.data_type == "enum" and schema.enum_values:
        str_value = str(value)
        if str_value not in schema.enum_values:
            logger.debug(
                "record_spec: {} not in enum_values for {}.{}, skipping",
                str_value,
                category,
                spec_key,
            )
            return

    # Normalize unit for numeric types
    canonical_value = value
    canonical_unit = schema.canonical_unit
    if schema.data_type == "numeric" and unit and canonical_unit:
        canonical_value = normalize_value(value, unit, canonical_unit)

    # Build the spec entry
    now_iso = datetime.now(timezone.utc).isoformat()
    new_entry = {
        "value": canonical_value if schema.data_type == "numeric" else value,
        "source": source,
        "confidence": confidence,
        "updated_at": now_iso,
    }

    # For display/JSONB, store original value + unit for readability
    if schema.data_type == "numeric" and unit:
        new_entry["original_value"] = value
        new_entry["original_unit"] = unit

    # Conflict resolution: 2-tier — vendor API sources are authoritative, otherwise latest wins
    specs = dict(card.specs_structured or {})
    existing = specs.get(spec_key)

    if existing:
        existing_source = existing.get("source", "")

        if existing_source != source:
            # Different source — check vendor API priority
            if existing_source in _VENDOR_API_SOURCES and source not in _VENDOR_API_SOURCES:
                logger.info(
                    "spec conflict: card={} key={} kept existing (vendor_api) "
                    "existing={}({}, conf={}) incoming={}({}, conf={})",
                    card_id,
                    spec_key,
                    existing.get("value", ""),
                    existing_source,
                    existing.get("confidence", 0),
                    value,
                    source,
                    confidence,
                )
                return

            # Latest write wins for different non-vendor sources
            logger.info(
                "spec conflict: card={} key={} overwriting existing={}({}, conf={}) with incoming={}({}, conf={})",
                card_id,
                spec_key,
                existing.get("value", ""),
                existing_source,
                existing.get("confidence", 0),
                value,
                source,
                confidence,
            )
        else:
            # Same source — only overwrite if confidence is higher or equal
            existing_conf = existing.get("confidence", 0)
            if confidence < existing_conf:
                logger.debug(
                    "spec skip: card={} key={} same source={} existing_conf={} > incoming_conf={}",
                    card_id,
                    spec_key,
                    source,
                    existing_conf,
                    confidence,
                )
                return

    # Write to JSONB (source of truth)
    if schema.data_type == "boolean":
        new_entry["value"] = bool(value)
    specs[spec_key] = new_entry
    card.specs_structured = specs

    # Upsert facet row
    facet = db.query(MaterialSpecFacet).filter_by(material_card_id=card_id, spec_key=spec_key).first()
    if facet is None:
        facet = MaterialSpecFacet(
            material_card_id=card_id,
            category=category,
            spec_key=spec_key,
        )
        db.add(facet)

    if schema.data_type == "numeric":
        facet.value_numeric = canonical_value
        facet.value_text = None
        facet.value_unit = canonical_unit
    elif schema.data_type == "boolean":
        facet.value_text = "true" if value else "false"
        facet.value_numeric = None
        facet.value_unit = None
    else:  # enum / string
        facet.value_text = str(value)
        facet.value_numeric = None
        facet.value_unit = None

    db.flush()
    logger.debug(
        "record_spec: card={} {}.{}={} (source={}, conf={})",
        card_id,
        category,
        spec_key,
        value,
        source,
        confidence,
    )
