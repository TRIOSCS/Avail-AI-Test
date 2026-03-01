"""Tests for API health monitoring — models, service, endpoints.

Covers: ApiUsageLog model, health_monitor service (ping/deep/run),
credential-based status fix, system alerts endpoint, dashboard endpoint.

Depends on: conftest.py (db_session, TestSessionLocal, engine)
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.models.config import ApiSource, ApiUsageLog
from app.models import User


# ── Model Tests ──────────────────────────────────────────────────────


def test_api_usage_log_creation(db_session):
    """ApiUsageLog records can be created and linked to an ApiSource."""
    src = ApiSource(
        name="test_src", display_name="Test", category="api",
        source_type="test", status="live",
    )
    db_session.add(src)
    db_session.flush()

    log = ApiUsageLog(
        source_id=src.id, timestamp=datetime.now(timezone.utc),
        endpoint="search", status_code=200, response_ms=150,
        success=True, check_type="ping",
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
        name="test_cols", display_name="Test Cols", category="api",
        source_type="test", status="pending", monthly_quota=1000,
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
        name="nexar", display_name="Nexar", category="api",
        source_type="aggregator", status="pending", is_active=True,
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
        name="digikey", display_name="DigiKey", category="api",
        source_type="authorized", status="live", is_active=True,
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
        name="unknown", display_name="Unknown", category="api",
        source_type="test", status="pending", is_active=True,
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
        name="mouser", display_name="Mouser", category="api",
        source_type="authorized", status="live", is_active=True,
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
        name="mouser_fail", display_name="Mouser", category="api",
        source_type="authorized", status="live", is_active=True,
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
        name="no_conn", display_name="No Conn", category="api",
        source_type="test", status="pending", is_active=True,
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


# ── Fixtures for Endpoint Tests ──────────────────────────────────────


@pytest.fixture()
def admin_user(db_session):
    """Admin user for endpoint tests."""
    user = User(
        email="admin@test.com", name="Test Admin", role="admin",
        azure_id="test-azure-health", created_at=datetime.now(timezone.utc),
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
        name="broken_but_creds_set", display_name="Broken API",
        category="api", source_type="test", status="error",
        is_active=True, env_vars=["SOME_API_KEY"],
        last_error="Connection refused",
    )
    db_session.add(src)
    db_session.commit()

    resp = admin_client.get("/api/sources")
    data = resp.json()
    broken = next(s for s in data["sources"] if s["name"] == "broken_but_creds_set")
    # Status must remain "error" — not be flipped to "live" just because creds exist
    assert broken["status"] == "error"
