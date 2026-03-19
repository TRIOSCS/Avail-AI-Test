"""Vendor API spec enrichment — parse structured specs from DigiKey/Nexar/Mouser.

What: Extracts technical parameters from sighting raw_data JSON, normalizes
      vendor-specific field names to our spec_keys, and writes via record_spec().
Called by: Backfill scripts, post-search enrichment hooks.
Depends on: spec_write_service (record_spec, load_schema_cache), Sighting model.
"""

import re

from loguru import logger
from sqlalchemy.orm import Session

from app.models.intelligence import MaterialCard
from app.models.sourcing import Sighting
from app.services.spec_write_service import load_schema_cache, record_spec

# ── Vendor parameter name → our spec_key, per commodity ──────────────
# Each vendor uses different field names for the same concept.
# Keys are lowercased vendor parameter names → our spec_key.

_DIGIKEY_MAP: dict[str, dict[str, str]] = {
    "capacitors": {
        "capacitance": "capacitance",
        "voltage - rated": "voltage_rating",
        "voltage rated": "voltage_rating",
        "temperature coefficient": "dielectric",
        "tolerance": "tolerance",
        "package / case": "package",
        "package": "package",
    },
    "resistors": {
        "resistance": "resistance",
        "power (watts)": "power_rating",
        "power rating": "power_rating",
        "tolerance": "tolerance",
        "package / case": "package",
        "package": "package",
    },
    "dram": {
        "memory type": "ddr_type",
        "technology": "ddr_type",
        "memory size": "capacity_gb",
        "speed": "speed_mhz",
        "module type": "form_factor",
        "format": "form_factor",
    },
    "ssd": {
        "memory size": "capacity_gb",
        "capacity": "capacity_gb",
        "form factor": "form_factor",
        "interface": "interface",
        "read speed": "read_speed_mbps",
    },
    "hdd": {
        "capacity": "capacity_gb",
        "memory size": "capacity_gb",
        "rotational speed": "rpm",
        "rpm": "rpm",
        "form factor": "form_factor",
        "interface": "interface",
    },
    "motherboards": {
        "socket type": "socket",
        "cpu socket": "socket",
        "socket": "socket",
        "form factor": "form_factor",
        "chipset": "chipset",
        "memory slots": "ram_slots",
        "ram slots": "ram_slots",
    },
    "power_supplies": {
        "power (watts)": "wattage",
        "wattage": "wattage",
        "output power": "wattage",
        "form factor": "form_factor",
        "efficiency": "efficiency",
        "efficiency rating": "efficiency",
    },
}

_NEXAR_MAP: dict[str, dict[str, str]] = {
    "capacitors": {
        "capacitance": "capacitance",
        "voltage rating": "voltage_rating",
        "voltage - rated": "voltage_rating",
        "voltage rated": "voltage_rating",
        "dielectric": "dielectric",
        "temperature coefficient": "dielectric",
        "tolerance": "tolerance",
        "case/package": "package",
        "case code (imperial)": "package",
        "package": "package",
    },
    "resistors": {
        "resistance": "resistance",
        "power rating": "power_rating",
        "power": "power_rating",
        "tolerance": "tolerance",
        "case/package": "package",
        "case code (imperial)": "package",
        "package": "package",
    },
    "dram": {
        "memory type": "ddr_type",
        "technology": "ddr_type",
        "memory size": "capacity_gb",
        "density": "capacity_gb",
        "speed": "speed_mhz",
        "clock frequency": "speed_mhz",
        "module type": "form_factor",
        "format": "form_factor",
    },
    "ssd": {
        "capacity": "capacity_gb",
        "memory size": "capacity_gb",
        "form factor": "form_factor",
        "interface": "interface",
    },
    "hdd": {
        "capacity": "capacity_gb",
        "memory size": "capacity_gb",
        "rotational speed": "rpm",
        "form factor": "form_factor",
        "interface": "interface",
    },
    "motherboards": {
        "socket type": "socket",
        "cpu socket": "socket",
        "socket": "socket",
        "form factor": "form_factor",
        "chipset": "chipset",
        "memory slots": "ram_slots",
    },
    "power_supplies": {
        "power": "wattage",
        "output power": "wattage",
        "wattage": "wattage",
        "form factor": "form_factor",
        "efficiency": "efficiency",
    },
}

_MOUSER_MAP: dict[str, dict[str, str]] = {
    "capacitors": {
        "capacitance": "capacitance",
        "voltage rated": "voltage_rating",
        "voltage rating": "voltage_rating",
        "voltage - rated": "voltage_rating",
        "dielectric": "dielectric",
        "temperature coefficient": "dielectric",
        "tolerance": "tolerance",
        "case/package": "package",
        "package / case": "package",
        "package": "package",
    },
    "resistors": {
        "resistance": "resistance",
        "power rating": "power_rating",
        "power (watts)": "power_rating",
        "tolerance": "tolerance",
        "case/package": "package",
        "package / case": "package",
        "package": "package",
    },
    "dram": {
        "memory type": "ddr_type",
        "technology": "ddr_type",
        "memory size": "capacity_gb",
        "density": "capacity_gb",
        "speed": "speed_mhz",
        "clock frequency": "speed_mhz",
        "module type": "form_factor",
        "format": "form_factor",
    },
    "ssd": {
        "capacity": "capacity_gb",
        "memory size": "capacity_gb",
        "form factor": "form_factor",
        "interface": "interface",
    },
    "hdd": {
        "capacity": "capacity_gb",
        "memory size": "capacity_gb",
        "rotational speed": "rpm",
        "form factor": "form_factor",
        "interface": "interface",
    },
    "motherboards": {
        "socket type": "socket",
        "cpu socket": "socket",
        "socket": "socket",
        "form factor": "form_factor",
        "chipset": "chipset",
        "memory slots": "ram_slots",
    },
    "power_supplies": {
        "power": "wattage",
        "output power": "wattage",
        "wattage": "wattage",
        "form factor": "form_factor",
        "efficiency": "efficiency",
    },
}

# Source labels for record_spec (match _VENDOR_API_SOURCES in spec_write_service)
_SOURCE_LABELS = {
    "digikey": "digikey_api",
    "nexar": "nexar_api",
    "mouser": "mouser_api",
}

# Regex for extracting numeric value + unit from strings like "100µF", "25V", "16GB"
_NUMERIC_RE = re.compile(r"^\s*([+-]?\d+(?:[.,]\d+)?(?:[eE][+-]?\d+)?)\s*([a-zA-Zµμ%Ω°/]+.*)?\s*$")


def _extract_numeric(value_str: str) -> tuple[float | None, str | None]:
    """Parse a string like '100µF' into (100.0, 'µF').

    Returns (None, None) if the string cannot be parsed as numeric.
    """
    if not value_str or not isinstance(value_str, str):
        return None, None

    value_str = value_str.strip()
    if not value_str:
        return None, None

    m = _NUMERIC_RE.match(value_str)
    if not m:
        return None, None

    num_str = m.group(1).replace(",", ".")
    try:
        num = float(num_str)
    except ValueError:
        return None, None

    unit = (m.group(2) or "").strip() or None
    return num, unit


def _apply_mapping(
    params: list[tuple[str, str]],
    mapping: dict[str, str],
    confidence: float,
) -> dict[str, dict]:
    """Apply a vendor→spec_key mapping to a list of (name, value) pairs.

    Returns {spec_key: {"value": ..., "confidence": ..., "unit": ...}, ...}
    """
    result: dict[str, dict] = {}
    for name, value in params:
        if not value or not name:
            continue
        key = name.lower().strip()
        spec_key = mapping.get(key)
        if not spec_key:
            continue
        # Don't overwrite if we already have this spec_key from a prior param
        if spec_key in result:
            continue

        num, unit = _extract_numeric(str(value))
        if num is not None:
            result[spec_key] = {"value": num, "confidence": confidence, "unit": unit}
        else:
            result[spec_key] = {"value": str(value).strip(), "confidence": confidence, "unit": None}

    return result


def parse_digikey_specs(raw_data: dict, category: str) -> dict[str, dict]:
    """Parse specs from DigiKey raw_data.

    DigiKey returns parameters as:
      raw_data["parameters"] = [{"parameter": "Name", "value": "Val"}, ...]
    """
    if not raw_data or not isinstance(raw_data, dict):
        return {}

    category = (category or "").lower().strip()
    mapping = _DIGIKEY_MAP.get(category)
    if not mapping:
        return {}

    parameters = raw_data.get("parameters") or raw_data.get("Parameters") or []
    if not isinstance(parameters, list):
        return {}

    params = []
    for p in parameters:
        name = p.get("parameter") or p.get("Parameter") or ""
        value = p.get("value") or p.get("Value") or ""
        params.append((name, value))

    return _apply_mapping(params, mapping, confidence=0.95)


def parse_nexar_specs(raw_data: dict, category: str) -> dict[str, dict]:
    """Parse specs from Nexar raw_data.

    Nexar returns specs as:
      raw_data["specs"] = [{"attribute": {"name": "..."}, "displayValue": "..."}, ...]
    """
    if not raw_data or not isinstance(raw_data, dict):
        return {}

    category = (category or "").lower().strip()
    mapping = _NEXAR_MAP.get(category)
    if not mapping:
        return {}

    specs = raw_data.get("specs") or []
    if not isinstance(specs, list):
        return {}

    params = []
    for s in specs:
        attr = s.get("attribute") or {}
        name = attr.get("name") or ""
        value = s.get("displayValue") or ""
        params.append((name, value))

    return _apply_mapping(params, mapping, confidence=0.95)


def parse_mouser_specs(raw_data: dict, category: str) -> dict[str, dict]:
    """Parse specs from Mouser raw_data.

    Mouser returns attributes as:
      raw_data["ProductAttributes"] = [{"AttributeName": "...", "AttributeValue": "..."}, ...]
    """
    if not raw_data or not isinstance(raw_data, dict):
        return {}

    category = (category or "").lower().strip()
    mapping = _MOUSER_MAP.get(category)
    if not mapping:
        return {}

    attributes = raw_data.get("ProductAttributes") or []
    if not isinstance(attributes, list):
        return {}

    params = []
    for a in attributes:
        name = a.get("AttributeName") or ""
        value = a.get("AttributeValue") or ""
        params.append((name, value))

    return _apply_mapping(params, mapping, confidence=0.95)


# Dispatch table: source_type → parser function
_PARSERS = {
    "digikey": parse_digikey_specs,
    "nexar": parse_nexar_specs,
    "mouser": parse_mouser_specs,
}


def enrich_card_from_sightings(db: Session, card_id: int) -> int:
    """Extract specs from vendor sightings and write them to the card.

    Looks at sightings for the given card where source_type is digikey/nexar/mouser and
    raw_data is not null. Parses specs and calls record_spec() for each.

    Returns the count of specs recorded.
    """
    card = db.get(MaterialCard, card_id)
    if card is None:
        logger.warning("enrich_card_from_sightings: card_id={} not found", card_id)
        return 0

    category = (card.category or "").lower().strip()
    if not category:
        logger.debug("enrich_card_from_sightings: card {} has no category", card_id)
        return 0

    # Pre-load schema cache for this commodity
    schema_cache = load_schema_cache(db, category)

    # Query sightings with vendor raw_data
    sightings = (
        db.query(Sighting)
        .filter(
            Sighting.material_card_id == card_id,
            Sighting.source_type.in_(list(_PARSERS.keys())),
            Sighting.raw_data.isnot(None),
        )
        .all()
    )

    if not sightings:
        logger.debug("enrich_card_from_sightings: no vendor sightings for card {}", card_id)
        return 0

    count = 0
    for sighting in sightings:
        parser = _PARSERS.get(sighting.source_type)
        if not parser:
            continue

        parsed = parser(sighting.raw_data, category)
        source_label = _SOURCE_LABELS.get(sighting.source_type, sighting.source_type)

        for spec_key, spec_data in parsed.items():
            record_spec(
                db,
                card_id,
                spec_key,
                spec_data["value"],
                source=source_label,
                confidence=spec_data["confidence"],
                unit=spec_data.get("unit"),
                schema_cache=schema_cache,
            )
            count += 1

    logger.info(
        "enrich_card_from_sightings: card={} wrote {} specs from {} sightings",
        card_id,
        count,
        len(sightings),
    )
    return count
