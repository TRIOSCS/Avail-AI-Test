"""Resistor description→spec extraction over real Mouser distributor strings.

Covers the resistors module + extract_desc routing: distributor thick/thin-film
resistor descriptions parse to the seeded resistors facets (resistance in ohms,
power_rating in W, tolerance / package / mounting enums) with NO false positives on
a foreign (capacitor / inductor / ferrite-bead / drive) description.

The numeric assertions pin the seed's canonical-unit forms: ``resistance`` is a plain
numeric in ohms and ``power_rating`` a plain numeric in W (record_spec is called with
no ``unit``), so "1/16 W" must arrive as 0.0625 and "10K ohms" as 10000 — pre-normalized
to the seeded canonical unit before the write. The seeded resistors tolerance enum is
BARE ("5%"/"1%"/"0.1%"), NOT the ±-prefixed capacitor form, and the package enum has NO
1210 member — both pinned by the drift guard below.
"""

import json
from pathlib import Path

from app.services.desc_extractor import extract_desc
from app.services.desc_extractor._common import SPEC_COMMODITIES
from app.services.desc_extractor.resistor import extract_resistor


def test_resistors_is_a_spec_commodity():
    assert "resistors" in SPEC_COMMODITIES


# ── extract_resistor: pure parsing over the uppercased description ──
# (extract_desc uppercases before dispatch, so the fixtures are uppercased here)


def test_zero_ohms_5pct_0402_aecq200():
    specs = extract_resistor("THICK FILM RESISTORS - SMD 0402 ZERO OHMS 5% TOL AEC-Q200")
    assert specs["resistance"] == 0
    assert specs["package"] == "0402"
    assert specs["tolerance"] == "5%"


def test_zero_ohms_with_working_voltage_and_power_fraction():
    # "50 V" here is a working-voltage rating — resistors have NO voltage_rating seed key,
    # so it must NOT be emitted. "1/16 W" normalizes to the canonical numeric watts.
    specs = extract_resistor("THICK FILM RESISTORS - SMD 0 OHMS 1/16 W 50 V 5 % 0402")
    assert specs["resistance"] == 0
    assert specs["power_rating"] == 0.0625  # 1/16 W
    assert specs["package"] == "0402"
    assert specs["tolerance"] == "5%"
    assert "voltage_rating" not in specs
    assert "voltage" not in specs


def test_watt_fraction_and_bare_zero_ohms():
    specs = extract_resistor("THICK FILM RESISTORS - SMD 1/10 WATT 0OHMS")
    assert specs["power_rating"] == 0.1  # 1/10 W
    assert specs["resistance"] == 0


def test_kilohm_resistance_normalizes_to_ohms():
    specs = extract_resistor("THICK FILM RESISTORS - SMD 10K OHMS 1% 0603")
    assert specs["resistance"] == 10000
    assert specs["tolerance"] == "1%"
    assert specs["package"] == "0603"


def test_decimal_kilohm_value():
    specs = extract_resistor("THICK FILM RESISTORS - SMD 4.7K OHMS 5% 0805")
    assert specs["resistance"] == 4700
    assert specs["tolerance"] == "5%"
    assert specs["package"] == "0805"


def test_rkm_code_value_4m7():
    # The RKM/BS-1852 "4M7" code means 4.7 MΩ (the letter is the decimal point and the
    # multiplier): 4.7e6 ohms. "R" = ×1, "K" = ×1e3, "M" = ×1e6.
    specs = extract_resistor("THICK FILM RESISTORS - SMD 4M7 1% 1206")
    assert specs["resistance"] == 4_700_000
    assert specs["tolerance"] == "1%"
    assert specs["package"] == "1206"


def test_mounting_smd_is_emitted():
    specs = extract_resistor("THICK FILM RESISTORS - SMD 10K OHMS 1% 0603")
    assert specs["mounting"] == "SMD"


def test_decimal_prefixed_percent_is_not_a_tolerance():
    # HIGH-2: the "5" in "0.5%" / "12.5%" and the "1" in "2.1%" must NOT read as a bare
    # resistor tolerance — the word boundary used to fire on the "."→digit transition, so
    # a negative lookbehind on [\d.] is required.
    assert "tolerance" not in extract_resistor("THICK FILM RESISTORS - SMD 10K OHMS 0.5% 0603")
    assert "tolerance" not in extract_resistor("THICK FILM RESISTORS - SMD 10K OHMS 2.1% 0603")
    assert "tolerance" not in extract_resistor("THICK FILM RESISTORS - SMD 10K OHMS 12.5% 0603")


def test_seeded_tolerance_forms_still_parse():
    # The decimal-lookbehind fix must not break the real seeded bare forms.
    assert extract_resistor("THICK FILM RESISTORS - SMD 10K OHMS 0.1% 0603")["tolerance"] == "0.1%"
    assert extract_resistor("THICK FILM RESISTORS - SMD 10K OHMS 1% 0603")["tolerance"] == "1%"
    assert extract_resistor("THICK FILM RESISTORS - SMD 10K OHMS 5% 0603")["tolerance"] == "5%"
    assert extract_resistor("THICK FILM RESISTORS - SMD 10K OHMS ±5% 0603")["tolerance"] == "5%"
    assert extract_resistor("THICK FILM RESISTORS - SMD 10K OHMS +/-5% 0603")["tolerance"] == "5%"
    assert extract_resistor("THICK FILM RESISTORS - SMD 10K OHMS 5 % 0603")["tolerance"] == "5%"
    assert extract_resistor("THICK FILM RESISTORS - SMD 100 OHMS 1/4W 1% 0603")["tolerance"] == "1%"


def test_milliohm_resistance_does_not_parse_as_megaohm():
    # HIGH-3: "1mOhm"/"100mOhms" are milliohm (current-sense/shunt) parts. After .upper()
    # the lowercase "m" collapses into the Mega multiplier "M" — 9 orders of magnitude
    # wrong. The milli multiplier must be detected case-sensitively from the ORIGINAL text.
    assert extract_resistor("1mOhm 1% 2512")["resistance"] == 0.001
    assert extract_resistor("100mOhms 1% 2512")["resistance"] == 0.1


def test_milliohm_routing_via_extract_desc():
    # The milliohm part must still route + extract under a resistors signal.
    result = extract_desc("Current Sense Resistors - SMD 1mOhm 1% 2512", commodity_hint="resistors")
    assert result is not None
    assert result.commodity == "resistors"
    assert result.specs["resistance"] == 0.001


def test_megaohm_resistance_still_parses():
    # The milli fix must NOT touch the Mega path ("M" before OHM, or the "4M7" RKM code).
    assert extract_resistor("1M OHM 1% 0603")["resistance"] == 1_000_000
    assert extract_resistor("4M7 1% 1206")["resistance"] == 4_700_000


def test_milliwatt_is_not_misread_as_watt():
    # HIGH-3 analog (power path): the W unit token requires \bW (the regex is anchored so
    # the wattage number must directly precede "W"/"WATT"), so a milliwatt "0.5mW" → upper
    # "0.5MW" leaves an "M" between the number and "W" and never matches _POWER_DECIMAL —
    # i.e. there is NO milliwatt-as-watt bug to fix (a milliwatt simply emits nothing).
    assert "power_rating" not in extract_resistor("THICK FILM RESISTORS - SMD 10K OHMS 0.5mW 0603")
    assert "power_rating" not in extract_resistor("THICK FILM RESISTORS - SMD 10K OHMS 250mW 0603")
    # A real fractional/decimal watt still parses (regression guard for the analog check).
    assert extract_resistor("THICK FILM RESISTORS - SMD 10K OHMS 0.5W 0603")["power_rating"] == 0.5


# ── extract_desc routing: a Mouser resistor description routes to resistors ──


def test_extract_desc_routes_mouser_thick_film_to_resistors():
    result = extract_desc("Thick Film Resistors - SMD 0402 Zero ohms 5% Tol AEC-Q200")
    assert result is not None
    assert result.commodity == "resistors"
    assert result.specs["resistance"] == 0
    assert result.specs["package"] == "0402"
    assert result.specs["tolerance"] == "5%"


def test_extract_desc_routes_nonzero_resistor():
    result = extract_desc("Thick Film Resistors - SMD 10K ohms 1% 0603")
    assert result is not None
    assert result.commodity == "resistors"
    assert result.specs["resistance"] == 10000
    assert result.specs["tolerance"] == "1%"
    assert result.specs["package"] == "0603"


def test_resistor_hint_routes_resistor_vocabulary():
    # A resistors-categorized card routes its own facets even without a strong lead.
    result = extract_desc("Res, 10K ohms 1% 0603", commodity_hint="resistors")
    assert result is not None
    assert result.commodity == "resistors"
    assert result.specs["resistance"] == 10000


# ── negative / cross-family guard: capacitor / inductor / drive must NOT parse here ──


def test_capacitor_description_emits_no_resistance():
    # An MLCC capacitor description carries a farad token, never an ohm/RKM value: the
    # resistor route must never invent a resistance from it.
    specs = extract_resistor("MULTILAYER CERAMIC CAPACITORS MLCC - SMD/SMT 0.1UF X7R 0402 10%")
    assert "resistance" not in specs


def test_capacitor_description_does_not_route_to_resistors():
    result = extract_desc("Multilayer Ceramic Capacitors MLCC - SMD/SMT 16V 0.1uF X7R 0402 10%")
    assert result is None or result.commodity != "resistors"


def test_drive_description_does_not_route_to_resistors():
    result = extract_desc('HD, 450GB, 15KRPM, 3.5", Fibre Channel')
    assert result is None or result.commodity != "resistors"
    forced = extract_desc('HD, 450GB, 15KRPM, 3.5", Fibre Channel', commodity_hint="resistors")
    if forced is not None:
        assert "resistance" not in forced.specs


def test_ferrite_bead_ohm_does_not_route_to_resistors():
    # Ferrite beads / inductors quote an impedance in OHMs but are NOT resistors — a bare
    # "OHM" token must never route a row to resistors (only RESISTOR(S)/THICK FILM/THIN
    # FILM do). Murata ferrite bead lines must stay unrouted.
    assert extract_desc("Ferrite Beads 330 OHM, Murata Electronics") is None
    assert extract_desc("Inductor, FIXED IND 10UH 690MA 168MOHM SMD, EPCOS - TDK Electronics") is None


def test_phantom_case_code_does_not_conflict_with_real_package():
    # A stray 4-digit token that is NOT a valid EIA case code ("0302") must not be parsed
    # as a package, otherwise it conflicts with the real 0402 and silences it.
    specs = extract_resistor("THICK FILM RESISTORS - SMD 0402 10K OHMS 1% SERIES 0302")
    assert specs["package"] == "0402"


# ── drift guard: every emittable enum / unit matches the seeds ──


def test_emittable_resistor_vocabulary_matches_commodity_seeds():
    seeds = json.loads((Path(__file__).resolve().parents[1] / "app" / "data" / "commodity_seeds.json").read_text())

    def spec(spec_key):
        for s in seeds["resistors"]:
            if s["spec_key"] == spec_key:
                return s
        raise AssertionError(f"no seeded resistors.{spec_key}")

    from app.services.desc_extractor.resistor import (
        _CANONICAL_OHM_UNIT,
        _CANONICAL_POWER_UNIT,
        _MOUNTING_VOCAB,
        _PACKAGE_VOCAB,
        _TOLERANCE_VOCAB,
    )

    assert _TOLERANCE_VOCAB <= set(spec("tolerance")["enum_values"])
    assert _PACKAGE_VOCAB <= set(spec("package")["enum_values"])
    assert _MOUNTING_VOCAB <= set(spec("mounting")["enum_values"])
    assert _CANONICAL_OHM_UNIT == spec("resistance")["canonical_unit"]
    assert _CANONICAL_POWER_UNIT == spec("power_rating")["canonical_unit"]
