"""Category normalizer — map free-text category variants to canonical commodity keys.

What: ``CATEGORY_ALIASES`` maps known variant strings (lower/trimmed) to canonical
      ``COMMODITY_TREE`` child keys so faceted filters bucket every card correctly.
      The faceted sidebar keys off ``lower(trim(category))``, so variant strings like
      "connectors, interconnects" are otherwise invisible to every filter.
Called by: enrichment services (forward hook at write time) and
      scripts/normalize_categories.py (one-off backfill).

Only UNAMBIGUOUS mappings are included. Generic strings whose canonical bucket is
ambiguous (e.g. "discrete semiconductor products", a bare manufacturer name) are
intentionally omitted so ``normalize_category`` returns ``None`` and the caller leaves
the existing value untouched rather than guessing. Formerly-ambiguous generic IC
strings ("ic", "integrated circuits (ics)") now have an honest coarse bucket
(``ics_other``) and map there.

The TRIO SFDC part-master block maps the raw ``Commodity_Code__c`` vocabulary used by
TRIO's Salesforce export so the source-ingest ladder lands every row on a canonical
tree key.
"""

from app.services.commodity_registry import get_all_commodities

_CANONICAL_KEYS = frozenset(get_all_commodities())

CATEGORY_ALIASES: dict[str, str] = {
    "connectors, interconnects": "connectors",
    "cable assemblies": "cables",
    "cables, wires": "cables",
    "cables, wires - management": "cables",
    "battery products": "batteries",
    "laptop battery / notebook primary battery": "batteries",
    "laptop battery / power": "batteries",
    "microprocessors - mpu": "microprocessors",
    "arm microcontrollers - mcu": "microcontrollers",
    "solid state drives - ssd": "ssd",
    "office & computer & networking products > computer products > drives > disk drives": "hdd",
    "emmc": "flash",
    "memory - modules, cards": "dram",
    "linear voltage regulators": "voltage_regulators",
    "switching voltage regulators": "voltage_regulators",
    "inductors, coils, chokes": "inductors",
    "crystals, oscillators, resonators": "oscillators",
    "counter shift registers": "logic_ic",
    "bipolar transistors - bjt": "transistors",
    "sensors, transducers": "sensors",
    "networking solutions": "networking",
    "fans, blowers, thermal management": "fans_cooling",
    "system board / motherboard": "motherboards",
    "laptop motherboard / system board": "motherboards",
    "system board / motherboard (laptop spare part)": "motherboards",
    "cpu - central processing units": "cpu",
    "tools": "tools_accessories",
    "soldering, desoldering, rework products": "tools_accessories",
    # TRIO SFDC part-master Commodity_Code__c vocabulary (cpu, ssd, other are
    # already canonical tree keys and resolve via _CANONICAL_KEYS).
    "main board": "motherboards",
    "hard drive": "hdd",
    "memory": "dram",
    "lcd": "displays",
    "lcd assy": "displays",
    "psu": "power_supplies",
    "graphics card": "gpu",
    "tape drive": "tape_drives",
    "ic": "ics_other",
    "oem assy": "oem_assemblies",
    # Legacy generic bucket seen on live cards — now has a coarse canonical key.
    "integrated circuits (ics)": "ics_other",
}


def normalize_category(raw: str | None) -> str | None:
    """Map a raw category string to a canonical commodity key.

    Returns the canonical key for a known alias or an already-canonical value; otherwise
    ``None`` (the caller should leave the existing category untouched rather than guess).
    """
    if not raw or not raw.strip():
        return None
    cleaned = raw.strip().lower()
    if cleaned in CATEGORY_ALIASES:
        return CATEGORY_ALIASES[cleaned]
    if cleaned in _CANONICAL_KEYS:
        return cleaned
    return None
