"""test_jobs_prospecting.py — Tests for prospecting background jobs

Covers: _job_pool_health_report, _job_discover_prospects, _job_enrich_pool,
_job_find_contacts, _job_refresh_scores, _job_expire_and_resurface.

All jobs use SessionLocal() internally, so we patch app.database.SessionLocal
to return the test DB session with close() disabled.
"""

import asyncio
from unittest.mock import AsyncMock, patch

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


# ── Prospecting Jobs ──────────────────────────────────────────────────


def test_pool_health_report(scheduler_db):
    """_job_pool_health_report delegates to prospect_scheduler."""
    mock_fn = AsyncMock()
    with patch("app.services.prospect_scheduler.job_pool_health_report", mock_fn):
        from app.jobs.prospecting_jobs import _job_pool_health_report

        asyncio.run(_job_pool_health_report())
    mock_fn.assert_called_once()


def test_discover_prospects(scheduler_db):
    """_job_discover_prospects delegates to prospect_scheduler."""
    mock_fn = AsyncMock()
    with patch("app.services.prospect_scheduler.job_discover_prospects", mock_fn):
        from app.jobs.prospecting_jobs import _job_discover_prospects

        asyncio.run(_job_discover_prospects())
    mock_fn.assert_called_once()


def test_enrich_pool(scheduler_db):
    """_job_enrich_pool delegates to prospect_scheduler."""
    mock_fn = AsyncMock()
    with patch("app.services.prospect_scheduler.job_enrich_pool", mock_fn):
        from app.jobs.prospecting_jobs import _job_enrich_pool

        asyncio.run(_job_enrich_pool())
    mock_fn.assert_called_once()


def test_find_contacts(scheduler_db):
    """_job_find_contacts delegates to prospect_scheduler."""
    mock_fn = AsyncMock()
    with patch("app.services.prospect_scheduler.job_find_contacts", mock_fn):
        from app.jobs.prospecting_jobs import _job_find_contacts

        asyncio.run(_job_find_contacts())
    mock_fn.assert_called_once()


def test_refresh_scores(scheduler_db):
    """_job_refresh_scores delegates to prospect_scheduler."""
    mock_fn = AsyncMock()
    with patch("app.services.prospect_scheduler.job_refresh_scores", mock_fn):
        from app.jobs.prospecting_jobs import _job_refresh_scores

        asyncio.run(_job_refresh_scores())
    mock_fn.assert_called_once()


def test_expire_and_resurface(scheduler_db):
    """_job_expire_and_resurface delegates to prospect_scheduler."""
    mock_fn = AsyncMock()
    with patch("app.services.prospect_scheduler.job_expire_and_resurface", mock_fn):
        from app.jobs.prospecting_jobs import _job_expire_and_resurface

        asyncio.run(_job_expire_and_resurface())
    mock_fn.assert_called_once()
