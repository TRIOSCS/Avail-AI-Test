"""test_jobs_tagging.py — Tests for tagging/material enrichment background jobs.

Covers: _job_material_enrichment.

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
