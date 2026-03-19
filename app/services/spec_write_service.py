"""Spec write service — single entry point for recording structured specs.

What: Normalizes, validates, conflict-resolves, and writes spec data to
      both the JSONB column (source of truth) and the facet table (indexed projection).
Called by: Data population jobs (SP2), vendor API enrichment, AI extraction.
Depends on: CommoditySpecSchema, MaterialSpecFacet, MaterialSpecConflict,
            MaterialCard, unit_normalizer.
"""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from app.models import CommoditySpecSchema, MaterialCard, MaterialSpecConflict, MaterialSpecFacet
from app.services.unit_normalizer import normalize_value

# Source priority: lower number = higher priority
_SOURCE_PRIORITY: dict[str, int] = {
    "digikey_api": 1,
    "nexar_api": 1,
    "mouser_api": 1,
    "newegg_scrape": 2,
    "octopart_scrape": 2,
    "haiku_extraction": 3,
    "vendor_freetext": 4,
}

_DEFAULT_PRIORITY = 5


def _get_priority(source: str) -> int:
    return _SOURCE_PRIORITY.get(source, _DEFAULT_PRIORITY)


def record_spec(
    db: Session,
    card_id: int,
    spec_key: str,
    value: str | int | float | bool,
    *,
    source: str,
    confidence: float,
    unit: str | None = None,
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

    # Look up schema
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

    # Conflict resolution
    specs = dict(card.specs_structured or {})
    existing = specs.get(spec_key)

    if existing and existing.get("source") != source:
        # Different source — check priority
        existing_priority = _get_priority(existing["source"])
        incoming_priority = _get_priority(source)
        existing_conf = existing.get("confidence", 0)
        incoming_conf = confidence

        # Determine resolution
        if incoming_conf >= 0.95 and existing_conf < 0.80:
            resolution = "overwrote"  # High confidence override
        elif abs(existing_conf - incoming_conf) <= 0.1 and existing_priority == incoming_priority:
            resolution = "flagged"  # Close confidence, same priority
        elif incoming_priority < existing_priority:
            resolution = "overwrote"  # Higher priority source
        elif incoming_priority == existing_priority and incoming_conf > existing_conf:
            resolution = "overwrote"  # Equal priority, higher confidence
        else:
            resolution = "kept_existing"

        # Log the conflict
        conflict = MaterialSpecConflict(
            material_card_id=card_id,
            spec_key=spec_key,
            existing_value=str(existing.get("value", "")),
            existing_source=existing.get("source", ""),
            existing_confidence=existing_conf,
            incoming_value=str(value),
            incoming_source=source,
            incoming_confidence=confidence,
            resolution=resolution,
            resolved_by="auto",
        )
        db.add(conflict)

        if resolution == "kept_existing" or resolution == "flagged":
            db.flush()
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
