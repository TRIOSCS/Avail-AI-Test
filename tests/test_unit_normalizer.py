"""tests/test_unit_normalizer.py -- Tests for unit normalization.

Covers: app/services/unit_normalizer.py
Depends on: conftest.py
"""

import pytest

from app.services.unit_normalizer import normalize_value


@pytest.mark.parametrize(
    ("value", "from_unit", "to_unit", "expected"),
    [
        pytest.param(100, "uF", "pF", 100_000_000, id="capacitance_uf_to_pf"),
        pytest.param(100, "nF", "pF", 100_000, id="capacitance_nf_to_pf"),
        pytest.param(47, "pF", "pF", 47, id="capacitance_pf_to_pf"),
        pytest.param(4.7, "kOhm", "ohms", 4700, id="resistance_kohm_to_ohm"),
        # MegaOhm converts to ohms (old 'MOhm' key was ambiguous).
        pytest.param(1.5, "MegaOhm", "ohms", 1_500_000, id="resistance_megaohm_to_ohm"),
        pytest.param(10, "uH", "nH", 10_000, id="inductance_uh_to_nh"),
        pytest.param(1, "mH", "nH", 1_000_000, id="inductance_mh_to_nh"),
        pytest.param(3.2, "GHz", "MHz", 3200, id="frequency_ghz_to_mhz"),
        pytest.param(42, "V", "V", 42, id="same_unit_passthrough"),
        pytest.param(99, "widgets", "gadgets", 99, id="unknown_conversion_returns_original"),
        # Non-numeric values pass through unchanged.
        pytest.param("DDR4", None, None, "DDR4", id="string_value_passthrough"),
        # KΩ (Unicode omega) converts to ohms.
        pytest.param(10, "kΩ", "ohms", 10_000, id="resistance_unicode_kohm"),
        # µF (Unicode micro) converts to pF.
        pytest.param(100, "µF", "pF", 100_000_000, id="capacitance_unicode_uf"),
        # Singular 'ohm' converts to 'ohms'.
        pytest.param(5, "ohm", "ohms", 5, id="resistance_ohm_to_ohms"),
        # MΩ (Unicode mega-ohms) converts to ohms.
        pytest.param(0.5, "MΩ", "ohms", 500_000, id="resistance_unicode_megaohm"),
        # Milliohm to mOhm identity conversion.
        pytest.param(45, "milliohm", "mOhm", 45, id="milliohm_identity"),
        # µH (Unicode micro) converts to nH.
        pytest.param(10, "µH", "nH", 10_000, id="inductance_unicode_uh"),
        # µA (Unicode micro) converts to A.
        pytest.param(500, "µA", "A", 0.0005, id="current_unicode_ua"),
        # Power and current conversions.
        pytest.param(500, "mW", "W", 0.5, id="power_mw_to_w"),
        pytest.param(2, "kW", "W", 2000, id="power_kw_to_w"),
        pytest.param(200, "mA", "A", 0.2, id="current_ma_to_a"),
        # None units return value unchanged.
        pytest.param(100, None, "pF", 100, id="none_from_unit"),
        pytest.param(100, "uF", None, 100, id="none_canonical_unit"),
        pytest.param(100, None, None, 100, id="both_units_none"),
        # Edge cases: zero, negative, very large.
        pytest.param(0, "uF", "pF", 0, id="zero_value_converts"),
        pytest.param(-10, "kOhm", "ohms", -10_000, id="negative_value_converts"),
        pytest.param(1_000_000, "uF", "pF", 1_000_000_000_000, id="very_large_value"),
        pytest.param(0.001, "mA", "A", 0.001 * 0.001, id="very_small_fractional_value"),
    ],
)
def test_normalize_value(value, from_unit, to_unit, expected):
    assert normalize_value(value, from_unit, to_unit) == expected


def test_current_ua_to_a():
    result = normalize_value(100, "uA", "A")
    assert abs(result - 0.0001) < 1e-10


def test_string_numeric_value_passthrough():
    """String '100' is not converted even if units suggest a conversion."""
    result = normalize_value("100", "uF", "pF")
    assert result == "100"
    assert isinstance(result, str)
