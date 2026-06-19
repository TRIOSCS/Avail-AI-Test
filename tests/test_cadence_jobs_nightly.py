"""test_cadence_jobs_nightly.py — Additional coverage for app/jobs/cadence_jobs.py.

Covers the _job_materialize_cadence async function body (success path,
rollback-on-exception) and register_cadence_jobs scheduler registration.

Called by: pytest autodiscovery
Depends on: app.jobs.cadence_jobs, app.scheduler._traced_job
"""

import os

os.environ["TESTING"] = "1"

import asyncio
from unittest.mock import MagicMock, patch

import pytest

# ── register_cadence_jobs ─────────────────────────────────────────────


def test_register_cadence_jobs_adds_job():
    """register_cadence_jobs must call scheduler.add_job exactly once."""
    from app.jobs.cadence_jobs import register_cadence_jobs

    mock_scheduler = MagicMock()
    register_cadence_jobs(mock_scheduler, settings=None)
    assert mock_scheduler.add_job.call_count == 1
    call_kwargs = mock_scheduler.add_job.call_args
    # job id should be cadence_materialize
    assert call_kwargs[1]["id"] == "cadence_materialize"


# ── _job_materialize_cadence ──────────────────────────────────────────


def test_job_materialize_cadence_success():
    """Success path: materialize_all_clocks called, result committed, db closed."""
    mock_db = MagicMock()
    mock_db.commit = MagicMock()
    mock_db.rollback = MagicMock()
    mock_db.close = MagicMock()

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch("app.services.cadence_service.materialize_all_clocks", return_value=7) as mock_mat,
    ):
        from app.jobs.cadence_jobs import _job_materialize_cadence

        asyncio.run(_job_materialize_cadence())

    mock_mat.assert_called_once_with(mock_db)
    mock_db.commit.assert_called_once()
    mock_db.rollback.assert_not_called()
    mock_db.close.assert_called_once()


def test_job_materialize_cadence_exception_triggers_rollback():
    """On exception, rollback is called and exception re-raises."""
    mock_db = MagicMock()
    mock_db.commit = MagicMock()
    mock_db.rollback = MagicMock()
    mock_db.close = MagicMock()

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch(
            "app.services.cadence_service.materialize_all_clocks",
            side_effect=RuntimeError("DB exploded"),
        ),
    ):
        from app.jobs.cadence_jobs import _job_materialize_cadence

        with pytest.raises(RuntimeError, match="DB exploded"):
            asyncio.run(_job_materialize_cadence())

    mock_db.rollback.assert_called_once()
    mock_db.commit.assert_not_called()
    mock_db.close.assert_called_once()


def test_job_materialize_cadence_zero_companies():
    """materialize_all_clocks returning 0 is still committed (no error)."""
    mock_db = MagicMock()

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch("app.services.cadence_service.materialize_all_clocks", return_value=0),
    ):
        from app.jobs.cadence_jobs import _job_materialize_cadence

        asyncio.run(_job_materialize_cadence())

    mock_db.commit.assert_called_once()
    mock_db.close.assert_called_once()
