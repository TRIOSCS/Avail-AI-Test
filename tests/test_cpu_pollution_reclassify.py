"""tests/test_cpu_pollution_reclassify.py — re-classify the recognizable pollution in
the `cpu` catch-all bucket to the correct commodity via deterministic MPN prefixes.

Depends on: conftest.py (db_session), seed_commodity_schemas, MaterialCard,
spec_tiers.SOURCE_TIER (cpu_pollution_fix=96), commodity_registry.CANONICAL_COMMODITY_KEYS.
"""

from app.services.spec_tiers import SOURCE_TIER, tier_for


def test_cpu_pollution_fix_registered_at_tier_96():
    assert SOURCE_TIER["cpu_pollution_fix"] == 96
    assert tier_for("cpu_pollution_fix") == 96
    # Beats the trio_source 'cpu' default (95), loses to manual (100).
    assert SOURCE_TIER["cpu_pollution_fix"] > SOURCE_TIER["trio_source"]
    assert SOURCE_TIER["cpu_pollution_fix"] < SOURCE_TIER["manual"]


import pytest

from app.services.commodity_registry import CANONICAL_COMMODITY_KEYS
from app.services.cpu_pollution.classifier import classify_polluted_mpn
from app.services.cpu_pollution.prefix_map import PREFIX_RULES


def test_every_prefix_rule_targets_valid_vocab():
    for _pattern, commodity in PREFIX_RULES:
        assert commodity in CANONICAL_COMMODITY_KEYS, f"{commodity} not a canonical commodity"


@pytest.mark.parametrize(
    "mpn,expected",
    [
        ("5-1437720-3", "connectors"),  # TE Connectivity
        ("1437259-6", "connectors"),  # TE Connectivity
        ("SSW-114-22-S-S-VS-P-TR", "connectors"),  # Samtec
        ("CLT-110-02-G-D-BE-A-K-TR", "connectors"),  # Samtec
        ("NRWA330M63V6.3X11TBF", "capacitors"),  # Nichicon
        ("TAJD475K050RNJ", "capacitors"),  # AVX tantalum
        ("B32520C474K189", "capacitors"),  # EPCOS film cap
        ("BLM21AJ601SN1D", "inductors"),  # Murata ferrite bead
        ("CRCW12102K21FKEA", "resistors"),  # Vishay
        ("CD74HC123EE4", "logic_ic"),  # TI CD74 logic
        ("74AUP2G14DW-7", "logic_ic"),  # 74-series logic
        ("BCM5488SA7IPBG", "logic_ic"),  # Broadcom
    ],
)
def test_known_pollution_classifies(mpn, expected):
    assert classify_polluted_mpn(mpn) == expected


@pytest.mark.parametrize(
    "cpu_mpn",
    [
        "SR3QS",
        "SL5CH",  # Intel sSpec
        "CM8068403654318",  # Intel ordering code
        "BX8070110700K",  # Intel boxed
        "CD8069504194701",  # Intel ordering — must NOT collide with CD74 logic
        "E5-2680V4",  # Intel model string
        "100-000000053",  # AMD OPN
        "EPYC 7742",  # AMD model word
    ],
)
def test_real_cpu_is_never_reclassified(cpu_mpn):
    assert classify_polluted_mpn(cpu_mpn) is None


def test_unrecognized_and_empty_return_none():
    assert classify_polluted_mpn("ZZQW9981XYZ") is None
    assert classify_polluted_mpn("") is None
    assert classify_polluted_mpn(None) is None
