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
