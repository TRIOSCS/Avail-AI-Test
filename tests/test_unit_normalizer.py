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


def test_resistance_mohm_to_ohm():
    assert normalize_value(1.5, "MOhm", "ohms") == 1_500_000


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
