"""Accuracy guard for the PSU description extractor — REAL corpus strings → exact specs.

Every description below is verbatim from TRIO's part master
(/root/source_ingest/LSC1__Material__c.csv, Material_Description__c) or the staged
inventory sheets (Firesale_inventory). Expectations are FULL equality — a new key
appearing unexpectedly is as much a failure as a missing one.
"""

import pytest

from app.services.desc_extractor import extract_desc

# (real description, commodity_hint or None, exact expected specs)
CASES = [
    # ── TRIO part-master "<Label>, …" grammar ────────────────────────────
    (
        "PSU, 1460W 240V/200V AC Hot Swap for EN 62368-1",
        None,
        {"wattage": 1460, "psu_class": "Server/Redundant"},
    ),
    ("PWR SPLY,180W,BRZ,D8,ACBL", None, {"wattage": 180}),
    (
        "AC ADAPTERS, 45Watt, 20V2.25A COO",  # WATT spelling; 20V2.25A is not a wattage
        None,
        {"wattage": 45, "psu_class": "AC-DC (External/Adapter)"},
    ),
    (
        "Power Supply, V7000 Gen2 Expansion 800W, IBM",  # V7000/Gen2 glued digits never match
        None,
        {"wattage": 800},
    ),
    (
        "PSU., 750W TT ITIC, Acbel PN FSF061-EL1G",  # dot-stripped "PSU." lead
        None,
        {"wattage": 750},
    ),
    # ── body-token routing (no lead label) ───────────────────────────────
    ("750W P/S 8871", None, {"wattage": 750}),  # P/S token routes; bare 8871 has no W
    (
        "460 watt AC hot-plug power supply",
        None,
        {"wattage": 460, "psu_class": "Server/Redundant"},
    ),
    (
        "ZT - 800W power supply, N+1 Redundancy (Delta p/n DPS-800AB-27 A)",
        None,
        # only 800W matches — the 800 inside DPS-800AB has no W unit token
        {"wattage": 800, "psu_class": "Server/Redundant"},
    ),
    (
        "Dell Model: A670P-00 Server Power Supply 670W",
        None,
        {"wattage": 670, "psu_class": "Server/Redundant"},
    ),
    ("PWR_SUPPLY 1300W Delta power supply", None, {"wattage": 1300}),
    ("150W NE0152T/AS4610 Lenovo/Mellanox PSU", None, {"wattage": 150}),
    (
        "Hon-Kwang AC Power Supply Charger 7.5VDC 300mA",  # 7.5VDC/300mA are not wattages
        None,
        {"psu_class": "AC-DC (External/Adapter)"},
    ),
    (
        "1200W AC Common Slot (CS) hot-plug power supply",
        None,
        {"wattage": 1200, "psu_class": "Server/Redundant"},
    ),
    (
        "HPE X311 400W 100 240VAC TO 12VDC POWER SUPPLY",  # VAC/VDC don't match — 400 only
        None,
        {"wattage": 400},
    ),
    (
        "AC Adapter;PA-1300-42;20V/1.5A;UL;white",  # 1300 inside the MPN, no W — class only
        None,
        {"psu_class": "AC-DC (External/Adapter)"},
    ),
    # ── deliberate misses (conservative > wrong) ─────────────────────────
    ("PS MT 250WENT17 92%EFF 12V2OUT", "power_supplies", {}),  # glued 250WENT17, no boundary
    ("PCA, PSUPLY BPLN 370, 240VA", "power_supplies", {}),  # VA is not W; PCA lead is neutral
    ("Aristocrat  MK7-400-1 power supply", None, {}),  # 400 inside the MPN, no W token
    ("CHANGER WALL FOR MZ220- MZ320", "power_supplies", {}),  # CHANGER ≠ CHARGER
    ("HP Aruba E3800 48G POE 4SFP SWITCH, one Power Supply", None, {}),  # switch row — nothing
]


@pytest.mark.parametrize("description,hint,expected", CASES)
def test_psu_extract_exact(description, hint, expected):
    result = extract_desc(description, commodity_hint=hint)
    assert result is not None, f"{description!r} did not extract"
    assert result.commodity == "power_supplies"
    assert result.specs == expected
    assert result.confidence == 0.90


def test_two_distinct_wattages_omit_the_key():
    # Conflict pin for the unique-survivor contract: 750W vs 800W ⇒ wattage omitted
    # (never max()/first-match picked); the hot-plug class still extracts.
    result = extract_desc("PSU, 750W or 800W hot-plug power supply")
    assert result is not None
    assert result.specs == {"psu_class": "Server/Redundant"}


def test_two_distinct_psu_classes_omit_the_key():
    # ATX vs AC-DC adapter ⇒ psu_class omitted; the unambiguous wattage survives.
    result = extract_desc("PSU, 65W ATX AC adapter")
    assert result is not None
    assert result.specs == {"wattage": 65}


def test_generic_power_supply_emits_no_psu_class():
    # Generic "Power Supply" rows (365 in the corpus) deliberately carry NO psu_class —
    # the commodity already says PSU; a generic member would add zero filter signal.
    result = extract_desc("Power Supply, V7000 Gen2 Expansion 800W, IBM")
    assert result is not None
    assert "psu_class" not in result.specs
