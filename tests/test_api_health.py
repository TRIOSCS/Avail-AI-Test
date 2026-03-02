"""Tests for API health monitoring — models, service, endpoints.

Covers: ApiUsageLog model, health_monitor service (ping/deep/run),
credential-based status fix, system alerts endpoint, dashboard endpoint.

Depends on: conftest.py (db_session, TestSessionLocal, engine)
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.models import User
from app.models.config import ApiSource, ApiUsageLog

# ── Model Tests ──────────────────────────────────────────────────────


def test_api_usage_log_creation(db_session):
    """ApiUsageLog records can be created and linked to an ApiSource."""
    src = ApiSource(
        name="test_src",
        display_name="Test",
        category="api",
        source_type="test",
        status="live",
    )
    db_session.add(src)
    db_session.flush()

    log = ApiUsageLog(
        source_id=src.id,
        timestamp=datetime.now(timezone.utc),
        endpoint="search",
        status_code=200,
        response_ms=150,
        success=True,
        check_type="ping",
    )
    db_session.add(log)
    db_session.commit()

    saved = db_session.query(ApiUsageLog).filter_by(source_id=src.id).first()
    assert saved is not None
    assert saved.success is True
    assert saved.check_type == "ping"


def test_api_source_new_columns(db_session):
    """ApiSource has the new health monitoring columns."""
    src = ApiSource(
        name="test_cols",
        display_name="Test Cols",
        category="api",
        source_type="test",
        status="pending",
        monthly_quota=1000,
        calls_this_month=50,
    )
    db_session.add(src)
    db_session.commit()

    loaded = db_session.get(ApiSource, src.id)
    assert loaded.monthly_quota == 1000
    assert loaded.calls_this_month == 50
    assert loaded.last_ping_at is None
    assert loaded.last_deep_test_at is None


# ── Health Monitor Service Tests ─────────────────────────────────────


@pytest.mark.asyncio
async def test_ping_source_success(db_session):
    """Successful ping updates status to live and records timestamp."""
    src = ApiSource(
        name="nexar",
        display_name="Nexar",
        category="api",
        source_type="aggregator",
        status="pending",
        is_active=True,
        env_vars=["NEXAR_CLIENT_ID", "NEXAR_CLIENT_SECRET"],
    )
    db_session.add(src)
    db_session.flush()

    mock_connector = MagicMock()
    mock_connector.search = AsyncMock(return_value=[{"mpn": "LM317"}])

    with patch("app.services.health_monitor._get_connector", return_value=mock_connector):
        from app.services.health_monitor import ping_source

        result = await ping_source(src, db_session)

    assert result["success"] is True
    assert src.status == "live"
    assert src.last_ping_at is not None


@pytest.mark.asyncio
async def test_ping_source_failure(db_session):
    """Failed ping updates status to error and records error message."""
    src = ApiSource(
        name="digikey",
        display_name="DigiKey",
        category="api",
        source_type="authorized",
        status="live",
        is_active=True,
        env_vars=["DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET"],
    )
    db_session.add(src)
    db_session.flush()

    mock_connector = MagicMock()
    mock_connector.search = AsyncMock(side_effect=Exception("401 Unauthorized"))

    with patch("app.services.health_monitor._get_connector", return_value=mock_connector):
        from app.services.health_monitor import ping_source

        result = await ping_source(src, db_session)

    assert result["success"] is False
    assert src.status == "error"
    assert "401" in src.last_error


@pytest.mark.asyncio
async def test_ping_source_no_connector(db_session):
    """Source with no connector gets status disabled."""
    src = ApiSource(
        name="unknown",
        display_name="Unknown",
        category="api",
        source_type="test",
        status="pending",
        is_active=True,
        env_vars=[],
    )
    db_session.add(src)
    db_session.flush()

    with patch("app.services.health_monitor._get_connector", return_value=None):
        from app.services.health_monitor import ping_source

        result = await ping_source(src, db_session)

    assert result["success"] is False
    assert src.status == "disabled"


@pytest.mark.asyncio
async def test_deep_test_records_usage_log(db_session):
    """Deep test writes an ApiUsageLog entry."""
    src = ApiSource(
        name="mouser",
        display_name="Mouser",
        category="api",
        source_type="authorized",
        status="live",
        is_active=True,
        env_vars=["MOUSER_API_KEY"],
    )
    db_session.add(src)
    db_session.flush()

    mock_connector = MagicMock()
    mock_connector.search = AsyncMock(return_value=[{"mpn": "LM317", "qty": 100}])

    with patch("app.services.health_monitor._get_connector", return_value=mock_connector):
        from app.services.health_monitor import deep_test_source

        await deep_test_source(src, db_session)

    logs = db_session.query(ApiUsageLog).filter_by(source_id=src.id).all()
    assert len(logs) == 1
    assert logs[0].check_type == "deep"
    assert logs[0].success is True


@pytest.mark.asyncio
async def test_deep_test_failure_records_log(db_session):
    """Deep test failure also writes a usage log entry with error."""
    src = ApiSource(
        name="mouser_fail",
        display_name="Mouser",
        category="api",
        source_type="authorized",
        status="live",
        is_active=True,
        env_vars=["MOUSER_API_KEY"],
    )
    db_session.add(src)
    db_session.flush()

    mock_connector = MagicMock()
    mock_connector.search = AsyncMock(side_effect=Exception("Timeout"))

    with patch("app.services.health_monitor._get_connector", return_value=mock_connector):
        from app.services.health_monitor import deep_test_source

        result = await deep_test_source(src, db_session)

    assert result["success"] is False
    assert src.status == "error"
    logs = db_session.query(ApiUsageLog).filter_by(source_id=src.id).all()
    assert len(logs) == 1
    assert logs[0].success is False
    assert "Timeout" in logs[0].error_message


@pytest.mark.asyncio
async def test_deep_test_no_connector_records_log(db_session):
    """Deep test with no connector writes a log entry and disables source."""
    src = ApiSource(
        name="no_conn",
        display_name="No Conn",
        category="api",
        source_type="test",
        status="pending",
        is_active=True,
    )
    db_session.add(src)
    db_session.flush()

    with patch("app.services.health_monitor._get_connector", return_value=None):
        from app.services.health_monitor import deep_test_source

        result = await deep_test_source(src, db_session)

    assert result["success"] is False
    assert src.status == "disabled"
    logs = db_session.query(ApiUsageLog).filter_by(source_id=src.id).all()
    assert len(logs) == 1
    assert logs[0].error_message == "No connector"


# ── _get_connector Tests (lines 29-33) ──────────────────────────────


def test_get_connector_success(db_session):
    """_get_connector returns a connector when _get_connector_for_source works."""
    src = ApiSource(
        name="nexar_gc",
        display_name="Nexar",
        category="api",
        source_type="aggregator",
        status="live",
        is_active=True,
    )
    db_session.add(src)
    db_session.flush()

    mock_connector = MagicMock()
    with patch(
        "app.routers.sources._get_connector_for_source",
        return_value=mock_connector,
    ):
        from app.services.health_monitor import _get_connector

        result = _get_connector(src, db_session)

    assert result is mock_connector


def test_get_connector_returns_none_on_exception(db_session):
    """_get_connector returns None when _get_connector_for_source raises."""
    src = ApiSource(
        name="broken_gc",
        display_name="Broken",
        category="api",
        source_type="test",
        status="pending",
        is_active=True,
    )
    db_session.add(src)
    db_session.flush()

    with patch(
        "app.routers.sources._get_connector_for_source",
        side_effect=ValueError("No credentials"),
    ):
        from app.services.health_monitor import _get_connector

        result = _get_connector(src, db_session)

    assert result is None


# ── run_health_checks Tests (lines 156-196) ─────────────────────────


@pytest.mark.asyncio
async def test_run_health_checks_ping(db_session):
    """run_health_checks with ping runs ping_source on all active sources."""
    src1 = ApiSource(
        name="src_ping_a",
        display_name="Source A",
        category="api",
        source_type="test",
        status="live",
        is_active=True,
    )
    src2 = ApiSource(
        name="src_ping_b",
        display_name="Source B",
        category="api",
        source_type="test",
        status="live",
        is_active=True,
    )
    db_session.add_all([src1, src2])
    db_session.commit()

    mock_session = MagicMock()
    mock_session.query.return_value.filter.return_value.all.return_value = [src1, src2]
    mock_session_cls = MagicMock(return_value=mock_session)
    ping_mock = AsyncMock(return_value={"success": True, "elapsed_ms": 42, "error": None})

    with (
        patch("app.database.SessionLocal", mock_session_cls),
        patch("app.services.health_monitor.ping_source", ping_mock),
    ):
        from app.services.health_monitor import run_health_checks

        result = await run_health_checks("ping")

    assert result["total"] == 2
    assert result["passed"] == 2
    assert result["failed"] == 0
    assert ping_mock.call_count == 2
    mock_session.commit.assert_called_once()
    mock_session.close.assert_called_once()


@pytest.mark.asyncio
async def test_run_health_checks_deep(db_session):
    """run_health_checks with deep runs deep_test_source."""
    src = ApiSource(
        name="src_deep",
        display_name="Deep Src",
        category="api",
        source_type="test",
        status="live",
        is_active=True,
    )
    db_session.add(src)
    db_session.commit()

    mock_session = MagicMock()
    mock_session.query.return_value.filter.return_value.all.return_value = [src]
    mock_session_cls = MagicMock(return_value=mock_session)
    deep_mock = AsyncMock(
        return_value={
            "success": True,
            "results_count": 5,
            "elapsed_ms": 200,
            "error": None,
        }
    )

    with (
        patch("app.database.SessionLocal", mock_session_cls),
        patch("app.services.health_monitor.deep_test_source", deep_mock),
    ):
        from app.services.health_monitor import run_health_checks

        result = await run_health_checks("deep")

    assert result["total"] == 1
    assert result["passed"] == 1
    assert deep_mock.call_count == 1
    mock_session.commit.assert_called_once()
    mock_session.close.assert_called_once()


@pytest.mark.asyncio
async def test_run_health_checks_mixed_results(db_session):
    """run_health_checks correctly counts passed and failed sources."""
    src_ok = ApiSource(
        name="hc_ok", display_name="OK", category="api", source_type="test", status="live", is_active=True
    )
    src_bad = ApiSource(
        name="hc_bad", display_name="Bad", category="api", source_type="test", status="error", is_active=True
    )
    db_session.add_all([src_ok, src_bad])
    db_session.commit()

    mock_session = MagicMock()
    mock_session.query.return_value.filter.return_value.all.return_value = [src_ok, src_bad]
    mock_session_cls = MagicMock(return_value=mock_session)
    ping_mock = AsyncMock(
        side_effect=[
            {"success": True, "elapsed_ms": 50, "error": None},
            {"success": False, "elapsed_ms": 100, "error": "Connection refused"},
        ]
    )

    with (
        patch("app.database.SessionLocal", mock_session_cls),
        patch("app.services.health_monitor.ping_source", ping_mock),
    ):
        from app.services.health_monitor import run_health_checks

        result = await run_health_checks("ping")

    assert result["total"] == 2
    assert result["passed"] == 1
    assert result["failed"] == 1
    mock_session.commit.assert_called_once()
    mock_session.close.assert_called_once()


@pytest.mark.asyncio
async def test_run_health_checks_source_crash(db_session):
    """When a per-source check crashes, it is counted as failed."""
    src = ApiSource(
        name="hc_crash", display_name="Crash", category="api", source_type="test", status="live", is_active=True
    )
    db_session.add(src)
    db_session.commit()

    mock_session = MagicMock()
    mock_session.query.return_value.filter.return_value.all.return_value = [src]
    mock_session_cls = MagicMock(return_value=mock_session)
    ping_mock = AsyncMock(side_effect=RuntimeError("unexpected crash"))

    with (
        patch("app.database.SessionLocal", mock_session_cls),
        patch("app.services.health_monitor.ping_source", ping_mock),
    ):
        from app.services.health_monitor import run_health_checks

        result = await run_health_checks("ping")

    assert result["total"] == 1
    assert result["passed"] == 0
    assert result["failed"] == 1
    assert "unexpected crash" in result["sources"]["hc_crash"]["error"]
    mock_session.commit.assert_called_once()
    mock_session.close.assert_called_once()


@pytest.mark.asyncio
async def test_run_health_checks_db_error_rollback():
    """When the DB query itself fails, the session is rolled back and closed."""
    mock_session = MagicMock()
    mock_session.query.side_effect = RuntimeError("DB connection lost")
    mock_session_cls = MagicMock(return_value=mock_session)

    with patch("app.database.SessionLocal", mock_session_cls):
        from app.services.health_monitor import run_health_checks

        result = await run_health_checks("ping")

    assert result["total"] == 0
    assert result["passed"] == 0
    assert result["failed"] == 0
    mock_session.rollback.assert_called_once()
    mock_session.close.assert_called_once()


@pytest.mark.asyncio
async def test_run_health_checks_no_active_sources():
    """run_health_checks with no active sources returns zeroes."""
    mock_session = MagicMock()
    mock_session.query.return_value.filter.return_value.all.return_value = []
    mock_session_cls = MagicMock(return_value=mock_session)

    with patch("app.database.SessionLocal", mock_session_cls):
        from app.services.health_monitor import run_health_checks

        result = await run_health_checks("ping")

    assert result["total"] == 0
    assert result["passed"] == 0
    assert result["failed"] == 0
    mock_session.commit.assert_called_once()
    mock_session.close.assert_called_once()


# ── Fixtures for Endpoint Tests ──────────────────────────────────────


@pytest.fixture()
def admin_user(db_session):
    """Admin user for endpoint tests."""
    user = User(
        email="admin@test.com",
        name="Test Admin",
        role="admin",
        azure_id="test-azure-health",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture()
def admin_client(db_session, admin_user):
    """TestClient authenticated as admin."""
    from app.database import get_db
    from app.dependencies import require_admin, require_settings_access, require_user
    from app.main import app

    def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = lambda: admin_user
    app.dependency_overrides[require_admin] = lambda: admin_user
    app.dependency_overrides[require_settings_access] = lambda: admin_user

    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


# ── Status Accuracy Regression Tests ─────────────────────────────────


def test_list_sources_does_not_auto_set_status(admin_client, db_session):
    """GET /api/sources must NOT change status based on credentials alone.

    Previously, if env_vars were set, status was auto-set to 'live' even
    if the API was actually unreachable. Status should only be set by
    health checks.
    """
    src = ApiSource(
        name="broken_but_creds_set",
        display_name="Broken API",
        category="api",
        source_type="test",
        status="error",
        is_active=True,
        env_vars=["SOME_API_KEY"],
        last_error="Connection refused",
    )
    db_session.add(src)
    db_session.commit()

    resp = admin_client.get("/api/sources")
    data = resp.json()
    broken = next(s for s in data["sources"] if s["name"] == "broken_but_creds_set")
    # Status must remain "error" — not be flipped to "live" just because creds exist
    assert broken["status"] == "error"


# ── Scheduler Integration Tests ──────────────────────────────────────


def test_scheduler_has_health_jobs():
    """Scheduler registers health check jobs."""
    from app.scheduler import configure_scheduler, scheduler

    configure_scheduler()
    job_ids = [j.id for j in scheduler.get_jobs()]
    assert "health_ping" in job_ids
    assert "health_deep" in job_ids
    assert "cleanup_usage_log" in job_ids
    assert "reset_monthly_usage" in job_ids
    scheduler.remove_all_jobs()


# ── System Alerts Endpoint Tests ─────────────────────────────────────


def test_system_alerts_returns_errors(admin_client, db_session):
    """GET /api/system/alerts returns sources in error/degraded state."""
    ok_src = ApiSource(
        name="ok_api",
        display_name="OK API",
        category="api",
        source_type="test",
        status="live",
        is_active=True,
    )
    bad_src = ApiSource(
        name="bad_api",
        display_name="Bad API",
        category="api",
        source_type="test",
        status="error",
        is_active=True,
        last_error="401 Unauthorized",
    )
    db_session.add_all([ok_src, bad_src])
    db_session.commit()

    resp = admin_client.get("/api/system/alerts")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    assert data["alerts"][0]["source_name"] == "bad_api"
    assert "401" in data["alerts"][0]["last_error"]


def test_system_alerts_includes_degraded(admin_client, db_session):
    """Degraded sources also appear in alerts."""
    src = ApiSource(
        name="degraded_api",
        display_name="Degraded API",
        category="api",
        source_type="test",
        status="degraded",
        is_active=True,
        last_error="Timeout",
    )
    db_session.add(src)
    db_session.commit()

    resp = admin_client.get("/api/system/alerts")
    data = resp.json()
    assert data["count"] == 1
    assert data["alerts"][0]["status"] == "degraded"


def test_system_alerts_empty_when_healthy(admin_client, db_session):
    """No alerts when all sources are live."""
    src = ApiSource(
        name="healthy",
        display_name="Healthy",
        category="api",
        source_type="test",
        status="live",
        is_active=True,
    )
    db_session.add(src)
    db_session.commit()

    resp = admin_client.get("/api/system/alerts")
    data = resp.json()
    assert data["count"] == 0
    assert data["alerts"] == []


# ── Dashboard Endpoint Tests ─────────────────────────────────────────


def test_api_health_dashboard(admin_client, db_session):
    """GET /api/admin/api-health/dashboard returns full connector stats."""
    src = ApiSource(
        name="dash_test",
        display_name="Dashboard Test",
        category="api",
        source_type="test",
        status="live",
        is_active=True,
        total_searches=100,
        total_results=500,
        avg_response_ms=200,
        monthly_quota=1000,
        calls_this_month=450,
    )
    db_session.add(src)
    db_session.commit()

    resp = admin_client.get("/api/admin/api-health/dashboard")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["sources"]) >= 1

    test_src = next(s for s in data["sources"] if s["name"] == "dash_test")
    assert test_src["monthly_quota"] == 1000
    assert test_src["calls_this_month"] == 450
    assert test_src["usage_pct"] == 45.0


def test_api_health_dashboard_usage_log(admin_client, db_session):
    """Dashboard includes recent health check history from usage log."""
    src = ApiSource(
        name="log_test",
        display_name="Log Test",
        category="api",
        source_type="test",
        status="live",
        is_active=True,
    )
    db_session.add(src)
    db_session.flush()

    for i in range(3):
        log = ApiUsageLog(
            source_id=src.id,
            timestamp=datetime.now(timezone.utc),
            success=(i != 1),
            response_ms=100 + i * 50,
            check_type="ping",
        )
        db_session.add(log)
    db_session.commit()

    resp = admin_client.get("/api/admin/api-health/dashboard")
    data = resp.json()
    log_src = next(s for s in data["sources"] if s["name"] == "log_test")
    assert log_src["recent_checks"] == 3
    assert log_src["recent_failures"] == 1


# ── Proactive Notification Tests ──────────────────────────────────


@pytest.mark.asyncio
async def test_ping_live_to_error_notifies_admins(db_session):
    """When a source transitions live → error, admin users get notified."""
    admin = User(
        email="notify_admin@test.com",
        name="Notify Admin",
        role="admin",
        azure_id="notify-admin-1",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(admin)
    db_session.flush()

    src = ApiSource(
        name="notif_test_src",
        display_name="Notif Test",
        category="api",
        source_type="test",
        status="live",
        is_active=True,
    )
    db_session.add(src)
    db_session.flush()

    mock_connector = MagicMock()
    mock_connector.search = AsyncMock(side_effect=Exception("403 Forbidden"))

    with patch("app.services.health_monitor._get_connector", return_value=mock_connector):
        from app.services.health_monitor import ping_source

        result = await ping_source(src, db_session)

    assert result["success"] is False
    assert src.status == "error"

    from app.models.notification import Notification

    notifs = db_session.query(Notification).filter_by(
        user_id=admin.id, event_type="api_source_down"
    ).all()
    assert len(notifs) == 1
    assert "Notif Test" in notifs[0].title
    assert "403" in notifs[0].body


@pytest.mark.asyncio
async def test_ping_error_to_error_no_duplicate_notification(db_session):
    """When a source stays in error, no duplicate notification fires."""
    admin = User(
        email="no_dup_admin@test.com",
        name="No Dup Admin",
        role="admin",
        azure_id="no-dup-admin-1",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(admin)
    db_session.flush()

    src = ApiSource(
        name="nodup_src",
        display_name="NoDup Source",
        category="api",
        source_type="test",
        status="error",
        is_active=True,
    )
    db_session.add(src)
    db_session.flush()

    mock_connector = MagicMock()
    mock_connector.search = AsyncMock(side_effect=Exception("Still broken"))

    with patch("app.services.health_monitor._get_connector", return_value=mock_connector):
        from app.services.health_monitor import ping_source

        await ping_source(src, db_session)

    from app.models.notification import Notification

    notifs = db_session.query(Notification).filter_by(
        user_id=admin.id, event_type="api_source_down"
    ).all()
    assert len(notifs) == 0


@pytest.mark.asyncio
async def test_quota_warning_at_80_percent(db_session):
    """Quota warning fires when usage crosses 80%."""
    admin = User(
        email="quota_admin@test.com",
        name="Quota Admin",
        role="admin",
        azure_id="quota-admin-1",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(admin)
    db_session.flush()

    src = ApiSource(
        name="quota_src",
        display_name="Quota Source",
        category="api",
        source_type="test",
        status="live",
        is_active=True,
        monthly_quota=100,
        calls_this_month=79,
    )
    db_session.add(src)
    db_session.flush()

    mock_connector = MagicMock()
    mock_connector.search = AsyncMock(return_value=[{"mpn": "LM317"}])

    with patch("app.services.health_monitor._get_connector", return_value=mock_connector):
        from app.services.health_monitor import ping_source

        result = await ping_source(src, db_session)

    assert result["success"] is True
    # calls_this_month is now 80 (79 + 1 from the ping)
    assert src.calls_this_month == 80

    from app.models.notification import Notification

    notifs = db_session.query(Notification).filter_by(
        user_id=admin.id, event_type="api_quota_warning"
    ).all()
    assert len(notifs) == 1
    assert "80%" in notifs[0].title


@pytest.mark.asyncio
async def test_quota_critical_at_95_percent(db_session):
    """Quota critical fires when usage crosses 95%."""
    admin = User(
        email="crit_admin@test.com",
        name="Critical Admin",
        role="admin",
        azure_id="crit-admin-1",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(admin)
    db_session.flush()

    src = ApiSource(
        name="crit_src",
        display_name="Critical Source",
        category="api",
        source_type="test",
        status="live",
        is_active=True,
        monthly_quota=100,
        calls_this_month=94,
    )
    db_session.add(src)
    db_session.flush()

    mock_connector = MagicMock()
    mock_connector.search = AsyncMock(return_value=[{"mpn": "LM317"}])

    with patch("app.services.health_monitor._get_connector", return_value=mock_connector):
        from app.services.health_monitor import ping_source

        result = await ping_source(src, db_session)

    assert result["success"] is True

    from app.models.notification import Notification

    notifs = db_session.query(Notification).filter_by(
        user_id=admin.id, event_type="api_quota_critical"
    ).all()
    assert len(notifs) == 1
    assert "critical" in notifs[0].title.lower()


# ── Auth Error Skip Tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_connector_skips_retry_on_401(db_session):
    """401 errors skip retry and fail immediately."""
    import httpx

    from app.connectors.sources import BaseConnector

    class MockConnector(BaseConnector):
        async def _do_search(self, part_number):
            resp = httpx.Response(401, request=httpx.Request("GET", "http://test"))
            raise httpx.HTTPStatusError("401 Unauthorized", request=resp.request, response=resp)

    conn = MockConnector(max_retries=2)
    with pytest.raises(httpx.HTTPStatusError):
        await conn.search("LM317")

    # Should only have been called once (no retries)
    assert conn._breaker._fail_count >= 1


@pytest.mark.asyncio
async def test_connector_skips_retry_on_403(db_session):
    """403 errors skip retry and fail immediately."""
    import httpx

    from app.connectors.sources import BaseConnector

    class MockConnector(BaseConnector):
        call_count = 0

        async def _do_search(self, part_number):
            self.call_count += 1
            resp = httpx.Response(403, request=httpx.Request("POST", "http://test"))
            raise httpx.HTTPStatusError("403 Forbidden", request=resp.request, response=resp)

    conn = MockConnector(max_retries=2)
    with pytest.raises(httpx.HTTPStatusError):
        await conn.search("LM317")

    # Only 1 call, no retries for auth errors
    assert conn.call_count == 1
