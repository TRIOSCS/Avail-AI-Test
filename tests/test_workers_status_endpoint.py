"""Test for GET /api/admin/workers/status (admin worker liveness snapshot)."""

from datetime import datetime, timedelta, timezone

from app.models import IcsWorkerStatus


def test_workers_status_endpoint(client, db_session):
    db_session.add(
        IcsWorkerStatus(
            id=1,
            is_running=True,
            last_heartbeat=datetime.now(timezone.utc) - timedelta(minutes=20),
            circuit_breaker_open=False,
        )
    )
    db_session.commit()

    r = client.get("/api/admin/workers/status")
    assert r.status_code == 200
    data = r.json()
    assert len(data["workers"]) == 4
    by_name = {w["name"]: w for w in data["workers"]}

    ics = by_name["ics"]
    assert ics["present"] is True
    assert ics["is_running"] is True
    assert ics["stale"] is True  # 20m heartbeat age > 15m default threshold
    assert ics["heartbeat_age_seconds"] >= 60 * 19
    assert "queue" in ics  # queue stats included for scraper workers

    # Unseeded singletons report present=False without erroring.
    assert by_name["netcomponents"]["present"] is False
    assert by_name["enrichment"]["present"] is False
    # The Broker Forum browser-worker is registered (unseeded → present=False).
    assert by_name["thebrokersite"]["present"] is False
