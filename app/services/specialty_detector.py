"""Specialty detector — identifies vendor brand/commodity specialties.

Uses keyword matching on email history, sighting data, and offer history
to generate brand_tags and commodity_tags for vendor cards.
"""

import logging
import re
from collections import Counter
from datetime import datetime, timezone

log = logging.getLogger("avail.specialty_detector")

# ── Known electronic component brands (~100) ─────────────────────────

BRAND_LIST = [
    # Major semiconductor companies
    "Intel", "AMD", "Nvidia", "Qualcomm", "Broadcom", "Texas Instruments",
    "NXP", "STMicroelectronics", "Infineon", "Microchip", "Analog Devices",
    "ON Semiconductor", "Renesas", "Marvell", "MediaTek", "Xilinx",
    "Lattice", "Cypress", "Maxim", "Linear Technology",
    # Server/Systems OEMs
    "IBM", "Dell", "HP", "HPE", "Lenovo", "Cisco", "Juniper",
    "Arista", "NetApp", "EMC", "Oracle", "Sun",
    # Memory
    "Samsung", "SK Hynix", "Micron", "Kingston", "Crucial",
    "Western Digital", "Seagate", "SanDisk", "Toshiba", "Kioxia",
    # Passive components
    "Murata", "TDK", "Yageo", "Vishay", "Kemet", "AVX", "Panasonic",
    "Nichicon", "Rubycon", "Bourns", "Ohmite",
    # Connectors & electromechanical
    "TE Connectivity", "Amphenol", "Molex", "JAE", "Hirose",
    "ITT", "Smiths Interconnect", "3M", "Phoenix Contact",
    # Power
    "Mean Well", "Delta Electronics", "Vicor", "Artesyn",
    "Lambda", "TDK-Lambda", "Cosel", "Bel Fuse",
    # Optoelectronics
    "Osram", "Lumileds", "Cree", "Broadcom", "Hamamatsu",
    # RF & wireless
    "Qorvo", "Skyworks", "MACOM", "Mini-Circuits",
    # Automotive
    "Bosch", "Continental", "Denso",
    # Test & measurement
    "Keysight", "Tektronix", "National Instruments",
    # PCB & assembly
    "JLCPCB", "PCBWay",
    # Other notable brands
    "Arrow", "Avnet", "Digi-Key", "Mouser", "Future Electronics",
    "Heilind", "TTI", "Sager", "Newark", "Farnell", "RS Components",
]

# Compile brand patterns (case-insensitive, word-boundary)
_BRAND_PATTERNS = {}
for brand in BRAND_LIST:
    # Escape special regex chars and create word-boundary pattern
    escaped = re.escape(brand)
    _BRAND_PATTERNS[brand] = re.compile(
        rf"\b{escaped}\b", re.IGNORECASE
    )

# ── Commodity category mapping ───────────────────────────────────────

COMMODITY_MAP = {
    # Memory & Storage
    "memory": ["ddr", "sdram", "dimm", "rdimm", "lrdimm", "sodimm", "ecc", "ram", "memory module"],
    "storage": ["ssd", "hdd", "nvme", "sata", "sas", "hard drive", "solid state", "flash storage"],
    "flash": ["nand", "nor flash", "eeprom", "flash memory", "emmc"],
    # Processors
    "processors": ["cpu", "processor", "xeon", "epyc", "core i", "ryzen", "arm", "mips", "risc-v"],
    "gpu": ["gpu", "graphics", "geforce", "radeon", "quadro", "tesla"],
    "fpga": ["fpga", "cpld", "programmable logic", "spartan", "virtex", "cyclone", "stratix"],
    # Integrated circuits
    "microcontrollers": ["mcu", "microcontroller", "stm32", "pic", "avr", "esp32", "arduino"],
    "analog_ic": ["op-amp", "opamp", "adc", "dac", "comparator", "amplifier", "voltage reference"],
    "power_ic": ["voltage regulator", "mosfet", "igbt", "power supply", "dc-dc", "ldo", "pmic"],
    "logic_ic": ["gate", "flip-flop", "multiplexer", "decoder", "buffer", "transceiver"],
    # Passive components
    "capacitors": ["capacitor", "mlcc", "electrolytic", "tantalum", "ceramic cap", "supercap"],
    "resistors": ["resistor", "potentiometer", "thermistor", "varistor", "ntc", "ptc"],
    "inductors": ["inductor", "choke", "ferrite", "transformer", "coil"],
    # Connectors
    "connectors": ["connector", "header", "socket", "plug", "jack", "terminal", "backplane"],
    "cables": ["cable", "wire", "harness", "ribbon", "coaxial", "fiber optic"],
    # RF & Wireless
    "rf": ["rf", "antenna", "filter", "mixer", "amplifier", "transceiver", "wireless module"],
    # Optoelectronics
    "leds": ["led", "oled", "display", "backlight", "indicator"],
    "sensors": ["sensor", "accelerometer", "gyroscope", "temperature sensor", "pressure sensor"],
    # Electromechanical
    "relays": ["relay", "contactor", "switch"],
    "motors": ["motor", "stepper", "servo", "actuator", "fan", "blower"],
    # Server/Systems
    "servers": ["server", "blade", "rack", "chassis", "motherboard", "mainboard"],
    "networking": ["switch", "router", "firewall", "nic", "sfp", "transceiver module", "ethernet"],
    # Power supplies
    "power_supplies": ["power supply", "psu", "ups", "inverter", "converter", "charger"],
}

# Flatten keywords for quick lookup
_COMMODITY_KEYWORDS = {}
for category, keywords in COMMODITY_MAP.items():
    for kw in keywords:
        _COMMODITY_KEYWORDS[kw.lower()] = category


def detect_brands_from_text(text: str) -> list[str]:
    """Scan text for brand mentions. Returns list of matched brand names."""
    if not text:
        return []

    found = []
    for brand, pattern in _BRAND_PATTERNS.items():
        if pattern.search(text):
            found.append(brand)
    return found


def detect_commodities_from_text(text: str) -> list[str]:
    """Scan text for commodity category signals. Returns unique category list."""
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
    from ..models import Sighting, Offer, VendorCard

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
    offers = (
        db.query(Offer.manufacturer, Offer.mpn)
        .filter(Offer.vendor_card_id == vendor_card_id)
        .limit(500)
        .all()
    )
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


def batch_analyze_specialties(vendor_card_ids: list[int], db) -> dict[int, dict]:
    """Analyze specialties for multiple vendors. Returns {vendor_card_id: result}."""
    results = {}
    for vid in vendor_card_ids:
        try:
            results[vid] = analyze_vendor_specialties(vid, db)
        except Exception as e:
            log.warning("Specialty analysis failed for vendor %d: %s", vid, e)
            results[vid] = {"brand_tags": [], "commodity_tags": [], "confidence": 0.0}
    return results
