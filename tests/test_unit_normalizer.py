"""tests/test_unit_normalizer.py -- Tests for unit normalization.

Covers: app/services/unit_normalizer.py
Depends on: conftest.py
"""

from app.services.unit_normalizer import normalize_value


def test_capacitance_uf_to_pf():
    assert normalize_value(100, "uF", "pF") == 100_000_000


def test_capacitance_nf_to_pf():
    assert normalize_value(100, "nF", "pF") == 100_000


def test_capacitance_pf_to_pf():
    assert normalize_value(47, "pF", "pF") == 47


def test_resistance_kohm_to_ohm():
    assert normalize_value(4.7, "kOhm", "ohms") == 4700


def test_resistance_megaohm_to_ohm():
    """MegaOhm converts to ohms (old 'MOhm' key was ambiguous)."""
    assert normalize_value(1.5, "MegaOhm", "ohms") == 1_500_000


def test_inductance_uh_to_nh():
    assert normalize_value(10, "uH", "nH") == 10_000


def test_inductance_mh_to_nh():
    assert normalize_value(1, "mH", "nH") == 1_000_000


def test_frequency_ghz_to_mhz():
    assert normalize_value(3.2, "GHz", "MHz") == 3200


def test_same_unit_passthrough():
    assert normalize_value(42, "V", "V") == 42


def test_unknown_conversion_returns_original():
    assert normalize_value(99, "widgets", "gadgets") == 99


def test_string_value_passthrough():
    """Non-numeric values pass through unchanged."""
    assert normalize_value("DDR4", None, None) == "DDR4"


def test_resistance_unicode_kohm():
    """KΩ (Unicode omega) converts to ohms."""
    assert normalize_value(10, "kΩ", "ohms") == 10_000


def test_capacitance_unicode_uf():
    """ΜF (Unicode micro) converts to pF."""
    assert normalize_value(100, "µF", "pF") == 100_000_000


def test_resistance_ohm_to_ohms():
    """Singular 'ohm' converts to 'ohms'."""
    assert normalize_value(5, "ohm", "ohms") == 5


def test_resistance_unicode_megaohm():
    """MΩ (Unicode mega-ohms) converts to ohms."""
    assert normalize_value(0.5, "MΩ", "ohms") == 500_000


def test_milliohm_identity():
    """Milliohm to mOhm identity conversion."""
    assert normalize_value(45, "milliohm", "mOhm") == 45


def test_inductance_unicode_uh():
    """ΜH (Unicode micro) converts to nH."""
    assert normalize_value(10, "µH", "nH") == 10_000


def test_current_unicode_ua():
    """ΜA (Unicode micro) converts to A."""
    assert normalize_value(500, "µA", "A") == 0.0005


# --- Power and current conversions ---


def test_power_mw_to_w():
    assert normalize_value(500, "mW", "W") == 0.5


def test_power_kw_to_w():
    assert normalize_value(2, "kW", "W") == 2000


def test_current_ma_to_a():
    assert normalize_value(200, "mA", "A") == 0.2


def test_current_ua_to_a():
    result = normalize_value(100, "uA", "A")
    assert abs(result - 0.0001) < 1e-10


# --- String numeric input passes through (not converted) ---


def test_string_numeric_value_passthrough():
    """String '100' is not converted even if units suggest a conversion."""
    result = normalize_value("100", "uF", "pF")
    assert result == "100"
    assert isinstance(result, str)


# --- None units return value unchanged ---


def test_none_from_unit():
    assert normalize_value(100, None, "pF") == 100


def test_none_canonical_unit():
    assert normalize_value(100, "uF", None) == 100


def test_both_units_none():
    assert normalize_value(100, None, None) == 100


# --- Edge cases: zero, negative, very large ---


def test_zero_value_converts():
    assert normalize_value(0, "uF", "pF") == 0


def test_negative_value_converts():
    assert normalize_value(-10, "kOhm", "ohms") == -10_000


def test_very_large_value():
    result = normalize_value(1_000_000, "uF", "pF")
    assert result == 1_000_000_000_000


def test_very_small_fractional_value():
    result = normalize_value(0.001, "mA", "A")
    assert result == 0.001 * 0.001
