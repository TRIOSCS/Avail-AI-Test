"""test_jobs_tagging.py — Tests for tagging/spec-enrichment background jobs.

Covers: _job_spec_enrichment and register_tagging_jobs registration.

SP1 (2026-06-09): the automated Haiku card-enrichment job (_job_material_enrichment)
was removed; the spec backlog sweep (_job_spec_enrichment) replaced it and runs the
status-gated enrich_pending_specs only — never the card-level Haiku path.

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


# ── register_tagging_jobs() ────────────────────────────────────────────


def test_registers_spec_enrichment_not_material_enrichment():
    """The Haiku 'material_enrichment' job is gone; 'spec_enrichment' is registered."""
    from app.jobs.tagging_jobs import register_tagging_jobs

    mock_scheduler = MagicMock()
    register_tagging_jobs(mock_scheduler, MagicMock())

    job_ids = [c.kwargs["id"] for c in mock_scheduler.add_job.call_args_list]
    assert "spec_enrichment" in job_ids
    assert "material_enrichment" not in job_ids


def test_job_material_enrichment_removed():
    """The removed Haiku card-enrichment job must no longer exist on the module."""
    import app.jobs.tagging_jobs as tagging_jobs

    assert not hasattr(tagging_jobs, "_job_material_enrichment")


# ── _job_spec_enrichment() ─────────────────────────────────────────────


def test_spec_enrichment_success(scheduler_db):
    """_job_spec_enrichment runs the status-gated spec sweep (no card-level Haiku
    path)."""
    mock_specs = AsyncMock(return_value={"cards_processed": 3, "specs_written": 7, "errors": 0, "skipped_no_schema": 1})
    with patch("app.services.spec_enrichment_service.enrich_pending_specs", mock_specs):
        from app.jobs.tagging_jobs import _job_spec_enrichment

        asyncio.run(_job_spec_enrichment())
    mock_specs.assert_called_once()


def test_spec_enrichment_does_not_call_card_enrichment(scheduler_db):
    """The spec sweep must NOT invoke the removed Haiku card-enrichment path."""
    mock_specs = AsyncMock(return_value={"cards_processed": 0, "specs_written": 0, "errors": 0, "skipped_no_schema": 0})
    with (
        patch("app.services.spec_enrichment_service.enrich_pending_specs", mock_specs),
        patch("app.services.material_enrichment_service.enrich_pending_cards", new_callable=AsyncMock) as mcards,
    ):
        from app.jobs.tagging_jobs import _job_spec_enrichment

        asyncio.run(_job_spec_enrichment())
    mcards.assert_not_called()


def test_spec_enrichment_error(scheduler_db):
    """Exception rolls back and re-raises so _traced_job can capture it."""
    mock_specs = AsyncMock(side_effect=Exception("Spec sweep failed"))
    with patch("app.services.spec_enrichment_service.enrich_pending_specs", mock_specs):
        from app.jobs.tagging_jobs import _job_spec_enrichment

        with pytest.raises(Exception, match="Spec sweep failed"):
            asyncio.run(_job_spec_enrichment())
