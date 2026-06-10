"""Routing + safety guards for extract_desc — REAL corpus strings.

Covers: commodity-hint routing, foreign-lead suppression (the "Other,"/"Tray,"
part-master labels), cross-family conflicts, degenerate MPN-as-description, the
non-spec commodity hints (motherboards/power_supplies/cpu), and a drift guard that
re-validates every emittable enum member against app/data/commodity_seeds.json.
"""

import json
from pathlib import Path

import pytest

from app.services.desc_extractor import extract_desc

# ── non-spec commodity hints (callers may use the commodity; specs stay empty) ──
HINT_ONLY_CASES = [
    ("MB, L 80YN, WIN, i-7-7820 HK 8 G - Lenovo", "motherboards"),
    ("MB, Motherboard, Cel 1007U y-TPM, W8p for ThinkPad X131e, Lenovo", "motherboards"),
    ("BDPLANAR Lenovo MB ALC WIN R7-5700U UMA", "motherboards"),  # body "MB" token
    ("CPU 6 Core E5-2640 15M Cache - 2.50 GHZ 00D0017, IBM", "cpu"),
    ("SPS-CPU BDW E5-2673v4 20C 2.3Gz 50M 135W", "cpu"),  # hyphenated body token
    ("CPU, I5-6500T, 6M 3.1G, SR2BZ, CM8066201920600, Intel", "cpu"),
    ("Power Supply, V7000 Gen2 Expansion 800W, IBM", "power_supplies"),
]


@pytest.mark.parametrize("description,commodity", HINT_ONLY_CASES)
def test_non_spec_commodity_hint(description, commodity):
    result = extract_desc(description)
    assert result is not None, f"{description!r} did not extract"
    assert result.commodity == commodity
    assert result.specs == {}


# ── none-of-the-above: foreign labels, prose lines, degenerate descriptions ──
NONE_CASES = [
    # capacitor / resistor / inductor prose lines (task-required class)
    "Other, CAP CER 22UF 10V X7R 1206, Yageo",
    "Other, Res Thin Film 1406 910m Ohm 5% 1/4W Conformal SMD, Vishay",
    "Inductor, FIXED IND 10UH 690MA 168MOHM SMD, EPCOS - TDK Electronics",
    "Ferrite Beads 330 OHM, Murata Electronics",
    # drive ACCESSORIES whose descriptions are full of drive tokens — the foreign
    # "Tray,"/"Other," lead must suppress extraction entirely
    'Tray, 3.5", SAS SATA Trays Caddy, IBM',
    'OTHER, 2.5" Hot-Swap, SAS HD Tray, IBM',
    'Other, 3.5" Server HDD Hard Drive tray, IBM',
    "Other,ServerRAID M5110 SAS/SATA Adapter PDA, IBM",
    "Other, DDR4 512Mx64 PC2133 (4GB), Kingston",  # loose module sold under "Other,"
    # foreign commodity labels
    'LCD, 21.5", LG',
    "Card, 1.9Ghz, 2-Way POWER5+ DCM, Processor Card, 36MB L3 Cache 53C4, pSeries z7",
    "VPD card, VPD CARD S824 52FE, IBM",
    # degenerate: the description IS the part number (real rows where desc == Name)
    "BCM84894B0IFSBG",
    "160-10020-01",
    "GG8067402569900",
    # laptop model prose — "20HD" must not read as an HD token
    "T470 (Type 20HD, 20HE) Laptop (ThinkPad) - Type 20HE",
]


@pytest.mark.parametrize("description", NONE_CASES)
def test_none_of_the_above(description):
    assert extract_desc(description) is None, f"{description!r} should not extract"


def test_empty_and_blank_return_none():
    assert extract_desc("") is None
    assert extract_desc("   ") is None
    assert extract_desc(None) is None  # type: ignore[arg-type]


def test_hint_outside_storage_and_memory_returns_none():
    # The extractor only speaks hdd/ssd/dram — a capacitor-categorized card never
    # gets drive facets, no matter what its description says.
    assert extract_desc('HD, 450GB, 15KRPM, 3.5", Fibre Channel', commodity_hint="capacitors") is None
    assert extract_desc("Mem, 16GB DDR4 RDIMM", commodity_hint="motherboards") is None


def test_hint_contradicted_by_foreign_lead_returns_none():
    # Card says hdd, TRIO's own label says "Other," (a cable) — never extract.
    assert extract_desc("Other, SAS Cable, IBM", commodity_hint="hdd") is None


def test_hint_contradicted_by_other_family_lead_returns_none():
    # dram-hinted card whose description is labeled as a motherboard.
    assert (
        extract_desc("MB, ATX server motherboard, LGA 1150 DDR3 1600/1333/1066, SuperMicro", commodity_hint="dram")
        is None
    )


def test_cross_family_conflict_with_storage_hint():
    # hdd-hinted card with DIMM grammar in the body — conflicting families, None.
    assert extract_desc("HDD, 16GB DDR4 RDIMM spares kit", commodity_hint="hdd") is None


def test_ambiguous_body_tokens_without_lead_return_none():
    # Both HDD and SSD appear with no lead label to arbitrate.
    assert extract_desc("Carrier supports SSD and HDD modules") is None


def test_same_family_lead_refines_hint():
    # ssd-labeled description on an hdd-hinted card routes the ssd vocabulary
    # (3.5" is not a seeded ssd member, so it is omitted — not mis-written).
    result = extract_desc('SSD, 600GB, 3.5", FC 4Gb/s, STEC Hikari MLC 41nm', commodity_hint="hdd")
    assert result is not None
    assert result.commodity == "ssd"
    assert result.specs == {"capacity_gb": 600}


def test_emittable_vocabulary_matches_commodity_seeds():
    """Drift guard: every enum member the extractor can emit must exist verbatim in
    app/data/commodity_seeds.json, and the hardcoded numeric-range constants must
    equal the seeded numeric_range (record_spec re-validates enums at runtime but
    performs NO numeric_range check, so the extractor constants are the only range
    gate). A seed rename or range change must fail HERE, loudly, not silently zero
    out extraction coverage."""
    seeds = json.loads((Path(__file__).resolve().parents[1] / "app" / "data" / "commodity_seeds.json").read_text())

    def enum_values(commodity: str, spec_key: str) -> set:
        for spec in seeds[commodity]:
            if spec["spec_key"] == spec_key:
                return set(spec.get("enum_values") or [])
        return set()

    def numeric_range(commodity: str, spec_key: str) -> tuple:
        for spec in seeds[commodity]:
            if spec["spec_key"] == spec_key:
                rng = spec.get("numeric_range") or {}
                return (rng.get("min"), rng.get("max"))
        return (None, None)

    from app.services.desc_extractor.memory import (
        _CAP_MAX,
        _CAP_MIN,
        _RANK_VALID,
        _SPEED_MAX,
        _SPEED_MIN,
        DIMM,
        LRDIMM,
        RDIMM,
        SODIMM,
        UDIMM,
    )
    from app.services.desc_extractor.storage import _FF_VOCAB, _IFACE_VOCAB, _RPM_VOCAB

    for commodity in ("hdd", "ssd"):
        assert _FF_VOCAB[commodity] <= enum_values(commodity, "form_factor")
        assert _IFACE_VOCAB[commodity] <= enum_values(commodity, "interface")
    assert set(_RPM_VOCAB.values()) == enum_values("hdd", "rpm")
    assert {RDIMM, LRDIMM, UDIMM, SODIMM, DIMM} <= enum_values("dram", "form_factor")
    assert {"DDR", "DDR2", "DDR3", "DDR3L", "DDR4", "DDR5"} <= enum_values("dram", "ddr_type")
    assert _RANK_VALID == enum_values("dram", "rank")
    assert (_CAP_MIN, _CAP_MAX) == numeric_range("dram", "capacity_gb")
    assert (_SPEED_MIN, _SPEED_MAX) == numeric_range("dram", "speed_mhz")
