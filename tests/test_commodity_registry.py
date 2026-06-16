"""tests/test_commodity_registry.py -- Tests for commodity registry.

Covers: app/services/commodity_registry.py
Depends on: conftest.py, faceted search models
"""

import pytest
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


@pytest.mark.parametrize(
    ("commodity", "expected_group"),
    [
        ("capacitors", "Passives"),
        ("network_cards", "IT / Server Hardware"),
        ("cpu", "Processors & Programmable"),
        ("not_a_real_commodity", "Misc"),
    ],
)
def test_get_parent_group_returns_group_name(commodity, expected_group):
    assert get_parent_group(commodity) == expected_group


def test_trio_taxonomy_additions_have_parents_and_display_names():
    """tape_drives / ics_other / oem_assemblies sit under the right parent groups."""
    from app.services.commodity_registry import get_display_name

    assert get_parent_group("tape_drives") == "Storage & Drives"
    assert get_parent_group("ics_other") == "Semiconductors — ICs"
    assert get_parent_group("oem_assemblies") == "IT / Server Hardware"
    assert get_display_name("tape_drives") == "Tape Drives"
    assert get_display_name("ics_other") == "ICs (General)"
    assert get_display_name("oem_assemblies") == "OEM Assemblies"


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


def test_expanded_seeds_have_minimum_specs():
    """Every commodity should have at least 4 specs after expansion."""
    for commodity, specs in COMMODITY_SPEC_SEEDS.items():
        assert len(specs) >= 4, f"{commodity} has only {len(specs)} specs, expected >= 4"
