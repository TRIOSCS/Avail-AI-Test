"""tests/test_spec_format.py — Unit tests for the human spec-value formatter.

Covers: SI-prefix promotion from canonical units (pF/nH/mOhm/ohms/MHz),
per-unit promotion caps (W never kilo-promotes, V/A stop at kilo),
non-scalable units rendered verbatim, booleans, enums, and junk input.
"""

import pytest

from app.services.spec_format import format_spec_value


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        # Capacitance canonical pF — the motivating case: raw 4700000000 today.
        (4_700_000_000, "pF", "4.7 mF"),
        (100_000, "pF", "100 nF"),
        (4_700, "pF", "4.7 nF"),
        (22, "pF", "22 pF"),
        (1_000_000, "pF", "1 µF"),
        # Inductance canonical nH.
        (10_000, "nH", "10 µH"),
        (100, "nH", "100 nH"),
        # Resistance: ohms and mOhm both normalize onto Ω.
        (10_000, "ohms", "10 kΩ"),
        (4_700_000, "ohms", "4.7 MΩ"),
        (0.05, "ohms", "50 mΩ"),
        (35, "mOhm", "35 mΩ"),
        (1_500, "mOhm", "1.5 Ω"),
        # Frequency canonical MHz; GHz stays GHz.
        (16, "MHz", "16 MHz"),
        (2_400, "MHz", "2.4 GHz"),
        (3.5, "GHz", "3.5 GHz"),
        # Voltage/current: milli promotes up, kilo is the ceiling.
        (50, "V", "50 V"),
        (0.8, "V", "800 mV"),
        (4_000, "V", "4 kV"),
        (0.02, "A", "20 mA"),
        (10_000, "A", "10 kA"),
        # Watts NEVER kilo-promote (a 1200 W PSU is not "1.2 kW").
        (1_200, "W", "1,200 W"),
        (0.25, "W", "250 mW"),
        # Zero doesn't explode.
        (0, "pF", "0 F"),
    ],
)
def test_scalable_units_promote(value, unit, expected):
    assert format_spec_value(value, "numeric", unit) == expected


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        (512, "GB", "512 GB"),
        (16, "cores", "16 cores"),
        (3_000, "mAh", "3,000 mAh"),
        (7_200, "RPM", "7,200 RPM"),
        (0.5, "mm", "0.5 mm"),
        (850, "nm", "850 nm"),
        (10, "%", "10%"),
        (1.35, None, "1.35"),
        (3, "ratio", "3"),  # silent unit — "3 ratio" reads wrong
        (4, "count", "4"),
    ],
)
def test_non_scalable_units_verbatim(value, unit, expected):
    assert format_spec_value(value, "numeric", unit) == expected


def test_booleans_render_yes_no():
    assert format_spec_value(True, "boolean") == "Yes"
    assert format_spec_value(False, "boolean") == "No"
    assert format_spec_value("true", "boolean") == "Yes"
    assert format_spec_value("false", "boolean") == "No"
    # Python bool wins even when the schema says numeric.
    assert format_spec_value(True, "numeric", "V") == "Yes"


def test_enums_and_strings_verbatim():
    assert format_spec_value("DDR4", "enum") == "DDR4"
    assert format_spec_value("X7R", None) == "X7R"


def test_junk_input_degrades():
    assert format_spec_value(None, "numeric", "V") == ""
    # Non-numeric string under a numeric schema renders as-is, never raises.
    assert format_spec_value("see datasheet", "numeric", "V") == "see datasheet"


# Canonical units that DELIBERATELY render verbatim (no SI promotion): counts,
# storage sizes, battery convention, decibels, physical dimensions, etc.
_VERBATIM_OK = {
    "%",
    "AWG",
    "C",
    "CFM",
    "DWPD",
    "GB",
    "KB",
    "KLE",
    "Kb",
    "MB",
    "MB/s",
    "RPM",
    "Vrms",
    "bays",
    "bits",
    "channels",
    "conductors",
    "cores",
    "count",
    "dB",
    "dBm",
    "deg",
    "in",
    "mAh",
    "mNm",
    "mcd",
    "mm",
    "nm",
    "outputs",
    "pins",
    "ports",
    "ppm",
    "ratio",
    "slots",
}


def test_seed_canonical_units_all_classified():
    """Every canonical unit the write path can produce (commodity_seeds.json) must be
    either SI-scalable (_SCALABLE_UNITS) or deliberately verbatim (_VERBATIM_OK above).

    This pins the two unit registries together: adding a canonical unit to the seeds
    without deciding how it renders fails HERE instead of silently showing buyers a
    raw canonical magnitude on the material cards.
    """
    import json
    from pathlib import Path

    from app.services.spec_format import _SCALABLE_UNITS

    seeds = json.loads(Path("app/data/commodity_seeds.json").read_text())
    canonical_units = {
        (row.get("canonical_unit") or row.get("unit"))
        for rows in seeds.values()
        for row in rows
        if row.get("canonical_unit") or row.get("unit")
    }
    unclassified = canonical_units - set(_SCALABLE_UNITS) - _VERBATIM_OK
    assert not unclassified, f"unclassified canonical units (decide: scalable or verbatim): {sorted(unclassified)}"
