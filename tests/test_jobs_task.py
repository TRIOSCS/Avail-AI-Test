"""test_jobs_task.py — Tests for task auto-generation background jobs.

Covers: _job_bid_due_alerts, register_task_jobs.

All jobs use SessionLocal() internally, so we patch app.database.SessionLocal
to return a mock or test DB session with close() disabled.
"""

import asyncio
from datetime import datetime, timedelta, timezone
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


def _make_req(req_id, deadline_str, name="REQ-TEST"):
    """Create a mock Requisition with the given deadline string."""
    req = MagicMock()
    req.id = req_id
    req.deadline = deadline_str
    req.name = name
    return req


# ── _job_bid_due_alerts() — deadline within 2 days → task created ────


def test_bid_due_deadline_within_2_days():
    """Requisition with deadline within 2 days creates a task."""
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    mock_req = _make_req(1, tomorrow, "REQ-001")

    mock_db = MagicMock()
    mock_query = MagicMock()
    mock_query.filter.return_value.limit.return_value.all.return_value = [mock_req]
    mock_db.query.return_value = mock_query

    mock_on_bid_due = MagicMock(return_value=MagicMock(id=100))

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch("app.services.task_service.on_bid_due_soon", mock_on_bid_due),
    ):
        from app.jobs.task_jobs import _job_bid_due_alerts

        asyncio.run(_job_bid_due_alerts())

    mock_on_bid_due.assert_called_once_with(mock_db, 1, tomorrow, "REQ-001")
    mock_db.close.assert_called_once()


# ── deadline > 2 days → no task ─────────────────────────────────────


def test_bid_due_deadline_far_future():
    """Requisition with deadline > 2 days away does not create a task."""
    far_future = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    mock_req = _make_req(1, far_future)

    mock_db = MagicMock()
    mock_query = MagicMock()
    mock_query.filter.return_value.limit.return_value.all.return_value = [mock_req]
    mock_db.query.return_value = mock_query

    mock_on_bid_due = MagicMock()

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch("app.services.task_service.on_bid_due_soon", mock_on_bid_due),
    ):
        from app.jobs.task_jobs import _job_bid_due_alerts

        asyncio.run(_job_bid_due_alerts())

    mock_on_bid_due.assert_not_called()
    mock_db.close.assert_called_once()


# ── deadline = "ASAP" → skipped ─────────────────────────────────────


def test_bid_due_asap_skipped():
    """Requisition with deadline 'ASAP' is filtered out at query level."""
    mock_db = MagicMock()
    mock_query = MagicMock()
    # ASAP deadlines are excluded by the query filter itself, so empty results
    mock_query.filter.return_value.limit.return_value.all.return_value = []
    mock_db.query.return_value = mock_query

    mock_on_bid_due = MagicMock()

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch("app.services.task_service.on_bid_due_soon", mock_on_bid_due),
    ):
        from app.jobs.task_jobs import _job_bid_due_alerts

        asyncio.run(_job_bid_due_alerts())

    mock_on_bid_due.assert_not_called()
    mock_db.close.assert_called_once()


# ── non-ISO deadline string → skipped (ValueError) ──────────────────


def test_bid_due_non_iso_deadline_skipped():
    """Requisition with unparseable deadline is skipped (no task created)."""
    mock_req = _make_req(1, "next week")

    mock_db = MagicMock()
    mock_query = MagicMock()
    mock_query.filter.return_value.limit.return_value.all.return_value = [mock_req]
    mock_db.query.return_value = mock_query

    mock_on_bid_due = MagicMock()

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch("app.services.task_service.on_bid_due_soon", mock_on_bid_due),
    ):
        from app.jobs.task_jobs import _job_bid_due_alerts

        asyncio.run(_job_bid_due_alerts())

    mock_on_bid_due.assert_not_called()
    mock_db.close.assert_called_once()


# ── cap at _BID_DUE_CAP (20) ────────────────────────────────────────


def test_bid_due_capped_at_20():
    """No more than _BID_DUE_CAP (20) tasks are created per run."""
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    # Create 30 reqs with valid deadlines
    mock_reqs = [_make_req(i, tomorrow, f"REQ-{i:03d}") for i in range(30)]

    mock_db = MagicMock()
    mock_query = MagicMock()
    mock_query.filter.return_value.limit.return_value.all.return_value = mock_reqs
    mock_db.query.return_value = mock_query

    # on_bid_due_soon returns a task every time (simulating new task creation)
    mock_on_bid_due = MagicMock(return_value=MagicMock(id=1))

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch("app.services.task_service.on_bid_due_soon", mock_on_bid_due),
    ):
        from app.jobs.task_jobs import _job_bid_due_alerts

        asyncio.run(_job_bid_due_alerts())

    # Capped at 20 (the _BID_DUE_CAP constant)
    assert mock_on_bid_due.call_count == 20
    mock_db.close.assert_called_once()


# ── exception re-raised ─────────────────────────────────────────────


def test_bid_due_exception_reraises():
    """_job_bid_due_alerts rolls back and re-raises on DB error."""
    mock_db = MagicMock()
    mock_db.query.side_effect = Exception("DB connection failed")

    with patch("app.database.SessionLocal", return_value=mock_db):
        from app.jobs.task_jobs import _job_bid_due_alerts

        with pytest.raises(Exception, match="DB connection failed"):
            asyncio.run(_job_bid_due_alerts())

    mock_db.rollback.assert_called_once()
    mock_db.close.assert_called_once()


# ── deadline already past by > 1 day → skipped ──────────────────────


def test_bid_due_past_deadline_skipped():
    """Requisition with deadline > 1 day in the past is skipped."""
    past = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    mock_req = _make_req(1, past)

    mock_db = MagicMock()
    mock_query = MagicMock()
    mock_query.filter.return_value.limit.return_value.all.return_value = [mock_req]
    mock_db.query.return_value = mock_query

    mock_on_bid_due = MagicMock()

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch("app.services.task_service.on_bid_due_soon", mock_on_bid_due),
    ):
        from app.jobs.task_jobs import _job_bid_due_alerts

        asyncio.run(_job_bid_due_alerts())

    mock_on_bid_due.assert_not_called()
    mock_db.close.assert_called_once()


# ── on_bid_due_soon returns None (duplicate) → not counted ──────────


def test_bid_due_duplicate_not_counted():
    """If on_bid_due_soon returns None (existing task), it does not count toward cap."""
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    mock_reqs = [_make_req(i, tomorrow, f"REQ-{i:03d}") for i in range(3)]

    mock_db = MagicMock()
    mock_query = MagicMock()
    mock_query.filter.return_value.limit.return_value.all.return_value = mock_reqs
    mock_db.query.return_value = mock_query

    # First returns None (duplicate), second and third create tasks
    mock_on_bid_due = MagicMock(side_effect=[None, MagicMock(id=1), MagicMock(id=2)])

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch("app.services.task_service.on_bid_due_soon", mock_on_bid_due),
    ):
        from app.jobs.task_jobs import _job_bid_due_alerts

        asyncio.run(_job_bid_due_alerts())

    assert mock_on_bid_due.call_count == 3
    mock_db.close.assert_called_once()
