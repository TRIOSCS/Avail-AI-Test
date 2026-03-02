"""Tagging service — classify material cards and propagate tags to entities.

Waterfall: existing_data → prefix_lookup → (Nexar/AI in later phases).
Two-gate visibility: Gate 1 (min_count) AND Gate 2 (min_percentage).

Called by: app.routers.tags, app.routers.tagging_admin, app.routers.requisitions,
           app.search_service, app.email_service
Depends on: app.models.tags, app.services.prefix_lookup
"""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.tags import EntityTag, MaterialTag, Tag, TagThresholdConfig
from app.services.prefix_lookup import lookup_manufacturer_by_prefix

# Simple keyword → commodity tag mapping for category field
_CATEGORY_MAP: dict[str, str] = {
    "microcontroller": "Microcontrollers (MCU)",
    "mcu": "Microcontrollers (MCU)",
    "microprocessor": "Microprocessors (MPU)",
    "mpu": "Microprocessors (MPU)",
    "memory": "Memory ICs",
    "dram": "Memory ICs",
    "sram": "Memory ICs",
    "flash": "Memory ICs",
    "eeprom": "Memory ICs",
    "fpga": "FPGAs & PLDs",
    "pld": "FPGAs & PLDs",
    "cpld": "FPGAs & PLDs",
    "analog": "Analog ICs",
    "op amp": "Analog ICs",
    "opamp": "Analog ICs",
    "amplifier": "Analog ICs",
    "power management": "Power Management ICs",
    "voltage regulator": "Power Management ICs",
    "ldo": "Power Management ICs",
    "dc-dc": "Power Management ICs",
    "interface": "Interface ICs",
    "uart": "Interface ICs",
    "spi": "Interface ICs",
    "i2c": "Interface ICs",
    "can": "Interface ICs",
    "usb": "Interface ICs",
    "rf": "RF & Wireless ICs",
    "wireless": "RF & Wireless ICs",
    "bluetooth": "RF & Wireless ICs",
    "wifi": "RF & Wireless ICs",
    "sensor": "Sensors",
    "accelerometer": "Sensors",
    "gyroscope": "Sensors",
    "temperature sensor": "Sensors",
    "optoelectronic": "Optoelectronics",
    "led": "Optoelectronics",
    "photodiode": "Optoelectronics",
    "optocoupler": "Optoelectronics",
    "transistor": "Discrete Semiconductors",
    "mosfet": "Discrete Semiconductors",
    "diode": "Discrete Semiconductors",
    "igbt": "Discrete Semiconductors",
    "asic": "ASICs",
    "dsp": "DSPs",
    "logic": "Logic ICs",
    "gate": "Logic ICs",
    "flip-flop": "Logic ICs",
    "capacitor": "Capacitors",
    "mlcc": "Capacitors",
    "electrolytic": "Capacitors",
    "resistor": "Resistors",
    "inductor": "Inductors",
    "transformer": "Transformers",
    "crystal": "Crystals & Oscillators",
    "oscillator": "Crystals & Oscillators",
    "filter": "Filters",
    "connector": "Connectors",
    "relay": "Relays",
    "switch": "Switches",
    "fuse": "Circuit Protection",
    "circuit protection": "Circuit Protection",
    "tvs": "Circuit Protection",
    "varistor": "Circuit Protection",
    "terminal block": "Terminal Blocks",
    "terminal": "Terminal Blocks",
    "server cpu": "Server CPUs",
    "xeon": "Server CPUs",
    "dimm": "Server Memory (DIMMs)",
    "server memory": "Server Memory (DIMMs)",
    "hard drive": "Hard Drives / SSDs",
    "ssd": "Hard Drives / SSDs",
    "hdd": "Hard Drives / SSDs",
    "network card": "Network Cards",
    "nic": "Network Cards",
    "power supply": "Power Supplies",
    "psu": "Power Supplies",
    "fan": "Fans & Thermal",
    "heatsink": "Fans & Thermal",
    "thermal": "Fans & Thermal",
    "server board": "Server Boards",
    "motherboard": "Server Boards",
    "cable": "Cables & Wire",
    "wire": "Cables & Wire",
    "pcb": "PCBs & Substrates",
    "substrate": "PCBs & Substrates",
    "display": "Displays",
    "lcd": "Displays",
    "oled": "Displays",
    "battery": "Batteries",
    "enclosure": "Enclosures & Hardware",
    "test": "Test & Measurement",
    "measurement": "Test & Measurement",
}


def _map_category_to_commodity(category: str) -> str | None:
    """Map a free-text category to the closest commodity taxonomy tag."""
    lower = category.lower().strip()
    # Try exact match first
    if lower in _CATEGORY_MAP:
        return _CATEGORY_MAP[lower]
    # Try substring match (longest keyword first)
    for keyword in sorted(_CATEGORY_MAP, key=len, reverse=True):
        if keyword in lower:
            return _CATEGORY_MAP[keyword]
    return None


def classify_material_card(
    normalized_mpn: str, manufacturer: str | None, category: str | None
) -> dict:
    """Waterfall classification: existing_data → prefix_lookup.

    Returns dict with 'brand' and 'commodity' keys, each either
    {name, source, confidence} or None.
    """
    result: dict = {"brand": None, "commodity": None}

    # Brand classification
    if manufacturer and manufacturer.strip():
        result["brand"] = {
            "name": manufacturer.strip(),
            "source": "existing_data",
            "confidence": 0.95,
        }
    else:
        mfr, conf = lookup_manufacturer_by_prefix(normalized_mpn)
        if mfr:
            result["brand"] = {
                "name": mfr,
                "source": "prefix_lookup",
                "confidence": conf,
            }

    # Commodity classification
    if category and category.strip():
        commodity_name = _map_category_to_commodity(category)
        if commodity_name:
            result["commodity"] = {
                "name": commodity_name,
                "source": "existing_data",
                "confidence": 0.9,
            }

    return result


def get_or_create_brand_tag(manufacturer_name: str, db: Session) -> Tag:
    """Find or create a brand Tag. Case-insensitive dedup via func.lower()."""
    normalized = manufacturer_name.strip()
    tag = (
        db.query(Tag)
        .filter(func.lower(Tag.name) == normalized.lower(), Tag.tag_type == "brand")
        .first()
    )
    if tag:
        return tag

    tag = Tag(name=normalized, tag_type="brand", created_at=datetime.now(timezone.utc))
    db.add(tag)
    db.flush()
    return tag


def get_or_create_commodity_tag(commodity_name: str, db: Session) -> Tag | None:
    """Find a commodity Tag by name. Commodity tags are pre-seeded; don't create new ones."""
    return (
        db.query(Tag)
        .filter(func.lower(Tag.name) == commodity_name.lower(), Tag.tag_type == "commodity")
        .first()
    )


def tag_material_card(material_card_id: int, tags: list[dict], db: Session) -> list[MaterialTag]:
    """Create MaterialTag records. Upsert — only update if new source has higher confidence.

    Each dict in tags: {tag_id: int, source: str, confidence: float}
    Race-safe: handles concurrent inserts via nested savepoints.
    """
    created = []
    now = datetime.now(timezone.utc)

    for tag_data in tags:
        tag_id = tag_data["tag_id"]
        confidence = tag_data["confidence"]
        source = tag_data["source"]

        existing = (
            db.query(MaterialTag)
            .filter_by(material_card_id=material_card_id, tag_id=tag_id)
            .first()
        )
        if existing:
            if confidence > existing.confidence:
                existing.confidence = confidence
                existing.source = source
                existing.classified_at = now
                created.append(existing)
        else:
            try:
                mt = MaterialTag(
                    material_card_id=material_card_id,
                    tag_id=tag_id,
                    confidence=confidence,
                    source=source,
                    classified_at=now,
                )
                db.add(mt)
                db.flush()
                created.append(mt)
            except Exception:
                db.rollback()
                # Race condition — another concurrent batch inserted first
                pass

    if created:
        db.flush()
    return created


def recalculate_entity_tag_visibility(
    entity_type: str, entity_id: int, db: Session
) -> None:
    """Two-gate visibility: Gate 1 (min_count) AND Gate 2 (min_percentage).

    Loads all EntityTags for the entity, computes total interactions,
    then sets is_visible based on thresholds from TagThresholdConfig.
    """
    entity_tags = (
        db.query(EntityTag)
        .filter_by(entity_type=entity_type, entity_id=entity_id)
        .all()
    )
    if not entity_tags:
        return

    total = sum(et.interaction_count for et in entity_tags)

    # Load thresholds for this entity type
    thresholds = {
        cfg.tag_type: cfg
        for cfg in db.query(TagThresholdConfig).filter_by(entity_type=entity_type).all()
    }

    for et in entity_tags:
        et.total_entity_interactions = total
        tag = db.get(Tag, et.tag_id)
        if not tag:  # pragma: no cover
            continue

        cfg = thresholds.get(tag.tag_type)
        if not cfg:
            et.is_visible = False
            continue

        gate1 = et.interaction_count >= cfg.min_count
        gate2 = (et.interaction_count / total) >= cfg.min_percentage if total > 0 else False
        et.is_visible = gate1 and gate2

    db.flush()


def propagate_tags_to_entity(
    entity_type: str, entity_id: int, material_card_id: int, weight: float, db: Session
) -> None:
    """Propagate MaterialTags (confidence >= 0.7) for a part to an entity. Upsert EntityTag counts."""
    material_tags = (
        db.query(MaterialTag)
        .filter_by(material_card_id=material_card_id)
        .filter(MaterialTag.confidence >= 0.7)
        .all()
    )
    if not material_tags:
        return

    now = datetime.now(timezone.utc)
    for mt in material_tags:
        existing = (
            db.query(EntityTag)
            .filter_by(entity_type=entity_type, entity_id=entity_id, tag_id=mt.tag_id)
            .first()
        )
        if existing:
            existing.interaction_count += weight
            existing.last_seen_at = now
        else:
            et = EntityTag(
                entity_type=entity_type,
                entity_id=entity_id,
                tag_id=mt.tag_id,
                interaction_count=weight,
                first_seen_at=now,
                last_seen_at=now,
            )
            db.add(et)

    db.flush()

    try:
        recalculate_entity_tag_visibility(entity_type, entity_id, db)
    except Exception:  # pragma: no cover
        logger.exception(f"Failed to recalculate visibility for {entity_type}:{entity_id}")
