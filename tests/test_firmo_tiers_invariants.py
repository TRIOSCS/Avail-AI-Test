"""Drift guard: invariant tests for firmo tier authority tables.

Ensures FIRMO_FIELD_TIER, FIRMO_BASE_TIER, CONTACT_FIELD_TIER, CONTACT_BASE_TIER
remain consistent with the tier-picking policy and don't accidentally introduce
unknown sources or break the intended authority ladder.
"""

from app.services import firmo_tiers as ft

KNOWN = {"manual", "explorium", "lusha", "clay", "apollo", "sam_gov", "hunter", "ai"}


def test_every_field_source_is_known():
    """All sources in per-field tables must be in KNOWN."""
    for field, table in {**ft.FIRMO_FIELD_TIER, **ft.CONTACT_FIELD_TIER}.items():
        for src in table:
            assert src in KNOWN, f"{field}:{src} not in KNOWN sources"


def test_manual_outranks_everything_for_firmo():
    """Manual via base tier (100) >= any per-field value.

    Manual is not in per-field tables, so it reads from base (100). All per-field
    overrides must be <= 100.
    """
    for field in ft.FIRMO_FIELD_TIER:
        manual_tier = ft.firmo_tier(field, "manual")
        max_field_tier = max(ft.FIRMO_FIELD_TIER[field].values())
        assert manual_tier >= max_field_tier, f"{field}: manual tier {manual_tier} < max per-field {max_field_tier}"


def test_ai_is_lowest_nonzero_everywhere():
    """AI must be the minimum tier value in every table where it appears."""
    for field, table in ft.FIRMO_FIELD_TIER.items():
        if "ai" in table:
            assert table["ai"] == min(table.values()), f"{field}: ai tier {table['ai']} is not minimum in {table}"
    for field, table in ft.CONTACT_FIELD_TIER.items():
        if "ai" in table:
            assert table["ai"] == min(table.values()), f"{field}: ai tier {table['ai']} is not minimum in {table}"
