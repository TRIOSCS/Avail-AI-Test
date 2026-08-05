"""test_integration_requisitions.py — Integration tests for Requisitions endpoints.

Tests full request->DB->response cycle for requisition and requirement CRUD.
Uses conftest.py fixtures (SQLite + TestClient with auth overrides).

Called by: pytest
Depends on: conftest.py (client, db_session, test_user fixtures)
"""

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.slow


def _create_req(client, **fields) -> int:
    """Create a requisition via the ORM as the authed test user, return its id.

    The legacy JSON POST /api/requisitions endpoint was removed (W2 B10, spec §5.1) —
    creation flows through the /v2 unified form now, so tests seed rows directly using
    the client's overridden db/user dependencies.
    """
    from app.constants import RequisitionStatus
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app
    from app.models import Requisition

    db = next(app.dependency_overrides[get_db]())
    user = app.dependency_overrides[require_user]()
    req = Requisition(
        name=fields.get("name") or "Untitled",
        customer_name=fields.get("customer_name"),
        status=RequisitionStatus.DRAFT,
        created_by=user.id,
        created_at=datetime.now(UTC),
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    return req.id


def _seed_requirement(req_id: int, mpn: str, qty: int = 10) -> int:
    """Create a Requirement row via the ORM, return its id.

    The legacy JSON POST /api/requisitions/{id}/requirements endpoint was removed (W2
    B11) — line items are added through the /v2 unified form now, so tests seed rows
    directly using the client's overridden db dependency.
    """
    from app.database import get_db
    from app.main import app
    from app.models import Requirement
    from app.utils.normalization import normalize_mpn_key

    db = next(app.dependency_overrides[get_db]())
    r = Requirement(
        requisition_id=req_id,
        primary_mpn=mpn,
        normalized_mpn=normalize_mpn_key(mpn),
        target_qty=qty,
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return r.id


# -- Requisition CRUD -----------------------------------------------------


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


def test_delete_requirement(client, db_session):
    from app.models import Requirement

    req_id = _create_req(client, name="REQ-DEL")
    item_id = _seed_requirement(req_id, "TMP123")

    resp = client.delete(f"/api/requirements/{item_id}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert db_session.query(Requirement).filter_by(requisition_id=req_id).count() == 0


def test_update_requirement(client, db_session):
    from app.models import Requirement

    req_id = _create_req(client, name="REQ-UPD")
    item_id = _seed_requirement(req_id, "OLD-MPN")

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
    # Verify via DB
    updated = db_session.get(Requirement, item_id)
    db_session.refresh(updated)
    assert updated.primary_mpn == "NEW-MPN"
    assert updated.target_qty == 999
