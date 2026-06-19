"""test_sp4_jobs.py — Tests for SP4 account sweep and reactivation scheduler jobs.

Covers: register_sweep_jobs(), _job_account_sweep, _job_auto_surface_reactivation
delegation tests.

Called by: pytest
Depends on: app/jobs/prospecting_jobs.py, app/scheduler.py
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.scheduler import scheduler

# ── Fixtures ─────────────────────────────────────────────────────────────────


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
    """Remove all scheduler jobs before/after each test to prevent leakage."""
    for job in scheduler.get_jobs():
        job.remove()
    yield
    for job in scheduler.get_jobs():
        job.remove()


# ── Job Registration Tests ────────────────────────────────────────────────────


def test_sweep_job_registered_when_enabled(scheduler_db):
    """account_sweep job is registered when account_sweep_enabled=True."""
    from app.config import Settings
    from app.jobs.prospecting_jobs import register_sweep_jobs

    s = Settings(account_sweep_enabled=True)
    register_sweep_jobs(scheduler, s)
    ids = [j.id for j in scheduler.get_jobs()]
    assert "account_sweep" in ids


def test_sweep_job_not_registered_when_disabled(scheduler_db):
    """account_sweep job is NOT registered when account_sweep_enabled=False."""
    from app.config import Settings
    from app.jobs.prospecting_jobs import register_sweep_jobs

    s = Settings(account_sweep_enabled=False)
    register_sweep_jobs(scheduler, s)
    ids = [j.id for j in scheduler.get_jobs()]
    assert "account_sweep" not in ids


def test_reactivation_job_registered(scheduler_db):
    """auto_surface_reactivation job is registered when
    account_reactivation_sweep_enabled=True."""
    from app.config import Settings
    from app.jobs.prospecting_jobs import register_sweep_jobs

    s = Settings(account_reactivation_sweep_enabled=True)
    register_sweep_jobs(scheduler, s)
    ids = [j.id for j in scheduler.get_jobs()]
    assert "auto_surface_reactivation" in ids


def test_reactivation_job_not_registered_when_disabled(scheduler_db):
    """auto_surface_reactivation job is NOT registered when disabled."""
    from app.config import Settings
    from app.jobs.prospecting_jobs import register_sweep_jobs

    s = Settings(account_reactivation_sweep_enabled=False)
    register_sweep_jobs(scheduler, s)
    ids = [j.id for j in scheduler.get_jobs()]
    assert "auto_surface_reactivation" not in ids


def test_sweep_job_delegates(scheduler_db):
    """_job_account_sweep delegates to job_account_sweep in prospect_reclamation."""
    import importlib

    mock_fn = AsyncMock()
    with patch("app.services.prospect_reclamation.job_account_sweep", mock_fn):
        job = getattr(importlib.import_module("app.jobs.prospecting_jobs"), "_job_account_sweep")
        asyncio.run(job())
    mock_fn.assert_awaited_once()


def test_reactivation_job_delegates(scheduler_db):
    """_job_auto_surface_reactivation delegates to job_auto_surface_reactivation."""
    import importlib

    mock_fn = AsyncMock()
    with patch("app.services.prospect_reclamation.job_auto_surface_reactivation", mock_fn):
        job = getattr(importlib.import_module("app.jobs.prospecting_jobs"), "_job_auto_surface_reactivation")
        asyncio.run(job())
    mock_fn.assert_awaited_once()


# ── prospecting_enabled gate tests ───────────────────────────────────────────


def test_no_sweep_jobs_when_prospecting_disabled(scheduler_db):
    """Neither sweep job registers when prospecting_enabled=False."""
    from app.config import Settings
    from app.jobs.prospecting_jobs import register_sweep_jobs

    s = Settings(prospecting_enabled=False, account_sweep_enabled=True, account_reactivation_sweep_enabled=True)
    register_sweep_jobs(scheduler, s)
    ids = [j.id for j in scheduler.get_jobs()]
    assert "account_sweep" not in ids
    assert "auto_surface_reactivation" not in ids


def test_both_sweep_jobs_register_when_both_flags_true(scheduler_db):
    """Both sweep jobs register when prospecting_enabled=True and per-feature flags are
    True."""
    from app.config import Settings
    from app.jobs.prospecting_jobs import register_sweep_jobs

    s = Settings(prospecting_enabled=True, account_sweep_enabled=True, account_reactivation_sweep_enabled=True)
    register_sweep_jobs(scheduler, s)
    ids = [j.id for j in scheduler.get_jobs()]
    assert "account_sweep" in ids
    assert "auto_surface_reactivation" in ids
