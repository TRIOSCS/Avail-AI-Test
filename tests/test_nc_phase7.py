"""Tests for NC Phase 7: Integration Trigger + Admin Endpoints.

Called by: pytest
Depends on: conftest.py, nc_worker modules, nc_admin router
"""

from app.models import NcSearchQueue

# ── Trigger Tests ────────────────────────────────────────────────────


def test_add_requirement_triggers_nc_queue(client, test_requisition, db_session):
    """Creating a requirement via API triggers NC queue entry via background task."""
    from unittest.mock import MagicMock, patch

    # Mock SessionLocal to return a session using the test DB (SQLite, not PostgreSQL)
    mock_session_local = MagicMock(return_value=db_session)
    with patch("app.database.SessionLocal", mock_session_local):
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/requirements",
            json={"primary_mpn": "AD8232ACPZ", "manufacturer": "ADI", "target_qty": 100},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["created"]) == 1

    req_id = data["created"][0]["id"]
    queue_item = db_session.query(NcSearchQueue).filter_by(requirement_id=req_id).first()
    assert queue_item is not None
    assert queue_item.mpn == "AD8232ACPZ"
    assert queue_item.status == "pending"


def test_add_requirement_nc_failure_doesnt_break_request(client, test_requisition, db_session):
    """NC queue failure doesn't prevent requirement creation."""
    from unittest.mock import patch

    with patch(
        "app.services.nc_worker.queue_manager.enqueue_for_nc_search",
        side_effect=Exception("DB error"),
    ):
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/requirements",
            json={"primary_mpn": "STM32F103", "manufacturer": "STMicro", "target_qty": 50},
        )
    assert resp.status_code == 200
    assert len(resp.json()["created"]) == 1


# ── Admin Endpoint Tests ─────────────────────────────────────────────


def test_nc_queue_stats(client, db_session):
    """GET /api/nc/queue/stats returns status counts."""
    resp = client.get("/api/nc/queue/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "pending" in data
    assert "queued" in data
    assert "completed" in data
    assert "total_today" in data
    assert "remaining" in data


def test_nc_queue_items(client, db_session, test_requisition):
    """GET /api/nc/queue/items returns filtered queue items."""
    req = test_requisition.requirements[0]
    item = NcSearchQueue(
        requirement_id=req.id,
        requisition_id=test_requisition.id,
        mpn="LM317T",
        normalized_mpn="LM317T",
        status="queued",
    )
    db_session.add(item)
    db_session.commit()

    resp = client.get("/api/nc/queue/items?status=queued")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    assert data[0]["mpn"] == "LM317T"


def test_nc_force_search(client, db_session, test_requisition):
    """POST /api/nc/queue/{id}/force-search re-queues a failed item."""
    req = test_requisition.requirements[0]
    item = NcSearchQueue(
        requirement_id=req.id,
        requisition_id=test_requisition.id,
        mpn="LM317T",
        normalized_mpn="LM317T",
        status="failed",
        error_message="Timeout",
    )
    db_session.add(item)
    db_session.commit()

    resp = client.post(f"/api/nc/queue/{item.id}/force-search")
    assert resp.status_code == 200
    db_session.refresh(item)
    assert item.status == "queued"


def test_nc_skip(client, db_session, test_requisition):
    """POST /api/nc/queue/{id}/skip gates out an item."""
    req = test_requisition.requirements[0]
    item = NcSearchQueue(
        requirement_id=req.id,
        requisition_id=test_requisition.id,
        mpn="RC0805FR",
        normalized_mpn="RC0805FR",
        status="pending",
    )
    db_session.add(item)
    db_session.commit()

    resp = client.post(f"/api/nc/queue/{item.id}/skip")
    assert resp.status_code == 200
    db_session.refresh(item)
    assert item.status == "gated_out"
    assert item.gate_decision == "skip"


def test_nc_force_search_not_found(client):
    """Force-search on nonexistent item returns 404."""
    resp = client.post("/api/nc/queue/99999/force-search")
    assert resp.status_code == 404


def test_nc_worker_health(client, db_session):
    """GET /api/nc/worker/health returns health info."""
    resp = client.get("/api/nc/worker/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "worker_status" in data
    assert "queue_stats" in data


def test_nc_admin_requires_admin(db_session, sales_user, monkeypatch):
    """Non-admin authenticated users are denied NC admin endpoints."""
    from fastapi.testclient import TestClient

    from app import dependencies
    from app.database import get_db
    from app.main import app

    def _override_db():
        yield db_session

    monkeypatch.setattr(dependencies, "get_user", lambda _req, _db: sales_user)
    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as c:
        resp = c.get("/api/nc/queue/stats")
    app.dependency_overrides.clear()
    assert resp.status_code == 403
