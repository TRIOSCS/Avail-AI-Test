"""Unit 6 — category normalizer maps free-text variants to canonical commodity keys."""

import pytest

from app.services.category_normalizer import normalize_category, normalize_trio_category


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
    """TRIO SFDC part-master Commodity_Code__c vocabulary lands on canonical keys via
    the source-scoped entry point (which falls back to the global map for codes that are
    unambiguous everywhere)."""
    assert normalize_trio_category(raw) == expected


@pytest.mark.parametrize("raw,expected", [("CPU", "cpu"), ("SSD", "ssd"), ("Other", "other")])
def test_trio_codes_already_canonical_resolve_via_lowercase(raw, expected):
    """TRIO codes that ARE tree keys resolve without needing an alias entry."""
    assert normalize_category(raw) == expected
    assert normalize_trio_category(raw) == expected


def test_bare_memory_is_source_scoped_not_global():
    """Bare "memory" is only unambiguous inside TRIO's part master (supplier taxonomies
    use it for flash/EEPROM/SRAM too), so the global forward-hook path must leave it
    untouched while the SFDC ingest path resolves it."""
    assert normalize_category("Memory") is None
    assert normalize_trio_category("Memory") == "dram"


def test_trio_source_map_targets_are_tree_keys():
    """Source-scoped TRIO entries obey the same invariants as the global map: lower/
    trimmed keys, targets on canonical COMMODITY_TREE keys."""
    from app.services.category_normalizer import CATEGORY_ALIASES, TRIO_SFDC_COMMODITY_CODES
    from app.services.commodity_registry import get_all_commodities

    tree_keys = set(get_all_commodities())
    for raw, target in TRIO_SFDC_COMMODITY_CODES.items():
        assert raw == raw.lower().strip(), f"TRIO code {raw!r} must be lower/trimmed"
        assert target in tree_keys, f"TRIO code {raw!r} -> {target!r} is not a COMMODITY_TREE key"
        assert raw not in CATEGORY_ALIASES, (
            f"TRIO code {raw!r} is source-scoped and must NOT also be in the global CATEGORY_ALIASES"
        )


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


# ── Runtime alias map ↔ migration 093 frozen snapshot ──────────────────────────────
#
# Migration 093 one-off-normalized legacy material_cards.category rows through a FROZEN
# snapshot of the alias map; the runtime map below is only a forward hook at write time.
# An alias added AFTER 093 therefore leaves all pre-existing rows matching it
# unnormalized (invisible to every commodity filter) unless a backfill ships with it.
# Every post-093 alias key must be registered here with the backfill that covers it
# (a follow-up data migration, or scripts/normalize_categories.py run noted by date),
# so "did we forget a backfill?" fails CI instead of waiting for code-review archaeology.
POST_093_ALIASES: dict[str, str] = {
    # "new alias key": "backfill reference (e.g. migration 09X / script run YYYY-MM-DD)",
}


def _migration_093():
    import importlib.util
    import os

    path = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions", "093_normalize_legacy_categories.py")
    spec = importlib.util.spec_from_file_location("migration_093_for_alias_sync", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_runtime_aliases_are_backfilled_by_093_or_documented():
    """Every runtime alias (global + TRIO source-scoped) is either covered by migration
    093's frozen snapshot or explicitly registered in POST_093_ALIASES with its own
    backfill.

    Shared keys must also keep 093's target — a retargeted alias would strand the rows
    093 already rewrote and needs a follow-up migration, not a silent edit.
    """
    from app.services.category_normalizer import CATEGORY_ALIASES, TRIO_SFDC_COMMODITY_CODES

    frozen = _migration_093()._CATEGORY_ALIASES
    runtime = {**CATEGORY_ALIASES, **TRIO_SFDC_COMMODITY_CODES}
    for raw, target in runtime.items():
        if raw in frozen:
            assert frozen[raw] == target, (
                f"alias {raw!r} was retargeted ({frozen[raw]!r} -> {target!r}) after migration 093 "
                "rewrote rows to the old target — ship a follow-up data migration for the stranded rows"
            )
        else:
            assert raw in POST_093_ALIASES, (
                f"alias {raw!r} -> {target!r} was added after migration 093, so legacy rows matching "
                "it were never normalized (the runtime hook only fires on NEW writes). Ship a backfill "
                "and register the alias in POST_093_ALIASES with the backfill reference."
            )
