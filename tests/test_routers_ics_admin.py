"""
tests/test_routers_ics_admin.py -- Tests for routers/ics_admin.py

Covers: queue stats, queue items listing, force-search, skip, and worker health.

Called by: pytest
Depends on: app/routers/ics_admin.py, conftest.py
"""

from datetime import datetime, timezone

from app.models import IcsSearchQueue, IcsWorkerStatus

# ── Queue Stats ────────────────────────────────────────────────────────


def test_queue_stats_empty(client):
    """Queue stats with no items returns zero counts."""
    resp = client.get("/api/ics/queue/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)


# ── Queue Items ────────────────────────────────────────────────────────


def test_queue_items_empty(client):
    """No items matching status returns empty list."""
    resp = client.get("/api/ics/queue/items?status=queued")
    assert resp.status_code == 200
    assert resp.json() == []


def test_queue_items_with_data(client, db_session, test_requisition):
    """Returns serialized queue items."""
    req = test_requisition
    item = IcsSearchQueue(
        requirement_id=req.requirements[0].id,
        requisition_id=req.id,
        mpn="LM317T",
        normalized_mpn="lm317t",
        status="queued",
        priority=2,
    )
    db_session.add(item)
    db_session.commit()

    resp = client.get("/api/ics/queue/items?status=queued")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["mpn"] == "LM317T"
    assert data[0]["status"] == "queued"
    assert data[0]["priority"] == 2


def test_queue_items_limit(client, db_session, test_requisition):
    """Limit param caps at 200."""
    resp = client.get("/api/ics/queue/items?status=queued&limit=500")
    assert resp.status_code == 200


# ── Force Search ───────────────────────────────────────────────────────


def test_force_search_success(client, db_session, test_requisition):
    """Force-search re-queues an item."""
    req = test_requisition
    item = IcsSearchQueue(
        requirement_id=req.requirements[0].id,
        requisition_id=req.id,
        mpn="LM317T",
        normalized_mpn="lm317t",
        status="completed",
        priority=3,
    )
    db_session.add(item)
    db_session.commit()

    resp = client.post(f"/api/ics/queue/{item.id}/force-search")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["status"] == "queued"
    db_session.refresh(item)
    assert item.status == "queued"


def test_force_search_not_found(client):
    """Force-search on missing item -> 404."""
    resp = client.post("/api/ics/queue/99999/force-search")
    assert resp.status_code == 404


# ── Skip ───────────────────────────────────────────────────────────────


def test_skip_success(client, db_session, test_requisition):
    """Skip sets item to gated_out with admin skip reason."""
    req = test_requisition
    item = IcsSearchQueue(
        requirement_id=req.requirements[0].id,
        requisition_id=req.id,
        mpn="LM317T",
        normalized_mpn="lm317t",
        status="queued",
        priority=3,
    )
    db_session.add(item)
    db_session.commit()

    resp = client.post(f"/api/ics/queue/{item.id}/skip")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["status"] == "gated_out"
    db_session.refresh(item)
    assert item.gate_decision == "skip"
    assert "admin" in item.gate_reason.lower()


def test_skip_not_found(client):
    """Skip on missing item -> 404."""
    resp = client.post("/api/ics/queue/99999/skip")
    assert resp.status_code == 404


# ── Worker Health ──────────────────────────────────────────────────────


def test_worker_health_no_status(client):
    """No IcsWorkerStatus row -> returns unknown status."""
    resp = client.get("/api/ics/worker/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["worker_status"] == "unknown"
    assert data["searches_today"] == 0
    assert data["circuit_breaker"]["is_open"] is False


def test_worker_health_running(client, db_session):
    """Worker status row with is_running=True -> 'running'."""
    ws = IcsWorkerStatus(
        id=1,
        is_running=True,
        circuit_breaker_open=False,
        searches_today=15,
        sightings_today=8,
        last_heartbeat=datetime.now(timezone.utc),
        last_search_at=datetime.now(timezone.utc),
    )
    db_session.add(ws)
    db_session.commit()

    resp = client.get("/api/ics/worker/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["worker_status"] == "running"
    assert data["searches_today"] == 15
    assert data["sightings_today"] == 8
    assert data["last_heartbeat"] is not None


def test_worker_health_stopped(client, db_session):
    """Worker status with is_running=False -> 'stopped'."""
    ws = IcsWorkerStatus(id=1, is_running=False, circuit_breaker_open=False)
    db_session.add(ws)
    db_session.commit()

    resp = client.get("/api/ics/worker/health")
    assert resp.status_code == 200
    assert resp.json()["worker_status"] == "stopped"


def test_worker_health_circuit_breaker(client, db_session):
    """Circuit breaker open takes priority over is_running."""
    ws = IcsWorkerStatus(
        id=1,
        is_running=True,
        circuit_breaker_open=True,
        circuit_breaker_reason="Too many 429 errors",
    )
    db_session.add(ws)
    db_session.commit()

    resp = client.get("/api/ics/worker/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["worker_status"] == "circuit_breaker_open"
    assert data["circuit_breaker"]["is_open"] is True
    assert "429" in data["circuit_breaker"]["trip_reason"]
