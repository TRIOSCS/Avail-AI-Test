"""test_integration_requisitions.py — Integration tests for Requisitions endpoints.

Tests full request->DB->response cycle for requisition and requirement CRUD.
Uses conftest.py fixtures (SQLite + TestClient with auth overrides).

Called by: pytest
Depends on: conftest.py (client, db_session, test_user fixtures)
"""

from datetime import UTC, datetime

import pytest

from app.models import BuyPlan, BuyPlanLine, Offer, Quote, QuoteLine, Requirement

pytestmark = pytest.mark.slow


def _create_req(client, **fields) -> int:
    """POST a requisition and return its id."""
    resp = client.post("/api/requisitions", json=fields)
    assert resp.status_code == 200
    return resp.json()["id"]


def _add_requirements(client, req_id: int, items) -> dict:
    """POST requirements to a requisition and return the parsed JSON response."""
    return client.post(f"/api/requisitions/{req_id}/requirements", json=items).json()


# -- Requisition CRUD -----------------------------------------------------


def test_create_requisition(client):
    resp = client.post(
        "/api/requisitions",
        json={
            "name": "REQ-INT-001",
            "customer_name": "Test Corp",
        },
    )
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
    _create_req(client, name="REQ-LIST-001", customer_name="ListCo")
    resp = client.get("/api/requisitions")
    assert resp.status_code == 200
    names = [r["name"] for r in resp.json()["requisitions"]]
    assert "REQ-LIST-001" in names


def test_list_requisitions_search_filter(client):
    _create_req(client, name="REQ-ALPHA", customer_name="Alpha Inc")
    _create_req(client, name="REQ-BETA", customer_name="Beta LLC")
    resp = client.get("/api/requisitions?q=ALPHA")
    assert resp.status_code == 200
    names = [r["name"] for r in resp.json()["requisitions"]]
    assert "REQ-ALPHA" in names


# -- Requirement CRUD -----------------------------------------------------


def test_add_requirement(client):
    req_id = _create_req(client, name="REQ-ITEMS")
    # Endpoint expects a list or single dict (not {"items": [...]})
    resp = client.post(
        f"/api/requisitions/{req_id}/requirements",
        json=[
            {"primary_mpn": "LM317T", "manufacturer": "TI", "target_qty": 500},
        ],
    )
    assert resp.status_code == 200
    assert resp.json()["created"][0]["primary_mpn"] == "LM317T"


def test_add_multiple_requirements(client):
    req_id = _create_req(client, name="REQ-MULTI")
    resp = client.post(
        f"/api/requisitions/{req_id}/requirements",
        json=[
            {"primary_mpn": "LM317T", "manufacturer": "TI", "target_qty": 100},
            {"primary_mpn": "NE555P", "manufacturer": "TI", "target_qty": 200},
            {"primary_mpn": "LM7805", "manufacturer": "TI", "target_qty": 300},
        ],
    )
    assert resp.status_code == 200
    assert len(resp.json()["created"]) == 3


def test_add_requirement_skips_blank_mpn(client):
    req_id = _create_req(client, name="REQ-BLANK")
    resp = client.post(
        f"/api/requisitions/{req_id}/requirements",
        json=[
            {"primary_mpn": "", "manufacturer": "TI", "target_qty": 10},
            {"primary_mpn": "VALID-MPN", "manufacturer": "TI", "target_qty": 20},
        ],
    )
    assert resp.status_code == 200
    assert len(resp.json()["created"]) == 1
    assert resp.json()["created"][0]["primary_mpn"] == "VALID-MPN"


def test_list_requirements(client):
    req_id = _create_req(client, name="REQ-LISTREQ")
    _add_requirements(
        client,
        req_id,
        [{"primary_mpn": "AD8045", "manufacturer": "ADI", "target_qty": 50}],
    )
    resp = client.get(f"/api/requisitions/{req_id}/requirements")
    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["primary_mpn"] == "AD8045"
    assert data[0]["target_qty"] == 50


def test_delete_requirement(client):
    req_id = _create_req(client, name="REQ-DEL")
    items = _add_requirements(
        client,
        req_id,
        [{"primary_mpn": "TMP123", "manufacturer": "TI", "target_qty": 10}],
    )
    item_id = items["created"][0]["id"]

    resp = client.delete(f"/api/requirements/{item_id}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    remaining = client.get(f"/api/requisitions/{req_id}/requirements").json()
    assert len(remaining) == 0


def test_delete_requirement_blocked_when_offer_referenced_by_buy_plan_line(client, db_session, test_user):
    """A requirement whose offer has already been picked onto a buy plan line must NOT
    be deletable — deleting it would cascade-delete the Offer and SET NULL the buy plan
    line's offer_id, silently losing the approved plan's offer provenance."""
    req_id = _create_req(client, name="REQ-DEL-BLOCKED")
    items = _add_requirements(
        client,
        req_id,
        [{"primary_mpn": "BP-MPN", "manufacturer": "TI", "target_qty": 10}],
    )
    item_id = items["created"][0]["id"]
    requirement = db_session.get(Requirement, item_id)

    offer = Offer(
        requisition_id=req_id,
        requirement_id=item_id,
        vendor_name="Arrow",
        mpn="BP-MPN",
        normalized_mpn="BP-MPN",
        status="active",
        unit_price=1.23,
        qty_available=50,
        created_at=datetime.now(UTC),
    )
    db_session.add(offer)
    db_session.flush()

    buy_plan = BuyPlan(requisition_id=req_id)
    db_session.add(buy_plan)
    db_session.flush()
    bp_line = BuyPlanLine(
        buy_plan_id=buy_plan.id,
        requirement_id=requirement.id,
        offer_id=offer.id,
        quantity=10,
    )
    db_session.add(bp_line)
    db_session.commit()

    resp = client.delete(f"/api/requirements/{item_id}")
    assert resp.status_code == 409
    assert "buy plan" in resp.json()["error"].lower()
    # The requirement (and its offer) must survive the blocked delete.
    assert db_session.get(Requirement, item_id) is not None
    assert db_session.get(Offer, offer.id) is not None

    # Once the buy plan line no longer references the offer, delete succeeds.
    db_session.delete(bp_line)
    db_session.commit()
    resp2 = client.delete(f"/api/requirements/{item_id}")
    assert resp2.status_code == 200
    assert resp2.json()["ok"] is True
    assert db_session.get(Requirement, item_id) is None


def test_delete_requirement_blocked_when_offer_referenced_by_quote_line(client, db_session, test_user):
    """Same guard, quote side: an offer already saved onto a QuoteLine must block the
    requirement delete too."""
    req_id = _create_req(client, name="REQ-DEL-BLOCKED-Q")
    items = _add_requirements(
        client,
        req_id,
        [{"primary_mpn": "QL-MPN", "manufacturer": "TI", "target_qty": 5}],
    )
    item_id = items["created"][0]["id"]

    offer = Offer(
        requisition_id=req_id,
        requirement_id=item_id,
        vendor_name="Avnet",
        mpn="QL-MPN",
        normalized_mpn="QL-MPN",
        status="active",
        unit_price=2.5,
        qty_available=25,
        created_at=datetime.now(UTC),
    )
    db_session.add(offer)
    db_session.flush()

    quote = Quote(
        requisition_id=req_id,
        quote_number="Q-DEL-GUARD-1",
        revision=1,
        line_items=[],
        created_by_id=test_user.id,
    )
    db_session.add(quote)
    db_session.flush()
    q_line = QuoteLine(quote_id=quote.id, offer_id=offer.id, mpn="QL-MPN", qty=5)
    db_session.add(q_line)
    db_session.commit()

    resp = client.delete(f"/api/requirements/{item_id}")
    assert resp.status_code == 409
    assert "quote" in resp.json()["error"].lower()


def test_update_requirement(client):
    req_id = _create_req(client, name="REQ-UPD")
    items = _add_requirements(
        client,
        req_id,
        [{"primary_mpn": "OLD-MPN", "manufacturer": "TI", "target_qty": 10}],
    )
    item_id = items["created"][0]["id"]

    resp = client.put(
        f"/api/requirements/{item_id}",
        json={
            "primary_mpn": "NEW-MPN",
            "manufacturer": "TI",
            "target_qty": 999,
        },
    )
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
    req_id = _create_req(client, name="REQ-SIGHT-EMPTY", customer_name="Test")
    resp = client.get(f"/api/requisitions/{req_id}/sightings")
    assert resp.status_code == 200
    assert resp.json() == {}


def test_get_saved_sightings_returns_data(client, db_session):
    """Sightings saved in DB are returned grouped by requirement."""
    from app.models import Sighting

    req_id = _create_req(client, name="REQ-SIGHT-DATA", customer_name="SightCo")
    _add_requirements(client, req_id, {"primary_mpn": "LM358N", "manufacturer": "TI"})
    # Get the requirement ID from the list endpoint
    reqs = client.get(f"/api/requisitions/{req_id}/requirements").json()
    item_id = reqs[0]["id"]

    # Insert sightings directly in DB
    from datetime import datetime, timedelta

    now = datetime.now(UTC)
    s1 = Sighting(
        requirement_id=item_id,
        vendor_name="Acme Chips",
        mpn_matched="LM358N",
        qty_available=500,
        unit_price=0.45,
        source_type="brokerbin",
        score=82.0,
        created_at=now - timedelta(hours=1),  # older
    )
    s2 = Sighting(
        requirement_id=item_id,
        vendor_name="Beta Semi",
        mpn_matched="LM358N",
        qty_available=200,
        unit_price=0.55,
        source_type="nexar",
        score=75.0,
        is_authorized=True,
        created_at=now,  # newer
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
    # Sorted by newest first — Beta Semi (created now) before Acme (1h ago)
    assert group["sightings"][0]["vendor_name"] == "Beta Semi"
    assert group["sightings"][1]["vendor_name"] == "Acme Chips"
    assert group["sightings"][1]["score"] == 82.0


def test_get_saved_sightings_404_bad_req(client):
    """Non-existent req returns 404."""
    resp = client.get("/api/requisitions/99999/sightings")
    assert resp.status_code == 404
