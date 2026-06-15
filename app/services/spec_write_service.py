"""Spec write service — single entry point for recording structured specs.

What: Normalizes, validates, conflict-resolves, and writes spec data to
      both the JSONB column (source of truth) and the facet table (indexed projection).
      ``spec_would_write`` is the read-only twin of ``record_spec`` (same gates, no
      writes) so dry-run reports cannot drift from apply-mode behavior.
Called by: Data population jobs (SP2), vendor API enrichment, AI extraction, the
      SP-Ingest pipeline (app/services/source_ingest/ingest.py).
Depends on: CommoditySpecSchema, MaterialSpecFacet, MaterialCard, unit_normalizer,
      spec_tiers (the F1 ladder — every writer's ``source`` string MUST be registered
      in spec_tiers.SOURCE_TIER or all its writes lose every conflict at tier 0).
"""

import re
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from app.models import CommoditySpecSchema, MaterialCard, MaterialSpecFacet
from app.services.spec_tiers import (
    count_ladder_rejection,
    record_evidence_dissent,
    record_validation_conflict,
    resolve,
    tier_for,
    values_contradict,
)
from app.services.unit_normalizer import normalize_value

_NUMERIC_RE = re.compile(r"^\s*([+-]?\d+(?:[.,]\d+)?(?:[eE][+-]?\d+)?)\s*([a-zA-Zµμ%Ω°/]+.*)?\s*$")


def load_schema_cache(db: Session, commodity: str) -> dict:
    """Pre-load all schemas for a commodity into a cache dict.

    Returns: {(commodity, spec_key): CommoditySpecSchema, ...}
    """
    schemas = db.query(CommoditySpecSchema).filter_by(commodity=commodity).all()
    return {(s.commodity, s.spec_key): s for s in schemas}


def _lookup_schema(db: Session, category: str, spec_key: str, schema_cache: dict | None):
    """Resolve the (category, spec_key) schema via the cache or a DB query."""
    if schema_cache is not None:
        return schema_cache.get((category, spec_key))
    return db.query(CommoditySpecSchema).filter_by(commodity=category, spec_key=spec_key).first()


def _validate_and_normalize(schema, value, unit: str | None, *, category: str, spec_key: str):
    """Apply the enum / numeric gates and unit normalization (shared by record_spec and
    spec_would_write so the dry-run cannot drift from apply mode).

    Returns ``(ok, canonical_value, unit)`` — ``ok`` False means the value is rejected
    (enum mismatch / unparseable numeric).
    """
    canonical_value = value
    if schema.data_type == "enum" and schema.enum_values:
        if str(value) not in schema.enum_values:
            logger.debug(
                "record_spec: {} not in enum_values for {}.{}, skipping",
                str(value),
                category,
                spec_key,
            )
            return False, value, unit
    if schema.data_type == "numeric":
        # If value is a string like "0.1µF", try to extract the numeric part
        if isinstance(value, str):
            m = _NUMERIC_RE.match(value)
            if m:
                try:
                    canonical_value = float(m.group(1).replace(",", "."))
                except ValueError:
                    logger.warning("record_spec: cannot parse numeric value '{}' for {}.{}", value, category, spec_key)
                    return False, value, unit
                if not unit and m.group(2):
                    unit = m.group(2).strip()
            else:
                logger.debug("record_spec: non-numeric string '{}' for numeric spec {}.{}", value, category, spec_key)
                return False, value, unit
        if unit and schema.canonical_unit:
            canonical_value = normalize_value(canonical_value, unit, schema.canonical_unit)
    return True, canonical_value, unit


def _incoming_loses(
    existing: dict | None,
    *,
    source: str,
    incoming_tier: int,
    confidence: float,
    now_iso: str,
    card_id,
    spec_key: str,
    value,
) -> bool:
    """Return True iff the existing entry beats the incoming write under the F1 ladder.

    Read-only: legacy entries pre-date the ``tier`` key, so the comparison backfills it
    from ``source`` on a COPY — never on the live (ORM-aliased) JSONB dict. Cross-source
    rejections log at INFO (a writer losing all of its writes must be visible at
    production log levels); same-source skips stay at DEBUG.
    """
    if existing is None:
        return False
    existing_cmp = dict(existing)
    existing_cmp.setdefault("tier", tier_for(existing_cmp.get("source", "")))
    incoming = {"tier": incoming_tier, "confidence": confidence, "updated_at": now_iso}
    if resolve(existing_cmp, incoming):
        return False
    log = logger.info if existing_cmp.get("source", "") != source else logger.debug
    log(
        "spec skip: card={} key={} existing={}({}, tier={}, conf={}) beats incoming={}({}, tier={}, conf={})",
        card_id,
        spec_key,
        existing_cmp.get("value", ""),
        existing_cmp.get("source", ""),
        existing_cmp.get("tier", 0),
        existing_cmp.get("confidence", 0),
        value,
        source,
        incoming_tier,
        confidence,
    )
    return True


def spec_would_write(
    db: Session,
    *,
    category: str | None,
    existing_specs: dict | None,
    spec_key: str,
    value: str | int | float | bool,
    source: str,
    confidence: float,
    unit: str | None = None,
    schema_cache: dict | None = None,
) -> bool:
    """Read-only twin of ``record_spec``: would this write be persisted?

    Runs the exact gates apply-mode runs — category present, schema exists for
    (category, spec_key), enum/numeric validation, and the F1 ladder against the
    existing JSONB entry — without touching the DB or any card. Used by dry-run
    accounting (SP-Ingest) so the operator's go/no-go report matches what ``--apply``
    will actually do.
    """
    category = (category or "").lower().strip()
    if not category:
        return False
    schema = _lookup_schema(db, category, spec_key, schema_cache)
    if schema is None:
        return False
    ok, _, _ = _validate_and_normalize(schema, value, unit, category=category, spec_key=spec_key)
    if not ok:
        return False
    confidence = min(max(float(confidence or 0.0), 0.0), 1.0)
    return not _incoming_loses(
        (existing_specs or {}).get(spec_key),
        source=source,
        incoming_tier=tier_for(source),
        confidence=confidence,
        now_iso=datetime.now(timezone.utc).isoformat(),
        card_id=None,
        spec_key=spec_key,
        value=value,
    )


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
) -> bool:
    """Record a structured spec value for a material card.

    Handles normalization, validation, conflict resolution, and facet sync. Does not
    commit — caller manages the transaction. *source* MUST be a key registered in
    spec_tiers.SOURCE_TIER — an unregistered source ranks at tier 0 and loses every
    conflict (tier_for warns once per unknown source).

    Returns True if the spec was persisted, False if it was skipped for any reason (card
    not found, no category, no schema, enum mismatch, unparseable numeric, or the F1
    tier ladder rejecting the write — a lower-(tier, confidence, updated_at) source
    loses to the existing entry; see app/services/spec_tiers.resolve).
    """
    card = db.get(MaterialCard, card_id)
    if card is None:
        logger.warning("record_spec: card_id={} not found, skipping", card_id)
        return False

    category = (card.category or "").lower().strip()
    if not category:
        logger.debug("record_spec: card {} has no category, skipping", card_id)
        return False

    schema = _lookup_schema(db, category, spec_key, schema_cache)
    if schema is None:
        logger.debug(
            "record_spec: no schema for commodity={} spec_key={}, skipping",
            category,
            spec_key,
        )
        return False

    ok, canonical_value, unit = _validate_and_normalize(schema, value, unit, category=category, spec_key=spec_key)
    if not ok:
        return False
    canonical_unit = schema.canonical_unit

    # Build the spec entry. ``tier`` (F2) is persisted alongside value/source/confidence so
    # later writes — and the backfill — can rank this entry without re-deriving it.
    # Confidence is clamped to [0, 1] at the boundary so a percent-style value (95) can
    # never be persisted and then dominate every same-tier comparison.
    confidence = min(max(float(confidence or 0.0), 0.0), 1.0)
    now_iso = datetime.now(timezone.utc).isoformat()
    incoming_tier = tier_for(source)
    new_entry = {
        "value": canonical_value if schema.data_type == "numeric" else value,
        "source": source,
        "confidence": confidence,
        "tier": incoming_tier,
        "updated_at": now_iso,
    }

    # For display/JSONB, store original value + unit for readability
    if schema.data_type == "numeric" and unit:
        new_entry["original_value"] = value
        new_entry["original_unit"] = unit

    # Conflict resolution: single uniform F1 ladder — incoming wins iff its
    # (tier, confidence, updated_at) tuple beats the existing entry's (spec_tiers.resolve).
    # Higher tier always overrides; equal tier → higher confidence; tie → newer.
    specs = dict(card.specs_structured or {})
    existing_entry = specs.get(spec_key)
    if _incoming_loses(
        existing_entry,
        source=source,
        incoming_tier=incoming_tier,
        confidence=confidence,
        now_iso=now_iso,
        card_id=card_id,
        spec_key=spec_key,
        value=value,
    ):
        # The ladder kept the existing entry. When that entry is a manual (tier 100)
        # value and the loser is an authoritative source (tier >= 80) reporting
        # something ELSE, persist the contradiction for human review — the helper
        # gates manual / tier>=80 / values-differ, so this call is safe on every lose.
        # Non-manual kept entries get the same artifact via record_evidence_dissent
        # (gates kept!=manual / loser tier>=80 / values-differ — at most one of the
        # two recorders fires per loss). Every rejection also bumps the persistent
        # daily counter, classified corroboration vs contradiction; the counter is
        # telemetry and can never break this write path (wrapped inside).
        incoming_cmp_value = bool(value) if schema.data_type == "boolean" else new_entry["value"]
        record_validation_conflict(card, spec_key, existing_entry, new_entry, incoming_cmp_value)
        record_evidence_dissent(card, spec_key, existing_entry, new_entry, incoming_cmp_value)
        count_ladder_rejection(
            (existing_entry or {}).get("source", ""),
            source,
            contradiction=values_contradict((existing_entry or {}).get("value"), incoming_cmp_value),
        )
        return False

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

    # Facet provenance projection (F2): the facet row always mirrors the winning JSONB
    # entry's source/confidence/tier (reached only when the write won the ladder above).
    facet.source = source
    facet.confidence = confidence
    facet.tier = incoming_tier

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
    return True
