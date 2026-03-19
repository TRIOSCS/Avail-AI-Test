"""Commodity tree and schema seed data.

What: Defines the 2-level commodity taxonomy and provides schema seed data for
      commodity_spec_schemas table. Parent groups are display-only, sub-categories
      map to material_cards.category (lowercased).
Called by: startup.py (seed), faceted search UI (tree rendering)
Depends on: CommoditySpecSchema model
"""

import json
from pathlib import Path

from loguru import logger
from sqlalchemy.orm import Session

from app.models import CommoditySpecSchema

# 2-level tree: parent group (display only) -> list of sub-categories
# Sub-category keys MUST match material_cards.category values exactly (verified from production DB)
COMMODITY_TREE: dict[str, list[str]] = {
    "Passives": ["capacitors", "resistors", "inductors", "transformers", "fuses", "oscillators", "filters"],
    "Semiconductors \u2014 Discrete": ["diodes", "transistors", "mosfets", "thyristors"],
    "Semiconductors \u2014 ICs": ["analog_ic", "logic_ic", "power_ic"],
    "Processors & Programmable": ["microcontrollers", "cpu", "microprocessors", "dsp", "fpga", "asic", "gpu"],
    "Memory & Storage": ["dram", "flash", "ssd", "hdd"],
    "Connectors & Electromechanical": ["connectors", "cables", "relays", "switches", "sockets"],
    "Power & Energy": ["power_supplies", "voltage_regulators", "batteries"],
    "Optoelectronics & Display": ["leds", "displays", "optoelectronics"],
    "Sensors & RF": ["sensors", "rf"],
    "IT / Server Hardware": [
        "motherboards",
        "network_cards",
        "raid_controllers",
        "server_chassis",
        "fans_cooling",
        "networking",
    ],
    "Misc": ["motors", "enclosures", "tools_accessories", "other"],
}

# Display names for sub-categories (for UI rendering)
_DISPLAY_NAMES: dict[str, str] = {
    "capacitors": "Capacitors",
    "resistors": "Resistors",
    "inductors": "Inductors",
    "transformers": "Transformers",
    "fuses": "Fuses",
    "oscillators": "Oscillators",
    "filters": "Filters",
    "diodes": "Diodes",
    "transistors": "Transistors",
    "mosfets": "MOSFETs",
    "thyristors": "Thyristors",
    "analog_ic": "Analog ICs",
    "logic_ic": "Logic ICs",
    "power_ic": "Power Management ICs",
    "microcontrollers": "Microcontrollers",
    "cpu": "CPUs",
    "microprocessors": "Microprocessors",
    "dsp": "DSP",
    "fpga": "FPGAs",
    "asic": "ASIC",
    "gpu": "GPU",
    "dram": "DRAM",
    "flash": "Flash",
    "ssd": "SSD",
    "hdd": "HDD",
    "connectors": "Connectors",
    "cables": "Cables",
    "relays": "Relays",
    "switches": "Switches",
    "sockets": "Sockets",
    "power_supplies": "Power Supplies",
    "voltage_regulators": "Voltage Regulators",
    "batteries": "Batteries",
    "leds": "LEDs",
    "displays": "Displays",
    "optoelectronics": "Optoelectronics",
    "sensors": "Sensors",
    "rf": "RF & Wireless",
    "motherboards": "Motherboards",
    "network_cards": "Network Cards",
    "raid_controllers": "RAID Controllers",
    "server_chassis": "Server Chassis",
    "fans_cooling": "Fans & Cooling",
    "networking": "Networking",
    "motors": "Motors",
    "enclosures": "Enclosures",
    "tools_accessories": "Tools & Accessories",
    "other": "Other",
}

# Reverse lookup: sub-category -> parent group
_PARENT_LOOKUP: dict[str, str] = {}
for _group, _subs in COMMODITY_TREE.items():
    for _sub in _subs:
        _PARENT_LOOKUP[_sub] = _group


def get_all_commodities() -> list[str]:
    """Return flat list of all sub-category keys."""
    result = []
    for subs in COMMODITY_TREE.values():
        result.extend(subs)
    return result


def get_parent_group(commodity: str) -> str:
    """Return the parent group name for a commodity, or 'Misc' if unknown."""
    return _PARENT_LOOKUP.get(commodity.lower().strip(), "Misc")


def get_display_name(commodity: str) -> str:
    """Return human-readable display name for a commodity key."""
    return _DISPLAY_NAMES.get(commodity.lower().strip(), commodity.title())


# Schema seed data loaded from JSON (was inline ~490 lines of Python dicts)
_SEEDS_PATH = Path(__file__).resolve().parent.parent / "data" / "commodity_seeds.json"


def _load_commodity_seeds() -> dict[str, list[dict]]:
    """Load commodity spec seed data from JSON file."""
    with open(_SEEDS_PATH) as f:
        return json.load(f)


COMMODITY_SPEC_SEEDS: dict[str, list[dict]] = _load_commodity_seeds()


def get_batch_spec_schema() -> dict[str, dict]:
    """Convert COMMODITY_SPEC_SEEDS to the format used by enrich_specs_batch.py.

    Returns: {category: {"specs": [{"key", "label", "type", "values"?, "unit"?}, ...]}}
    """
    result = {}
    for commodity, seeds in COMMODITY_SPEC_SEEDS.items():
        specs = []
        for seed in seeds:
            spec = {
                "key": seed["spec_key"],
                "label": seed["display_name"],
                "type": seed["data_type"],
            }
            if seed.get("enum_values"):
                spec["values"] = ", ".join(seed["enum_values"])
            if seed.get("canonical_unit"):
                spec["canonical_unit"] = seed["canonical_unit"]
            if seed.get("unit"):
                spec["unit_hint"] = seed["unit"]  # For display in prompts
            specs.append(spec)
        result[commodity] = {"specs": specs}
    return result


def seed_commodity_schemas(db: Session) -> int:
    """Seed commodity_spec_schemas table. Idempotent -- skips existing rows.

    Uses a single query to find existing (commodity, spec_key) pairs, then bulk-inserts
    only the missing ones.

    Returns number of rows inserted.
    """
    # Collect all commodities referenced in seeds
    seed_commodities = list(COMMODITY_SPEC_SEEDS.keys())

    # Single query: get all existing (commodity, spec_key) pairs for seeded commodities
    existing_rows = (
        db.query(CommoditySpecSchema.commodity, CommoditySpecSchema.spec_key)
        .filter(CommoditySpecSchema.commodity.in_(seed_commodities))
        .all()
    )
    existing_pairs: set[tuple[str, str]] = {(row[0], row[1]) for row in existing_rows}

    # Bulk-insert only missing entries
    inserted = 0
    for commodity, specs in COMMODITY_SPEC_SEEDS.items():
        for spec in specs:
            if (commodity, spec["spec_key"]) in existing_pairs:
                continue

            row = CommoditySpecSchema(
                commodity=commodity,
                spec_key=spec["spec_key"],
                display_name=spec["display_name"],
                data_type=spec["data_type"],
                unit=spec.get("unit"),
                canonical_unit=spec.get("canonical_unit"),
                enum_values=spec.get("enum_values"),
                numeric_range=spec.get("numeric_range"),
                sort_order=spec.get("sort_order", 0),
                is_filterable=spec.get("is_filterable", True),
                is_primary=spec.get("is_primary", False),
            )
            db.add(row)
            inserted += 1

    if inserted:
        db.commit()
        logger.info("Seeded {} commodity_spec_schemas rows", inserted)
    return inserted
