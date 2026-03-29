"""test_jobs_health.py — Tests for health monitoring background jobs.

Covers: _job_health_ping, _job_health_deep, _job_cleanup_usage_log,
_job_reset_monthly_usage.

All jobs use SessionLocal() internally, so we patch app.database.SessionLocal
to return the test DB session with close() disabled.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.scheduler import scheduler

# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture()
def scheduler_db(db_session: Session):
    """Patch SessionLocal so scheduler jobs use the test DB."""
    original_close = db_session.close
    db_session.close = lambda: None
    with patch("app.database.SessionLocal", return_value=db_session):
        yield db_session
    db_session.close = original_close


@pytest.fixture(autouse=True)
def _clear_scheduler_jobs():
    """Remove all jobs before/after each test to prevent leakage."""
    for job in scheduler.get_jobs():
        job.remove()
    yield
    for job in scheduler.get_jobs():
        job.remove()


# ── _job_health_ping() ────────────────────────────────────────────────


def test_job_health_ping():
    """_job_health_ping delegates to run_health_checks('ping')."""
    with patch(
        "app.services.health_monitor.run_health_checks",
        new_callable=AsyncMock,
        return_value={"total": 3, "passed": 3, "failed": 0},
    ) as mock_check:
        from app.jobs.health_jobs import _job_health_ping

        asyncio.run(_job_health_ping())
        mock_check.assert_awaited_once_with("ping")


# ── _job_health_deep() ────────────────────────────────────────────────


def test_job_health_deep():
    """_job_health_deep delegates to run_health_checks('deep')."""
    with patch(
        "app.services.health_monitor.run_health_checks",
        new_callable=AsyncMock,
        return_value={"total": 3, "passed": 2, "failed": 1},
    ) as mock_check:
        from app.jobs.health_jobs import _job_health_deep

        asyncio.run(_job_health_deep())
        mock_check.assert_awaited_once_with("deep")


# ── _job_cleanup_usage_log() ──────────────────────────────────────────


def test_job_cleanup_usage_log_deletes_old(scheduler_db):
    """_job_cleanup_usage_log deletes entries older than 90 days."""
    from app.models.config import ApiSource, ApiUsageLog

    src = ApiSource(
        name="cleanup_src",
        display_name="Cleanup Src",
        category="api",
        source_type="test",
        status="live",
        is_active=True,
    )
    scheduler_db.add(src)
    scheduler_db.commit()

    old = ApiUsageLog(
        source_id=src.id,
        timestamp=datetime.now(timezone.utc) - timedelta(days=120),
        endpoint="/test",
        status_code=200,
        response_ms=100,
        success=True,
        check_type="ping",
    )
    recent = ApiUsageLog(
        source_id=src.id,
        timestamp=datetime.now(timezone.utc) - timedelta(days=10),
        endpoint="/test",
        status_code=200,
        response_ms=50,
        success=True,
        check_type="ping",
    )
    scheduler_db.add_all([old, recent])
    scheduler_db.commit()

    from app.jobs.health_jobs import _job_cleanup_usage_log

    asyncio.run(_job_cleanup_usage_log())

    remaining = scheduler_db.query(ApiUsageLog).all()
    assert len(remaining) == 1
    assert remaining[0].id == recent.id


def test_job_cleanup_usage_log_handles_error():
    """_job_cleanup_usage_log rolls back and re-raises on error."""
    mock_db = MagicMock()
    mock_db.query.side_effect = Exception("DB error")

    with patch("app.database.SessionLocal", return_value=mock_db):
        from app.jobs.health_jobs import _job_cleanup_usage_log

        with pytest.raises(Exception, match="DB error"):
            asyncio.run(_job_cleanup_usage_log())

    mock_db.rollback.assert_called_once()
    mock_db.close.assert_called_once()


# ── _job_reset_monthly_usage() ────────────────────────────────────────


def test_job_reset_monthly_usage(scheduler_db):
    """_job_reset_monthly_usage resets calls_this_month to 0."""
    from app.models.config import ApiSource

    src1 = ApiSource(
        name="monthly_a",
        display_name="A",
        category="api",
        source_type="test",
        status="live",
        is_active=True,
        calls_this_month=150,
    )
    src2 = ApiSource(
        name="monthly_b",
        display_name="B",
        category="api",
        source_type="test",
        status="live",
        is_active=True,
        calls_this_month=300,
    )
    scheduler_db.add_all([src1, src2])
    scheduler_db.commit()

    from app.jobs.health_jobs import _job_reset_monthly_usage

    asyncio.run(_job_reset_monthly_usage())

    scheduler_db.refresh(src1)
    scheduler_db.refresh(src2)
    assert src1.calls_this_month == 0
    assert src2.calls_this_month == 0


def test_job_reset_monthly_usage_handles_error():
    """_job_reset_monthly_usage rolls back and re-raises on error."""
    mock_db = MagicMock()
    mock_db.query.return_value.update.side_effect = Exception("DB error")

    with patch("app.database.SessionLocal", return_value=mock_db):
        from app.jobs.health_jobs import _job_reset_monthly_usage

        with pytest.raises(Exception, match="DB error"):
            asyncio.run(_job_reset_monthly_usage())
