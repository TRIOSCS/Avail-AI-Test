"""
test_integration_requisitions.py — Integration tests for Requisitions endpoints

Tests full request->DB->response cycle for requisition and requirement CRUD.
Uses conftest.py fixtures (SQLite + TestClient with auth overrides).

Called by: pytest
Depends on: conftest.py (client, db_session, test_user fixtures)
"""


# -- Requisition CRUD -----------------------------------------------------


def test_create_requisition(client):
    resp = client.post("/api/requisitions", json={
        "name": "REQ-INT-001", "customer_name": "Test Corp",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "REQ-INT-001"
    assert "id" in data


def test_create_requisition_defaults_name(client):
    resp = client.post("/api/requisitions", json={"customer_name": "X"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Untitled"


def test_list_requisitions_empty(client):
    resp = client.get("/api/requisitions")
    assert resp.status_code == 200
    data = resp.json()
    assert "requisitions" in data
    assert isinstance(data["requisitions"], list)
    assert "total" in data


def test_list_requisitions_after_create(client):
    client.post("/api/requisitions", json={
        "name": "REQ-LIST-001", "customer_name": "ListCo",
    })
    resp = client.get("/api/requisitions")
    assert resp.status_code == 200
    names = [r["name"] for r in resp.json()["requisitions"]]
    assert "REQ-LIST-001" in names


def test_list_requisitions_search_filter(client):
    client.post("/api/requisitions", json={
        "name": "REQ-ALPHA", "customer_name": "Alpha Inc",
    })
    client.post("/api/requisitions", json={
        "name": "REQ-BETA", "customer_name": "Beta LLC",
    })
    resp = client.get("/api/requisitions?q=ALPHA")
    assert resp.status_code == 200
    names = [r["name"] for r in resp.json()["requisitions"]]
    assert "REQ-ALPHA" in names


def test_archive_requisition(client):
    create = client.post("/api/requisitions", json={
        "name": "REQ-ARCH", "customer_name": "ArchCo",
    })
    req_id = create.json()["id"]
    resp = client.put(f"/api/requisitions/{req_id}/archive")
    assert resp.status_code == 200
    assert resp.json()["status"] == "archived"
    # Toggle back
    resp2 = client.put(f"/api/requisitions/{req_id}/archive")
    assert resp2.json()["status"] == "active"


def test_archive_nonexistent(client):
    resp = client.put("/api/requisitions/99999/archive")
    assert resp.status_code == 404


# -- Requirement CRUD -----------------------------------------------------


def test_add_requirement(client):
    create = client.post("/api/requisitions", json={"name": "REQ-ITEMS"})
    req_id = create.json()["id"]
    # Endpoint expects a list or single dict (not {"items": [...]})
    resp = client.post(f"/api/requisitions/{req_id}/requirements", json=[
        {"primary_mpn": "LM317T", "target_qty": 500},
    ])
    assert resp.status_code == 200
    assert resp.json()[0]["primary_mpn"] == "LM317T"


def test_add_multiple_requirements(client):
    create = client.post("/api/requisitions", json={"name": "REQ-MULTI"})
    req_id = create.json()["id"]
    resp = client.post(f"/api/requisitions/{req_id}/requirements", json=[
        {"primary_mpn": "LM317T", "target_qty": 100},
        {"primary_mpn": "NE555P", "target_qty": 200},
        {"primary_mpn": "LM7805", "target_qty": 300},
    ])
    assert resp.status_code == 200
    assert len(resp.json()) == 3


def test_add_requirement_skips_blank_mpn(client):
    create = client.post("/api/requisitions", json={"name": "REQ-BLANK"})
    req_id = create.json()["id"]
    resp = client.post(f"/api/requisitions/{req_id}/requirements", json=[
        {"primary_mpn": "", "target_qty": 10},
        {"primary_mpn": "VALID-MPN", "target_qty": 20},
    ])
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["primary_mpn"] == "VALID-MPN"


def test_list_requirements(client):
    create = client.post("/api/requisitions", json={"name": "REQ-LISTREQ"})
    req_id = create.json()["id"]
    client.post(f"/api/requisitions/{req_id}/requirements", json=[
        {"primary_mpn": "AD8045", "target_qty": 50},
    ])
    resp = client.get(f"/api/requisitions/{req_id}/requirements")
    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["primary_mpn"] == "AD8045"
    assert data[0]["target_qty"] == 50


def test_delete_requirement(client):
    create = client.post("/api/requisitions", json={"name": "REQ-DEL"})
    req_id = create.json()["id"]
    items = client.post(f"/api/requisitions/{req_id}/requirements", json=[
        {"primary_mpn": "TMP123", "target_qty": 10},
    ]).json()
    item_id = items[0]["id"]

    resp = client.delete(f"/api/requirements/{item_id}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    remaining = client.get(f"/api/requisitions/{req_id}/requirements").json()
    assert len(remaining) == 0


def test_update_requirement(client):
    create = client.post("/api/requisitions", json={"name": "REQ-UPD"})
    req_id = create.json()["id"]
    items = client.post(f"/api/requisitions/{req_id}/requirements", json=[
        {"primary_mpn": "OLD-MPN", "target_qty": 10},
    ]).json()
    item_id = items[0]["id"]

    resp = client.put(f"/api/requirements/{item_id}", json={
        "primary_mpn": "NEW-MPN", "target_qty": 999,
    })
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    # Verify via list
    reqs = client.get(f"/api/requisitions/{req_id}/requirements").json()
    updated = [r for r in reqs if r["id"] == item_id][0]
    assert updated["primary_mpn"] == "NEW-MPN"
    assert updated["target_qty"] == 999


# -- Saved Sightings (GET, no re-search) ----------------------------------


def test_get_saved_sightings_empty(client):
    """Req with no sightings returns empty dict."""
    resp = client.post("/api/requisitions", json={
        "name": "REQ-SIGHT-EMPTY", "customer_name": "Test",
    })
    req_id = resp.json()["id"]
    resp = client.get(f"/api/requisitions/{req_id}/sightings")
    assert resp.status_code == 200
    assert resp.json() == {}


def test_get_saved_sightings_returns_data(client, db_session):
    """Sightings saved in DB are returned grouped by requirement."""
    from app.models import Requirement, Sighting

    resp = client.post("/api/requisitions", json={
        "name": "REQ-SIGHT-DATA", "customer_name": "SightCo",
    })
    req_id = resp.json()["id"]
    client.post(f"/api/requisitions/{req_id}/requirements", json={
        "primary_mpn": "LM358N",
    })
    # Get the requirement ID from the list endpoint
    reqs = client.get(f"/api/requisitions/{req_id}/requirements").json()
    item_id = reqs[0]["id"]

    # Insert sightings directly in DB
    s1 = Sighting(
        requirement_id=item_id, vendor_name="Acme Chips",
        mpn_matched="LM358N", qty_available=500, unit_price=0.45,
        source_type="brokerbin", score=82.0,
    )
    s2 = Sighting(
        requirement_id=item_id, vendor_name="Beta Semi",
        mpn_matched="LM358N", qty_available=200, unit_price=0.55,
        source_type="nexar", score=75.0, is_authorized=True,
    )
    db_session.add_all([s1, s2])
    db_session.commit()

    resp = client.get(f"/api/requisitions/{req_id}/sightings")
    assert resp.status_code == 200
    data = resp.json()
    assert str(item_id) in data
    group = data[str(item_id)]
    assert group["label"] == "LM358N"
    assert len(group["sightings"]) == 2
    # Sorted by score desc — Acme (82) first
    assert group["sightings"][0]["vendor_name"] == "Acme Chips"
    assert group["sightings"][0]["score"] == 82.0
    assert group["sightings"][1]["vendor_name"] == "Beta Semi"


def test_get_saved_sightings_404_bad_req(client):
    """Non-existent req returns 404."""
    resp = client.get("/api/requisitions/99999/sightings")
    assert resp.status_code == 404
