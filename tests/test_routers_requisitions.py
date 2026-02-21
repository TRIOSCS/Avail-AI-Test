"""
tests/test_routers_requisitions.py — Tests for routers/requisitions.py

Covers: CRUD for requisitions and requirements, archive/clone,
search endpoints, sighting management, stock import, and access control.

Called by: pytest
Depends on: routers/requisitions.py, conftest fixtures
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest


# ── Requisition CRUD ──────────────────────────────────────────────────


def test_create_requisition(client, db_session, test_user):
    """POST /api/requisitions creates a new draft requisition."""
    resp = client.post("/api/requisitions", json={"name": "REQ-NEW-001"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "REQ-NEW-001"
    assert "id" in data


def test_create_requisition_defaults(client):
    """Requisition defaults to 'Untitled' when name not provided."""
    resp = client.post("/api/requisitions", json={})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Untitled"


def test_create_requisition_with_customer(client, test_customer_site):
    """Requisition can be linked to a customer site."""
    resp = client.post(
        "/api/requisitions",
        json={"name": "Customer RFQ", "customer_site_id": test_customer_site.id},
    )
    assert resp.status_code == 200


def test_list_requisitions_empty(client):
    """GET /api/requisitions returns empty list when none exist."""
    resp = client.get("/api/requisitions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["requisitions"] == []
    assert data["total"] == 0


def test_list_requisitions_with_data(client, test_requisition):
    """Requisition appears in list with computed fields."""
    resp = client.get("/api/requisitions")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["requisitions"]) >= 1
    req = data["requisitions"][0]
    assert req["id"] == test_requisition.id
    assert "requirement_count" in req
    assert "sourcing_score" in req


def test_list_requisitions_search(client, test_requisition):
    """Search filter matches requisition by name."""
    resp = client.get("/api/requisitions", params={"q": "REQ-TEST"})
    assert resp.status_code == 200
    assert len(resp.json()["requisitions"]) >= 1


def test_list_requisitions_search_no_match(client, test_requisition):
    """Search filter returns nothing for non-matching query."""
    resp = client.get("/api/requisitions", params={"q": "NONEXISTENT-XYZ"})
    assert resp.status_code == 200
    assert len(resp.json()["requisitions"]) == 0


def test_list_requisitions_archive_filter(client, db_session, test_requisition):
    """Status=archive shows only archived/won/lost/closed requisitions."""
    test_requisition.status = "archived"
    db_session.commit()
    resp = client.get("/api/requisitions", params={"status": "archive"})
    assert resp.status_code == 200
    assert len(resp.json()["requisitions"]) >= 1


def test_list_requisitions_pagination(client, db_session, test_user):
    """Limit and offset work correctly."""
    from app.models import Requisition

    for i in range(5):
        db_session.add(Requisition(
            name=f"REQ-PAGE-{i}",
            status="open",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        ))
    db_session.commit()
    resp = client.get("/api/requisitions", params={"limit": 2, "offset": 0})
    assert resp.status_code == 200
    assert len(resp.json()["requisitions"]) == 2


def test_update_requisition(client, test_requisition):
    """PUT /api/requisitions/{id} updates name."""
    resp = client.put(
        f"/api/requisitions/{test_requisition.id}",
        json={"name": "Updated Name"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["name"] == "Updated Name"


def test_update_requisition_not_found(client):
    """PUT returns 404 for non-existent requisition."""
    resp = client.put("/api/requisitions/99999", json={"name": "x"})
    assert resp.status_code == 404


def test_toggle_archive(client, test_requisition):
    """PUT /api/requisitions/{id}/archive toggles between archived and active."""
    resp = client.put(f"/api/requisitions/{test_requisition.id}/archive")
    assert resp.status_code == 200
    assert resp.json()["status"] == "archived"

    # Toggle back
    resp = client.put(f"/api/requisitions/{test_requisition.id}/archive")
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"


def test_toggle_archive_not_found(client):
    """Archive returns 404 for non-existent requisition."""
    resp = client.put("/api/requisitions/99999/archive")
    assert resp.status_code == 404


def test_clone_requisition(client, test_requisition):
    """POST /api/requisitions/{id}/clone creates a copy with requirements."""
    resp = client.post(f"/api/requisitions/{test_requisition.id}/clone")
    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data
    assert data["id"] != test_requisition.id


def test_clone_requisition_not_found(client):
    """Clone returns 404 for non-existent requisition."""
    resp = client.post("/api/requisitions/99999/clone")
    assert resp.status_code == 404


# ── Requirements CRUD ─────────────────────────────────────────────────


def test_list_requirements(client, test_requisition):
    """GET /api/requisitions/{id}/requirements returns the items."""
    resp = client.get(f"/api/requisitions/{test_requisition.id}/requirements")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) >= 1
    assert items[0]["primary_mpn"] == "LM317T"


def test_list_requirements_not_found(client):
    """Returns 404 for non-existent requisition."""
    resp = client.get("/api/requisitions/99999/requirements")
    assert resp.status_code == 404


def test_add_requirement(client, test_requisition):
    """POST /api/requisitions/{id}/requirements adds a new line item."""
    resp = client.post(
        f"/api/requisitions/{test_requisition.id}/requirements",
        json={"primary_mpn": "NE555P", "target_qty": 500},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["primary_mpn"] == "NE555P"


def test_add_requirement_batch(client, test_requisition):
    """Posting a list creates multiple requirements at once."""
    resp = client.post(
        f"/api/requisitions/{test_requisition.id}/requirements",
        json=[
            {"primary_mpn": "LM7805", "target_qty": 100},
            {"primary_mpn": "LM7812", "target_qty": 200},
        ],
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_add_requirement_skips_invalid(client, test_requisition):
    """Invalid items in batch are skipped, valid ones still created."""
    resp = client.post(
        f"/api/requisitions/{test_requisition.id}/requirements",
        json=[
            {"primary_mpn": "", "target_qty": 1},  # blank MPN — invalid
            {"primary_mpn": "TL431", "target_qty": 50},
        ],
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["primary_mpn"] == "TL431"


def test_add_requirement_not_found(client):
    """Add requirement to non-existent requisition returns 404."""
    resp = client.post(
        "/api/requisitions/99999/requirements",
        json={"primary_mpn": "X", "target_qty": 1},
    )
    assert resp.status_code == 404


def test_update_requirement(client, db_session, test_requisition):
    """PUT /api/requirements/{id} updates an existing line item."""
    from app.models import Requirement

    req_item = db_session.query(Requirement).filter_by(
        requisition_id=test_requisition.id
    ).first()
    resp = client.put(
        f"/api/requirements/{req_item.id}",
        json={"target_qty": 2000, "notes": "Urgent"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_update_requirement_not_found(client):
    """Update returns 404 for non-existent requirement."""
    resp = client.put("/api/requirements/99999", json={"target_qty": 1})
    assert resp.status_code == 404


def test_delete_requirement(client, db_session, test_requisition):
    """DELETE /api/requirements/{id} removes a line item."""
    from app.models import Requirement

    req_item = db_session.query(Requirement).filter_by(
        requisition_id=test_requisition.id
    ).first()
    resp = client.delete(f"/api/requirements/{req_item.id}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # Verify it's gone
    assert db_session.get(Requirement, req_item.id) is None


def test_delete_requirement_not_found(client):
    """Delete returns 404 for non-existent requirement."""
    resp = client.delete("/api/requirements/99999")
    assert resp.status_code == 404


# ── Sourcing Score ────────────────────────────────────────────────────


def test_sourcing_score(client, test_requisition):
    """GET /api/requisitions/{id}/sourcing-score returns score data."""
    resp = client.get(f"/api/requisitions/{test_requisition.id}/sourcing-score")
    assert resp.status_code == 200


def test_sourcing_score_not_found(client):
    """Sourcing score returns 404 for non-existent requisition."""
    resp = client.get("/api/requisitions/99999/sourcing-score")
    assert resp.status_code == 404


# ── Search ────────────────────────────────────────────────────────────


def test_search_all(client, test_requisition):
    """POST /api/requisitions/{id}/search triggers parallel search."""
    with patch(
        "app.routers.requisitions.search_requirement",
        new_callable=AsyncMock,
        return_value={"sightings": [], "source_stats": []},
    ):
        resp = client.post(f"/api/requisitions/{test_requisition.id}/search")
    assert resp.status_code == 200
    data = resp.json()
    assert "source_stats" in data


def test_search_all_not_found(client):
    """Search returns 404 for non-existent requisition."""
    resp = client.post("/api/requisitions/99999/search")
    assert resp.status_code == 404


def test_search_all_transitions_draft_to_active(client, db_session, test_requisition):
    """First search transitions a draft requisition to active."""
    test_requisition.status = "draft"
    db_session.commit()
    with patch(
        "app.routers.requisitions.search_requirement",
        new_callable=AsyncMock,
        return_value={"sightings": [], "source_stats": []},
    ):
        resp = client.post(f"/api/requisitions/{test_requisition.id}/search")
    assert resp.status_code == 200
    db_session.refresh(test_requisition)
    assert test_requisition.status == "active"


def test_search_one(client, db_session, test_requisition):
    """POST /api/requirements/{id}/search searches a single item."""
    from app.models import Requirement

    req_item = db_session.query(Requirement).filter_by(
        requisition_id=test_requisition.id
    ).first()
    with patch(
        "app.routers.requisitions.search_requirement",
        new_callable=AsyncMock,
        return_value={"sightings": [], "source_stats": []},
    ):
        resp = client.post(f"/api/requirements/{req_item.id}/search")
    assert resp.status_code == 200
    data = resp.json()
    assert "sightings" in data
    assert "source_stats" in data


def test_search_one_not_found(client):
    """Search single returns 404 for non-existent requirement."""
    resp = client.post("/api/requirements/99999/search")
    assert resp.status_code == 404


# ── Saved Sightings ──────────────────────────────────────────────────


def test_get_saved_sightings_empty(client, test_requisition):
    """GET /api/requisitions/{id}/sightings returns empty when no sightings."""
    resp = client.get(f"/api/requisitions/{test_requisition.id}/sightings")
    assert resp.status_code == 200


def test_get_saved_sightings_with_data(client, db_session, test_requisition):
    """Sightings endpoint returns saved results."""
    from app.models import Requirement, Sighting

    req_item = db_session.query(Requirement).filter_by(
        requisition_id=test_requisition.id
    ).first()
    s = Sighting(
        requirement_id=req_item.id,
        vendor_name="Arrow",
        mpn_matched="LM317T",
        qty_available=500,
        unit_price=0.45,
        source_type="api",
        score=75.0,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(s)
    db_session.commit()

    resp = client.get(f"/api/requisitions/{test_requisition.id}/sightings")
    assert resp.status_code == 200
    data = resp.json()
    assert str(req_item.id) in data
    assert len(data[str(req_item.id)]["sightings"]) >= 1


def test_get_saved_sightings_not_found(client):
    """Sightings returns 404 for non-existent requisition."""
    resp = client.get("/api/requisitions/99999/sightings")
    assert resp.status_code == 404


# ── Mark Sighting Unavailable ─────────────────────────────────────────


def test_mark_sighting_unavailable(client, db_session, test_requisition):
    """PUT /api/sightings/{id}/unavailable toggles the flag."""
    from app.models import Requirement, Sighting

    req_item = db_session.query(Requirement).filter_by(
        requisition_id=test_requisition.id
    ).first()
    s = Sighting(
        requirement_id=req_item.id,
        vendor_name="Mouser",
        mpn_matched="LM317T",
        source_type="api",
        score=50.0,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)

    resp = client.put(
        f"/api/sightings/{s.id}/unavailable",
        json={"unavailable": True},
    )
    assert resp.status_code == 200
    assert resp.json()["is_unavailable"] is True


def test_mark_sighting_unavailable_not_found(client):
    """Returns 404 for non-existent sighting."""
    resp = client.put("/api/sightings/99999/unavailable", json={"unavailable": True})
    assert resp.status_code == 404


# ── Sales Role Access ─────────────────────────────────────────────────


def test_sales_user_sees_only_own_requisitions(
    client, db_session, test_user, sales_user
):
    """Sales role can only see requisitions they created."""
    from app.database import get_db
    from app.dependencies import require_buyer, require_user
    from app.main import app
    from app.models import Requisition

    # Create a requisition owned by test_user (buyer)
    buyer_req = Requisition(
        name="Buyer-REQ", status="open",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    # Create a requisition owned by sales_user
    sales_req = Requisition(
        name="Sales-REQ", status="open",
        created_by=sales_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add_all([buyer_req, sales_req])
    db_session.commit()

    # Temporarily override auth to return sales_user (reuse existing client)
    app.dependency_overrides[require_user] = lambda: sales_user
    app.dependency_overrides[require_buyer] = lambda: sales_user
    try:
        resp = client.get("/api/requisitions")
        assert resp.status_code == 200
        reqs = resp.json()["requisitions"]
        # Sales should only see their own
        for r in reqs:
            assert r["created_by"] == sales_user.id
    finally:
        # Restore overrides for the buyer user
        app.dependency_overrides[require_user] = lambda: test_user
        app.dependency_overrides[require_buyer] = lambda: test_user
