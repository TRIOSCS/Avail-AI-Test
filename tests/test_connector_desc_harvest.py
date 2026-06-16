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


import asyncio
from unittest.mock import patch

from app.services import enrichment


def test_try_connector_config_carries_harvest_fields():
    # A connector result shaped like DigiKey's: rich fields beyond manufacturer/category.
    fake_result = {
        "manufacturer": "Samsung",
        "category": "Memory",
        "description": "16GB DDR4-2666 ECC RDIMM 288-pin",
        "package_type": "DIMM-288",
        "pin_count": 288,
        "rohs_status": "compliant",
        "datasheet_url": "https://example.com/ds.pdf",
    }
    config = {"name": "digikey", "module": "x", "class": "y", "creds": [], "confidence": 0.95}

    class _Conn:
        def __init__(self, *a):
            pass

        async def search(self, mpn):
            return [fake_result]

    with (
        patch.object(enrichment, "get_credential_cached", return_value="cred"),
        patch("importlib.import_module") as imp,
    ):
        imp.return_value = type("M", (), {"y": _Conn})
        out = asyncio.run(enrichment._try_connector_config(config, "MEM123"))

    assert out["manufacturer"] == "Samsung"
    assert out["description"] == "16GB DDR4-2666 ECC RDIMM 288-pin"
    assert out["package_type"] == "DIMM-288"
    assert out["pin_count"] == 288
    assert out["rohs_status"] == "compliant"
    assert out["datasheet_url"] == "https://example.com/ds.pdf"
