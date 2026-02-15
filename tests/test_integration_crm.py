"""
test_integration_crm.py — Integration tests for CRM endpoints

Tests Companies, CustomerSites, Offers, and Quotes CRUD.

Called by: pytest
Depends on: conftest.py (client, db_session, test_user fixtures)
"""


# -- Companies ----------------------------------------------------------------


def test_create_company(client):
    resp = client.post("/api/companies", json={
        "name": "Acme Electronics", "industry": "Semiconductors",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Acme Electronics"
    assert "id" in data


def test_create_company_missing_name(client):
    resp = client.post("/api/companies", json={"industry": "Test"})
    assert resp.status_code == 422  # Pydantic validation rejects missing required field


def test_list_companies(client):
    client.post("/api/companies", json={"name": "ListCo Alpha"})
    client.post("/api/companies", json={"name": "ListCo Beta"})
    resp = client.get("/api/companies")
    assert resp.status_code == 200
    names = [c["name"] for c in resp.json()]
    assert "ListCo Alpha" in names
    assert "ListCo Beta" in names


def test_update_company(client):
    created = client.post("/api/companies", json={"name": "OldName"}).json()
    resp = client.put(f"/api/companies/{created['id']}", json={
        "name": "NewName", "industry": "Connectors",
    })
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_update_nonexistent_company(client):
    resp = client.put("/api/companies/99999", json={"name": "X"})
    assert resp.status_code == 404


# -- Customer Sites -----------------------------------------------------------


def test_add_site(client):
    co = client.post("/api/companies", json={"name": "SiteCo"}).json()
    resp = client.post(f"/api/companies/{co['id']}/sites", json={
        "site_name": "Austin HQ", "city": "Austin", "state": "TX",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["site_name"] == "Austin HQ"
    assert "id" in data


def test_add_site_missing_name(client):
    co = client.post("/api/companies", json={"name": "SiteCo2"}).json()
    resp = client.post(f"/api/companies/{co['id']}/sites", json={
        "city": "Dallas",
    })
    assert resp.status_code == 422  # Pydantic validation rejects missing required field


def test_add_site_nonexistent_company(client):
    resp = client.post("/api/companies/99999/sites", json={
        "site_name": "Nowhere",
    })
    assert resp.status_code == 404


# -- Offers -------------------------------------------------------------------


def _create_req_with_requirement(client):
    """Helper: create requisition + requirement, return (req_id, requirement_id)."""
    req = client.post("/api/requisitions", json={"name": "OfferReq"}).json()
    items = client.post(f"/api/requisitions/{req['id']}/requirements", json=[
        {"primary_mpn": "LM317T", "target_qty": 100},
    ]).json()
    return req["id"], items[0]["id"]


def test_create_offer(client):
    req_id, req_item_id = _create_req_with_requirement(client)
    resp = client.post(f"/api/requisitions/{req_id}/offers", json={
        "mpn": "LM317T", "vendor_name": "Arrow Electronics",
        "unit_price": 1.25, "qty_available": 500,
        "requirement_id": req_item_id,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["mpn"] == "LM317T"
    assert "id" in data


def test_create_offer_missing_fields(client):
    req_id, _ = _create_req_with_requirement(client)
    resp = client.post(f"/api/requisitions/{req_id}/offers", json={
        "mpn": "LM317T",  # missing vendor_name
    })
    assert resp.status_code == 422  # Pydantic validation rejects missing required field


def test_list_offers(client):
    req_id, req_item_id = _create_req_with_requirement(client)
    client.post(f"/api/requisitions/{req_id}/offers", json={
        "mpn": "LM317T", "vendor_name": "Vendor A", "unit_price": 1.00,
        "requirement_id": req_item_id,
    })
    client.post(f"/api/requisitions/{req_id}/offers", json={
        "mpn": "LM317T", "vendor_name": "Vendor B", "unit_price": 1.50,
        "requirement_id": req_item_id,
    })
    resp = client.get(f"/api/requisitions/{req_id}/offers")
    assert resp.status_code == 200
    data = resp.json()
    # Response is grouped by requirement — each group has an "offers" list
    assert len(data) >= 1
    total_offers = sum(len(g["offers"]) for g in data)
    assert total_offers >= 2


def test_update_offer(client):
    req_id, req_item_id = _create_req_with_requirement(client)
    offer = client.post(f"/api/requisitions/{req_id}/offers", json={
        "mpn": "LM317T", "vendor_name": "UpdateVendor", "unit_price": 1.00,
        "requirement_id": req_item_id,
    }).json()
    resp = client.put(f"/api/offers/{offer['id']}", json={
        "unit_price": 0.95, "lead_time": "2 weeks",
    })
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_delete_offer(client):
    req_id, req_item_id = _create_req_with_requirement(client)
    offer = client.post(f"/api/requisitions/{req_id}/offers", json={
        "mpn": "LM317T", "vendor_name": "DeleteVendor", "unit_price": 2.00,
        "requirement_id": req_item_id,
    }).json()
    resp = client.delete(f"/api/offers/{offer['id']}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_delete_nonexistent_offer(client):
    resp = client.delete("/api/offers/99999")
    assert resp.status_code == 404
