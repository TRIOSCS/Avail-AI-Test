"""test_jobs_prospecting.py — Tests for prospecting background jobs.

Covers: _job_pool_health_report, _job_discover_prospects, _job_enrich_pool,
_job_find_contacts, _job_refresh_scores, _job_expire_and_resurface.

All jobs use SessionLocal() internally, so we patch app.database.SessionLocal
to return the test DB session with close() disabled.
"""

import asyncio
import importlib
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


@pytest.mark.parametrize(
    ("job_name", "delegate_name"),
    [
        ("_job_pool_health_report", "job_pool_health_report"),
        ("_job_discover_prospects", "job_discover_prospects"),
        ("_job_enrich_pool", "job_enrich_pool"),
        ("_job_find_contacts", "job_find_contacts"),
        ("_job_refresh_scores", "job_refresh_scores"),
        ("_job_expire_and_resurface", "job_expire_and_resurface"),
    ],
)
def test_job_delegates_to_prospect_scheduler(scheduler_db, job_name, delegate_name):
    """Each prospecting job delegates to its prospect_scheduler counterpart."""
    mock_fn = AsyncMock()
    with patch(f"app.services.prospect_scheduler.{delegate_name}", mock_fn):
        job = getattr(importlib.import_module("app.jobs.prospecting_jobs"), job_name)
        asyncio.run(job())
    mock_fn.assert_called_once()
