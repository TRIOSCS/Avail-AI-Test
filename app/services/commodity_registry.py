"""Commodity tree and schema seed data.

What: Defines the 2-level commodity taxonomy and provides schema seed data for
      commodity_spec_schemas table. Parent groups are display-only, sub-categories
      map to material_cards.category (lowercased).
Called by: startup.py (seed), faceted search UI (tree rendering)
Depends on: CommoditySpecSchema model
"""

from loguru import logger
from sqlalchemy.orm import Session

from app.models import CommoditySpecSchema

# 2-level tree: parent group (display only) → list of sub-categories
# Sub-category keys MUST match material_cards.category values exactly (verified from production DB)
COMMODITY_TREE: dict[str, list[str]] = {
    "Passives": ["capacitors", "resistors", "inductors", "transformers", "fuses", "oscillators", "filters"],
    "Semiconductors — Discrete": ["diodes", "transistors", "mosfets", "thyristors"],
    "Semiconductors — ICs": ["analog_ic", "logic_ic", "power_ic"],
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

# Reverse lookup: sub-category → parent group
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


# Schema seed data: specs for the top 16 commodities
# Each entry becomes rows in commodity_spec_schemas
COMMODITY_SPEC_SEEDS: dict[str, list[dict]] = {
    "dram": [
        {
            "spec_key": "ddr_type",
            "display_name": "DDR Type",
            "data_type": "enum",
            "enum_values": ["DDR3", "DDR4", "DDR5", "DDR5X", "LPDDR4", "LPDDR5"],
            "sort_order": 1,
            "is_primary": True,
        },
        {
            "spec_key": "capacity_gb",
            "display_name": "Capacity (GB)",
            "data_type": "numeric",
            "unit": "GB",
            "canonical_unit": "GB",
            "numeric_range": {"min": 1, "max": 256},
            "sort_order": 2,
            "is_primary": True,
        },
        {
            "spec_key": "speed_mhz",
            "display_name": "Speed (MHz)",
            "data_type": "numeric",
            "unit": "MHz",
            "canonical_unit": "MHz",
            "numeric_range": {"min": 800, "max": 8400},
            "sort_order": 3,
        },
        {"spec_key": "ecc", "display_name": "ECC", "data_type": "boolean", "sort_order": 4},
        {
            "spec_key": "form_factor",
            "display_name": "Form Factor",
            "data_type": "enum",
            "enum_values": ["DIMM", "SO-DIMM", "UDIMM", "RDIMM", "LRDIMM"],
            "sort_order": 5,
        },
    ],
    "capacitors": [
        {
            "spec_key": "capacitance",
            "display_name": "Capacitance",
            "data_type": "numeric",
            "unit": "pF",
            "canonical_unit": "pF",
            "numeric_range": {"min": 0.1, "max": 1000000000000},
            "sort_order": 1,
            "is_primary": True,
        },
        {
            "spec_key": "voltage_rating",
            "display_name": "Voltage Rating (V)",
            "data_type": "numeric",
            "unit": "V",
            "canonical_unit": "V",
            "numeric_range": {"min": 1, "max": 10000},
            "sort_order": 2,
            "is_primary": True,
        },
        {
            "spec_key": "dielectric",
            "display_name": "Dielectric",
            "data_type": "enum",
            "enum_values": ["X7R", "X5R", "C0G", "Y5V", "NP0"],
            "sort_order": 3,
        },
        {
            "spec_key": "tolerance",
            "display_name": "Tolerance",
            "data_type": "enum",
            "enum_values": ["±1%", "±5%", "±10%", "±20%"],
            "sort_order": 4,
        },
        {
            "spec_key": "package",
            "display_name": "Package",
            "data_type": "enum",
            "enum_values": ["0402", "0603", "0805", "1206", "1210", "through-hole"],
            "sort_order": 5,
        },
    ],
    "resistors": [
        {
            "spec_key": "resistance",
            "display_name": "Resistance",
            "data_type": "numeric",
            "unit": "ohms",
            "canonical_unit": "ohms",
            "sort_order": 1,
            "is_primary": True,
        },
        {
            "spec_key": "power_rating",
            "display_name": "Power Rating (W)",
            "data_type": "numeric",
            "unit": "W",
            "canonical_unit": "W",
            "sort_order": 2,
        },
        {
            "spec_key": "tolerance",
            "display_name": "Tolerance",
            "data_type": "enum",
            "enum_values": ["0.1%", "1%", "5%"],
            "sort_order": 3,
        },
        {
            "spec_key": "package",
            "display_name": "Package",
            "data_type": "enum",
            "enum_values": ["0402", "0603", "0805", "1206", "through-hole"],
            "sort_order": 4,
        },
    ],
    "hdd": [
        {
            "spec_key": "capacity_gb",
            "display_name": "Capacity (GB)",
            "data_type": "numeric",
            "unit": "GB",
            "canonical_unit": "GB",
            "sort_order": 1,
            "is_primary": True,
        },
        {
            "spec_key": "rpm",
            "display_name": "RPM",
            "data_type": "enum",
            "enum_values": ["5400", "7200", "10000", "15000"],
            "sort_order": 2,
        },
        {
            "spec_key": "form_factor",
            "display_name": "Form Factor",
            "data_type": "enum",
            "enum_values": ['2.5"', '3.5"'],
            "sort_order": 3,
        },
        {
            "spec_key": "interface",
            "display_name": "Interface",
            "data_type": "enum",
            "enum_values": ["SATA", "SAS", "NVMe"],
            "sort_order": 4,
        },
    ],
    "ssd": [
        {
            "spec_key": "capacity_gb",
            "display_name": "Capacity (GB)",
            "data_type": "numeric",
            "unit": "GB",
            "canonical_unit": "GB",
            "sort_order": 1,
            "is_primary": True,
        },
        {
            "spec_key": "form_factor",
            "display_name": "Form Factor",
            "data_type": "enum",
            "enum_values": ['2.5"', "M.2", "U.2", "mSATA"],
            "sort_order": 2,
        },
        {
            "spec_key": "interface",
            "display_name": "Interface",
            "data_type": "enum",
            "enum_values": ["SATA", "NVMe", "SAS"],
            "sort_order": 3,
        },
        {
            "spec_key": "read_speed_mbps",
            "display_name": "Read Speed (MB/s)",
            "data_type": "numeric",
            "unit": "MB/s",
            "canonical_unit": "MB/s",
            "sort_order": 4,
        },
    ],
    "connectors": [
        {
            "spec_key": "pin_count",
            "display_name": "Pin Count",
            "data_type": "numeric",
            "unit": "pins",
            "canonical_unit": "pins",
            "sort_order": 1,
            "is_primary": True,
        },
        {
            "spec_key": "pitch_mm",
            "display_name": "Pitch (mm)",
            "data_type": "numeric",
            "unit": "mm",
            "canonical_unit": "mm",
            "sort_order": 2,
        },
        {
            "spec_key": "mounting",
            "display_name": "Mounting",
            "data_type": "enum",
            "enum_values": ["through-hole", "SMD", "press-fit"],
            "sort_order": 3,
        },
        {
            "spec_key": "gender",
            "display_name": "Gender",
            "data_type": "enum",
            "enum_values": ["male", "female", "genderless"],
            "sort_order": 4,
        },
        {"spec_key": "series", "display_name": "Series", "data_type": "enum", "sort_order": 5},
    ],
    "motherboards": [
        {
            "spec_key": "socket",
            "display_name": "CPU Socket",
            "data_type": "enum",
            "enum_values": ["LGA1700", "AM5", "LGA4677", "LGA1151", "LGA2066", "SP3"],
            "sort_order": 1,
            "is_primary": True,
        },
        {
            "spec_key": "form_factor",
            "display_name": "Form Factor",
            "data_type": "enum",
            "enum_values": ["ATX", "mATX", "EATX", "Mini-ITX"],
            "sort_order": 2,
        },
        {"spec_key": "chipset", "display_name": "Chipset", "data_type": "enum", "sort_order": 3},
        {
            "spec_key": "ram_slots",
            "display_name": "RAM Slots",
            "data_type": "numeric",
            "unit": "slots",
            "canonical_unit": "slots",
            "numeric_range": {"min": 1, "max": 16},
            "sort_order": 4,
        },
    ],
    "cpu": [
        {"spec_key": "socket", "display_name": "Socket", "data_type": "enum", "sort_order": 1, "is_primary": True},
        {
            "spec_key": "core_count",
            "display_name": "Core Count",
            "data_type": "numeric",
            "unit": "cores",
            "canonical_unit": "cores",
            "sort_order": 2,
            "is_primary": True,
        },
        {
            "spec_key": "clock_speed_ghz",
            "display_name": "Clock Speed (GHz)",
            "data_type": "numeric",
            "unit": "GHz",
            "canonical_unit": "GHz",
            "sort_order": 3,
        },
        {
            "spec_key": "tdp_watts",
            "display_name": "TDP (W)",
            "data_type": "numeric",
            "unit": "W",
            "canonical_unit": "W",
            "sort_order": 4,
        },
        {"spec_key": "architecture", "display_name": "Architecture", "data_type": "enum", "sort_order": 5},
    ],
    "power_supplies": [
        {
            "spec_key": "wattage",
            "display_name": "Wattage (W)",
            "data_type": "numeric",
            "unit": "W",
            "canonical_unit": "W",
            "sort_order": 1,
            "is_primary": True,
        },
        {
            "spec_key": "form_factor",
            "display_name": "Form Factor",
            "data_type": "enum",
            "enum_values": ["ATX", "SFX", "1U server", "2U server", "redundant"],
            "sort_order": 2,
        },
        {
            "spec_key": "efficiency",
            "display_name": "Efficiency",
            "data_type": "enum",
            "enum_values": ["80+ Bronze", "80+ Silver", "80+ Gold", "80+ Platinum", "80+ Titanium"],
            "sort_order": 3,
        },
    ],
    "gpu": [
        {
            "spec_key": "memory_gb",
            "display_name": "Memory (GB)",
            "data_type": "numeric",
            "unit": "GB",
            "canonical_unit": "GB",
            "sort_order": 1,
            "is_primary": True,
        },
        {
            "spec_key": "memory_type",
            "display_name": "Memory Type",
            "data_type": "enum",
            "enum_values": ["GDDR5", "GDDR6", "GDDR6X", "HBM2", "HBM3"],
            "sort_order": 2,
        },
        {
            "spec_key": "interface",
            "display_name": "Interface",
            "data_type": "enum",
            "enum_values": ["PCIe 3.0", "PCIe 4.0", "PCIe 5.0"],
            "sort_order": 3,
        },
    ],
    "inductors": [
        {
            "spec_key": "inductance",
            "display_name": "Inductance",
            "data_type": "numeric",
            "unit": "nH",
            "canonical_unit": "nH",
            "sort_order": 1,
            "is_primary": True,
        },
        {
            "spec_key": "current_rating",
            "display_name": "Current Rating (A)",
            "data_type": "numeric",
            "unit": "A",
            "canonical_unit": "A",
            "sort_order": 2,
        },
        {"spec_key": "package", "display_name": "Package", "data_type": "enum", "sort_order": 3},
    ],
    "diodes": [
        {
            "spec_key": "type",
            "display_name": "Type",
            "data_type": "enum",
            "enum_values": ["rectifier", "zener", "Schottky", "TVS"],
            "sort_order": 1,
            "is_primary": True,
        },
        {
            "spec_key": "voltage",
            "display_name": "Voltage (V)",
            "data_type": "numeric",
            "unit": "V",
            "canonical_unit": "V",
            "sort_order": 2,
        },
        {
            "spec_key": "current",
            "display_name": "Current (A)",
            "data_type": "numeric",
            "unit": "A",
            "canonical_unit": "A",
            "sort_order": 3,
        },
        {"spec_key": "package", "display_name": "Package", "data_type": "enum", "sort_order": 4},
    ],
    "mosfets": [
        {
            "spec_key": "channel_type",
            "display_name": "Channel",
            "data_type": "enum",
            "enum_values": ["N-channel", "P-channel"],
            "sort_order": 1,
            "is_primary": True,
        },
        {
            "spec_key": "vds",
            "display_name": "Vds (V)",
            "data_type": "numeric",
            "unit": "V",
            "canonical_unit": "V",
            "sort_order": 2,
        },
        {
            "spec_key": "rds_on",
            "display_name": "Rds(on) (mΩ)",
            "data_type": "numeric",
            "unit": "mOhm",
            "canonical_unit": "mOhm",
            "sort_order": 3,
        },
        {
            "spec_key": "id_max",
            "display_name": "Id max (A)",
            "data_type": "numeric",
            "unit": "A",
            "canonical_unit": "A",
            "sort_order": 4,
        },
        {"spec_key": "package", "display_name": "Package", "data_type": "enum", "sort_order": 5},
    ],
    "microcontrollers": [
        {
            "spec_key": "core",
            "display_name": "Core",
            "data_type": "enum",
            "enum_values": ["ARM Cortex-M0", "Cortex-M3", "Cortex-M4", "Cortex-M7", "RISC-V", "AVR", "PIC"],
            "sort_order": 1,
            "is_primary": True,
        },
        {
            "spec_key": "flash_kb",
            "display_name": "Flash (KB)",
            "data_type": "numeric",
            "unit": "KB",
            "canonical_unit": "KB",
            "sort_order": 2,
        },
        {
            "spec_key": "ram_kb",
            "display_name": "RAM (KB)",
            "data_type": "numeric",
            "unit": "KB",
            "canonical_unit": "KB",
            "sort_order": 3,
        },
        {
            "spec_key": "clock_mhz",
            "display_name": "Clock (MHz)",
            "data_type": "numeric",
            "unit": "MHz",
            "canonical_unit": "MHz",
            "sort_order": 4,
        },
        {"spec_key": "package", "display_name": "Package", "data_type": "enum", "sort_order": 5},
    ],
    "network_cards": [
        {
            "spec_key": "speed",
            "display_name": "Speed",
            "data_type": "enum",
            "enum_values": ["1GbE", "10GbE", "25GbE", "40GbE", "100GbE"],
            "sort_order": 1,
            "is_primary": True,
        },
        {
            "spec_key": "ports",
            "display_name": "Ports",
            "data_type": "numeric",
            "unit": "ports",
            "canonical_unit": "ports",
            "numeric_range": {"min": 1, "max": 8},
            "sort_order": 2,
        },
        {
            "spec_key": "interface",
            "display_name": "Interface",
            "data_type": "enum",
            "enum_values": ["PCIe", "OCP", "LOM"],
            "sort_order": 3,
        },
        {
            "spec_key": "controller",
            "display_name": "Controller",
            "data_type": "enum",
            "enum_values": ["Intel", "Broadcom", "Mellanox"],
            "sort_order": 4,
        },
    ],
    "flash": [
        {
            "spec_key": "capacity_gb",
            "display_name": "Capacity (GB)",
            "data_type": "numeric",
            "unit": "GB",
            "canonical_unit": "GB",
            "sort_order": 1,
            "is_primary": True,
        },
        {
            "spec_key": "interface",
            "display_name": "Interface",
            "data_type": "enum",
            "enum_values": ["SPI", "NAND", "NOR", "eMMC", "UFS"],
            "sort_order": 2,
        },
        {"spec_key": "package", "display_name": "Package", "data_type": "enum", "sort_order": 3},
    ],
}


def seed_commodity_schemas(db: Session) -> int:
    """Seed commodity_spec_schemas table. Idempotent — skips existing rows.

    Returns number of rows inserted.
    """
    inserted = 0
    for commodity, specs in COMMODITY_SPEC_SEEDS.items():
        for spec in specs:
            existing = db.query(CommoditySpecSchema).filter_by(commodity=commodity, spec_key=spec["spec_key"]).first()
            if existing:
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
