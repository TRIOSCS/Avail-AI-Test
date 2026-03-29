"""test_jobs_sourcing_refresh.py — Tests for sourcing refresh background jobs.

Covers: _job_refresh_stale_requisitions.

All jobs use SessionLocal() internally, so we patch app.database.SessionLocal
to return a mock or test DB session with close() disabled.
"""

import asyncio
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


# ── _job_refresh_stale_requisitions() ────────────────────────────────


def test_refresh_stale_happy_path():
    """Stale requirements found — search_requirement called for each."""
    mock_db = MagicMock()
    mock_req1 = MagicMock(id=1, primary_mpn="LM317T")
    mock_req2 = MagicMock(id=2, primary_mpn="NE555P")

    # Chain the query mock for the stale reqs lookup
    mock_query = MagicMock()
    mock_query.join.return_value.outerjoin.return_value.filter.return_value.group_by.return_value.having.return_value.limit.return_value.all.return_value = [
        mock_req1,
        mock_req2,
    ]
    mock_db.query.return_value = mock_query

    mock_search = AsyncMock(return_value={"sightings": [{"id": 1}]})

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch("app.search_service.search_requirement", mock_search),
    ):
        from app.jobs.sourcing_refresh_jobs import _job_refresh_stale_requisitions

        asyncio.run(_job_refresh_stale_requisitions())

    assert mock_search.call_count == 2
    mock_search.assert_any_call(mock_req1, mock_db)
    mock_search.assert_any_call(mock_req2, mock_db)
    mock_db.close.assert_called_once()


def test_refresh_stale_no_stale_reqs():
    """No stale requirements — returns early without calling search."""
    mock_db = MagicMock()
    mock_query = MagicMock()
    mock_query.join.return_value.outerjoin.return_value.filter.return_value.group_by.return_value.having.return_value.limit.return_value.all.return_value = []
    mock_db.query.return_value = mock_query

    mock_search = AsyncMock()

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch("app.search_service.search_requirement", mock_search),
    ):
        from app.jobs.sourcing_refresh_jobs import _job_refresh_stale_requisitions

        asyncio.run(_job_refresh_stale_requisitions())

    mock_search.assert_not_called()
    mock_db.close.assert_called_once()


def test_refresh_stale_one_search_fails_others_continue():
    """If one requirement's search fails, processing continues for others."""
    mock_db = MagicMock()
    mock_req1 = MagicMock(id=1, primary_mpn="LM317T")
    mock_req2 = MagicMock(id=2, primary_mpn="NE555P")
    mock_req3 = MagicMock(id=3, primary_mpn="LM7805")

    mock_query = MagicMock()
    mock_query.join.return_value.outerjoin.return_value.filter.return_value.group_by.return_value.having.return_value.limit.return_value.all.return_value = [
        mock_req1,
        mock_req2,
        mock_req3,
    ]
    mock_db.query.return_value = mock_query

    # First succeeds, second fails, third succeeds
    mock_search = AsyncMock(
        side_effect=[
            {"sightings": [{"id": 1}]},
            Exception("API rate limited"),
            {"sightings": [{"id": 3}]},
        ]
    )

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch("app.search_service.search_requirement", mock_search),
    ):
        from app.jobs.sourcing_refresh_jobs import _job_refresh_stale_requisitions

        asyncio.run(_job_refresh_stale_requisitions())

    # All three search calls attempted
    assert mock_search.call_count == 3
    mock_db.close.assert_called_once()


def test_refresh_stale_db_error_rollback():
    """DB error on the query itself triggers rollback."""
    mock_db = MagicMock()
    mock_db.query.side_effect = Exception("Connection lost")

    with patch("app.database.SessionLocal", return_value=mock_db):
        from app.jobs.sourcing_refresh_jobs import _job_refresh_stale_requisitions

        # The function catches the outer exception (logs + rollback), does not re-raise
        asyncio.run(_job_refresh_stale_requisitions())

    mock_db.rollback.assert_called_once()
    mock_db.close.assert_called_once()


def test_refresh_stale_search_returns_no_sightings():
    """Search that returns empty sightings does not count as refreshed."""
    mock_db = MagicMock()
    mock_req1 = MagicMock(id=1, primary_mpn="LM317T")

    mock_query = MagicMock()
    mock_query.join.return_value.outerjoin.return_value.filter.return_value.group_by.return_value.having.return_value.limit.return_value.all.return_value = [
        mock_req1,
    ]
    mock_db.query.return_value = mock_query

    mock_search = AsyncMock(return_value={"sightings": []})

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch("app.search_service.search_requirement", mock_search),
    ):
        from app.jobs.sourcing_refresh_jobs import _job_refresh_stale_requisitions

        asyncio.run(_job_refresh_stale_requisitions())

    mock_search.assert_called_once_with(mock_req1, mock_db)
    mock_db.close.assert_called_once()
