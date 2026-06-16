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


from sqlalchemy.orm import Session

from app.config import settings
from app.models import MaterialCard, MaterialSpecFacet
from app.services.commodity_registry import seed_commodity_schemas
from app.services.spec_write_service import record_spec


def _facets(db: Session, card_id: int) -> dict:
    rows = db.query(MaterialSpecFacet).filter_by(material_card_id=card_id).all()
    return {r.spec_key: r for r in rows}


def _component_commodity_with(*keys: str) -> str:
    """A seeded commodity whose schema defines all of *keys* (e.g.

    package/pin_count/rohs).
    """
    import json

    seeds = json.load(open("app/data/commodity_seeds.json"))
    for commodity, specs in seeds.items():
        skeys = {s["spec_key"] for s in specs}
        if set(keys).issubset(skeys):
            return commodity
    raise AssertionError(f"no seeded commodity defines all of {keys}")


def test_description_categorizes_uncategorized_card_at_connector_desc(db_session: Session):
    seed_commodity_schemas(db_session)
    card = MaterialCard(normalized_mpn="mem123", display_mpn="MEM123", category=None)
    db_session.add(card)
    db_session.flush()
    enrichment_data = {
        "manufacturer": "Samsung",
        "confidence": 0.95,
        "source": "digikey",
        "category": None,
        "description": "16GB (1x16GB) DUAL RANK X4 DDR4-2666 REGISTERED ECC MEMORY",
        "package_type": None,
        "pin_count": None,
        "rohs_status": None,
        "datasheet_url": "https://example.com/ds.pdf",
    }
    enrichment._apply_enrichment_to_card(card, enrichment_data, db_session)
    db_session.commit()
    assert card.category == "dram"
    assert card.category_source == "connector_desc"
    assert card.category_tier == 84
    assert card.datasheet_url == "https://example.com/ds.pdf"


def test_structured_fields_recorded_at_vendor_tier_for_component_card(db_session: Session):
    seed_commodity_schemas(db_session)
    commodity = _component_commodity_with("pin_count")
    card = MaterialCard(normalized_mpn="cmp1", display_mpn="CMP1", category=commodity)
    db_session.add(card)
    db_session.flush()
    enrichment_data = {
        "manufacturer": "TE",
        "confidence": 0.95,
        "source": "digikey",
        "category": None,
        "description": None,
        "package_type": "SMD",
        "pin_count": 4,
        "rohs_status": "compliant",
        "datasheet_url": None,
    }
    enrichment._apply_enrichment_to_card(card, enrichment_data, db_session)
    db_session.commit()
    facets = _facets(db_session, card.id)
    assert "pin_count" in facets and facets["pin_count"].source == "digikey_api"
    assert facets["pin_count"].tier == 90


def test_flag_off_skips_harvest(db_session: Session, monkeypatch):
    seed_commodity_schemas(db_session)
    monkeypatch.setattr(settings, "connector_desc_harvest_enabled", False)
    card = MaterialCard(normalized_mpn="mem9", display_mpn="MEM9", category=None)
    db_session.add(card)
    db_session.flush()
    enrichment_data = {
        "manufacturer": "Samsung",
        "confidence": 0.95,
        "source": "digikey",
        "category": None,
        "description": "16GB DDR4-2666 REGISTERED ECC MEMORY",
        "package_type": None,
        "pin_count": None,
        "rohs_status": None,
        "datasheet_url": "https://example.com/x.pdf",
    }
    enrichment._apply_enrichment_to_card(card, enrichment_data, db_session)
    db_session.commit()
    assert card.category is None
    assert card.datasheet_url is None


def test_connector_desc_loses_to_mpn_decode(db_session: Session):
    seed_commodity_schemas(db_session)
    card = MaterialCard(normalized_mpn="mem5", display_mpn="MEM5", category="dram")
    db_session.add(card)
    db_session.flush()
    cache = None
    # A higher-tier decoder value lands first.
    record_spec(db_session, int(card.id), "ddr_type", "DDR4", source="mpn_decode", confidence=0.95, schema_cache=cache)
    db_session.commit()
    enrichment_data = {
        "manufacturer": "Samsung",
        "confidence": 0.95,
        "source": "digikey",
        "category": None,
        "description": "8GB DDR3-1600 REGISTERED ECC MEMORY",
        "package_type": None,
        "pin_count": None,
        "rohs_status": None,
        "datasheet_url": None,
    }
    enrichment._apply_enrichment_to_card(card, enrichment_data, db_session)
    db_session.commit()
    facets = _facets(db_session, card.id)
    # mpn_decode (85) is not clobbered by connector_desc (84).
    assert facets["ddr_type"].value_text == "DDR4"
    assert facets["ddr_type"].source == "mpn_decode"
