"""Tests for app/routers/nc_admin.py — NC queue admin endpoints.

Called by: pytest
Depends on: conftest fixtures (client, db_session), NcSearchQueue, NcWorkerStatus models
"""

from datetime import datetime, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import NcSearchQueue, NcWorkerStatus


def _make_queue_item(
    db: Session,
    *,
    mpn: str = "LM317T",
    status: str = "queued",
    priority: int = 3,
    requirement_id: int | None = None,
    requisition_id: int | None = None,
) -> NcSearchQueue:
    """Create a NC search queue item for testing."""
    from app.models import Requirement, Requisition

    if not requisition_id:
        req = Requisition(
            name=f"REQ-NC-{mpn}",
            customer_name="Test",
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db.add(req)
        db.flush()
        requisition_id = req.id

    if not requirement_id:
        requirement = Requirement(
            requisition_id=requisition_id,
            primary_mpn=mpn,
            target_qty=100,
            created_at=datetime.now(timezone.utc),
        )
        db.add(requirement)
        db.flush()
        requirement_id = requirement.id

    item = NcSearchQueue(
        requirement_id=requirement_id,
        requisition_id=requisition_id,
        mpn=mpn,
        normalized_mpn=mpn.upper().replace("-", ""),
        status=status,
        priority=priority,
        created_at=datetime.now(timezone.utc),
    )
    db.add(item)
    db.flush()
    return item


class TestNcQueueStats:
    def test_returns_stats(self, client: TestClient):
        with patch(
            "app.services.nc_worker.queue_manager.get_queue_stats",
            return_value={"queued": 5, "completed": 10, "error": 1},
        ):
            resp = client.get("/api/nc/queue/stats")
        assert resp.status_code == 200

    def test_empty_queue_stats(self, client: TestClient):
        with patch(
            "app.services.nc_worker.queue_manager.get_queue_stats",
            return_value={"queued": 0, "completed": 0, "error": 0},
        ):
            resp = client.get("/api/nc/queue/stats")
        assert resp.status_code == 200


class TestNcQueueItems:
    def test_list_queued_items(self, client: TestClient, db_session: Session):
        _make_queue_item(db_session, mpn="LM317T", status="queued")
        _make_queue_item(db_session, mpn="TPS54331", status="queued")
        db_session.commit()

        resp = client.get("/api/nc/queue/items", params={"status": "queued"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 2

    def test_list_filtered_by_status(self, client: TestClient, db_session: Session):
        _make_queue_item(db_session, mpn="COMPLETED1", status="completed")
        _make_queue_item(db_session, mpn="QUEUED1", status="queued")
        db_session.commit()

        resp = client.get("/api/nc/queue/items", params={"status": "completed"})
        assert resp.status_code == 200
        data = resp.json()
        assert all(item["status"] == "completed" for item in data)

    def test_list_respects_limit(self, client: TestClient, db_session: Session):
        for i in range(5):
            _make_queue_item(db_session, mpn=f"LIMIT{i}", status="queued")
        db_session.commit()

        resp = client.get("/api/nc/queue/items", params={"status": "queued", "limit": 2})
        assert resp.status_code == 200
        assert len(resp.json()) <= 2


class TestNcForceSearch:
    def test_force_search_success(self, client: TestClient, db_session: Session):
        item = _make_queue_item(db_session, mpn="FORCE1", status="completed")
        db_session.commit()

        with patch("app.services.nc_worker.queue_manager.mark_status") as mock_mark:
            resp = client.post(f"/api/nc/queue/{item.id}/force-search")
        assert resp.status_code == 200
        assert resp.json()["status"] == "queued"
        mock_mark.assert_called_once()

    def test_force_search_not_found(self, client: TestClient):
        resp = client.post("/api/nc/queue/99999/force-search")
        assert resp.status_code == 404


class TestNcSkip:
    def test_skip_success(self, client: TestClient, db_session: Session):
        item = _make_queue_item(db_session, mpn="SKIP1", status="queued")
        db_session.commit()

        with patch("app.services.nc_worker.queue_manager.mark_status") as mock_mark:
            resp = client.post(f"/api/nc/queue/{item.id}/skip")
        assert resp.status_code == 200
        assert resp.json()["status"] == "gated_out"
        mock_mark.assert_called_once()

    def test_skip_not_found(self, client: TestClient):
        resp = client.post("/api/nc/queue/99999/skip")
        assert resp.status_code == 404


class TestNcWorkerHealth:
    def test_health_no_worker_status(self, client: TestClient):
        with patch(
            "app.services.nc_worker.queue_manager.get_queue_stats",
            return_value={"queued": 0},
        ):
            resp = client.get("/api/nc/worker/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["worker_status"] == "unknown"

    def test_health_with_running_worker(self, client: TestClient, db_session: Session):
        ws = NcWorkerStatus(
            id=1,
            is_running=True,
            circuit_breaker_open=False,
            searches_today=50,
            sightings_today=20,
            last_heartbeat=datetime.now(timezone.utc),
            last_search_at=datetime.now(timezone.utc),
        )
        db_session.add(ws)
        db_session.commit()

        with patch(
            "app.services.nc_worker.queue_manager.get_queue_stats",
            return_value={"queued": 5},
        ):
            resp = client.get("/api/nc/worker/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["worker_status"] == "running"
        assert data["searches_today"] == 50
        assert data["circuit_breaker"]["is_open"] is False

    def test_health_circuit_breaker_open(self, client: TestClient, db_session: Session):
        ws = NcWorkerStatus(
            id=1,
            is_running=True,
            circuit_breaker_open=True,
            circuit_breaker_reason="Rate limit exceeded",
            searches_today=100,
            sightings_today=0,
        )
        db_session.add(ws)
        db_session.commit()

        with patch(
            "app.services.nc_worker.queue_manager.get_queue_stats",
            return_value={"queued": 10},
        ):
            resp = client.get("/api/nc/worker/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["worker_status"] == "circuit_breaker_open"
        assert data["circuit_breaker"]["is_open"] is True
        assert data["circuit_breaker"]["trip_reason"] == "Rate limit exceeded"
