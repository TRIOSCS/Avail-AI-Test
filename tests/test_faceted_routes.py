"""tests/test_faceted_routes.py -- Tests for faceted search HTMX routes.

Covers: Faceted search routes in app/routers/htmx_views.py
Depends on: conftest.py, faceted search models, commodity_registry
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import CommoditySpecSchema, MaterialCard, MaterialSpecFacet
from tests.conftest import engine  # noqa: F401


def test_materials_workspace_renders(client):
    resp = client.get("/v2/partials/materials/workspace")
    assert resp.status_code == 200
    assert "materialsFilter" in resp.text


def test_commodity_tree_renders(client):
    resp = client.get("/v2/partials/materials/filters/tree")
    assert resp.status_code == 200
    assert "Passives" in resp.text


def test_subfilters_renders_for_commodity(client, db_session: Session):
    card = MaterialCard(
        normalized_mpn="ddr4-001",
        display_mpn="DDR4-001",
        manufacturer="MemCo",
        category="dram",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.flush()
    db_session.add(
        CommoditySpecSchema(
            commodity="dram",
            spec_key="ddr_type",
            display_name="DDR Type",
            data_type="enum",
            enum_values=["DDR4", "DDR5"],
            sort_order=1,
            is_filterable=True,
            is_primary=False,
        )
    )
    db_session.add(
        MaterialSpecFacet(
            material_card_id=card.id,
            category="dram",
            spec_key="ddr_type",
            value_text="DDR4",
        )
    )
    db_session.commit()
    resp = client.get("/v2/partials/materials/filters/sub?commodity=dram")
    assert resp.status_code == 200
    assert "DDR Type" in resp.text


def test_subfilters_empty_for_no_commodity(client):
    resp = client.get("/v2/partials/materials/filters/sub")
    assert resp.status_code == 200
    assert resp.text == ""


def test_faceted_results_returns_materials(client, db_session: Session):
    card = MaterialCard(
        normalized_mpn="test-001",
        display_mpn="TEST-001",
        manufacturer="TestCo",
        category="DRAM",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.commit()
    resp = client.get("/v2/partials/materials/faceted?commodity=dram")
    assert resp.status_code == 200
    assert "TEST-001" in resp.text


def test_faceted_results_no_search_bar(client, db_session: Session):
    """When called in faceted mode, the search bar and pill filters are hidden."""
    resp = client.get("/v2/partials/materials/faceted")
    assert resp.status_code == 200
    # The search bar input with hx-get="/v2/partials/materials" should not appear
    assert 'hx-get="/v2/partials/materials"' not in resp.text


def test_faceted_results_bad_sub_filters_json(client):
    """Malformed sub_filters JSON should not crash — treated as empty."""
    resp = client.get("/v2/partials/materials/faceted?sub_filters=NOT_JSON")
    assert resp.status_code == 200


def test_faceted_results_sub_filters_actually_filter(client, db_session: Session):
    """sub_filters should filter results — only matching cards returned."""
    import json

    # Create schema
    db_session.add(
        CommoditySpecSchema(
            commodity="dram",
            spec_key="ddr_type",
            display_name="DDR Type",
            data_type="enum",
            enum_values=["DDR4", "DDR5"],
            sort_order=1,
            is_filterable=True,
            is_primary=False,
        )
    )
    db_session.flush()

    # Create two cards with different spec values
    card_ddr4 = MaterialCard(
        normalized_mpn="ddr4-chip",
        display_mpn="DDR4-CHIP",
        manufacturer="MemCo",
        category="dram",
        created_at=datetime.now(timezone.utc),
    )
    card_ddr5 = MaterialCard(
        normalized_mpn="ddr5-chip",
        display_mpn="DDR5-CHIP",
        manufacturer="MemCo",
        category="dram",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add_all([card_ddr4, card_ddr5])
    db_session.flush()

    db_session.add(
        MaterialSpecFacet(
            material_card_id=card_ddr4.id,
            category="dram",
            spec_key="ddr_type",
            value_text="DDR4",
        )
    )
    db_session.add(
        MaterialSpecFacet(
            material_card_id=card_ddr5.id,
            category="dram",
            spec_key="ddr_type",
            value_text="DDR5",
        )
    )
    db_session.commit()

    # Filter for DDR5 only
    filters_json = json.dumps({"ddr_type": ["DDR5"]})
    resp = client.get(f"/v2/partials/materials/faceted?commodity=dram&sub_filters={filters_json}")
    assert resp.status_code == 200
    assert "DDR5-CHIP" in resp.text
    assert "DDR4-CHIP" not in resp.text
