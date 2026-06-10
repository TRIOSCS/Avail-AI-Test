"""Category normalizer — map free-text category variants to canonical commodity keys.

What: ``CATEGORY_ALIASES`` maps known variant strings (lower/trimmed) to canonical
      ``COMMODITY_TREE`` child keys so faceted filters bucket every card correctly.
      The faceted sidebar keys off ``lower(trim(category))``, so variant strings like
      "connectors, interconnects" are otherwise invisible to every filter.
Called by: enrichment services (forward hook at write time) and
      scripts/normalize_categories.py (one-off backfill).

Only UNAMBIGUOUS mappings are included in ``CATEGORY_ALIASES``: the forward hook fires
at every category write site for ANY enrichment source, so an entry must hold in every
taxonomy, not just one vendor's. Generic strings whose canonical bucket is ambiguous
(e.g. "discrete semiconductor products", a bare manufacturer name) are intentionally
omitted so ``normalize_category`` returns ``None`` and the caller leaves the existing
value untouched rather than guessing. Formerly-ambiguous generic IC strings ("ic",
"integrated circuits (ics)") now have an honest coarse bucket (``ics_other``) and map
there.

Source-scoped vocabulary lives in separate maps. ``TRIO_SFDC_COMMODITY_CODES`` holds
TRIO SFDC part-master ``Commodity_Code__c`` codes that are only unambiguous WITHIN that
export (bare "memory" is always a DRAM module there, but covers flash/EEPROM/SRAM in
supplier taxonomies like DigiKey's). The SFDC ingest ladder resolves through
``normalize_trio_category`` (source map first, then the global path); the global
``normalize_category`` never consults source-scoped maps.
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
    # High-frequency distributor/OEM taxonomy strings (unambiguous in every taxonomy).
    # Since the F1 ladder routed ALL enrichment category writes through
    # normalize_category, an off-map connector string is DROPPED instead of persisted —
    # these entries keep the authoritative tier's category fill-rate from silently
    # regressing (bare "memory" stays out: ambiguous, see TRIO_SFDC_COMMODITY_CODES).
    "hard drives": "hdd",
    "internal hard drives": "hdd",
    "memory module": "dram",
    "memory modules": "dram",
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
    # TRIO SFDC part-master Commodity_Code__c vocabulary — only the codes that are
    # unambiguous in ANY taxonomy live here (cpu, ssd, other are already canonical tree
    # keys and resolve via _CANONICAL_KEYS; bare "memory" is source-scoped, see
    # TRIO_SFDC_COMMODITY_CODES).
    "main board": "motherboards",
    "hard drive": "hdd",
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

# TRIO SFDC Commodity_Code__c codes that are only unambiguous WITHIN that export.
# Bare "memory" means a DRAM module in TRIO's part master, but in supplier taxonomies
# (e.g. DigiKey's "Memory" IC category) it covers flash/EEPROM/SRAM too — the global map
# already encodes that distinction ("emmc" -> "flash" vs "memory - modules, cards" ->
# "dram"), so this entry must never reach the global normalize_category path. Consumed
# only by normalize_trio_category (the SFDC ingest ladder).
TRIO_SFDC_COMMODITY_CODES: dict[str, str] = {
    "memory": "dram",
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


def normalize_trio_category(raw: str | None) -> str | None:
    """Map a TRIO SFDC ``Commodity_Code__c`` value to a canonical commodity key.

    Source-scoped entry point for the SFDC ingest ladder: consults the TRIO-only
    vocabulary first (codes like bare "Memory" that are unambiguous only within TRIO's
    part master), then falls back to the global ``normalize_category`` path. Returns
    ``None`` for unknown codes, same contract as ``normalize_category``.
    """
    if not raw or not raw.strip():
        return None
    cleaned = raw.strip().lower()
    if cleaned in TRIO_SFDC_COMMODITY_CODES:
        return TRIO_SFDC_COMMODITY_CODES[cleaned]
    return normalize_category(cleaned)
