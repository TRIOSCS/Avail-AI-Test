"""Unit 6 — category normalizer maps free-text variants to canonical commodity keys."""

import pytest

from app.services.category_normalizer import normalize_category, normalize_trio_category


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("solid state drives - ssd", "ssd"),
        ("connectors, interconnects", "connectors"),
        ("memory - modules, cards", "dram"),
        ("battery products", "batteries"),
    ],
)
def test_known_alias_maps_to_canonical(raw, expected):
    assert normalize_category(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Main Board", "motherboards"),
        ("Hard Drive", "hdd"),
        ("Memory", "dram"),
        ("LCD", "displays"),
        ("LCD ASSY", "displays"),
        ("PSU", "power_supplies"),
        ("Graphics Card", "gpu"),
        ("Tape Drive", "tape_drives"),
        ("IC", "ics_other"),
        ("OEM ASSY", "oem_assemblies"),
    ],
)
def test_trio_sfdc_commodity_codes_map_to_tree_keys(raw, expected):
    """TRIO SFDC part-master Commodity_Code__c vocabulary lands on canonical keys via
    the source-scoped entry point (which falls back to the global map for codes that are
    unambiguous everywhere)."""
    assert normalize_trio_category(raw) == expected


@pytest.mark.parametrize("raw,expected", [("CPU", "cpu"), ("SSD", "ssd"), ("Other", "other")])
def test_trio_codes_already_canonical_resolve_via_lowercase(raw, expected):
    """TRIO codes that ARE tree keys resolve without needing an alias entry."""
    assert normalize_category(raw) == expected
    assert normalize_trio_category(raw) == expected


def test_bare_memory_is_source_scoped_not_global():
    """Bare "memory" is only unambiguous inside TRIO's part master (supplier taxonomies
    use it for flash/EEPROM/SRAM too), so the global forward-hook path must leave it
    untouched while the SFDC ingest path resolves it."""
    assert normalize_category("Memory") is None
    assert normalize_trio_category("Memory") == "dram"


def test_trio_source_map_targets_are_tree_keys():
    """Source-scoped TRIO entries obey the same invariants as the global map: lower/
    trimmed keys, targets on canonical COMMODITY_TREE keys."""
    from app.services.category_normalizer import CATEGORY_ALIASES, TRIO_SFDC_COMMODITY_CODES
    from app.services.commodity_registry import get_all_commodities

    tree_keys = set(get_all_commodities())
    for raw, target in TRIO_SFDC_COMMODITY_CODES.items():
        assert raw == raw.lower().strip(), f"TRIO code {raw!r} must be lower/trimmed"
        assert target in tree_keys, f"TRIO code {raw!r} -> {target!r} is not a COMMODITY_TREE key"
        assert raw not in CATEGORY_ALIASES, (
            f"TRIO code {raw!r} is source-scoped and must NOT also be in the global CATEGORY_ALIASES"
        )


@pytest.mark.parametrize("raw", ["Integrated Circuits (ICs)", "integrated circuits (ics)"])
def test_legacy_generic_ic_bucket_maps_to_ics_other(raw):
    """'Integrated Circuits (ICs)' was ambiguous before ics_other existed; now it
    maps."""
    assert normalize_category(raw) == "ics_other"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Hard Drives", "hdd"),
        ("Internal Hard Drives", "hdd"),
        ("Memory Module", "dram"),
        ("Memory Modules", "dram"),
        ("Solid State Drives - SSD", "ssd"),
        ("Memory - Modules, Cards", "dram"),
    ],
)
def test_distributor_taxonomy_strings_normalize(raw, expected):
    """High-frequency distributor/OEM taxonomy strings land on canonical keys.

    Since the F1 ladder routed EVERY enrichment category write through
    normalize_category, an off-map connector/extractor string is silently DROPPED
    instead of persisted — alias-map drift here would suppress the authoritative tier's
    category fill-rate, so the common vocabulary is pinned.
    """
    assert normalize_category(raw) == expected


@pytest.mark.parametrize("raw,expected", [("Capacitors", "capacitors"), ("Resistors", "resistors")])
def test_legacy_capitalized_canonical_variants_resolve(raw, expected):
    """Capitalized variants of canonical keys (live-DB legacy rows) resolve to
    lowercase."""
    assert normalize_category(raw) == expected


# The FULL 2026-07 residue-remap vocabulary (all 64 aliases; migration 189 backfill) in
# vendor casing, in the runtime block's declaration order. Pinned exhaustively — a
# wrong-but-valid-tree-key target on any single alias would pass the structural gates
# (test_every_alias_target_is_a_tree_key, POST_093_ALIASES) and misfile every future
# ingest of that string, so each raw→target pair is asserted individually.
# test_residue_pin_covers_the_full_189_vocabulary keeps this list complete.
_RESIDUE_2026_07_CASES: list[tuple[str, str]] = [
    ("Power Inductors - SMD", "inductors"),
    ("Common Mode Chokes / Filters", "inductors"),
    ("Aluminum Electrolytic Capacitors - Radial Leaded", "capacitors"),
    ("Multilayer Ceramic Capacitors MLCC - SMD/SMT", "capacitors"),
    ("Aluminum Organic Polymer Capacitors", "capacitors"),
    ("Current Sense Resistors - SMD", "resistors"),
    ("Trimmer Resistors - Through Hole", "resistors"),
    ("Crystals", "oscillators"),
    ("MEMS Oscillators", "oscillators"),
    ("Standard Clock Oscillators", "oscillators"),
    ("TCXO Oscillators", "oscillators"),
    ("IGBT Modules", "transistors"),
    ("Schottky Diodes & Rectifiers", "diodes"),
    ("ESD Protection Diodes / TVS Diodes", "diodes"),
    ("Zener Diodes", "diodes"),
    ("Rectifiers", "diodes"),
    ("Diode", "diodes"),
    ("MOSFET", "mosfets"),
    ("Power Switch ICs - Power Distribution", "power_ic"),
    ("Switching Controllers", "power_ic"),
    ("Supervisory Circuits", "power_ic"),
    ("Motor / Motion / Ignition Controllers & Drivers", "power_ic"),
    ("Audio Amplifiers", "analog_ic"),
    ("Precision Amplifiers", "analog_ic"),
    ("Analog to Digital Converters - ADC", "analog_ic"),
    ("Data Converter (ADC)", "analog_ic"),
    ("Digital to Analog Converters - DAC", "analog_ic"),
    ("Logic IC", "logic_ic"),
    ("Clock Buffer", "ics_other"),
    ("RS-232 Interface IC", "ics_other"),
    ("RS-422/RS-485 Interface IC", "ics_other"),
    ("PCI Interface IC", "ics_other"),
    ("Interface IC", "ics_other"),
    ("LIN Transceivers", "ics_other"),
    ("Integrated Circuit (Timer)", "ics_other"),
    ("Timers & Support Products", "ics_other"),
    ("8-Bit Microcontrollers - MCU", "microcontrollers"),
    ("Microcontroller", "microcontrollers"),
    ("Digital Signal Processors & Controllers - DSP, DSC", "dsp"),
    ("FPGA - Field Programmable Gate Array", "fpga"),
    ("Hard Disk Drives - HDD", "hdd"),
    ("LDO Voltage Regulators", "voltage_regulators"),
    ("Voltage Regulator", "voltage_regulators"),
    ("Power Supplies - Board Mount", "power_supplies"),
    ("Electronic Battery", "batteries"),
    ("Laptop Battery", "batteries"),
    ("Laptop Battery (FRU / CRU Replacement Part)", "batteries"),
    ("Storage Controller Battery", "batteries"),
    ("RAID Controller Accessory / Battery Backup (BBWC Battery Module)", "batteries"),
    ("RAID Controller Accessory / Battery Module", "batteries"),
    ("Automotive Connectors", "connectors"),
    ("Board to Board & Mezzanine Connectors", "connectors"),
    ("Circular Metric Connectors", "connectors"),
    ("Terminals", "connectors"),
    ("Conduit Fittings & Accessories", "cables"),
    ("Reed Relays", "relays"),
    ("Board Mount Current Sensors", "sensors"),
    ("IMUs - Inertial Measurement Units", "sensors"),
    ("Bluetooth Modules - 802.15.1", "rf"),
    ("Multiprotocol Modules", "rf"),
    ("RF Transceiver", "rf"),
    ("RF/Wireless Module", "rf"),
    ("Development Boards, Kits, Programmers", "tools_accessories"),
    ("Server Maintenance Consumable / Thermal Management Accessory", "tools_accessories"),
]


@pytest.mark.parametrize("raw,expected", _RESIDUE_2026_07_CASES)
def test_2026_07_residue_aliases_normalize(raw, expected):
    """The 2026-07 residue remap strings (live-DB stranded cards; migration 189) land on
    canonical keys via the forward hook, so re-ingesting the same vendor taxonomies can
    never strand new cards."""
    assert normalize_category(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ssd", "ssd"),
        ("connectors", "connectors"),
        ("tape_drives", "tape_drives"),
        ("ics_other", "ics_other"),
        ("oem_assemblies", "oem_assemblies"),
    ],
)
def test_canonical_value_passes_through(raw, expected):
    assert normalize_category(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  SSD  ", "ssd"),
        ("Connectors, Interconnects", "connectors"),
        ("  Hard Drive  ", "hdd"),
    ],
)
def test_case_insensitive_and_trimmed(raw, expected):
    assert normalize_category(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        # Strings with no unambiguous canonical bucket are intentionally NOT mapped.
        "discrete semiconductor products",
        "random garbage xyz",
    ],
)
def test_unknown_returns_none(raw):
    assert normalize_category(raw) is None


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_empty_or_none_returns_none(raw):
    assert normalize_category(raw) is None


def test_idempotent():
    once = normalize_category("solid state drives - ssd")
    assert once == "ssd"
    assert normalize_category(once) == "ssd"
    assert normalize_category(normalize_category("IC")) == "ics_other"


def test_every_alias_target_is_a_tree_key():
    """No alias may point at a key absent from COMMODITY_TREE (silent facet black
    hole)."""
    from app.services.category_normalizer import CATEGORY_ALIASES
    from app.services.commodity_registry import get_all_commodities

    tree_keys = set(get_all_commodities())
    for raw, target in CATEGORY_ALIASES.items():
        assert target in tree_keys, f"alias {raw!r} -> {target!r} is not a COMMODITY_TREE key"
        assert raw == raw.lower().strip(), f"alias key {raw!r} must be lower/trimmed"


# ── Runtime alias map ↔ migration 093 frozen snapshot ──────────────────────────────
#
# Migration 093 one-off-normalized legacy material_cards.category rows through a FROZEN
# snapshot of the alias map; the runtime map below is only a forward hook at write time.
# An alias added AFTER 093 therefore leaves all pre-existing rows matching it
# unnormalized (invisible to every commodity filter) unless a backfill ships with it.
# Every post-093 alias key must be registered here with the backfill that covers it
# (a follow-up data migration, or scripts/normalize_categories.py run noted by date),
# so "did we forget a backfill?" fails CI instead of waiting for code-review archaeology.
POST_093_ALIASES: dict[str, str] = {
    # "new alias key": "backfill reference (e.g. migration 09X / script run YYYY-MM-DD)",
    "hard drives": "migration 100_taxonomy_alias_backfill",
    "internal hard drives": "migration 100_taxonomy_alias_backfill",
    "memory module": "migration 100_taxonomy_alias_backfill",
    "memory modules": "migration 100_taxonomy_alias_backfill",
    # 2026-07 residue remap — 64 aliases backfilled by migration 189.
    "power inductors - smd": "migration 189_category_residue_backfill",
    "common mode chokes / filters": "migration 189_category_residue_backfill",
    "aluminum electrolytic capacitors - radial leaded": "migration 189_category_residue_backfill",
    "multilayer ceramic capacitors mlcc - smd/smt": "migration 189_category_residue_backfill",
    "aluminum organic polymer capacitors": "migration 189_category_residue_backfill",
    "current sense resistors - smd": "migration 189_category_residue_backfill",
    "trimmer resistors - through hole": "migration 189_category_residue_backfill",
    "crystals": "migration 189_category_residue_backfill",
    "mems oscillators": "migration 189_category_residue_backfill",
    "standard clock oscillators": "migration 189_category_residue_backfill",
    "tcxo oscillators": "migration 189_category_residue_backfill",
    "igbt modules": "migration 189_category_residue_backfill",
    "schottky diodes & rectifiers": "migration 189_category_residue_backfill",
    "esd protection diodes / tvs diodes": "migration 189_category_residue_backfill",
    "zener diodes": "migration 189_category_residue_backfill",
    "rectifiers": "migration 189_category_residue_backfill",
    "diode": "migration 189_category_residue_backfill",
    "mosfet": "migration 189_category_residue_backfill",
    "power switch ics - power distribution": "migration 189_category_residue_backfill",
    "switching controllers": "migration 189_category_residue_backfill",
    "supervisory circuits": "migration 189_category_residue_backfill",
    "motor / motion / ignition controllers & drivers": "migration 189_category_residue_backfill",
    "audio amplifiers": "migration 189_category_residue_backfill",
    "precision amplifiers": "migration 189_category_residue_backfill",
    "analog to digital converters - adc": "migration 189_category_residue_backfill",
    "data converter (adc)": "migration 189_category_residue_backfill",
    "digital to analog converters - dac": "migration 189_category_residue_backfill",
    "logic ic": "migration 189_category_residue_backfill",
    "clock buffer": "migration 189_category_residue_backfill",
    "rs-232 interface ic": "migration 189_category_residue_backfill",
    "rs-422/rs-485 interface ic": "migration 189_category_residue_backfill",
    "pci interface ic": "migration 189_category_residue_backfill",
    "interface ic": "migration 189_category_residue_backfill",
    "lin transceivers": "migration 189_category_residue_backfill",
    "integrated circuit (timer)": "migration 189_category_residue_backfill",
    "timers & support products": "migration 189_category_residue_backfill",
    "8-bit microcontrollers - mcu": "migration 189_category_residue_backfill",
    "microcontroller": "migration 189_category_residue_backfill",
    "digital signal processors & controllers - dsp, dsc": "migration 189_category_residue_backfill",
    "fpga - field programmable gate array": "migration 189_category_residue_backfill",
    "hard disk drives - hdd": "migration 189_category_residue_backfill",
    "ldo voltage regulators": "migration 189_category_residue_backfill",
    "voltage regulator": "migration 189_category_residue_backfill",
    "power supplies - board mount": "migration 189_category_residue_backfill",
    "electronic battery": "migration 189_category_residue_backfill",
    "laptop battery": "migration 189_category_residue_backfill",
    "laptop battery (fru / cru replacement part)": "migration 189_category_residue_backfill",
    "storage controller battery": "migration 189_category_residue_backfill",
    "raid controller accessory / battery backup (bbwc battery module)": "migration 189_category_residue_backfill",
    "raid controller accessory / battery module": "migration 189_category_residue_backfill",
    "automotive connectors": "migration 189_category_residue_backfill",
    "board to board & mezzanine connectors": "migration 189_category_residue_backfill",
    "circular metric connectors": "migration 189_category_residue_backfill",
    "terminals": "migration 189_category_residue_backfill",
    "conduit fittings & accessories": "migration 189_category_residue_backfill",
    "reed relays": "migration 189_category_residue_backfill",
    "board mount current sensors": "migration 189_category_residue_backfill",
    "imus - inertial measurement units": "migration 189_category_residue_backfill",
    "bluetooth modules - 802.15.1": "migration 189_category_residue_backfill",
    "multiprotocol modules": "migration 189_category_residue_backfill",
    "rf transceiver": "migration 189_category_residue_backfill",
    "rf/wireless module": "migration 189_category_residue_backfill",
    "development boards, kits, programmers": "migration 189_category_residue_backfill",
    "server maintenance consumable / thermal management accessory": "migration 189_category_residue_backfill",
}


def _migration_093():
    import importlib.util
    import os

    path = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions", "093_normalize_legacy_categories.py")
    spec = importlib.util.spec_from_file_location("migration_093_for_alias_sync", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _migration_189():
    import importlib.util
    import os

    path = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions", "189_category_residue_backfill.py")
    spec = importlib.util.spec_from_file_location("migration_189_for_alias_sync", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_MIGRATION_189_REF = "migration 189_category_residue_backfill"


def test_residue_pin_covers_the_full_189_vocabulary():
    """_RESIDUE_2026_07_CASES pins ALL of migration 189's aliases, not a sample — an un-
    pinned alias could be retargeted to a wrong-but-valid tree key without any test
    noticing (the structural gates check key shape and target validity, not which
    target)."""
    pinned = {raw.strip().lower() for raw, _ in _RESIDUE_2026_07_CASES}
    assert pinned == set(_migration_189()._NEW_ALIASES)


def test_189_registrations_match_the_migration_snapshot():
    """Every POST_093_ALIASES entry claiming migration 189 as its backfill must actually
    be in 189's frozen _NEW_ALIASES snapshot, and vice versa — registering a LATER alias
    against the already-shipped 189 would be a lie (the frozen backfill never covered
    it; it needs its own migration and reference)."""
    registered = {raw for raw, ref in POST_093_ALIASES.items() if ref == _MIGRATION_189_REF}
    assert registered == set(_migration_189()._NEW_ALIASES)


def test_runtime_aliases_are_backfilled_by_093_or_documented():
    """Every runtime alias (global + TRIO source-scoped) is either covered by migration
    093's frozen snapshot or explicitly registered in POST_093_ALIASES with its own
    backfill.

    Shared keys must also keep 093's target — a retargeted alias would strand the rows
    093 already rewrote and needs a follow-up migration, not a silent edit.
    """
    from app.services.category_normalizer import CATEGORY_ALIASES, TRIO_SFDC_COMMODITY_CODES

    frozen = _migration_093()._CATEGORY_ALIASES
    runtime = {**CATEGORY_ALIASES, **TRIO_SFDC_COMMODITY_CODES}
    for raw, target in runtime.items():
        if raw in frozen:
            assert frozen[raw] == target, (
                f"alias {raw!r} was retargeted ({frozen[raw]!r} -> {target!r}) after migration 093 "
                "rewrote rows to the old target — ship a follow-up data migration for the stranded rows"
            )
        else:
            assert raw in POST_093_ALIASES, (
                f"alias {raw!r} -> {target!r} was added after migration 093, so legacy rows matching "
                "it were never normalized (the runtime hook only fires on NEW writes). Ship a backfill "
                "and register the alias in POST_093_ALIASES with the backfill reference."
            )
