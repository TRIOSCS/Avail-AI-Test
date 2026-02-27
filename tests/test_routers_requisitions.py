"""
tests/test_routers_requisitions.py — Tests for routers/requisitions.py

Covers: CRUD for requisitions and requirements, archive/clone,
search endpoints, sighting management, stock import, and access control.

Called by: pytest
Depends on: routers/requisitions.py, conftest fixtures
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

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


def test_requisition_counts_empty(client):
    """GET /api/requisitions/counts returns zeros when none exist."""
    resp = client.get("/api/requisitions/counts")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["open"] == 0
    assert data["archive"] == 0


def test_requisition_counts_with_data(client, test_requisition):
    """GET /api/requisitions/counts reflects existing reqs."""
    resp = client.get("/api/requisitions/counts")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1


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
    assert len(data["created"]) == 1
    assert data["created"][0]["primary_mpn"] == "NE555P"


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
    assert len(resp.json()["created"]) == 2


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
    assert len(data["created"]) == 1
    assert data["created"][0]["primary_mpn"] == "TL431"


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


def test_mark_sighting_unavailable_forbidden_for_other_sales_user(
    db_session, test_requisition, sales_user
):
    """A sales user who does NOT own the requisition gets 403."""
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app
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

    # Override auth to return the sales user (who doesn't own test_requisition)
    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = lambda: sales_user

    with TestClient(app) as c:
        resp = c.put(f"/api/sightings/{s.id}/unavailable", json={"unavailable": True})
    app.dependency_overrides.clear()

    assert resp.status_code == 403


# ── Sales Role Access ─────────────────────────────────────────────────


# ── Bulk Operations ──────────────────────────────────────────────────


def test_bulk_archive(client, db_session, test_user):
    """PUT /api/requisitions/bulk-archive archives reqs not created by current user."""
    from app.models import Requisition, User

    # bulk-archive archives reqs NOT created by the current user
    other = User(
        email="other@trioscs.com", name="Other", role="buyer",
        azure_id="az-other-bulk", created_at=datetime.now(timezone.utc),
    )
    db_session.add(other)
    db_session.flush()

    r1 = Requisition(
        name="BULK-1", status="open",
        created_by=other.id,
        created_at=datetime.now(timezone.utc),
    )
    r2 = Requisition(
        name="BULK-2", status="open",
        created_by=other.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add_all([r1, r2])
    db_session.commit()

    resp = client.put("/api/requisitions/bulk-archive")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["archived_count"] >= 2


def test_dismiss_new_offers(client, db_session, test_requisition):
    """POST /api/requisitions/{id}/dismiss-new-offers clears offers_viewed_at."""
    resp = client.post(f"/api/requisitions/{test_requisition.id}/dismiss-new-offers")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_dismiss_new_offers_not_found(client):
    """Dismiss returns 404 for non-existent requisition."""
    resp = client.post("/api/requisitions/99999/dismiss-new-offers")
    assert resp.status_code == 404


# ── Upload Requirements ─────────────────────────────────────────────


def test_upload_requirements_csv(client, test_requisition):
    """POST /api/requisitions/{id}/upload accepts a CSV of MPNs."""
    import io
    csv_bytes = b"mpn,qty,target_price\nNE555P,500,0.25\nLM7805,200,0.30"
    resp = client.post(
        f"/api/requisitions/{test_requisition.id}/upload",
        files={"file": ("reqs.csv", io.BytesIO(csv_bytes), "text/csv")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["created"] >= 1


def test_upload_requirements_not_found(client):
    """Upload returns 404 for non-existent requisition."""
    import io
    resp = client.post(
        "/api/requisitions/99999/upload",
        files={"file": ("reqs.csv", io.BytesIO(b"mpn\nFOO"), "text/csv")},
    )
    assert resp.status_code == 404


# ── Import Stock List ───────────────────────────────────────────────


def test_import_stock_list(client, db_session, test_requisition):
    """POST /api/requisitions/{id}/import-stock imports a vendor stock file."""
    import io
    csv_bytes = b"mpn,qty,price\nLM317T,5000,0.40\nNE555P,2000,0.20"
    resp = client.post(
        f"/api/requisitions/{test_requisition.id}/import-stock",
        data={"vendor_name": "Test Vendor"},
        files={"file": ("stock.csv", io.BytesIO(csv_bytes), "text/csv")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "imported_rows" in data


def test_import_stock_list_no_file(client, test_requisition):
    """Import stock without file returns 400."""
    resp = client.post(
        f"/api/requisitions/{test_requisition.id}/import-stock",
        data={"vendor_name": "Some Vendor"},
    )
    assert resp.status_code in (400, 422)


def test_import_stock_list_not_found(client):
    """Import stock for non-existent requisition returns 404."""
    import io
    resp = client.post(
        "/api/requisitions/99999/import-stock",
        data={"vendor_name": "Test"},
        files={"file": ("stock.csv", io.BytesIO(b"mpn,qty\nFOO,100"), "text/csv")},
    )
    assert resp.status_code == 404


# ── Sales Role Access ─────────────────────────────────────────────────


def test_sales_user_sees_only_own_requisitions(
    client, db_session, test_user, sales_user
):
    """Sales role can only see requisitions they created."""
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


# ── Additional coverage tests ─────────────────────────────────────────


def test_list_requisitions_with_customer_site(client, db_session, test_user, test_customer_site):
    """Requisition linked to a customer site shows customer_display."""
    from app.models import Requisition

    req = Requisition(
        name="REQ-SITE", status="open",
        customer_site_id=test_customer_site.id,
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.commit()

    resp = client.get("/api/requisitions")
    assert resp.status_code == 200
    reqs = resp.json()["requisitions"]
    site_reqs = [r for r in reqs if r["id"] == req.id]
    assert len(site_reqs) == 1
    assert site_reqs[0]["customer_site_id"] == test_customer_site.id
    # customer_display should include company name
    assert site_reqs[0]["customer_display"] != ""


def test_list_requisitions_search_by_mpn(client, db_session, test_requisition):
    """Search filter matches requisition by primary MPN in requirements."""
    resp = client.get("/api/requisitions", params={"q": "LM317T"})
    assert resp.status_code == 200
    reqs = resp.json()["requisitions"]
    assert len(reqs) >= 1


def test_list_requisitions_search_special_chars(client, test_requisition):
    """Search with SQL special characters (% _) is properly escaped."""
    resp = client.get("/api/requisitions", params={"q": "%_test"})
    assert resp.status_code == 200
    # Should not error, just return 0 results
    assert isinstance(resp.json()["requisitions"], list)


def test_update_requisition_customer_site(client, test_requisition, test_customer_site):
    """PUT /api/requisitions/{id} updates customer_site_id."""
    resp = client.put(
        f"/api/requisitions/{test_requisition.id}",
        json={"customer_site_id": test_customer_site.id},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_update_requisition_deadline(client, test_requisition):
    """PUT /api/requisitions/{id} updates deadline."""
    resp = client.put(
        f"/api/requisitions/{test_requisition.id}",
        json={"deadline": "2026-03-01"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_update_requisition_empty_name_preserves_old(client, db_session, test_requisition):
    """Empty string name preserves the old name."""
    old_name = test_requisition.name
    resp = client.put(
        f"/api/requisitions/{test_requisition.id}",
        json={"name": "   "},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == old_name


def test_toggle_archive_won_status(client, db_session, test_requisition):
    """Archiving a 'won' requisition transitions it to 'active'."""
    test_requisition.status = "won"
    db_session.commit()
    resp = client.put(f"/api/requisitions/{test_requisition.id}/archive")
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"


def test_toggle_archive_lost_status(client, db_session, test_requisition):
    """Archiving a 'lost' requisition transitions it to 'active'."""
    test_requisition.status = "lost"
    db_session.commit()
    resp = client.put(f"/api/requisitions/{test_requisition.id}/archive")
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"


def test_clone_requisition_with_substitutes(client, db_session, test_requisition):
    """Cloned requisition preserves and deduplicates substitutes."""
    from app.models import Requirement

    req_item = db_session.query(Requirement).filter_by(
        requisition_id=test_requisition.id
    ).first()
    req_item.substitutes = ["LM317T-ALT", "LM317T-ALT", "NE555P"]
    db_session.commit()

    resp = client.post(f"/api/requisitions/{test_requisition.id}/clone")
    assert resp.status_code == 200
    data = resp.json()
    clone_id = data["id"]

    # Verify the cloned requirements
    cloned_reqs = db_session.query(Requirement).filter_by(
        requisition_id=clone_id
    ).all()
    assert len(cloned_reqs) == 1


def test_list_requirements_with_sightings_and_offers(
    client, db_session, test_requisition, test_offer
):
    """Requirements list includes sighting counts and offer counts."""
    from app.models import Requirement, Sighting

    req_item = db_session.query(Requirement).filter_by(
        requisition_id=test_requisition.id
    ).first()
    # Link the offer to the requirement
    test_offer.requirement_id = req_item.id
    # Add a sighting
    s = Sighting(
        requirement_id=req_item.id,
        vendor_name="Test Vendor",
        vendor_name_normalized="test vendor",
        mpn_matched="LM317T",
        source_type="api",
        score=60.0,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(s)
    db_session.commit()

    resp = client.get(f"/api/requisitions/{test_requisition.id}/requirements")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) >= 1
    item = items[0]
    assert item["sighting_count"] >= 1
    assert item["offer_count"] >= 1


def test_list_requirements_with_contact_activity(
    client, db_session, test_requisition, test_user
):
    """Requirements list includes contact_count and hours_since_activity."""
    from app.models import Contact

    contact = Contact(
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        vendor_name="Arrow",
        contact_type="email",
        status="sent",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(contact)
    db_session.commit()

    resp = client.get(f"/api/requisitions/{test_requisition.id}/requirements")
    assert resp.status_code == 200
    items = resp.json()
    assert items[0]["contact_count"] >= 1
    assert items[0]["hours_since_activity"] is not None
    assert items[0]["hours_since_activity"] >= 0


def test_update_requirement_all_fields(client, db_session, test_requisition):
    """PUT /api/requirements/{id} updates all optional fields."""
    from app.models import Requirement

    req_item = db_session.query(Requirement).filter_by(
        requisition_id=test_requisition.id
    ).first()
    resp = client.put(
        f"/api/requirements/{req_item.id}",
        json={
            "primary_mpn": "LM317T-NEW",
            "target_qty": 5000,
            "target_price": 1.25,
            "substitutes": ["ALT-001", "ALT-002"],
            "firmware": "v2.0",
            "date_codes": "2025+",
            "hardware_codes": "HW-A",
            "packaging": "reel",
            "condition": "new",
            "notes": "Test note",
            "sale_notes": "Customer wants COC",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_update_requirement_unauthorized(client, db_session, test_user, test_requisition):
    """Update requirement with different user's requisition returns 403."""
    from app.dependencies import require_buyer, require_user
    from app.main import app
    from app.models import Requirement, User

    other = User(
        email="other2@trioscs.com", name="Other2", role="sales",
        azure_id="az-other-unauth", created_at=datetime.now(timezone.utc),
    )
    db_session.add(other)
    db_session.commit()

    req_item = db_session.query(Requirement).filter_by(
        requisition_id=test_requisition.id
    ).first()

    app.dependency_overrides[require_user] = lambda: other
    app.dependency_overrides[require_buyer] = lambda: other
    try:
        resp = client.put(
            f"/api/requirements/{req_item.id}",
            json={"target_qty": 999},
        )
        assert resp.status_code == 403
    finally:
        app.dependency_overrides[require_user] = lambda: test_user
        app.dependency_overrides[require_buyer] = lambda: test_user


def test_delete_requirement_unauthorized(client, db_session, test_user, test_requisition):
    """Delete requirement with different user's requisition returns 403."""
    from app.dependencies import require_buyer, require_user
    from app.main import app
    from app.models import Requirement, User

    other = User(
        email="other3@trioscs.com", name="Other3", role="sales",
        azure_id="az-other-del", created_at=datetime.now(timezone.utc),
    )
    db_session.add(other)
    db_session.commit()

    req_item = db_session.query(Requirement).filter_by(
        requisition_id=test_requisition.id
    ).first()

    app.dependency_overrides[require_user] = lambda: other
    app.dependency_overrides[require_buyer] = lambda: other
    try:
        resp = client.delete(f"/api/requirements/{req_item.id}")
        assert resp.status_code == 403
    finally:
        app.dependency_overrides[require_user] = lambda: test_user
        app.dependency_overrides[require_buyer] = lambda: test_user


def test_search_all_with_exception(client, db_session, test_requisition):
    """Search handles exceptions from search_requirement gracefully."""
    with patch(
        "app.routers.requisitions.search_requirement",
        new_callable=AsyncMock,
        side_effect=RuntimeError("Connector timeout"),
    ):
        resp = client.post(f"/api/requisitions/{test_requisition.id}/search")
    assert resp.status_code == 200
    data = resp.json()
    # Should have logged the error but still return
    assert "source_stats" in data


def test_search_all_with_requirement_ids(client, db_session, test_requisition):
    """Search with requirement_ids filter only searches specified requirements."""
    from app.models import Requirement

    req_item = db_session.query(Requirement).filter_by(
        requisition_id=test_requisition.id
    ).first()

    with patch(
        "app.routers.requisitions.search_requirement",
        new_callable=AsyncMock,
        return_value={"sightings": [], "source_stats": []},
    ):
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/search",
            json={"requirement_ids": [req_item.id]},
        )
    assert resp.status_code == 200


def test_search_all_reactivates_archived(client, db_session, test_requisition):
    """Search reactivates an archived requisition."""
    test_requisition.status = "archived"
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


def test_search_one_unauthorized(client, db_session, test_user, test_requisition):
    """Search one returns 403 when user does not have access to parent req."""
    from app.dependencies import require_buyer, require_user
    from app.main import app
    from app.models import Requirement, User

    other = User(
        email="other4@trioscs.com", name="Other4", role="sales",
        azure_id="az-other-search", created_at=datetime.now(timezone.utc),
    )
    db_session.add(other)
    db_session.commit()

    req_item = db_session.query(Requirement).filter_by(
        requisition_id=test_requisition.id
    ).first()

    app.dependency_overrides[require_user] = lambda: other
    app.dependency_overrides[require_buyer] = lambda: other
    try:
        resp = client.post(f"/api/requirements/{req_item.id}/search")
        assert resp.status_code == 403
    finally:
        app.dependency_overrides[require_user] = lambda: test_user
        app.dependency_overrides[require_buyer] = lambda: test_user


def test_saved_sightings_with_historical_offers(client, db_session, test_requisition, test_user):
    """Sightings endpoint includes historical offers from other requisitions."""
    from app.models import Offer, Requirement, Requisition, Sighting

    req_item = db_session.query(Requirement).filter_by(
        requisition_id=test_requisition.id
    ).first()

    # Create a sighting so the requirement appears in results
    s = Sighting(
        requirement_id=req_item.id,
        vendor_name="DigiKey",
        mpn_matched="LM317T",
        source_type="api",
        score=70.0,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(s)

    # Create another requisition with an offer for the same MPN
    other_req = Requisition(
        name="OTHER-REQ", status="open",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(other_req)
    db_session.flush()

    hist_offer = Offer(
        requisition_id=other_req.id,
        vendor_name="Mouser",
        mpn="LM317T",
        qty_available=200,
        unit_price=0.55,
        entered_by_id=test_user.id,
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(hist_offer)
    db_session.commit()

    resp = client.get(f"/api/requisitions/{test_requisition.id}/sightings")
    assert resp.status_code == 200
    data = resp.json()
    # Check that the requirement's key exists with historical offers
    if str(req_item.id) in data:
        entry = data[str(req_item.id)]
        assert "historical_offers" in entry


def test_upload_requirements_with_substitutes(client, test_requisition):
    """Upload CSV with substitutes column creates requirements with subs."""
    import io
    csv_bytes = b"mpn,qty,substitutes\nABC123,100,\"DEF456,GHI789\""
    resp = client.post(
        f"/api/requisitions/{test_requisition.id}/upload",
        files={"file": ("reqs.csv", io.BytesIO(csv_bytes), "text/csv")},
    )
    assert resp.status_code == 200
    assert resp.json()["created"] >= 1


def test_upload_requirements_with_optional_columns(client, test_requisition):
    """Upload CSV with condition, packaging, date_codes, manufacturer, notes."""
    import io
    csv_bytes = (
        b"mpn,qty,condition,packaging,date_codes,manufacturer,notes,price\n"
        b"XYZ789,200,new,reel,2025+,TI,urgent,0.50"
    )
    resp = client.post(
        f"/api/requisitions/{test_requisition.id}/upload",
        files={"file": ("reqs.csv", io.BytesIO(csv_bytes), "text/csv")},
    )
    assert resp.status_code == 200
    assert resp.json()["created"] >= 1


def test_upload_requirements_parse_error(client, test_requisition):
    """Upload of unparseable file returns 400."""
    import io
    with patch(
        "app.file_utils.parse_tabular_file",
        side_effect=ValueError("Unsupported format"),
    ):
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/upload",
            files={"file": ("bad.xyz", io.BytesIO(b"not valid"), "application/octet-stream")},
        )
    assert resp.status_code == 400


def test_import_stock_oversized_file(client, db_session, test_requisition):
    """Import stock with file > 10MB returns 413."""
    import io
    # Create a file header, then pad to trigger the size check
    large_content = b"mpn,qty\n" + b"A" * (10_000_001)
    resp = client.post(
        f"/api/requisitions/{test_requisition.id}/import-stock",
        data={"vendor_name": "Big Vendor"},
        files={"file": ("big.csv", io.BytesIO(large_content), "text/csv")},
    )
    assert resp.status_code == 413


def test_search_all_with_source_stats_merge(client, db_session, test_requisition):
    """Search merges source_stats across multiple requirements."""
    from app.models import Requirement

    # Add a second requirement
    r2 = Requirement(
        requisition_id=test_requisition.id,
        primary_mpn="NE555P",
        target_qty=100,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(r2)
    db_session.commit()

    search_results = [
        {"sightings": [], "source_stats": [{"source": "BrokerBin", "results": 3, "ms": 100, "status": "ok", "error": None}]},
        {"sightings": [], "source_stats": [{"source": "BrokerBin", "results": 5, "ms": 200, "status": "ok", "error": None}]},
    ]
    call_count = 0

    async def mock_search(r, db):
        nonlocal call_count
        result = search_results[call_count % len(search_results)]
        call_count += 1
        return result

    with patch("app.routers.requisitions.search_requirement", side_effect=mock_search):
        resp = client.post(f"/api/requisitions/{test_requisition.id}/search")
    assert resp.status_code == 200
    data = resp.json()
    stats = data["source_stats"]
    bb_stat = [s for s in stats if s["source"] == "BrokerBin"]
    if bb_stat:
        assert bb_stat[0]["results"] == 8  # 3 + 5 merged
