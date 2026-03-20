# tests/test_materials_tab.py
"""Integration tests for Materials tab routes."""


def test_materials_list_returns_workspace(client, db_session):
    """GET /v2/partials/materials returns faceted workspace shell."""
    resp = client.get("/v2/partials/materials")
    assert resp.status_code == 200
    assert "materials-workspace" in resp.text
    assert "materialsFilter" in resp.text


def test_materials_faceted_returns_results(client, db_session):
    """GET /v2/partials/materials/faceted returns material rows."""
    from app.models import MaterialCard

    card = MaterialCard(normalized_mpn="INT-TEST-001", display_mpn="INT-TEST-001", manufacturer="TestMfg")
    db_session.add(card)
    db_session.commit()

    resp = client.get("/v2/partials/materials/faceted")
    assert resp.status_code == 200
    assert "INT-TEST-001" in resp.text


def test_materials_faceted_search_mpn(client, db_session):
    """Faceted search by MPN returns matching material."""
    from app.models import MaterialCard

    card = MaterialCard(normalized_mpn="LM358DR", display_mpn="LM358DR")
    db_session.add(card)
    db_session.commit()

    resp = client.get("/v2/partials/materials/faceted?q=LM358")
    assert resp.status_code == 200
    assert "LM358DR" in resp.text


def test_material_detail_returns_html(client, db_session):
    """GET /v2/partials/materials/{id} returns detail page."""
    from app.models import MaterialCard

    card = MaterialCard(normalized_mpn="DETAIL-001", display_mpn="DETAIL-001")
    db_session.add(card)
    db_session.commit()

    resp = client.get(f"/v2/partials/materials/{card.id}")
    assert resp.status_code == 200
    assert "DETAIL-001" in resp.text


def test_material_tab_vendors(client, db_session):
    """GET /v2/partials/materials/{id}/tab/vendors returns vendor table."""
    from app.models import MaterialCard

    card = MaterialCard(normalized_mpn="TAB-001", display_mpn="TAB-001")
    db_session.add(card)
    db_session.commit()

    resp = client.get(f"/v2/partials/materials/{card.id}/tab/vendors")
    assert resp.status_code == 200
    assert "No vendor history" in resp.text


def test_material_tab_customers(client, db_session):
    """Customers tab returns HTML."""
    from app.models import MaterialCard

    card = MaterialCard(normalized_mpn="CUST-001", display_mpn="CUST-001")
    db_session.add(card)
    db_session.commit()

    resp = client.get(f"/v2/partials/materials/{card.id}/tab/customers")
    assert resp.status_code == 200
    assert "No customer purchase history" in resp.text


def test_material_tab_sourcing(client, db_session):
    """Sourcing tab returns HTML."""
    from app.models import MaterialCard

    card = MaterialCard(normalized_mpn="SRC-001", display_mpn="SRC-001")
    db_session.add(card)
    db_session.commit()

    resp = client.get(f"/v2/partials/materials/{card.id}/tab/sourcing")
    assert resp.status_code == 200
    assert "No sourcing activity" in resp.text


def test_material_tab_price_history_empty(client, db_session):
    """Price history tab shows empty state."""
    from app.models import MaterialCard

    card = MaterialCard(normalized_mpn="PRICE-001", display_mpn="PRICE-001")
    db_session.add(card)
    db_session.commit()

    resp = client.get(f"/v2/partials/materials/{card.id}/tab/price_history")
    assert resp.status_code == 200
    assert "Price tracking active" in resp.text


def test_material_detail_shows_cross_references(client, db_session):
    """Cross-references section appears on material detail when data exists."""
    from app.models import MaterialCard

    card = MaterialCard(
        normalized_mpn="XREF-001",
        display_mpn="XREF-001",
        cross_references=[
            {"mpn": "ALT-100", "manufacturer": "Micron"},
            {"mpn": "ALT-200", "manufacturer": "SK Hynix"},
        ],
    )
    db_session.add(card)
    db_session.commit()

    resp = client.get(f"/v2/partials/materials/{card.id}")
    assert resp.status_code == 200
    assert "Crosses" in resp.text
    assert "ALT-100" in resp.text
    assert "ALT-200" in resp.text
    assert "Micron" in resp.text


def test_material_detail_shows_find_crosses_button_when_empty(client, db_session):
    """Crosses section always visible; shows Find Crosses button when no data."""
    from app.models import MaterialCard

    card = MaterialCard(normalized_mpn="NOXREF-001", display_mpn="NOXREF-001")
    db_session.add(card)
    db_session.commit()

    resp = client.get(f"/v2/partials/materials/{card.id}")
    assert resp.status_code == 200
    assert "Crosses" in resp.text
    assert "Find Crosses" in resp.text
    assert "find-crosses" in resp.text


def test_material_tab_unknown_returns_404(client, db_session):
    """Unknown tab name returns 404."""
    from app.models import MaterialCard

    card = MaterialCard(normalized_mpn="UNK-001", display_mpn="UNK-001")
    db_session.add(card)
    db_session.commit()

    resp = client.get(f"/v2/partials/materials/{card.id}/tab/nonexistent")
    assert resp.status_code == 404
