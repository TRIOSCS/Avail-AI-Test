"""Unit 6 — category normalizer maps free-text variants to canonical commodity keys."""

from app.services.category_normalizer import normalize_category


def test_known_alias_maps_to_canonical():
    assert normalize_category("solid state drives - ssd") == "ssd"
    assert normalize_category("connectors, interconnects") == "connectors"
    assert normalize_category("memory - modules, cards") == "dram"
    assert normalize_category("battery products") == "batteries"


def test_canonical_value_passes_through():
    assert normalize_category("ssd") == "ssd"
    assert normalize_category("connectors") == "connectors"


def test_case_insensitive_and_trimmed():
    assert normalize_category("  SSD  ") == "ssd"
    assert normalize_category("Connectors, Interconnects") == "connectors"


def test_unknown_returns_none():
    # Ambiguous generics are intentionally NOT mapped.
    assert normalize_category("integrated circuits (ics)") is None
    assert normalize_category("random garbage xyz") is None


def test_empty_or_none_returns_none():
    assert normalize_category(None) is None
    assert normalize_category("") is None
    assert normalize_category("   ") is None


def test_idempotent():
    once = normalize_category("solid state drives - ssd")
    assert once == "ssd"
    assert normalize_category(once) == "ssd"
