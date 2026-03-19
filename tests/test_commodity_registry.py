"""tests/test_commodity_registry.py -- Tests for commodity registry.

Covers: app/services/commodity_registry.py
Depends on: conftest.py, faceted search models
"""

from sqlalchemy.orm import Session

from app.models import CommoditySpecSchema
from app.services.commodity_registry import (
    COMMODITY_SPEC_SEEDS,
    COMMODITY_TREE,
    get_all_commodities,
    get_parent_group,
    seed_commodity_schemas,
)
from tests.conftest import engine  # noqa: F401


def test_commodity_tree_has_parent_groups():
    assert len(COMMODITY_TREE) >= 10


def test_get_all_commodities_returns_flat_list():
    commodities = get_all_commodities()
    assert "capacitors" in commodities
    assert "dram" in commodities
    assert len(commodities) >= 40


def test_get_parent_group_returns_group_name():
    assert get_parent_group("capacitors") == "Passives"
    assert get_parent_group("network_cards") == "IT / Server Hardware"
    assert get_parent_group("cpu") == "Processors & Programmable"


def test_get_parent_group_unknown_returns_misc():
    group = get_parent_group("not_a_real_commodity")
    assert group == "Misc"


def test_seed_commodity_schemas_inserts_rows(db_session: Session):
    count = seed_commodity_schemas(db_session)
    total_expected = sum(len(specs) for specs in COMMODITY_SPEC_SEEDS.values())
    assert count == total_expected

    rows = db_session.query(CommoditySpecSchema).all()
    assert len(rows) == total_expected


def test_seed_commodity_schemas_is_idempotent(db_session: Session):
    seed_commodity_schemas(db_session)
    count2 = seed_commodity_schemas(db_session)
    assert count2 == 0
