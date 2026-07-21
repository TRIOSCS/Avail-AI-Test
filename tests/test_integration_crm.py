"""test_integration_crm.py — Integration tests for CRM endpoints.

Tests Companies, CustomerSites, Offers, and Quotes CRUD.

Called by: pytest
Depends on: conftest.py (client, db_session, test_user fixtures)
"""

import pytest

pytestmark = pytest.mark.slow

# -- Companies ----------------------------------------------------------------


def test_create_company(client):
    resp = client.post(
        "/api/companies",
        json={
            "name": "Acme Electronics",
            "industry": "Semiconductors",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Acme Electronics"
    assert "id" in data


def test_create_company_missing_name(client):
    resp = client.post("/api/companies", json={"industry": "Test"})
    assert resp.status_code == 422  # Pydantic validation rejects missing required field


def test_list_companies(client, db_session, test_user):
    from app.models import Company

    alpha = client.post("/api/companies", json={"name": "ListCo Alpha"}).json()
    beta = client.post("/api/companies?force=true", json={"name": "ListCo Beta"}).json()
    # create_company assigns no owner; own both so they pass the rep-scoped visibility
    # filter on the list endpoint (phase1-authz IDOR fix), matching test_update_company.
    for created in (alpha, beta):
        db_session.get(Company, created["id"]).account_owner_id = test_user.id
    db_session.commit()
    resp = client.get("/api/companies")
    assert resp.status_code == 200
    names = [c["name"] for c in resp.json()["items"]]
    assert "ListCo Alpha" in names
    assert "ListCo Beta" in names


def test_update_company(client, db_session, test_user):
    from app.models import Company

    created = client.post("/api/companies", json={"name": "OldName"}).json()
    # create_company does not assign an owner; make the acting user the owner so the
    # update passes the can_manage_account gate (phase1-authz IDOR fix).
    company = db_session.get(Company, created["id"])
    company.account_owner_id = test_user.id
    db_session.commit()
    resp = client.put(
        f"/api/companies/{created['id']}",
        json={
            "name": "NewName",
            "industry": "Connectors",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_update_nonexistent_company(client):
    resp = client.put("/api/companies/99999", json={"name": "X"})
    assert resp.status_code == 404


def test_add_site_nonexistent_company(client):
    resp = client.post(
        "/api/companies/99999/sites",
        json={
            "site_name": "Nowhere",
        },
    )
    assert resp.status_code == 404


# -- Offers -------------------------------------------------------------------


def _create_req_with_requirement(client):
    """Helper: create requisition + requirement, return (req_id, requirement_id)."""
    req = client.post("/api/requisitions", json={"name": "OfferReq"}).json()
    items = client.post(
        f"/api/requisitions/{req['id']}/requirements",
        json=[
            {"primary_mpn": "LM317T", "manufacturer": "TI", "target_qty": 100},
        ],
    ).json()
    return req["id"], items["created"][0]["id"]


def _make_vendor_card(db_session, normalized_name, display_name, **kwargs):
    """Helper: create + commit a VendorCard, return the refreshed instance."""
    from app.models import VendorCard

    card = VendorCard(
        normalized_name=normalized_name,
        display_name=display_name,
        emails=[],
        phones=[],
        **kwargs,
    )
    db_session.add(card)
    db_session.commit()
    db_session.refresh(card)
    return card


def test_create_offer(client):
    req_id, req_item_id = _create_req_with_requirement(client)
    resp = client.post(
        f"/api/requisitions/{req_id}/offers",
        json={
            "mpn": "LM317T",
            "vendor_name": "Arrow Electronics",
            "unit_price": 1.25,
            "qty_available": 500,
            "requirement_id": req_item_id,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["mpn"] == "LM317T"
    assert "id" in data


def test_create_offer_missing_fields(client):
    req_id, _ = _create_req_with_requirement(client)
    resp = client.post(
        f"/api/requisitions/{req_id}/offers",
        json={
            "mpn": "LM317T",  # missing vendor_name
        },
    )
    assert resp.status_code == 422  # Pydantic validation rejects missing required field


def test_list_offers(client):
    req_id, req_item_id = _create_req_with_requirement(client)
    client.post(
        f"/api/requisitions/{req_id}/offers",
        json={
            "mpn": "LM317T",
            "vendor_name": "Vendor A",
            "unit_price": 1.00,
            "requirement_id": req_item_id,
        },
    )
    client.post(
        f"/api/requisitions/{req_id}/offers",
        json={
            "mpn": "LM317T",
            "vendor_name": "Vendor B",
            "unit_price": 1.50,
            "requirement_id": req_item_id,
        },
    )
    resp = client.get(f"/api/requisitions/{req_id}/offers")
    assert resp.status_code == 200
    data = resp.json()
    # Response is wrapped: {has_new_offers, groups}
    assert "groups" in data
    groups = data["groups"]
    assert len(groups) >= 1
    total_offers = sum(len(g["offers"]) for g in groups)
    assert total_offers >= 2


def test_update_offer(client):
    req_id, req_item_id = _create_req_with_requirement(client)
    offer = client.post(
        f"/api/requisitions/{req_id}/offers",
        json={
            "mpn": "LM317T",
            "vendor_name": "UpdateVendor",
            "unit_price": 1.00,
            "requirement_id": req_item_id,
        },
    ).json()
    resp = client.put(
        f"/api/offers/{offer['id']}",
        json={
            "unit_price": 0.95,
            "lead_time": "2 weeks",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_delete_offer(client):
    req_id, req_item_id = _create_req_with_requirement(client)
    offer = client.post(
        f"/api/requisitions/{req_id}/offers",
        json={
            "mpn": "LM317T",
            "vendor_name": "DeleteVendor",
            "unit_price": 2.00,
            "requirement_id": req_item_id,
        },
    ).json()
    resp = client.delete(f"/api/offers/{offer['id']}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_delete_nonexistent_offer(client):
    resp = client.delete("/api/offers/99999")
    assert resp.status_code == 404


# -- Vendor Dedup on Offer Creation -------------------------------------------


def test_offer_fuzzy_match_reuses_existing_card(client, db_session):
    """Fuzzy match reuses existing VendorCard instead of creating a new one."""
    card = _make_vendor_card(db_session, "arrow electronics", "Arrow Electronics", sighting_count=10)

    req_id, req_item_id = _create_req_with_requirement(client)
    # "Arrow Electronic" (missing trailing 's') — close enough for fuzzy ≥88
    resp = client.post(
        f"/api/requisitions/{req_id}/offers",
        json={
            "mpn": "LM317T",
            "vendor_name": "Arrow Electronic",
            "requirement_id": req_item_id,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["vendor_card_id"] == card.id
    # The submitted name should be added as an alternate
    db_session.refresh(card)
    assert "Arrow Electronic" in (card.alternate_names or [])


def test_offer_vendor_card_id_skips_name_matching(client, db_session):
    """When vendor_card_id is provided, skip all name matching."""
    card = _make_vendor_card(db_session, "totally different name", "Totally Different Name")

    req_id, req_item_id = _create_req_with_requirement(client)
    resp = client.post(
        f"/api/requisitions/{req_id}/offers",
        json={
            "mpn": "LM317T",
            "vendor_name": "Whatever Name",
            "vendor_card_id": card.id,
            "requirement_id": req_item_id,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["vendor_card_id"] == card.id


def test_offer_exact_match_before_fuzzy(client, db_session):
    """Exact normalized match is preferred over fuzzy match."""
    card = _make_vendor_card(db_session, "mouser electronics", "Mouser Electronics")

    req_id, req_item_id = _create_req_with_requirement(client)
    resp = client.post(
        f"/api/requisitions/{req_id}/offers",
        json={
            "mpn": "LM317T",
            "vendor_name": "Mouser Electronics Inc.",
            "requirement_id": req_item_id,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["vendor_card_id"] == card.id
