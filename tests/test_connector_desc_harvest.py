"""tests/test_connector_desc_harvest.py — harvest the structured fields + description
the connector pipeline already fetches (previously discarded), into the F1 ladder.

Depends on: conftest.py (db_session), seed_commodity_schemas, MaterialCard +
MaterialSpecFacet, spec_tiers.SOURCE_TIER (connector_desc=84).
"""

from app.services.spec_tiers import SOURCE_TIER, tier_for


def test_connector_desc_registered_at_tier_84():
    assert SOURCE_TIER["connector_desc"] == 84
    assert tier_for("connector_desc") == 84
    # Above the card's own desc_parse (83), below the deterministic decoders (85).
    assert SOURCE_TIER["connector_desc"] > SOURCE_TIER["desc_parse"]
    assert SOURCE_TIER["connector_desc"] < SOURCE_TIER["mpn_decode"]
