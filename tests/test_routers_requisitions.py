"""tests/test_routers_requisitions.py — Tests for routers/requisitions.py.

Covers: CRUD for requisitions and requirements,
search endpoints, sighting management, stock import, and access control.

Called by: pytest
Depends on: routers/requisitions.py, conftest fixtures
"""

from datetime import UTC, datetime

import pytest

# ── Requisition CRUD ──────────────────────────────────────────────────


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


def test_list_requisitions_pagination(client, db_session, test_user):
    """Limit and offset work correctly."""
    from app.models import Requisition

    for i in range(5):
        db_session.add(
            Requisition(
                name=f"REQ-PAGE-{i}",
                status="open",
                created_by=test_user.id,
                created_at=datetime.now(UTC),
            )
        )
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


def test_update_requirement(client, db_session, test_requisition):
    """PUT /api/requirements/{id} updates an existing line item."""
    from app.models import Requirement

    req_item = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
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

    req_item = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
    resp = client.delete(f"/api/requirements/{req_item.id}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # Verify it's gone
    assert db_session.get(Requirement, req_item.id) is None


def test_delete_requirement_not_found(client):
    """Delete returns 404 for non-existent requirement."""
    resp = client.delete("/api/requirements/99999")
    assert resp.status_code == 404


def test_update_requirement_new_fields(client, db_session, test_requisition):
    """PUT /api/requirements/{id} can update description, package_type, revision."""
    from app.models import Requirement

    req_item = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
    resp = client.put(
        f"/api/requirements/{req_item.id}",
        json={
            "description": "Quad 2-input NAND gate",
            "package_type": "SOIC-14",
            "revision": "Rev C",
        },
    )
    assert resp.status_code == 200
    db_session.refresh(req_item)
    assert req_item.description == "Quad 2-input NAND gate"
    assert req_item.package_type == "SOIC-14"
    assert req_item.revision == "Rev C"


# ── Bulk Operations ──────────────────────────────────────────────────


def test_batch_assign(client, db_session, test_user):
    """PUT /api/requisitions/batch-assign assigns owner to specific reqs (admin
    only)."""
    from app.dependencies import require_admin
    from app.main import app
    from app.models import Requisition

    # batch-assign requires admin; override for this test
    app.dependency_overrides[require_admin] = lambda: test_user

    r1 = Requisition(name="ASSIGN-1", status="open", created_by=test_user.id, created_at=datetime.now(UTC))
    r2 = Requisition(name="ASSIGN-2", status="open", created_by=test_user.id, created_at=datetime.now(UTC))
    db_session.add_all([r1, r2])
    db_session.commit()

    resp = client.put("/api/requisitions/batch-assign", json={"ids": [r1.id, r2.id], "owner_id": test_user.id})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["assigned_count"] == 2


def test_batch_assign_invalid_user(client, test_user):
    """PUT /api/requisitions/batch-assign with invalid user returns 404."""
    from app.dependencies import require_admin
    from app.main import app

    # batch-assign requires admin; override for this test
    app.dependency_overrides[require_admin] = lambda: test_user

    resp = client.put("/api/requisitions/batch-assign", json={"ids": [1], "owner_id": 99999})
    assert resp.status_code == 404


# ── Sales Role Access ─────────────────────────────────────────────────


def test_sales_user_sees_only_own_requisitions(client, db_session, test_user, sales_user):
    """Sales role can only see requisitions they created."""
    from app.dependencies import require_buyer, require_user
    from app.main import app
    from app.models import Requisition

    # Create a requisition owned by test_user (buyer)
    buyer_req = Requisition(
        name="Buyer-REQ",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(UTC),
    )
    # Create a requisition owned by sales_user
    sales_req = Requisition(
        name="Sales-REQ",
        status="open",
        created_by=sales_user.id,
        created_at=datetime.now(UTC),
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
        name="REQ-SITE",
        status="open",
        customer_site_id=test_customer_site.id,
        created_by=test_user.id,
        created_at=datetime.now(UTC),
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


def test_update_requirement_all_fields(client, db_session, test_requisition):
    """PUT /api/requirements/{id} updates all optional fields."""
    from app.models import Requirement

    req_item = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
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


@pytest.mark.parametrize(
    ("email", "azure_id", "action"),
    [
        pytest.param(
            "other2@trioscs.com",
            "az-other-unauth",
            lambda c, rid: c.put(f"/api/requirements/{rid}", json={"target_qty": 999}),
            id="update",
        ),
        pytest.param(
            "other3@trioscs.com",
            "az-other-del",
            lambda c, rid: c.delete(f"/api/requirements/{rid}"),
            id="delete",
        ),
        pytest.param(
            "other4@trioscs.com",
            "az-other-search",
            lambda c, rid: c.post(f"/api/requirements/{rid}/search"),
            id="search",
        ),
    ],
)
def test_requirement_action_unauthorized(client, db_session, test_user, test_requisition, email, azure_id, action):
    """Update/delete/search a requirement on another user's requisition returns
    403/404."""
    from app.dependencies import require_buyer, require_user
    from app.main import app
    from app.models import Requirement, User

    other = User(
        email=email,
        name="Other",
        role="sales",
        azure_id=azure_id,
        created_at=datetime.now(UTC),
    )
    db_session.add(other)
    db_session.commit()

    req_item = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()

    app.dependency_overrides[require_user] = lambda: other
    app.dependency_overrides[require_buyer] = lambda: other
    try:
        resp = action(client, req_item.id)
        assert resp.status_code in (403, 404)
    finally:
        app.dependency_overrides[require_user] = lambda: test_user
        app.dependency_overrides[require_buyer] = lambda: test_user
