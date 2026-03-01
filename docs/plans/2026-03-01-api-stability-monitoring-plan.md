# API Stability & Monitoring System — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix inaccurate API status reporting (shows "live" when dead, "off" when working) by adding real health checks that verify actual connectivity. Add a full API Health dashboard tab, persistent warning banner, and usage tracking so broken APIs are immediately visible.

**Core Problem:** Status is currently derived from "are credentials set?" not "does the API actually respond?" The `list_api_sources` endpoint (sources.py:370-376) sets status to "live" just because env vars exist. This is wrong — credentials can be expired, rate-limited, or the API can be down. Status must come from real connectivity checks.

**Architecture:** Build on the existing `ApiSource` model and health endpoints. Add a `health_monitor` service for scheduled checks that actually hit each API every 15 minutes, an `api_usage_log` table for call tracking, a `/api/system/alerts` endpoint for the banner, a `view-apihealth` dashboard tab, and upgrade the Settings > Sources panel. Status is ONLY set by health checks and manual tests, never by credential presence alone.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, APScheduler, Alembic, vanilla JS, httpx (async)

**Existing Infrastructure to Build On:**
- `ApiSource` model (`app/models/config.py`) — already has `status`, `last_success`, `last_error`, `error_count_24h`, `avg_response_ms`
- `GET /api/sources/health-summary` (`app/routers/sources.py:509`) — already polls every 60s from frontend
- `POST /api/sources/{source_id}/test` (`app/routers/sources.py:418`) — manual test with "LM358N"
- `_get_connector_for_source()` (`app/routers/sources.py:50`) — instantiates connectors by name
- `CircuitBreaker` + `BaseConnector` (`app/connectors/sources.py`) — retry + circuit breaker
- `pollApiHealth()` (`app/static/app.js:773`) — 60s polling loop already exists
- APScheduler (`app/scheduler.py`) — already runs ~15 background jobs

**XSS Note:** The codebase uses `innerHTML` throughout (crm.js, app.js). All user-facing strings go through the existing `_esc()` helper. API data (source names, error messages) is escaped before insertion. This follows the established pattern.

---

### Task 1: Database Migration — Usage Log Table + ApiSource Columns

**Files:**
- Create: `alembic/versions/038_api_health_monitoring.py`
- Modify: `app/models/config.py` (add new columns to `ApiSource`, add `ApiUsageLog` model)

**Step 1: Write the migration**

Create `alembic/versions/038_api_health_monitoring.py`:

```python
"""Add api_usage_log table and health monitoring columns to api_sources.

Revision ID: 038_api_health_monitoring
Revises: 037_company_denormalized_counts
Create Date: 2026-03-01
"""

import sqlalchemy as sa
from alembic import op

revision = "038_api_health_monitoring"
down_revision = "037_company_denormalized_counts"


def upgrade() -> None:
    op.add_column("api_sources", sa.Column("monthly_quota", sa.Integer(), nullable=True))
    op.add_column("api_sources", sa.Column("calls_this_month", sa.Integer(), server_default="0"))
    op.add_column("api_sources", sa.Column("last_ping_at", sa.DateTime(), nullable=True))
    op.add_column("api_sources", sa.Column("last_deep_test_at", sa.DateTime(), nullable=True))

    op.create_table(
        "api_usage_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("api_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("endpoint", sa.String(200)),
        sa.Column("status_code", sa.Integer()),
        sa.Column("response_ms", sa.Integer()),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("error_message", sa.String(500)),
        sa.Column("check_type", sa.String(20), nullable=False),
    )
    op.create_index("ix_usage_log_source_ts", "api_usage_log", ["source_id", "timestamp"])


def downgrade() -> None:
    op.drop_index("ix_usage_log_source_ts", table_name="api_usage_log")
    op.drop_table("api_usage_log")
    op.drop_column("api_sources", "last_deep_test_at")
    op.drop_column("api_sources", "last_ping_at")
    op.drop_column("api_sources", "calls_this_month")
    op.drop_column("api_sources", "monthly_quota")
```

**Step 2: Add model classes**

In `app/models/config.py`, add columns to `ApiSource` class (after `avg_response_ms` line 31):
```python
    monthly_quota = Column(Integer, nullable=True)
    calls_this_month = Column(Integer, default=0, server_default="0")
    last_ping_at = Column(DateTime)
    last_deep_test_at = Column(DateTime)
```

Add new class after `SystemConfig`:
```python
class ApiUsageLog(Base):
    """Tracks individual API calls for usage monitoring and health history."""

    __tablename__ = "api_usage_log"
    id = Column(Integer, primary_key=True)
    source_id = Column(Integer, ForeignKey("api_sources.id", ondelete="CASCADE"), nullable=False)
    timestamp = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    endpoint = Column(String(200))
    status_code = Column(Integer)
    response_ms = Column(Integer)
    success = Column(Boolean, nullable=False)
    error_message = Column(String(500))
    check_type = Column(String(20), nullable=False)

    __table_args__ = (
        Index("ix_usage_log_source_ts", "source_id", "timestamp"),
    )
```

**Step 3: Register `ApiUsageLog` in `app/models/__init__.py`**

Add `ApiUsageLog` to the imports from `.config`.

**Step 4: Run migration**

```bash
cd /root/availai && alembic upgrade head
```

**Step 5: Write tests for the model**

In `tests/test_api_health.py`:

```python
"""Tests for API health monitoring — models, service, endpoints."""

import pytest
from datetime import datetime, timezone
from app.models.config import ApiSource, ApiUsageLog


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
```

**Step 6: Run tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_api_health.py -v
```

**Step 7: Commit**

```bash
cd /root/availai && git add alembic/versions/038_api_health_monitoring.py app/models/config.py app/models/__init__.py tests/test_api_health.py
git commit -m "feat: add api_usage_log table and health columns on api_sources (migration 038)"
```

---

### Task 2: Health Monitor Service

**Files:**
- Create: `app/services/health_monitor.py`
- Test: `tests/test_api_health.py` (append)

**CRITICAL: This is the accuracy fix.** Status is set ONLY by actual API responses, not by credential presence.

**Step 1: Write failing tests**

Append to `tests/test_api_health.py`:

```python
from unittest.mock import AsyncMock, patch, MagicMock
from app.services.health_monitor import ping_source, deep_test_source, run_health_checks


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
        await deep_test_source(src, db_session)

    logs = db_session.query(ApiUsageLog).filter_by(source_id=src.id).all()
    assert len(logs) == 1
    assert logs[0].check_type == "deep"
    assert logs[0].success is True
```

**Step 2: Run tests — verify they fail**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_api_health.py::test_ping_source_success -v
```
Expected: FAIL (module not found)

**Step 3: Write the health monitor service**

Create `app/services/health_monitor.py`:

```python
"""Health monitor service — scheduled API health checking.

Called by scheduler jobs to verify all active API connectors are reachable.
Two check levels:
  - ping: lightweight connectivity test via real API call (every 15 min)
  - deep: full search with known MPN + usage log (every 2 hours)

Status is determined ONLY by actual API responses, never by credential presence.
This is the source of truth for whether an API is actually working.

Depends on: app.models.config (ApiSource, ApiUsageLog), app.routers.sources (_get_connector_for_source)
Called by: app.scheduler (health_check_ping, health_check_deep jobs)
"""

import time
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from ..models.config import ApiSource, ApiUsageLog

# Known good MPN for testing — universally available across all distributors
DEEP_TEST_MPN = "LM317"


def _get_connector(source: ApiSource, db: Session):
    """Get a connector instance for the given source. Returns None if unavailable."""
    from ..routers.sources import _get_connector_for_source
    try:
        return _get_connector_for_source(source.name, db)
    except Exception:
        return None


async def ping_source(source: ApiSource, db: Session) -> dict:
    """Lightweight health check — verify connector can be instantiated and respond.

    This is the ONLY place that should set source.status to 'live' or 'error'.
    Status is based on actual API response, not credential presence.

    Updates source status, last_ping_at, last_error fields.
    Returns dict with success, elapsed_ms, error keys.
    """
    now = datetime.now(timezone.utc)
    connector = _get_connector(source, db)

    if not connector:
        source.status = "disabled"
        source.last_ping_at = now
        db.flush()
        return {"success": False, "error": "No connector available", "elapsed_ms": 0}

    start = time.time()
    try:
        await connector.search(DEEP_TEST_MPN)
        elapsed_ms = int((time.time() - start) * 1000)

        source.status = "live"
        source.last_success = now
        source.last_ping_at = now
        source.last_error = None
        source.avg_response_ms = elapsed_ms
        source.calls_this_month = (source.calls_this_month or 0) + 1
        db.flush()

        return {"success": True, "elapsed_ms": elapsed_ms, "error": None}

    except Exception as e:
        elapsed_ms = int((time.time() - start) * 1000)
        error_msg = str(e)[:500]

        source.status = "error"
        source.last_error = error_msg
        source.last_error_at = now
        source.last_ping_at = now
        source.error_count_24h = (source.error_count_24h or 0) + 1
        db.flush()

        logger.warning("Health ping failed for {}: {}", source.name, error_msg)
        return {"success": False, "elapsed_ms": elapsed_ms, "error": error_msg}


async def deep_test_source(source: ApiSource, db: Session) -> dict:
    """Full functional test — search a known MPN and verify results.

    Writes an ApiUsageLog entry for every test. Updates source timing fields.
    Returns dict with success, results_count, elapsed_ms, error keys.
    """
    now = datetime.now(timezone.utc)
    connector = _get_connector(source, db)

    if not connector:
        log = ApiUsageLog(
            source_id=source.id, timestamp=now, endpoint="deep_test",
            success=False, error_message="No connector", check_type="deep",
        )
        db.add(log)
        source.status = "disabled"
        source.last_deep_test_at = now
        db.flush()
        return {"success": False, "results_count": 0, "elapsed_ms": 0, "error": "No connector"}

    start = time.time()
    try:
        results = await connector.search(DEEP_TEST_MPN)
        elapsed_ms = int((time.time() - start) * 1000)

        source.status = "live"
        source.last_success = now
        source.last_deep_test_at = now
        source.last_error = None
        source.avg_response_ms = elapsed_ms
        source.calls_this_month = (source.calls_this_month or 0) + 1

        log = ApiUsageLog(
            source_id=source.id, timestamp=now, endpoint="deep_test",
            status_code=200, response_ms=elapsed_ms,
            success=True, check_type="deep",
        )
        db.add(log)
        db.flush()

        return {"success": True, "results_count": len(results), "elapsed_ms": elapsed_ms, "error": None}

    except Exception as e:
        elapsed_ms = int((time.time() - start) * 1000)
        error_msg = str(e)[:500]

        source.status = "error"
        source.last_error = error_msg
        source.last_error_at = now
        source.last_deep_test_at = now
        source.error_count_24h = (source.error_count_24h or 0) + 1

        log = ApiUsageLog(
            source_id=source.id, timestamp=now, endpoint="deep_test",
            response_ms=elapsed_ms, success=False,
            error_message=error_msg, check_type="deep",
        )
        db.add(log)
        db.flush()

        logger.warning("Deep test failed for {}: {}", source.name, error_msg)
        return {"success": False, "results_count": 0, "elapsed_ms": elapsed_ms, "error": error_msg}


async def run_health_checks(check_type: str = "ping") -> dict:
    """Run health checks on all active sources.

    Args:
        check_type: "ping" for lightweight, "deep" for full functional test.

    Returns dict with total, passed, failed counts and per-source results.
    """
    from ..database import SessionLocal

    db = SessionLocal()
    results = {"total": 0, "passed": 0, "failed": 0, "sources": {}}

    try:
        sources = (
            db.query(ApiSource)
            .filter(ApiSource.is_active == True)  # noqa: E712
            .all()
        )
        results["total"] = len(sources)

        check_fn = deep_test_source if check_type == "deep" else ping_source

        for source in sources:
            try:
                result = await check_fn(source, db)
                results["sources"][source.name] = result
                if result["success"]:
                    results["passed"] += 1
                else:
                    results["failed"] += 1
            except Exception as e:
                logger.error("Health check crashed for {}: {}", source.name, e)
                results["sources"][source.name] = {"success": False, "error": str(e)}
                results["failed"] += 1

        db.commit()
        logger.info(
            "Health check ({}) complete: {}/{} passed",
            check_type, results["passed"], results["total"],
        )

    except Exception as e:
        logger.exception("Health check run failed: {}", e)
        db.rollback()
    finally:
        db.close()

    return results
```

**Step 4: Run tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_api_health.py -v
```

**Step 5: Commit**

```bash
cd /root/availai && git add app/services/health_monitor.py tests/test_api_health.py
git commit -m "feat: add health_monitor service — status from real API checks, not credential presence"
```

---

### Task 3: Fix Status Accuracy — Remove Credential-Based Status Setting

**CRITICAL TASK — This is the root cause of inaccurate status.**

**Files:**
- Modify: `app/routers/sources.py` (remove auto-status in list_api_sources)
- Test: `tests/test_api_health.py` (append)

**Step 1: Write test proving the problem**

```python
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
```

**Step 2: Fix the list_api_sources endpoint**

In `app/routers/sources.py`, in the `list_api_sources` function (lines 367-376), **remove** the auto-status logic that flips status based on credential presence:

```python
    # REMOVE THIS BLOCK (lines 367-376):
    # for src in sources:
    #     env_vars = src.env_vars or []
    #     if env_vars:
    #         all_set = all(credential_is_set(db, src.name, v) for v in env_vars)
    #         any_set = any(credential_is_set(db, src.name, v) for v in env_vars)
    #         if all_set and src.status == "pending":
    #             src.status = "live"
    #         elif not any_set and src.status == "live":
    #             src.status = "pending"
    # db.commit()
```

Replace with a simpler version that only marks sources as "pending" when credentials are completely missing (never auto-sets to "live"):

```python
    for src in sources:
        env_vars = src.env_vars or []
        if env_vars:
            any_set = any(credential_is_set(db, src.name, v) for v in env_vars)
            # Only downgrade to pending if ALL credentials are missing
            # Never auto-upgrade to "live" — that's the health checker's job
            if not any_set and src.status not in ("disabled", "error"):
                src.status = "pending"
    db.commit()
```

**Step 3: Run tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_api_health.py -v
```

**Step 4: Commit**

```bash
cd /root/availai && git add app/routers/sources.py tests/test_api_health.py
git commit -m "fix: stop auto-setting status to 'live' from credential presence — health checks are source of truth"
```

---

### Task 4: Scheduler Integration

**Files:**
- Modify: `app/scheduler.py` (add 4 new jobs)
- Test: `tests/test_api_health.py` (append)

**Step 1: Write failing test**

Append to `tests/test_api_health.py`:

```python
def test_scheduler_has_health_jobs():
    """Scheduler registers health check jobs."""
    from app.scheduler import scheduler, configure_scheduler
    configure_scheduler()
    job_ids = [j.id for j in scheduler.get_jobs()]
    assert "health_ping" in job_ids
    assert "health_deep" in job_ids
    assert "reset_error_counts" in job_ids
    assert "cleanup_usage_log" in job_ids
    scheduler.remove_all_jobs()
```

**Step 2: Run test — verify it fails**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_api_health.py::test_scheduler_has_health_jobs -v
```

**Step 3: Add jobs to scheduler.py**

Add import at top of `app/scheduler.py`:
```python
from .services.health_monitor import run_health_checks
```

Add job functions (after existing `@_traced_job` decorated functions):
```python
@_traced_job
async def _job_health_ping():
    """Lightweight health check on all active API sources."""
    await run_health_checks("ping")


@_traced_job
async def _job_health_deep():
    """Full functional test on all active API sources."""
    await run_health_checks("deep")


@_traced_job
async def _job_reset_error_counts():
    """Reset 24h error counts at midnight UTC."""
    from .database import SessionLocal
    from .models.config import ApiSource
    db = SessionLocal()
    try:
        db.query(ApiSource).update({ApiSource.error_count_24h: 0})
        db.commit()
        logger.info("Reset error_count_24h for all API sources")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@_traced_job
async def _job_cleanup_usage_log():
    """Delete usage log entries older than 90 days."""
    from .database import SessionLocal
    from .models.config import ApiUsageLog
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=90)
        deleted = db.query(ApiUsageLog).filter(ApiUsageLog.timestamp < cutoff).delete()
        db.commit()
        if deleted:
            logger.info("Cleaned up {} old usage log entries", deleted)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@_traced_job
async def _job_reset_monthly_usage():
    """Reset calls_this_month on the 1st of each month."""
    from .database import SessionLocal
    from .models.config import ApiSource
    db = SessionLocal()
    try:
        db.query(ApiSource).update({ApiSource.calls_this_month: 0})
        db.commit()
        logger.info("Reset monthly API usage counters")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
```

In `configure_scheduler()`, add registrations:
```python
    # API Health Monitoring
    scheduler.add_job(_job_health_ping, IntervalTrigger(minutes=15),
                      id="health_ping", name="API health ping")
    scheduler.add_job(_job_health_deep, IntervalTrigger(hours=2),
                      id="health_deep", name="API deep health test")
    scheduler.add_job(_job_reset_error_counts, CronTrigger(hour=0, minute=0),
                      id="reset_error_counts", name="Reset 24h error counts")
    scheduler.add_job(_job_cleanup_usage_log, CronTrigger(day=1, hour=1),
                      id="cleanup_usage_log", name="Cleanup old usage logs")
    scheduler.add_job(_job_reset_monthly_usage, CronTrigger(day=1, hour=0, minute=5),
                      id="reset_monthly_usage", name="Reset monthly API usage")
```

**Step 4: Run tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_api_health.py -v
```

**Step 5: Commit**

```bash
cd /root/availai && git add app/scheduler.py tests/test_api_health.py
git commit -m "feat: register health check scheduler jobs (15min ping, 2h deep, daily reset, monthly cleanup)"
```

---

### Task 5: System Alerts Endpoint + Enhanced Health Summary

**Files:**
- Modify: `app/routers/sources.py` (enhance health-summary, add alerts endpoint)
- Test: `tests/test_api_health.py` (append)

**Step 1: Write failing tests**

Append to `tests/test_api_health.py`:

```python
from tests.conftest import engine
from fastapi.testclient import TestClient
from app.models import User


@pytest.fixture()
def admin_client(db_session):
    """TestClient authenticated as admin user."""
    admin = User(name="Admin", email="admin@test.com", role="admin")
    db_session.add(admin)
    db_session.flush()

    from app.database import get_db
    from app.dependencies import require_admin, require_settings_access, require_user
    from app.main import app

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = lambda: admin
    app.dependency_overrides[require_admin] = lambda: admin
    app.dependency_overrides[require_settings_access] = lambda: admin

    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def test_system_alerts_returns_errors(admin_client, db_session):
    """GET /api/system/alerts returns sources in error/degraded state."""
    ok_src = ApiSource(
        name="ok_api", display_name="OK API", category="api",
        source_type="test", status="live", is_active=True,
    )
    bad_src = ApiSource(
        name="bad_api", display_name="Bad API", category="api",
        source_type="test", status="error", is_active=True,
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
        name="degraded_api", display_name="Degraded API", category="api",
        source_type="test", status="degraded", is_active=True,
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
        name="healthy", display_name="Healthy", category="api",
        source_type="test", status="live", is_active=True,
    )
    db_session.add(src)
    db_session.commit()

    resp = admin_client.get("/api/system/alerts")
    data = resp.json()
    assert data["count"] == 0
    assert data["alerts"] == []
```

**Step 2: Run tests — verify they fail**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_api_health.py::test_system_alerts_returns_errors -v
```

**Step 3: Add the alerts endpoint**

In `app/routers/sources.py`, add after the existing `health_summary` endpoint (after line 530):

```python
@router.get("/api/system/alerts")
async def system_alerts(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Active API alerts — sources in error or degraded state.

    Polled by frontend every 60s for the warning banner.
    Available to all authenticated users (not admin-only).
    """
    problem_sources = (
        db.query(ApiSource)
        .filter(
            ApiSource.is_active == True,  # noqa: E712
            ApiSource.status.in_(["error", "degraded"]),
        )
        .order_by(ApiSource.display_name)
        .all()
    )
    return {
        "alerts": [
            {
                "source_name": s.name,
                "display_name": s.display_name,
                "status": s.status,
                "last_error": s.last_error,
                "since": s.last_error_at.isoformat() if s.last_error_at else None,
            }
            for s in problem_sources
        ],
        "count": len(problem_sources),
    }
```

**Step 4: Enhance the existing health-summary to include degraded**

At line 517, change the filter from:
```python
.filter(ApiSource.is_active == True, ApiSource.status == "error")
```
To:
```python
.filter(ApiSource.is_active == True, ApiSource.status.in_(["error", "degraded"]))
```

**Step 5: Run tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_api_health.py -v
```

**Step 6: Commit**

```bash
cd /root/availai && git add app/routers/sources.py tests/test_api_health.py
git commit -m "feat: add /api/system/alerts endpoint for banner + include degraded in health-summary"
```

---

### Task 6: API Health Dashboard Endpoint

**Files:**
- Modify: `app/routers/admin.py` (add dashboard endpoint)
- Test: `tests/test_api_health.py` (append)

**Step 1: Write failing tests**

Append to `tests/test_api_health.py`:

```python
def test_api_health_dashboard(admin_client, db_session):
    """GET /api/admin/api-health/dashboard returns full connector stats."""
    src = ApiSource(
        name="dash_test", display_name="Dashboard Test", category="api",
        source_type="test", status="live", is_active=True,
        total_searches=100, total_results=500, avg_response_ms=200,
        monthly_quota=1000, calls_this_month=450,
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
        name="log_test", display_name="Log Test", category="api",
        source_type="test", status="live", is_active=True,
    )
    db_session.add(src)
    db_session.flush()

    for i in range(3):
        log = ApiUsageLog(
            source_id=src.id, timestamp=datetime.now(timezone.utc),
            success=(i != 1), response_ms=100 + i * 50,
            check_type="ping",
        )
        db_session.add(log)
    db_session.commit()

    resp = admin_client.get("/api/admin/api-health/dashboard")
    data = resp.json()
    log_src = next(s for s in data["sources"] if s["name"] == "log_test")
    assert log_src["recent_checks"] == 3
    assert log_src["recent_failures"] == 1
```

**Step 2: Add the dashboard endpoint**

In `app/routers/admin.py`, add after the existing `connector-health` endpoint. Add `ApiUsageLog` to the model imports at the top. See full endpoint code in design doc — it queries all ApiSources with aggregated usage log stats.

```python
@router.get("/api/admin/api-health/dashboard")
@limiter.limit("30/minute")
def api_health_dashboard(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Full API health dashboard data — status, usage, recent check history."""
    from datetime import timedelta
    from sqlalchemy import func

    sources = db.query(ApiSource).order_by(ApiSource.display_name).all()
    cutoff_24h = datetime.now(timezone.utc) - timedelta(hours=24)

    check_stats = (
        db.query(
            ApiUsageLog.source_id,
            func.count(ApiUsageLog.id).label("total"),
            func.count(ApiUsageLog.id).filter(ApiUsageLog.success == False).label("failures"),  # noqa: E712
        )
        .filter(ApiUsageLog.timestamp >= cutoff_24h)
        .group_by(ApiUsageLog.source_id)
        .all()
    )
    stats_map = {row.source_id: {"total": row.total, "failures": row.failures} for row in check_stats}

    result = []
    for src in sources:
        quota = src.monthly_quota
        calls = src.calls_this_month or 0
        usage_pct = round((calls / quota) * 100, 1) if quota and quota > 0 else None
        checks = stats_map.get(src.id, {"total": 0, "failures": 0})

        result.append({
            "id": src.id,
            "name": src.name,
            "display_name": src.display_name,
            "category": src.category,
            "source_type": src.source_type,
            "status": src.status,
            "is_active": src.is_active,
            "last_success": src.last_success.isoformat() if src.last_success else None,
            "last_error": src.last_error,
            "last_error_at": src.last_error_at.isoformat() if src.last_error_at else None,
            "error_count_24h": src.error_count_24h or 0,
            "avg_response_ms": src.avg_response_ms or 0,
            "total_searches": src.total_searches or 0,
            "monthly_quota": quota,
            "calls_this_month": calls,
            "usage_pct": usage_pct,
            "last_ping_at": src.last_ping_at.isoformat() if src.last_ping_at else None,
            "last_deep_test_at": src.last_deep_test_at.isoformat() if src.last_deep_test_at else None,
            "recent_checks": checks["total"],
            "recent_failures": checks["failures"],
        })

    return {"sources": result}
```

**Step 3: Run tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_api_health.py -v
```

**Step 4: Commit**

```bash
cd /root/availai && git add app/routers/admin.py tests/test_api_health.py
git commit -m "feat: add /api/admin/api-health/dashboard endpoint with usage and check history"
```

---

### Task 7: Frontend — Persistent Warning Banner

**Files:**
- Modify: `app/templates/index.html` (add banner HTML + CSS)
- Modify: `app/static/app.js` (upgrade polling to show banner)

**Step 1: Add banner HTML to index.html**

After the topbar div, add the banner element. Style it as a fixed amber/red bar. See design doc for exact HTML.

Key elements: `#apiHealthBanner` div (hidden by default), `#apiHealthBannerText` span, dismiss button, "View Details" link to `sidebarNav('apihealth',...)`.

**Step 2: Add banner CSS**

Add styles for `.api-health-banner`, `.api-health-banner.critical`, `.api-health-banner-content`, dismiss button, link button. See design doc for CSS.

**Step 3: Upgrade pollApiHealth() in app.js**

Replace the existing `pollApiHealth()` (line 773-783) to:
- Call `/api/system/alerts` instead of `/api/sources/health-summary`
- Show/hide the banner based on alert count
- Set banner color: amber for degraded, red for error
- Track dismissed alerts in a Set (per-session)
- Update the `#apiHealthBadge` on the sidebar nav button

**Step 4: Commit**

```bash
cd /root/availai && git add app/templates/index.html app/static/app.js
git commit -m "feat: add persistent API health warning banner with 60s polling"
```

---

### Task 8: Frontend — API Health Dashboard Tab

**Files:**
- Modify: `app/templates/index.html` (add sidebar button + view div)
- Modify: `app/static/app.js` (add to ALL_VIEWS + sidebarNav routes)
- Modify: `app/static/crm.js` (add dashboard rendering functions)

**Step 1: Add sidebar nav button in index.html**

In the settings group (line 154-158), add before the Settings button:
```html
<button type="button" class="sb-nav-btn" id="navApiHealth" data-tip="API Health"
  onclick="sidebarNav('apihealth',this)">
  <span class="sb-nav-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
    stroke-width="1.75"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg></span>
  API Health<span id="apiHealthBadge" class="sb-badge" style="display:none;background:var(--red);color:#fff"></span>
</button>
```

**Step 2: Add view container in index.html**

Near line 1165 (with other view divs):
```html
<div id="view-apihealth" class="hidden" style="display:none">
    <div class="vendor-header">
        <h2>API Health Monitor</h2>
        <button class="btn btn-sm" onclick="refreshApiHealthDashboard()" style="margin-left:auto">Refresh</button>
    </div>
    <div id="apiHealthDashboard"><p class="empty">Loading...</p></div>
</div>
```

**Step 3: Register in app.js**

- Add `'view-apihealth'` to `ALL_VIEWS` array (line 873)
- Add `apihealth: () => window.showApiHealth()` to sidebarNav routes (line 6844)

**Step 4: Add dashboard rendering in crm.js**

Add `showApiHealth()`, `loadApiHealthDashboard()`, `renderApiHealthDashboard()`, `_renderHealthCard()`, `_timeAgo()`, `testSourceNow()` functions. These render:

- **Summary bar**: Live/Degraded/Error/Total counts
- **Status grid**: Card per active API with dot indicator, status, last success, last error, response time, recent check dots, "Test Now" button
- **Usage overview**: Progress bars for APIs with quotas
- **Inactive section**: Collapsible list of planned/disabled sources

All display strings are escaped using the existing `_esc()` helper. See design doc for full function code.

**Step 5: Add dashboard CSS**

Add styles for `.ahd-summary`, `.ahd-grid`, `.ahd-card`, `.ahd-dot`, `.ahd-usage-bar`, `.ahd-mini-dots` etc. See design doc for CSS.

**Step 6: Commit**

```bash
cd /root/availai && git add app/templates/index.html app/static/app.js app/static/crm.js
git commit -m "feat: add API Health dashboard tab with status grid, usage overview, and test buttons"
```

---

### Task 9: Enhanced Settings > Sources Panel

**Files:**
- Modify: `app/routers/sources.py` (add health fields to list response)
- Modify: `app/static/crm.js` (update source card rendering)

**Step 1: Add health fields to sources list response**

In `app/routers/sources.py`, in `list_api_sources()`, add to each item dict (around line 410):
```python
                "last_error_at": src.last_error_at.isoformat() if src.last_error_at else None,
                "error_count_24h": src.error_count_24h or 0,
                "monthly_quota": src.monthly_quota,
                "calls_this_month": src.calls_this_month or 0,
                "last_ping_at": src.last_ping_at.isoformat() if src.last_ping_at else None,
```

**Step 2: Update source card rendering in crm.js**

In `_renderSourceCards()`, add health metadata line to each card showing:
- Last checked time (from `last_ping_at`)
- Response time (from `avg_response_ms`)
- Usage count if quota set (e.g., "450/1000 calls")
- Error count (from `error_count_24h`)

**Step 3: Commit**

```bash
cd /root/availai && git add app/routers/sources.py app/static/crm.js
git commit -m "feat: enhance Settings > Sources panel with health data and usage indicators"
```

---

### Task 10: Seed Known Quotas

**Files:**
- Modify: `app/startup.py` or seed function in `app/main.py`

**Step 1: Add quota seeding**

In the `_seed_api_sources()` function, add a quota backfill after the source seeding:

```python
    quota_map = {
        "apollo": 10000,
        "hunter": 500,
        "lusha": 100,
        "clearbit": 1000,
        "digikey": 1000,
        "mouser": 1000,
        "oemsecrets": 5000,
    }
    for name, quota in quota_map.items():
        src = db.query(ApiSource).filter_by(name=name).first()
        if src and not src.monthly_quota:
            src.monthly_quota = quota
    db.commit()
```

Only sets quota if currently NULL — admin overrides are preserved.

**Step 2: Commit**

```bash
cd /root/availai && git add app/main.py
git commit -m "feat: seed known API monthly quotas for usage tracking"
```

---

### Task 11: Full Test Suite + Coverage Check

**Step 1: Run full test suite with coverage**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=short -q
```

Verify: 100% coverage maintained, no regressions.

**Step 2: Run linting**

```bash
cd /root/availai && ruff check app/services/health_monitor.py app/routers/sources.py app/routers/admin.py app/scheduler.py app/models/config.py
```

Fix any issues.

**Step 3: Commit any fixes**

```bash
cd /root/availai && git add tests/ && git commit -m "test: comprehensive API health monitoring test coverage"
```

---

### Task 12: Deploy + Verify

**Step 1: Build and deploy**

```bash
cd /root/availai && docker compose up -d --build
```

**Step 2: Check logs for clean startup**

```bash
docker compose logs -f app 2>&1 | head -50
```

Verify: Health check jobs registered, migration applied, no errors.

**Step 3: Manual verification checklist**

- [ ] "API Health" tab visible in sidebar
- [ ] Dashboard loads showing all connectors with accurate status
- [ ] Warning banner appears when any API is in error state
- [ ] "Test Now" button works and updates status correctly
- [ ] Settings > Sources shows enhanced health data
- [ ] After 15 minutes, health ping runs (check logs)
- [ ] Status reflects ACTUAL API state, not just credential presence
