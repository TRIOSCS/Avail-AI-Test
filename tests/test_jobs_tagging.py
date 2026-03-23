"""test_jobs_tagging.py — Tests for tagging/material enrichment background jobs.

Covers: _job_material_enrichment, _job_nexar_backfill, _job_connector_enrichment.

All jobs use SessionLocal() internally, so we patch app.database.SessionLocal
to return the test DB session with close() disabled.
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


# ── _job_material_enrichment() ────────────────────────────────────────


def test_material_enrichment_success(scheduler_db):
    """_job_material_enrichment enriches pending cards."""
    mock_enrich = AsyncMock(return_value={"enriched": 5, "errors": 1, "pending": 10})
    with patch("app.services.material_enrichment_service.enrich_pending_cards", mock_enrich):
        from app.jobs.tagging_jobs import _job_material_enrichment

        asyncio.run(_job_material_enrichment())
    mock_enrich.assert_called_once()


def test_material_enrichment_error(scheduler_db):
    """Exception rolls back and re-raises so _traced_job can capture it."""
    mock_enrich = AsyncMock(side_effect=Exception("Enrichment failed"))
    with patch("app.services.material_enrichment_service.enrich_pending_cards", mock_enrich):
        from app.jobs.tagging_jobs import _job_material_enrichment

        with pytest.raises(Exception, match="Enrichment failed"):
            asyncio.run(_job_material_enrichment())


# ── _job_nexar_backfill() ─────────────────────────────────────────────


def test_nexar_backfill_job_runs():
    """_job_nexar_backfill calls nexar_backfill_untagged and logs result."""
    mock_db = MagicMock()

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch(
            "app.services.enrichment.nexar_backfill_untagged",
            new_callable=AsyncMock,
            return_value={"total_checked": 10, "tagged": 3, "no_result": 7},
        ) as mock_backfill,
    ):
        from app.jobs.tagging_jobs import _job_nexar_backfill

        asyncio.run(_job_nexar_backfill())

    mock_backfill.assert_called_once_with(mock_db, limit=5000)
    mock_db.close.assert_called_once()


def test_nexar_backfill_job_handles_error():
    """_job_nexar_backfill rolls back on error and re-raises."""
    mock_db = MagicMock()

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch(
            "app.services.enrichment.nexar_backfill_untagged",
            new_callable=AsyncMock,
            side_effect=Exception("Nexar API error"),
        ),
    ):
        from app.jobs.tagging_jobs import _job_nexar_backfill

        with pytest.raises(Exception, match="Nexar API error"):
            asyncio.run(_job_nexar_backfill())

    mock_db.rollback.assert_called_once()
    mock_db.close.assert_called_once()


# ── _job_connector_enrichment() ───────────────────────────────────────


def test_connector_enrichment_boost_cascade():
    """_job_connector_enrichment runs boost cascade after enrichment."""
    mock_db = MagicMock()
    mock_db.query.return_value.join.return_value.join.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []

    with (
        patch("app.database.SessionLocal", return_value=mock_db),
        patch(
            "app.services.enrichment.cross_validate_batch",
            new_callable=AsyncMock,
            return_value={"total": 0},
        ),
        patch(
            "app.services.enrichment.boost_confidence_internal",
            return_value={"total_boosted": 5},
        ) as mock_boost,
        patch(
            "app.services.tagging_backfill.backfill_manufacturer_from_sightings",
            return_value={"total_tagged": 3},
        ) as mock_sighting,
    ):
        from app.jobs.tagging_jobs import _job_connector_enrichment

        asyncio.run(_job_connector_enrichment())

    mock_boost.assert_called_once_with(mock_db)
    mock_sighting.assert_called_once_with(mock_db)
    mock_db.close.assert_called_once()
