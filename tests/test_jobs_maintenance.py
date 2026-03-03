"""test_jobs_maintenance.py — Tests for maintenance background jobs

Covers: _job_cache_cleanup, _job_reset_connector_errors, _job_auto_attribute_activities,
_job_auto_dedup, _job_integrity_check.

All jobs use SessionLocal() internally, so we patch app.database.SessionLocal
to return the test DB session with close() disabled.
"""

import asyncio
from unittest.mock import MagicMock, patch

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


# ── _job_cache_cleanup() ──────────────────────────────────────────────


def test_cache_cleanup_calls_cleanup_expired():
    """Cache cleanup job delegates to intel_cache.cleanup_expired."""
    with patch("app.cache.intel_cache.cleanup_expired") as mock_cleanup:
        from app.jobs.maintenance_jobs import _job_cache_cleanup

        asyncio.run(_job_cache_cleanup())
        mock_cleanup.assert_called_once()


def test_cache_cleanup_handles_error():
    """Cache cleanup handles import or execution errors."""
    with patch(
        "app.cache.intel_cache.cleanup_expired",
        side_effect=Exception("Cache corrupted"),
    ):
        from app.jobs.maintenance_jobs import _job_cache_cleanup

        asyncio.run(_job_cache_cleanup())


# ── _job_reset_connector_errors() ─────────────────────────────────────


def test_reset_connector_errors(scheduler_db):
    """_job_reset_connector_errors zeroes error_count_24h on all sources."""
    from app.models import ApiSource

    src1 = ApiSource(
        name="src_a", display_name="A", category="dist",
        source_type="api", status="live", error_count_24h=5,
    )
    src2 = ApiSource(
        name="src_b", display_name="B", category="broker",
        source_type="api", status="live", error_count_24h=0,
    )
    scheduler_db.add_all([src1, src2])
    scheduler_db.commit()

    from app.jobs.maintenance_jobs import _job_reset_connector_errors

    asyncio.run(_job_reset_connector_errors())

    scheduler_db.refresh(src1)
    scheduler_db.refresh(src2)
    assert src1.error_count_24h == 0
    assert src2.error_count_24h == 0


def test_reset_connector_errors_exception(scheduler_db):
    """_job_reset_connector_errors handles DB exceptions gracefully."""
    with patch.object(scheduler_db, "query", side_effect=Exception("DB crashed")):
        from app.jobs.maintenance_jobs import _job_reset_connector_errors

        asyncio.run(_job_reset_connector_errors())


# ── _job_auto_attribute_activities() ──────────────────────────────────


def test_auto_attribute_activities_success(scheduler_db):
    """_job_auto_attribute_activities happy path with matches."""
    mock_attribution = MagicMock(
        return_value={
            "rule_matched": 5,
            "ai_matched": 3,
            "auto_dismissed": 1,
        }
    )
    with patch("app.services.auto_attribution_service.run_auto_attribution", mock_attribution):
        from app.jobs.maintenance_jobs import _job_auto_attribute_activities

        asyncio.run(_job_auto_attribute_activities())
    mock_attribution.assert_called_once()


def test_auto_attribute_activities_no_matches(scheduler_db):
    """_job_auto_attribute_activities no matches."""
    mock_attribution = MagicMock(
        return_value={
            "rule_matched": 0,
            "ai_matched": 0,
            "auto_dismissed": 0,
        }
    )
    with patch("app.services.auto_attribution_service.run_auto_attribution", mock_attribution):
        from app.jobs.maintenance_jobs import _job_auto_attribute_activities

        asyncio.run(_job_auto_attribute_activities())


def test_auto_attribute_activities_error(scheduler_db):
    """Exception rolls back."""
    mock_attribution = MagicMock(side_effect=Exception("Attribution failed"))
    with patch("app.services.auto_attribution_service.run_auto_attribution", mock_attribution):
        from app.jobs.maintenance_jobs import _job_auto_attribute_activities

        asyncio.run(_job_auto_attribute_activities())


# ── _job_auto_dedup() ─────────────────────────────────────────────────


def test_auto_dedup_success(scheduler_db):
    """_job_auto_dedup happy path with merges."""
    mock_dedup = MagicMock(
        return_value={
            "vendors_merged": 2,
            "companies_merged": 1,
        }
    )
    with patch("app.services.auto_dedup_service.run_auto_dedup", mock_dedup):
        from app.jobs.maintenance_jobs import _job_auto_dedup

        asyncio.run(_job_auto_dedup())
    mock_dedup.assert_called_once()


def test_auto_dedup_no_merges(scheduler_db):
    """_job_auto_dedup no merges."""
    mock_dedup = MagicMock(
        return_value={
            "vendors_merged": 0,
            "companies_merged": 0,
        }
    )
    with patch("app.services.auto_dedup_service.run_auto_dedup", mock_dedup):
        from app.jobs.maintenance_jobs import _job_auto_dedup

        asyncio.run(_job_auto_dedup())


def test_auto_dedup_error(scheduler_db):
    """Exception rolls back."""
    mock_dedup = MagicMock(side_effect=Exception("Dedup failed"))
    with patch("app.services.auto_dedup_service.run_auto_dedup", mock_dedup):
        from app.jobs.maintenance_jobs import _job_auto_dedup

        asyncio.run(_job_auto_dedup())


# ── _job_integrity_check() ────────────────────────────────────────────


def test_integrity_check_success(scheduler_db):
    """_job_integrity_check runs integrity service."""
    mock_report = {
        "status": "healthy",
        "material_cards_total": 100,
        "healed": {"requirements": 0, "sightings": 0, "offers": 0},
    }
    mock_check = MagicMock(return_value=mock_report)
    with patch("app.services.integrity_service.run_integrity_check", mock_check):
        from app.jobs.maintenance_jobs import _job_integrity_check

        asyncio.run(_job_integrity_check())
    mock_check.assert_called_once()


def test_integrity_check_error(scheduler_db):
    """Exception is caught."""
    mock_check = MagicMock(side_effect=Exception("Integrity failed"))
    with patch("app.services.integrity_service.run_integrity_check", mock_check):
        from app.jobs.maintenance_jobs import _job_integrity_check

        asyncio.run(_job_integrity_check())
