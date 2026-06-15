"""Grammar tests for desc_extractor.categorizer.categorize_from_desc.

Per-grammar positive + negative/ambiguous coverage. The categorizer is the SHARED
single-source-of-truth grammar used by the categorize stage (writer.categorize_and_record),
the one-shot CLI, and ingest (source_ingest.clean). It must:
  * categorize each handled commodity from an unambiguous lead/body description;
  * return None for ambiguous / foreign / conflicting / pollution descriptions;
  * NEVER return an off-vocab key (every return value is a canonical commodity_seeds key,
    so set_category's normalize_category never drops it).
"""

import pytest

from app.services.category_normalizer import normalize_category
from app.services.commodity_registry import get_all_commodities
from app.services.desc_extractor.categorizer import categorize_from_desc

# Positive cases: (description, expected canonical commodity key).
_POSITIVE = [
    # SPEC_COMMODITIES via the reused extract_desc router.
    ("HDD, 6Gbps 1.2TB 10K 2.5 Inch HDD, IBM", "hdd"),
    ('HD, 450GB, 15KRPM, 3.5", Fibre Channel', "hdd"),
    ("SSD 480GB 7mmH 6Gb SATA", "ssd"),
    ("Mem, 16GB DDR4 2Rx4 PC4-2400T RDIMM", "dram"),
    ("Memory, 8GB DDR3 1600MHz Non-ECC UDIMM, Kingston", "dram"),
    ("PSU, 1460W 240V/200V AC Hot Swap for EN 62368-1", "power_supplies"),
    ("LCD, FHD AG LED UWVA 15.6 panel", "displays"),
    ("Tape Drive, 400/800gb Ultrium Lto-3 HH SCSI LVD External", "tape_drives"),
    ("MB, WHL I7 DIS 4G 8G WIN", "motherboards"),
    ("SPS-MB DSC GTX1050 4GB i7-7700HQ WIN", "motherboards"),  # GPU/CPU words subordinate
    ("GPU, NVIDIA Tesla V100 32GB Module", "gpu"),
    # CPU requires explicit CPU identity (the CPU + Xeon/Core/Ryzen gate).
    ("SPS-CPU BDW E5-2650L V4 14C 1_7GHZ 65W", "cpu"),
    ("SPS-PROC HSW E5-1630v3 4C 3.7GHz 140W", "cpu"),
    ("Xeon GOLD 6134 3.2G 8C 130W", "cpu"),
    ("Intel Core i7-7700HQ 2.8GHz Processor", "cpu"),
    # New-category anchored leads (extract_desc has no grammar for these).
    ("CABLE, LVDS 40-pin display harness", "cables"),
    ("CBL, USB 3.0 internal 500mm", "cables"),
    ("FAN, 80mm hot-swap blower assembly", "fans_cooling"),
    ("HEATSINK, CPU thermal module with fan", "fans_cooling"),
    ("HEAT SINK, passive aluminum for DIMM", "fans_cooling"),
    ("BATTERY, 3.6V NVRAM cache lithium pack", "batteries"),
    ("BTRY, UPS sealed lead-acid 12V 9Ah", "batteries"),
]

# Negative / ambiguous cases: the grammar must decline (None) — a wrong category is worse
# than a missing one.
_NEGATIVE = [
    # Bare CPU/PROC lead with NO CPU identity — the polluted-bucket trap.
    "CPU, spare module for system board",
    "PROC, replacement part",
    # Foreign lead suppresses (TRIO classified it as something else).
    'Other, 3.5" Server HDD Hard Drive tray',
    "Tray, 2.5 inch drive carrier",
    "Card, PCI-X Quad Channel Ultra4 Controller",
    # Hard storage x dram conflict — never pick a side.
    "Memory, 256GB, LiteOn SSD, M.2 2280",
    # Battery-management IC is NOT a battery.
    "BATTERY MANAGEMENT IC BQ40Z50 fuel gauge stack monitor",
    "BATT GAS GAUGE LTC6803 monitor",
    # MPN-as-description / no commodity signal at all.
    "00AR327",
    "GRM155R71C104MA88D",
    # Empty / None.
    "",
    "   ",
    None,
]


@pytest.mark.parametrize("description,expected", _POSITIVE)
def test_categorizes_unambiguous_description(description, expected):
    assert categorize_from_desc(description) == expected


@pytest.mark.parametrize("description", _NEGATIVE)
def test_declines_ambiguous_or_foreign_description(description):
    assert categorize_from_desc(description) is None


def test_every_returned_key_is_canonical_vocab():
    # Discipline: the categorizer must never emit a key set_category would drop as
    # off-vocab. Assert every positive expectation is a canonical commodity_seeds key
    # AND round-trips through normalize_category unchanged.
    canonical = set(get_all_commodities())
    for _description, expected in _POSITIVE:
        assert expected in canonical, f"{expected!r} is not a canonical commodity key"
        assert normalize_category(expected) == expected


def test_cable_lead_is_anchored_not_substring():
    # The CABLE lead is start-anchored: "ANTENNA CABLE" inside a display row stays a
    # display (routed by extract_desc), never a cable.
    assert categorize_from_desc("LCD, 15.6 FHD panel with ANTENNA CABLE") == "displays"
    assert categorize_from_desc("CABLE, antenna coax for LCD") == "cables"
