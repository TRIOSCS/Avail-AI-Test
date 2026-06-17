"""Capacitor description→spec extraction over real Mouser distributor strings.

Covers the capacitors module + extract_desc routing: distributor MLCC descriptions
parse to the seeded capacitors facets (capacitance in pF, voltage_rating in V,
dielectric / tolerance / package enums) with NO false positives on a foreign
(resistor / drive) description.

The capacitance assertions pin the seed's canonical-unit normalization: the seeded
``capacitance`` canonical_unit is pF, the extractor emits a plain numeric SpecDict
(record_spec is called with no ``unit``), so 0.1µF and 100nF must BOTH arrive as
100000 pF — the same number, normalized consistently before the write.
"""

import json
from pathlib import Path

from app.services.desc_extractor import extract_desc
from app.services.desc_extractor._common import SPEC_COMMODITIES
from app.services.desc_extractor.capacitor import extract_capacitor

# 0.1 µF = 100 nF = 100,000 pF — the seeded canonical capacitance unit is pF.
_UF_TO_PF = 1_000_000
_NF_TO_PF = 1_000


def test_capacitors_is_a_spec_commodity():
    assert "capacitors" in SPEC_COMMODITIES


# ── extract_capacitor: pure parsing over the uppercased description ──
# (extract_desc uppercases before dispatch, so the unit fixtures are uppercased here)


def test_uf_microfarad_capacitance_normalizes_to_pf():
    specs = extract_capacitor("MULTILAYER CERAMIC CAPACITORS MLCC - SMD/SMT 16V 0.1UF X7R 0402 10%")
    assert specs["capacitance"] == 0.1 * _UF_TO_PF  # 100000 pF
    assert specs["voltage_rating"] == 16
    assert specs["dielectric"] == "X7R"
    assert specs["package"] == "0402"
    assert specs["tolerance"] == "±10%"


def test_nf_nanofarad_capacitance_normalizes_to_same_pf():
    specs = extract_capacitor("MULTILAYER CERAMIC CAPACITORS MLCC - SMD/SMT 100NF+/-10% 16V X7R 0402")
    assert specs["capacitance"] == 100 * _NF_TO_PF  # 100000 pF — identical to 0.1µF
    assert specs["voltage_rating"] == 16
    assert specs["dielectric"] == "X7R"
    assert specs["package"] == "0402"
    assert specs["tolerance"] == "±10%"


def test_x5r_0201_10v_variant():
    specs = extract_capacitor("MULTILAYER CERAMIC CAPACITORS MLCC - SMD/SMT 100NF+/-10% 10V X5R 0201")
    assert specs["capacitance"] == 100 * _NF_TO_PF
    assert specs["voltage_rating"] == 10
    assert specs["dielectric"] == "X5R"
    assert specs["package"] == "0201"  # not a seeded package enum → omitted downstream
    assert specs["tolerance"] == "±10%"


# ── extract_desc routing: a Mouser MLCC description routes to capacitors ──


def test_extract_desc_routes_mouser_mlcc_to_capacitors():
    result = extract_desc("Multilayer Ceramic Capacitors MLCC - SMD/SMT 16V 0.1uF X7R 0402 10%")
    assert result is not None
    assert result.commodity == "capacitors"
    assert result.specs["capacitance"] == 0.1 * _UF_TO_PF
    assert result.specs["voltage_rating"] == 16
    assert result.specs["dielectric"] == "X7R"
    assert result.specs["package"] == "0402"
    assert result.specs["tolerance"] == "±10%"


def test_extract_desc_routes_nf_variant_to_capacitors():
    result = extract_desc("Multilayer Ceramic Capacitors MLCC - SMD/SMT 100nF+/-10% 16V X7R 0402")
    assert result is not None
    assert result.commodity == "capacitors"
    assert result.specs["capacitance"] == 100 * _NF_TO_PF


def test_capacitor_hint_routes_capacitor_vocabulary():
    # A capacitors-categorized card routes its own facets even without a strong lead.
    result = extract_desc("Cap, 0.1uF 16V X7R 0402 10%", commodity_hint="capacitors")
    assert result is not None
    assert result.commodity == "capacitors"
    assert result.specs["capacitance"] == 0.1 * _UF_TO_PF


# ── negative / cross-family guard: a resistor or drive must NOT parse as a capacitor ──


def test_y5v_asymmetric_temperature_coefficient_is_not_a_tolerance():
    # Y5V caps list their temperature characteristic as "-20%+80%". The "-20%" in that
    # notation is NOT a symmetric ±20% tolerance — a leading minus sign makes it the
    # asymmetric temperature-coefficient lower bound, so tolerance must be omitted.
    specs = extract_capacitor("MULTILAYER CERAMIC CAPACITORS MLCC - SMD/SMT 100NF 16V Y5V 0402 -20%+80%")
    assert "tolerance" not in specs
    assert specs["dielectric"] == "Y5V"
    assert specs["package"] == "0402"


def test_phantom_case_code_does_not_conflict_with_real_package():
    # A stray 4-digit token that is NOT a valid EIA case code ("0302"/"0101") must not
    # be parsed as a package — otherwise it conflicts with the real 0402 and silences it.
    specs = extract_capacitor("MULTILAYER CERAMIC CAPACITORS MLCC - SMD/SMT 16V 0.1UF X7R 0402 SERIES 0302")
    assert specs["package"] == "0402"


def test_resistor_description_emits_no_capacitance():
    # A thin-film resistor description carries Ohms / %, never a farad token: the
    # capacitor route must never invent a capacitance from it.
    specs = extract_capacitor("RES THIN FILM 1406 910M OHM 5% 1/4W CONFORMAL SMD")
    assert "capacitance" not in specs


def test_drive_description_does_not_route_to_capacitors():
    # A hard-drive description must not be mis-routed to capacitors, and even when
    # forced under a capacitors hint it must emit no capacitance (no µF/nF token).
    result = extract_desc('HD, 450GB, 15KRPM, 3.5", Fibre Channel')
    assert result is None or result.commodity != "capacitors"
    forced = extract_desc('HD, 450GB, 15KRPM, 3.5", Fibre Channel', commodity_hint="capacitors")
    if forced is not None:
        assert "capacitance" not in forced.specs


# ── drift guard: every emittable enum / range matches the seeds ──


def test_emittable_capacitor_vocabulary_matches_commodity_seeds():
    seeds = json.loads((Path(__file__).resolve().parents[1] / "app" / "data" / "commodity_seeds.json").read_text())

    def spec(spec_key):
        for s in seeds["capacitors"]:
            if s["spec_key"] == spec_key:
                return s
        raise AssertionError(f"no seeded capacitors.{spec_key}")

    from app.services.desc_extractor.capacitor import (
        _CANONICAL_CAP_UNIT,
        _DIELECTRIC_VOCAB,
        _PACKAGE_VOCAB,
        _TOLERANCE_VOCAB,
        _VOLT_MAX,
        _VOLT_MIN,
    )

    assert _DIELECTRIC_VOCAB <= set(spec("dielectric")["enum_values"])
    assert _TOLERANCE_VOCAB <= set(spec("tolerance")["enum_values"])
    assert _PACKAGE_VOCAB <= set(spec("package")["enum_values"])
    cap = spec("capacitance")
    assert _CANONICAL_CAP_UNIT == cap["canonical_unit"]
    volt = spec("voltage_rating")
    assert (_VOLT_MIN, _VOLT_MAX) == (volt["numeric_range"]["min"], volt["numeric_range"]["max"])
