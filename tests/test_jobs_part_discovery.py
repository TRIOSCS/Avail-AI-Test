"""test_jobs_part_discovery.py — Tests for part discovery background jobs.

Covers: _job_cross_ref_expansion, _job_family_expansion, _job_commodity_gap_fill,
register_discovery_jobs.

All jobs use SessionLocal() internally, so we patch app.database.SessionLocal
to return a mock session.
"""

import asyncio
from importlib import reload
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


# ── register_discovery_jobs() ────────────────────────────────────────


def test_register_discovery_jobs_disabled():
    """When part_discovery_enabled=False, no jobs are added."""
    mock_settings = MagicMock()
    mock_settings.part_discovery_enabled = False

    mock_scheduler = MagicMock()

    with patch("app.config.settings", mock_settings):
        from app.jobs.part_discovery_jobs import register_discovery_jobs

        register_discovery_jobs(mock_scheduler)

    mock_scheduler.add_job.assert_not_called()


def test_register_discovery_jobs_enabled():
    """When part_discovery_enabled=True, three jobs are registered."""
    mock_settings = MagicMock()
    mock_settings.part_discovery_enabled = True

    mock_scheduler = MagicMock()

    with patch("app.config.settings", mock_settings):
        from app.jobs.part_discovery_jobs import register_discovery_jobs

        register_discovery_jobs(mock_scheduler)

    assert mock_scheduler.add_job.call_count == 3
    job_ids = [call.kwargs.get("id") or call[1].get("id", "") for call in mock_scheduler.add_job.call_args_list]
    assert "part_discovery_crossref" in job_ids
    assert "part_discovery_family" in job_ids
    assert "part_discovery_commodity" in job_ids


# ── _job_cross_ref_expansion() ───────────────────────────────────────


def test_cross_ref_expansion_happy_path():
    """_job_cross_ref_expansion calls expand_cross_references and closes DB."""
    mock_db = MagicMock()
    mock_expand = AsyncMock(return_value={"created": 10, "skipped": 5})

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch(
            "app.services.part_discovery_service.expand_cross_references",
            mock_expand,
        ),
    ):
        import app.jobs.part_discovery_jobs as mod

        reload(mod)
        asyncio.run(mod._job_cross_ref_expansion())

    mock_expand.assert_awaited_once_with(mock_db, limit=500)
    mock_db.close.assert_called_once()


def test_cross_ref_expansion_error_propagates():
    """_job_cross_ref_expansion propagates exceptions and still closes DB."""
    mock_db = MagicMock()
    mock_expand = AsyncMock(side_effect=Exception("API timeout"))

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch(
            "app.services.part_discovery_service.expand_cross_references",
            mock_expand,
        ),
    ):
        import app.jobs.part_discovery_jobs as mod

        reload(mod)
        with pytest.raises(Exception, match="API timeout"):
            asyncio.run(mod._job_cross_ref_expansion())

    mock_db.close.assert_called_once()


# ── _job_family_expansion() ──────────────────────────────────────────


def test_family_expansion_happy_path():
    """_job_family_expansion calls expand_families and closes DB."""
    mock_db = MagicMock()
    mock_expand = AsyncMock(return_value={"families_expanded": 3, "cards_created": 15})

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch(
            "app.services.part_discovery_service.expand_families",
            mock_expand,
        ),
    ):
        import app.jobs.part_discovery_jobs as mod

        reload(mod)
        asyncio.run(mod._job_family_expansion())

    mock_expand.assert_awaited_once_with(mock_db, batch_size=100)
    mock_db.close.assert_called_once()


def test_family_expansion_error_propagates():
    """_job_family_expansion propagates exceptions and still closes DB."""
    mock_db = MagicMock()
    mock_expand = AsyncMock(side_effect=Exception("Service unavailable"))

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch(
            "app.services.part_discovery_service.expand_families",
            mock_expand,
        ),
    ):
        import app.jobs.part_discovery_jobs as mod

        reload(mod)
        with pytest.raises(Exception, match="Service unavailable"):
            asyncio.run(mod._job_family_expansion())

    mock_db.close.assert_called_once()


# ── _job_commodity_gap_fill() ────────────────────────────────────────


def test_commodity_gap_fill_happy_path():
    """_job_commodity_gap_fill calls fill_commodity_gaps and closes DB."""
    mock_db = MagicMock()
    mock_fill = AsyncMock(return_value={"gaps_filled": 8, "cards_created": 25})

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch(
            "app.services.part_discovery_service.fill_commodity_gaps",
            mock_fill,
        ),
    ):
        import app.jobs.part_discovery_jobs as mod

        reload(mod)
        asyncio.run(mod._job_commodity_gap_fill())

    mock_fill.assert_awaited_once_with(mock_db)
    mock_db.close.assert_called_once()


def test_commodity_gap_fill_error_propagates():
    """_job_commodity_gap_fill propagates exceptions and still closes DB."""
    mock_db = MagicMock()
    mock_fill = AsyncMock(side_effect=Exception("Commodity lookup failed"))

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch(
            "app.services.part_discovery_service.fill_commodity_gaps",
            mock_fill,
        ),
    ):
        import app.jobs.part_discovery_jobs as mod

        reload(mod)
        with pytest.raises(Exception, match="Commodity lookup failed"):
            asyncio.run(mod._job_commodity_gap_fill())

    mock_db.close.assert_called_once()
