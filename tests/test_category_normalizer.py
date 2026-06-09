"""Unit 6 — category normalizer maps free-text variants to canonical commodity keys."""

import pytest

from app.services.category_normalizer import normalize_category


def test_known_alias_maps_to_canonical():
    assert normalize_category("solid state drives - ssd") == "ssd"
    assert normalize_category("connectors, interconnects") == "connectors"
    assert normalize_category("memory - modules, cards") == "dram"
    assert normalize_category("battery products") == "batteries"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Main Board", "motherboards"),
        ("Hard Drive", "hdd"),
        ("Memory", "dram"),
        ("LCD", "displays"),
        ("LCD ASSY", "displays"),
        ("PSU", "power_supplies"),
        ("Graphics Card", "gpu"),
        ("Tape Drive", "tape_drives"),
        ("IC", "ics_other"),
        ("OEM ASSY", "oem_assemblies"),
    ],
)
def test_trio_sfdc_commodity_codes_map_to_tree_keys(raw, expected):
    """TRIO SFDC part-master Commodity_Code__c vocabulary lands on canonical keys."""
    assert normalize_category(raw) == expected


@pytest.mark.parametrize("raw,expected", [("CPU", "cpu"), ("SSD", "ssd"), ("Other", "other")])
def test_trio_codes_already_canonical_resolve_via_lowercase(raw, expected):
    """TRIO codes that ARE tree keys resolve without needing an alias entry."""
    assert normalize_category(raw) == expected


def test_legacy_generic_ic_bucket_maps_to_ics_other():
    """'Integrated Circuits (ICs)' was ambiguous before ics_other existed; now it
    maps."""
    assert normalize_category("Integrated Circuits (ICs)") == "ics_other"
    assert normalize_category("integrated circuits (ics)") == "ics_other"


def test_legacy_capitalized_canonical_variants_resolve():
    """Capitalized variants of canonical keys (live-DB legacy rows) resolve to
    lowercase."""
    assert normalize_category("Capacitors") == "capacitors"
    assert normalize_category("Resistors") == "resistors"


def test_canonical_value_passes_through():
    assert normalize_category("ssd") == "ssd"
    assert normalize_category("connectors") == "connectors"
    assert normalize_category("tape_drives") == "tape_drives"
    assert normalize_category("ics_other") == "ics_other"
    assert normalize_category("oem_assemblies") == "oem_assemblies"


def test_case_insensitive_and_trimmed():
    assert normalize_category("  SSD  ") == "ssd"
    assert normalize_category("Connectors, Interconnects") == "connectors"
    assert normalize_category("  Hard Drive  ") == "hdd"


def test_unknown_returns_none():
    # Strings with no unambiguous canonical bucket are intentionally NOT mapped.
    assert normalize_category("discrete semiconductor products") is None
    assert normalize_category("random garbage xyz") is None


def test_empty_or_none_returns_none():
    assert normalize_category(None) is None
    assert normalize_category("") is None
    assert normalize_category("   ") is None


def test_idempotent():
    once = normalize_category("solid state drives - ssd")
    assert once == "ssd"
    assert normalize_category(once) == "ssd"
    assert normalize_category(normalize_category("IC")) == "ics_other"


def test_every_alias_target_is_a_tree_key():
    """No alias may point at a key absent from COMMODITY_TREE (silent facet black
    hole)."""
    from app.services.category_normalizer import CATEGORY_ALIASES
    from app.services.commodity_registry import get_all_commodities

    tree_keys = set(get_all_commodities())
    for raw, target in CATEGORY_ALIASES.items():
        assert target in tree_keys, f"alias {raw!r} -> {target!r} is not a COMMODITY_TREE key"
        assert raw == raw.lower().strip(), f"alias key {raw!r} must be lower/trimmed"
