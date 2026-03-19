"""Specialty detector — identifies vendor brand/commodity specialties.

Uses keyword matching on email history, sighting data, and offer history to generate
brand_tags and commodity_tags for vendor cards.
"""

import re
from collections import Counter

# ── Known electronic component brands (~100) ─────────────────────────

BRAND_LIST = [
    # Major semiconductor companies
    "Intel",
    "AMD",
    "Nvidia",
    "Qualcomm",
    "Broadcom",
    "Texas Instruments",
    "NXP",
    "STMicroelectronics",
    "Infineon",
    "Microchip",
    "Analog Devices",
    "ON Semiconductor",
    "Renesas",
    "Marvell",
    "MediaTek",
    "Xilinx",
    "Lattice",
    "Cypress",
    "Maxim",
    "Linear Technology",
    # Server/Systems OEMs
    "IBM",
    "Dell",
    "HP",
    "HPE",
    "Lenovo",
    "Cisco",
    "Juniper",
    "Arista",
    "NetApp",
    "EMC",
    "Oracle",
    "Sun",
    # Memory
    "Samsung",
    "SK Hynix",
    "Micron",
    "Kingston",
    "Crucial",
    "Western Digital",
    "Seagate",
    "SanDisk",
    "Toshiba",
    "Kioxia",
    # Passive components
    "Murata",
    "TDK",
    "Yageo",
    "Vishay",
    "Kemet",
    "AVX",
    "Panasonic",
    "Nichicon",
    "Rubycon",
    "Bourns",
    "Ohmite",
    # Connectors & electromechanical
    "TE Connectivity",
    "Amphenol",
    "Molex",
    "JAE",
    "Hirose",
    "ITT",
    "Smiths Interconnect",
    "3M",
    "Phoenix Contact",
    # Power
    "Mean Well",
    "Delta Electronics",
    "Vicor",
    "Artesyn",
    "Lambda",
    "TDK-Lambda",
    "Cosel",
    "Bel Fuse",
    # Optoelectronics
    "Osram",
    "Lumileds",
    "Cree",
    "Broadcom",
    "Hamamatsu",
    # RF & wireless
    "Qorvo",
    "Skyworks",
    "MACOM",
    "Mini-Circuits",
    # Automotive
    "Bosch",
    "Continental",
    "Denso",
    # Test & measurement
    "Keysight",
    "Tektronix",
    "National Instruments",
    # PCB & assembly
    "JLCPCB",
    "PCBWay",
    # Other notable brands
    "Arrow",
    "Avnet",
    "Digi-Key",
    "Mouser",
    "Future Electronics",
    "Heilind",
    "TTI",
    "Sager",
    "Newark",
    "Farnell",
    "RS Components",
]

# Compile brand patterns (case-insensitive, word-boundary)
_BRAND_PATTERNS = {}
for brand in BRAND_LIST:
    # Escape special regex chars and create word-boundary pattern
    escaped = re.escape(brand)
    _BRAND_PATTERNS[brand] = re.compile(rf"\b{escaped}\b", re.IGNORECASE)

# ── Commodity category mapping ───────────────────────────────────────

COMMODITY_MAP = {
    # ── Passives ──────────────────────────────────────────────────────
    "capacitors": ["capacitor", "mlcc", "electrolytic", "tantalum", "ceramic cap", "supercap"],
    "resistors": ["resistor", "potentiometer", "thermistor", "varistor", "ntc", "ptc"],
    "inductors": ["inductor", "choke", "ferrite", "coil"],
    "transformers": ["transformer", "current transformer", "isolation transformer"],
    "fuses": ["fuse", "ptc fuse", "circuit breaker", "fusible"],
    "oscillators": ["oscillator", "crystal", "resonator", "clock generator", "tcxo", "vcxo"],
    "filters": ["emi filter", "common mode", "ferrite bead", "lc filter", "band pass"],
    # ── Semiconductors — Discrete ─────────────────────────────────────
    "diodes": ["diode", "rectifier", "zener", "schottky", "tvs", "led diode"],
    "transistors": ["transistor", "bjt", "jfet", "darlington"],
    "mosfets": ["mosfet", "igbt", "fet", "power mosfet"],
    "thyristors": ["thyristor", "scr", "triac", "diac"],
    # ── Semiconductors — ICs ──────────────────────────────────────────
    "analog_ic": ["op-amp", "opamp", "adc", "dac", "comparator", "amplifier", "voltage reference"],
    "logic_ic": ["gate", "flip-flop", "multiplexer", "decoder", "buffer", "logic ic"],
    "power_ic": ["voltage regulator", "dc-dc", "ldo", "pmic", "power management"],
    # ── Processors & Programmable ─────────────────────────────────────
    "microcontrollers": ["mcu", "microcontroller", "stm32", "pic", "avr", "esp32", "arduino"],
    "microprocessors": ["microprocessor", "mpu", "application processor"],
    "cpu": ["cpu", "processor", "xeon", "epyc", "core i", "ryzen", "threadripper"],
    "dsp": ["dsp", "digital signal processor", "codec"],
    "fpga": ["fpga", "cpld", "programmable logic", "spartan", "virtex", "cyclone", "stratix"],
    "asic": ["asic", "custom ic", "gate array"],
    "gpu": ["gpu", "graphics", "geforce", "radeon", "quadro", "tesla"],
    # ── Memory & Storage ──────────────────────────────────────────────
    "dram": ["ddr", "sdram", "dimm", "rdimm", "lrdimm", "sodimm", "ecc", "ram", "memory module", "dram"],
    "flash": ["nand", "nor flash", "eeprom", "flash memory", "emmc"],
    "ssd": ["ssd", "solid state drive", "nvme drive", "m.2 drive"],
    "hdd": ["hdd", "hard drive", "hard disk", "sata drive", "sas drive"],
    # ── Connectors & Electromechanical ────────────────────────────────
    "connectors": ["connector", "header", "plug", "jack", "terminal", "backplane"],
    "cables": ["cable", "wire", "harness", "ribbon", "coaxial", "fiber optic"],
    "relays": ["relay", "contactor"],
    "switches": ["switch", "toggle", "pushbutton", "dip switch", "rocker"],
    "sockets": ["socket", "ic socket", "cpu socket", "zif socket"],
    # ── Optoelectronics & Display ─────────────────────────────────────
    "leds": ["led", "oled", "backlight", "indicator"],
    "displays": ["display", "lcd", "tft", "oled display", "7-segment"],
    "optoelectronics": ["optocoupler", "photodiode", "phototransistor", "laser diode", "fiber transceiver"],
    # ── Sensors & RF ──────────────────────────────────────────────────
    "sensors": ["sensor", "accelerometer", "gyroscope", "temperature sensor", "pressure sensor"],
    "rf": ["rf", "antenna", "rf filter", "rf mixer", "rf amplifier", "wireless module"],
    # ── Power & Energy ────────────────────────────────────────────────
    "power_supplies": ["power supply", "psu", "ups", "inverter", "converter", "charger"],
    "voltage_regulators": ["voltage regulator", "linear regulator", "switching regulator"],
    "batteries": ["battery", "lithium", "nimh", "battery charger", "battery pack"],
    # ── IT / Server Hardware ──────────────────────────────────────────
    "motherboards": ["motherboard", "mainboard", "system board", "server board"],
    "network_cards": ["nic", "network card", "ethernet adapter", "hba", "fiber channel"],
    "raid_controllers": ["raid", "raid controller", "storage controller"],
    "server_chassis": ["server", "blade", "rack", "chassis", "server chassis"],
    "fans_cooling": ["fan", "heatsink", "cooling", "blower", "thermal pad"],
    # ── Networking ────────────────────────────────────────────────────
    "networking": ["switch", "router", "firewall", "sfp", "transceiver module", "ethernet"],
    # ── Misc ──────────────────────────────────────────────────────────
    "motors": ["motor", "stepper", "servo", "actuator"],
    "enclosures": ["enclosure", "housing", "case", "rackmount"],
    "tools_accessories": ["tool", "accessory", "test probe", "solder"],
    "other": [],
}

# Slug → display name mapping for commodity tags stored on VendorCard/Company
COMMODITY_DISPLAY_NAMES: dict[str, str] = {
    # Passives
    "capacitors": "Capacitors",
    "resistors": "Resistors",
    "inductors": "Inductors",
    "transformers": "Transformers",
    "fuses": "Fuses",
    "oscillators": "Oscillators & Crystals",
    "filters": "Filters",
    # Semiconductors — Discrete
    "diodes": "Diodes",
    "transistors": "Transistors",
    "mosfets": "MOSFETs & IGBTs",
    "thyristors": "Thyristors",
    # Semiconductors — ICs
    "analog_ic": "Analog ICs",
    "logic_ic": "Logic ICs",
    "power_ic": "Power Management ICs",
    # Processors & Programmable
    "microcontrollers": "Microcontrollers",
    "microprocessors": "Microprocessors",
    "cpu": "CPUs & Processors",
    "dsp": "DSPs",
    "fpga": "FPGAs & PLDs",
    "asic": "ASICs",
    "gpu": "GPUs",
    # Memory & Storage
    "dram": "DRAM & Memory Modules",
    "flash": "Flash Memory",
    "ssd": "SSDs",
    "hdd": "Hard Drives",
    # Connectors & Electromechanical
    "connectors": "Connectors",
    "cables": "Cables & Wire",
    "relays": "Relays",
    "switches": "Switches",
    "sockets": "Sockets",
    # Optoelectronics & Display
    "leds": "LEDs",
    "displays": "Displays",
    "optoelectronics": "Optoelectronics",
    # Sensors & RF
    "sensors": "Sensors",
    "rf": "RF & Wireless",
    # Power & Energy
    "power_supplies": "Power Supplies",
    "voltage_regulators": "Voltage Regulators",
    "batteries": "Batteries",
    # IT / Server Hardware
    "motherboards": "Motherboards",
    "network_cards": "Network Cards",
    "raid_controllers": "RAID Controllers",
    "server_chassis": "Server Chassis",
    "fans_cooling": "Fans & Cooling",
    # Networking
    "networking": "Networking",
    # Misc
    "motors": "Motors & Actuators",
    "enclosures": "Enclosures",
    "tools_accessories": "Tools & Accessories",
    "other": "Other",
}


def commodity_slug_to_display(slug: str) -> str:
    """Convert a commodity slug (e.g. 'capacitors') to display name (e.g. 'Capacitors').

    Falls back to title-casing the slug with underscores replaced by spaces.
    """
    return COMMODITY_DISPLAY_NAMES.get(slug, slug.replace("_", " ").title())


# Flatten keywords for quick lookup
_COMMODITY_KEYWORDS = {}
for category, keywords in COMMODITY_MAP.items():
    for kw in keywords:
        _COMMODITY_KEYWORDS[kw.lower()] = category


def detect_brands_from_text(text: str) -> list[str]:
    """Scan text for brand mentions.

    Returns list of matched brand names.
    """
    if not text:
        return []

    found = []
    for brand, pattern in _BRAND_PATTERNS.items():
        if pattern.search(text):
            found.append(brand)
    return found


def detect_commodities_from_text(text: str) -> list[str]:
    """Scan text for commodity category signals.

    Returns unique category list.
    """
    if not text:
        return []

    text_lower = text.lower()
    categories = set()

    for keyword, category in _COMMODITY_KEYWORDS.items():
        if keyword in text_lower:
            categories.add(category)

    return sorted(categories)


def analyze_vendor_specialties(vendor_card_id: int, db) -> dict:
    """Aggregate brand/commodity tags from email content, sightings, and offers.

    Returns: {brand_tags: [...], commodity_tags: [...], confidence: float}
    """
    from ..models import Offer, Sighting, VendorCard

    card = db.get(VendorCard, vendor_card_id)
    if not card:
        return {"brand_tags": [], "commodity_tags": [], "confidence": 0.0}

    brand_counter = Counter()
    commodity_counter = Counter()

    # 1. From sightings (part data)
    sightings = (
        db.query(Sighting.manufacturer, Sighting.mpn_matched)
        .filter(Sighting.vendor_name == card.display_name)
        .limit(500)
        .all()
    )
    for s in sightings:
        if s.manufacturer:
            brands = detect_brands_from_text(s.manufacturer)
            for b in brands:
                brand_counter[b] += 1
        if s.mpn_matched:
            commodities = detect_commodities_from_text(s.mpn_matched)
            for c in commodities:
                commodity_counter[c] += 1

    # 2. From offers
    offers = db.query(Offer.manufacturer, Offer.mpn).filter(Offer.vendor_card_id == vendor_card_id).limit(500).all()
    for o in offers:
        if o.manufacturer:
            brands = detect_brands_from_text(o.manufacturer)
            for b in brands:
                brand_counter[b] += 2  # Weight offers more
        if o.mpn:
            commodities = detect_commodities_from_text(o.mpn)
            for c in commodities:
                commodity_counter[c] += 1

    # 3. From existing vendor card data
    for field in [card.display_name, card.industry or ""]:
        brands = detect_brands_from_text(field)
        for b in brands:
            brand_counter[b] += 1

    # Top results
    brand_tags = [b for b, _ in brand_counter.most_common(15)]
    commodity_tags = [c for c, _ in commodity_counter.most_common(10)]

    total = sum(brand_counter.values()) + sum(commodity_counter.values())
    confidence = min(0.3 + (total * 0.02), 0.95) if total > 0 else 0.0

    return {
        "brand_tags": brand_tags,
        "commodity_tags": commodity_tags,
        "confidence": confidence,
    }
