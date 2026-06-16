# tests/test_materials_tab.py
"""Integration tests for Materials tab routes."""

import pytest

from app.models import MaterialCard


def _add_card(db_session, mpn, **kwargs):
    card = MaterialCard(normalized_mpn=mpn, display_mpn=mpn, **kwargs)
    db_session.add(card)
    db_session.commit()
    return card


def test_materials_list_returns_workspace(client, db_session):
    """GET /v2/partials/materials returns faceted workspace shell."""
    resp = client.get("/v2/partials/materials")
    assert resp.status_code == 200
    assert "materials-workspace" in resp.text
    assert "materialsFilter" in resp.text


def test_materials_faceted_returns_results(client, db_session):
    """GET /v2/partials/materials/faceted returns material rows."""
    _add_card(db_session, "INT-TEST-001", manufacturer="TestMfg")

    resp = client.get("/v2/partials/materials/faceted")
    assert resp.status_code == 200
    assert "INT-TEST-001" in resp.text


def test_materials_faceted_search_mpn(client, db_session):
    """Faceted search by MPN returns matching material."""
    _add_card(db_session, "LM358DR")

    resp = client.get("/v2/partials/materials/faceted?q=LM358")
    assert resp.status_code == 200
    assert "LM358DR" in resp.text


@pytest.mark.parametrize(
    ("mpn", "path", "expected_substring"),
    [
        pytest.param("DETAIL-001", "", "DETAIL-001", id="detail"),
        pytest.param("TAB-001", "/tab/vendors", "No vendor history", id="tab_vendors"),
        pytest.param("CUST-001", "/tab/customers", "No customer purchase history", id="tab_customers"),
        pytest.param("SRC-001", "/tab/sourcing", "No sourcing activity", id="tab_sourcing"),
        pytest.param("PRICE-001", "/tab/price_history", "Price tracking active", id="tab_price_history_empty"),
    ],
)
def test_material_detail_and_tabs_render(client, db_session, mpn, path, expected_substring):
    """Material detail and each tab partial return HTML with the expected content."""
    card = _add_card(db_session, mpn)

    resp = client.get(f"/v2/partials/materials/{card.id}{path}")
    assert resp.status_code == 200
    assert expected_substring in resp.text


def test_material_detail_shows_cross_references(client, db_session):
    """Cross-references section appears on material detail when data exists."""
    card = _add_card(
        db_session,
        "XREF-001",
        cross_references=[
            {"mpn": "ALT-100", "manufacturer": "Micron"},
            {"mpn": "ALT-200", "manufacturer": "SK Hynix"},
        ],
    )

    resp = client.get(f"/v2/partials/materials/{card.id}")
    assert resp.status_code == 200
    assert "Crosses" in resp.text
    assert "ALT-100" in resp.text
    assert "ALT-200" in resp.text
    assert "Micron" in resp.text


def test_material_detail_shows_find_crosses_button_when_empty(client, db_session):
    """Crosses section always visible; shows Find Crosses button when no data."""
    card = _add_card(db_session, "NOXREF-001")

    resp = client.get(f"/v2/partials/materials/{card.id}")
    assert resp.status_code == 200
    assert "Crosses" in resp.text
    assert "Find Crosses" in resp.text
    assert "find-crosses" in resp.text


def test_material_tab_unknown_returns_404(client, db_session):
    """Unknown tab name returns 404."""
    card = _add_card(db_session, "UNK-001")

    resp = client.get(f"/v2/partials/materials/{card.id}/tab/nonexistent")
    assert resp.status_code == 404
