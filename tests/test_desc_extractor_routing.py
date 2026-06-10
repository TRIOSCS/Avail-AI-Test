"""Routing + safety guards for extract_desc — REAL corpus strings.

Covers: commodity-hint routing, foreign-lead suppression (the "Other,"/"Tray,"
part-master labels), NEUTRAL leads (packaging words / brands / SPS- prefixes fall
through to body+hint arbitration), lead-label dot-strip normalization, cross-family
conflicts, degenerate MPN-as-description, and a drift guard that re-validates every
emittable enum member, numeric-range constant AND the curated cpu model→spec table
against app/data/commodity_seeds.json.

Phase-2 migrations: the motherboards entries and "Power Supply, V7000 …" moved out
of HINT_ONLY_CASES (they now emit board_type / wattage — see
test_desc_extractor_board.py / test_desc_extractor_power.py), and 'LCD, 21.5", LG'
moved out of NONE_CASES (now displays, diagonal_size 21.5 — see
test_desc_extractor_display.py). Wave 3B promoted cpu from hint-only to extracting
(HINT_ONLY_CASES dissolved into test_desc_extractor_cpu.py).
"""

import json
from pathlib import Path

import pytest

from app.services.desc_extractor import extract_desc


def test_cpu_wattage_key_is_structurally_unreachable():
    # "135W" on the cpu route is a TDP: extract_cpu emits tdp_watts and the wattage
    # key only exists on the power_supplies route — no extractor can leak it.
    result = extract_desc("SPS-CPU BDW E5-2673v4 20C 2.3Gz 50M 135W")
    assert result is not None
    assert result.commodity == "cpu"
    assert "wattage" not in result.specs
    assert result.specs["tdp_watts"] == 135
    # A cpu HINT routes the cpu vocabulary (cpu joined SPEC_COMMODITIES in wave 3B).
    hinted = extract_desc("SPS-CPU BDW E5-2673v4 20C 2.3Gz 50M 135W", commodity_hint="cpu")
    assert hinted is not None and hinted.specs == result.specs


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
    # foreign commodity labels — "Card,"/"Library," stay foreign even with handled
    # tokens in the body (the GC bucket is contaminated; a library is not a drive)
    "Card, 1.9Ghz, 2-Way POWER5+ DCM, Processor Card, 36MB L3 Cache 53C4, pSeries z7",
    "Card, FRU ThinkSerRSC_1ux16_v1.0",
    "VPD card, VPD CARD S824 52FE, IBM",
    "Library, 3592 Tape Drive, Jag6 Drive",
    # packaging-SUFFIXED accessory labels stay foreign — "CBL ASSY," mixes a foreign
    # word with a packaging word, so the all-words-neutral rule never rescues it
    # (the body MB token must not emit a System Board for a cable assembly)
    "CBL ASSY,RPS REAR MB, XL",
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


def test_hint_outside_spec_commodities_returns_none():
    # The extractor only speaks the nine SPEC_COMMODITIES — a capacitor-categorized
    # card never gets drive facets, no matter what its description says.
    assert extract_desc('HD, 450GB, 15KRPM, 3.5", Fibre Channel', commodity_hint="capacitors") is None
    assert extract_desc("PSU, 1460W 240V/200V AC Hot Swap for EN 62368-1", commodity_hint="networking") is None


def test_hint_contradicted_by_foreign_lead_returns_none():
    # Card says hdd, TRIO's own label says "Other," (a cable) — never extract.
    assert extract_desc("Other, SAS Cable, IBM", commodity_hint="hdd") is None


def test_hint_contradicted_by_other_family_lead_returns_none():
    # dram-hinted card whose description is labeled as a motherboard.
    assert (
        extract_desc("MB, ATX server motherboard, LGA 1150 DDR3 1600/1333/1066, SuperMicro", commodity_hint="dram")
        is None
    )
    # The contradiction rule covers the phase-2 families too: a "MEMORY," lead on a
    # gpu-hinted card / an "MB," lead on a motherboards-vs-dram mismatch ⇒ None.
    assert extract_desc("Mem, 16GB DDR4 RDIMM", commodity_hint="gpu") is None
    assert extract_desc("Mem, 16GB DDR4 RDIMM", commodity_hint="motherboards") is None


def test_neutral_leads_fall_through_to_body_and_hint():
    # Packaging-word / brand / SPS- leads are NEUTRAL — they must not die foreign.
    # "ASSY," rescued by the body LTO token; "Innolux," by the body LCD token;
    # "MSI," and "SPS-PCA," by the caller's gpu hint (no routing token in the body).
    result = extract_desc("ASSY,DR,BAY,FC,LTO8,TL2/4K")
    assert result is not None and result.commodity == "tape_drives"
    result = extract_desc('Innolux, LCD, 21.5"')
    assert result is not None and result.commodity == "displays"
    result = extract_desc("MSI, RTX3080, 10G/D6X/3DP/H", commodity_hint="gpu")
    assert result is not None and result.commodity == "gpu"
    result = extract_desc("SPS-PCA, NVIDIA Tesla V100 32GB Module", commodity_hint="gpu")
    assert result is not None and result.commodity == "gpu"
    # Without a body token or hint, a neutral lead still extracts NOTHING.
    assert extract_desc("MSI, RTX3080, 10G/D6X/3DP/H") is None


def test_packaging_suffixed_foreign_labels_stay_foreign_even_hinted():
    # The all-words-neutral boundary: a label whose last word is packaging but whose
    # other words are foreign ("Drive Tray Kit,"/"Cable Assy,") must NOT fall through
    # to body/hint arbitration — an accessory row would otherwise take the facets of
    # the part it fits (wrong facet values are worse than missing ones).
    assert extract_desc("Drive Tray Kit, 600GB 15K rpm SAS HDD trays", commodity_hint="hdd") is None
    assert extract_desc('Cable Assy, for LCD 15.6" FHD panel', commodity_hint="displays") is None
    # All-words-neutral labels still rescue: brand+packaging falls through to the hint.
    result = extract_desc("SUPERMICRO FRU,DAUGHTER CARD REPLACEMENT KIT", commodity_hint="motherboards")
    assert result is not None and result.commodity == "motherboards"


def test_neutral_lead_keeps_phase1_conflict_guards():
    # Behind a neutral brand lead, a body mixing HDD+DIMM tokens still hard-conflicts
    # to None (constructed string — the guard predates phase 2 and must survive it).
    assert extract_desc("HP, 16GB DDR4 DIMM + 512GB SSD bundle") is None


# ── phase-2 lead/first-token map coverage: every new entry routes ─────────
# (the two-letter "TD,"/"GC," labels carry the highest future-collision risk —
# a typo or accidental map removal must fail here, not silently lose recall)
PHASE2_LEAD_CASES = [
    ("TD, LTO-6 HH SAS", "tape_drives"),
    ("GC, NVIDIA Quadro P2000 5GB", "gpu"),
    ("PANEL, 15.6 FHD AG", "displays"),
    ("VIDEO CARD, NVIDIA Quadro 600 1GB", "gpu"),
    ("VIDEO BOARD, GeForce GT730 2GB", "gpu"),
    ("GRAPHIC CARD, Radeon RX550 4GB", "gpu"),
    ("MONITOR 27INCH HP EliteDisplay", "displays"),  # comma-less first token
    ("DSPLY 23.8 FHD TOUCH", "displays"),  # comma-less first token
    ("GC NVIDIA T1000 4GB", "gpu"),  # comma-less first token
]


@pytest.mark.parametrize("description,commodity", PHASE2_LEAD_CASES)
def test_phase2_lead_and_first_token_routing(description, commodity):
    result = extract_desc(description)
    assert result is not None, f"{description!r} did not route"
    assert result.commodity == commodity


def test_lead_label_dot_strip_normalization():
    # "PSU., 750W TT ITIC, Acbel…" captures the label "PSU." — the trailing dot is
    # stripped before the map lookup instead of sending the row foreign.
    result = extract_desc("PSU., 750W TT ITIC, Acbel PN FSF061-EL1G")
    assert result is not None
    assert result.commodity == "power_supplies"
    assert result.specs == {"wattage": 750}


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

    # ── phase-2 commodities (power / display / tape / gpu / board) ──
    from app.services.desc_extractor import board, display, gpu, power, tape

    assert {p[0] for p in power._CLASS_PATTERNS} <= enum_values("power_supplies", "psu_class")
    assert (power._WATT_MIN, power._WATT_MAX) == numeric_range("power_supplies", "wattage")

    assert set(display._RES_BY_NAME.values()) <= enum_values("displays", "resolution")
    assert display._RES_SEEDED <= enum_values("displays", "resolution")
    assert display.LED in enum_values("displays", "backlight")
    assert (display._DIAG_MIN, display._DIAG_MAX) == numeric_range("displays", "diagonal_size")

    emittable_drive_types = (
        {f"LTO-{g}" for g in range(3, 10)}
        | {f"TS11{m}" for m in ("40", "50", "55", "60", "70")}
        | set(tape._3592_BY_MODEL.values())
        | set(tape._JAG_BY_GEN.values())
        | {tape.DAT, tape.AIT}
    )
    assert emittable_drive_types == enum_values("tape_drives", "drive_type")
    assert {p[0] for p in tape._IFACE_PATTERNS} == enum_values("tape_drives", "interface")
    assert {p[0] for p in tape._FORM_PATTERNS} <= enum_values("tape_drives", "form_factor")

    gpu_members = {p[0] for p in gpu._FAMILY_PATTERNS} | {gpu.GEFORCE, gpu.RTX}
    assert gpu_members == enum_values("gpu", "gpu_family")
    assert (gpu._MEM_MIN, gpu._MEM_MAX) == numeric_range("gpu", "memory_gb")

    assert {p[0] for p in board._BOARD_PATTERNS} == enum_values("motherboards", "board_type")

    # ── cpu (wave 3B): grammar vocabulary + the WHOLE curated model table ──
    from app.services.desc_extractor import cpu

    arch_enum = enum_values("cpu", "architecture")
    family_enum = enum_values("cpu", "family")
    socket_enum = enum_values("cpu", "socket")
    assert set(cpu._CODENAME_ARCH.values()) <= arch_enum
    assert set(cpu._ARCH_NAMES.values()) <= arch_enum
    assert set(cpu._VN_ARCH.values()) <= arch_enum
    assert {cpu.XEON, cpu.CORE_I, cpu.EPYC, cpu.RYZEN, cpu.THREADRIPPER, cpu.ATOM} <= family_enum
    assert (cpu._CORE_MIN, cpu._CORE_MAX) == numeric_range("cpu", "core_count")
    assert (cpu._GHZ_MIN, cpu._GHZ_MAX) == numeric_range("cpu", "clock_speed_ghz")
    assert (cpu._TDP_MIN, cpu._TDP_MAX) == numeric_range("cpu", "tdp_watts")
    # Every value in app/data/cpu_model_specs.json must pass the SAME seeded gates
    # the grammar enforces — a table typo must fail HERE, not silently be dropped
    # by record_spec (enums) or sail through unchecked (numerics).
    table = cpu.load_model_specs()
    allowed_keys = {"family", "socket", "core_count", "clock_speed_ghz", "tdp_watts", "architecture"}
    for model, entry in table.items():
        assert model == model.upper().strip(), model  # parser-normalized key form
        assert set(entry) <= allowed_keys, model
        if "family" in entry:
            assert entry["family"] in family_enum, model
        if "socket" in entry:
            assert entry["socket"] in socket_enum, model
        if "architecture" in entry:
            assert entry["architecture"] in arch_enum, model
        if "core_count" in entry:
            assert cpu._CORE_MIN <= entry["core_count"] <= cpu._CORE_MAX, model
        if "clock_speed_ghz" in entry:
            assert cpu._GHZ_MIN <= entry["clock_speed_ghz"] <= cpu._GHZ_MAX, model
        if "tdp_watts" in entry:
            assert cpu._TDP_MIN <= entry["tdp_watts"] <= cpu._TDP_MAX, model
