"""test_jobs_enrichment.py — Tests for enrichment background jobs

Covers: _job_engagement_scoring, _job_deep_enrichment, _job_customer_enrichment_sweep,
_job_monthly_enrichment_refresh.

All jobs use SessionLocal() internally, so we patch app.database.SessionLocal
to return the test DB session with close() disabled.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import VendorCard
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


# ── _job_engagement_scoring() ─────────────────────────────────────────


def test_engagement_scoring_runs_when_stale(scheduler_db):
    """Vendor scoring runs when no recent computation exists."""
    with patch("app.jobs.email_jobs._compute_vendor_scores_job", new_callable=AsyncMock) as mock_compute:
        from app.jobs.enrichment_jobs import _job_engagement_scoring

        asyncio.run(_job_engagement_scoring())
        mock_compute.assert_called_once()


def test_engagement_scoring_skips_when_recent(scheduler_db):
    """Vendor scoring skips when computed recently."""
    card = VendorCard(
        normalized_name="test vendor",
        display_name="Test Vendor",
        emails=[],
        phones=[],
        vendor_score_computed_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    scheduler_db.add(card)
    scheduler_db.commit()

    with patch("app.jobs.email_jobs._compute_vendor_scores_job", new_callable=AsyncMock) as mock_compute:
        from app.jobs.enrichment_jobs import _job_engagement_scoring

        asyncio.run(_job_engagement_scoring())
        mock_compute.assert_not_called()


def test_engagement_scoring_error_handling(scheduler_db):
    """Vendor scoring handles errors gracefully."""
    with patch(
        "app.jobs.email_jobs._compute_vendor_scores_job",
        new_callable=AsyncMock,
        side_effect=Exception("DB error"),
    ):
        from app.jobs.enrichment_jobs import _job_engagement_scoring

        # Should not raise
        asyncio.run(_job_engagement_scoring())


def test_engagement_scoring_runs_when_old_computation(scheduler_db):
    """Vendor scoring runs when the last computation is >12h old."""
    card = VendorCard(
        normalized_name="old score vendor",
        display_name="Old Score Vendor",
        emails=[],
        phones=[],
        vendor_score_computed_at=datetime.now(timezone.utc) - timedelta(hours=20),
    )
    scheduler_db.add(card)
    scheduler_db.commit()

    with patch("app.jobs.email_jobs._compute_vendor_scores_job", new_callable=AsyncMock) as mock_compute:
        from app.jobs.enrichment_jobs import _job_engagement_scoring

        asyncio.run(_job_engagement_scoring())
        mock_compute.assert_called_once()


def test_engagement_scoring_naive_datetime(scheduler_db):
    """Vendor with naive vendor_score_computed_at gets UTC-ified."""
    card = VendorCard(
        normalized_name="naive dt vendor",
        display_name="Naive DT Vendor",
        emails=[],
        phones=[],
        # Naive datetime (no tzinfo) — recent enough to skip
        vendor_score_computed_at=datetime.now() - timedelta(hours=2),
    )
    scheduler_db.add(card)
    scheduler_db.commit()

    with patch("app.jobs.email_jobs._compute_vendor_scores_job", new_callable=AsyncMock) as mock_compute:
        from app.jobs.enrichment_jobs import _job_engagement_scoring

        asyncio.run(_job_engagement_scoring())
        # Naive datetime should be made UTC-aware; 2h old = skip
        mock_compute.assert_not_called()


# ── _job_deep_enrichment() ────────────────────────────────────────────


def test_deep_enrichment_enriches_stale_vendors(scheduler_db):
    """Stale vendor cards (no deep_enrichment_at) are enriched."""
    card = VendorCard(
        normalized_name="stale vendor",
        display_name="Stale Vendor",
        emails=[],
        phones=[],
        deep_enrichment_at=None,
        sighting_count=10,
        created_at=datetime.now(timezone.utc) - timedelta(days=30),
    )
    scheduler_db.add(card)
    scheduler_db.commit()

    with (
        patch("app.config.settings") as mock_settings,
        patch(
            "app.services.deep_enrichment_service.deep_enrich_vendor",
            new_callable=AsyncMock,
        ) as mock_enrich_v,
        patch(
            "app.services.deep_enrichment_service.deep_enrich_company",
            new_callable=AsyncMock,
        ),
    ):
        mock_settings.deep_enrichment_stale_days = 30
        from app.jobs.enrichment_jobs import _job_deep_enrichment

        asyncio.run(_job_deep_enrichment())
        mock_enrich_v.assert_called_once_with(card.id, scheduler_db)


def test_deep_enrichment_enriches_stale_companies(scheduler_db, test_company):
    """Stale companies (no deep_enrichment_at) are enriched."""
    test_company.deep_enrichment_at = None
    scheduler_db.commit()

    with (
        patch("app.config.settings") as mock_settings,
        patch(
            "app.services.deep_enrichment_service.deep_enrich_vendor",
            new_callable=AsyncMock,
        ),
        patch(
            "app.services.deep_enrichment_service.deep_enrich_company",
            new_callable=AsyncMock,
        ) as mock_enrich_c,
    ):
        mock_settings.deep_enrichment_stale_days = 30
        from app.jobs.enrichment_jobs import _job_deep_enrichment

        asyncio.run(_job_deep_enrichment())
        mock_enrich_c.assert_called_once_with(test_company.id, scheduler_db)


def test_deep_enrichment_skips_recently_enriched(scheduler_db):
    """Recently enriched vendors are not re-enriched."""
    card = VendorCard(
        normalized_name="fresh vendor",
        display_name="Fresh Vendor",
        emails=[],
        phones=[],
        deep_enrichment_at=datetime.now(timezone.utc) - timedelta(days=1),
        sighting_count=10,
    )
    scheduler_db.add(card)
    scheduler_db.commit()

    with (
        patch("app.config.settings") as mock_settings,
        patch(
            "app.services.deep_enrichment_service.deep_enrich_vendor",
            new_callable=AsyncMock,
        ) as mock_enrich_v,
        patch(
            "app.services.deep_enrichment_service.deep_enrich_company",
            new_callable=AsyncMock,
        ),
    ):
        mock_settings.deep_enrichment_stale_days = 30
        from app.jobs.enrichment_jobs import _job_deep_enrichment

        asyncio.run(_job_deep_enrichment())
        mock_enrich_v.assert_not_called()


def test_deep_enrichment_enriches_new_vendors(scheduler_db):
    """Recently created vendors without enrichment are enriched."""
    card = VendorCard(
        normalized_name="new vendor",
        display_name="New Vendor",
        emails=[],
        phones=[],
        deep_enrichment_at=None,
        sighting_count=1,
        created_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    scheduler_db.add(card)
    scheduler_db.commit()

    with (
        patch("app.config.settings") as mock_settings,
        patch(
            "app.services.deep_enrichment_service.deep_enrich_vendor",
            new_callable=AsyncMock,
        ) as mock_enrich_v,
        patch(
            "app.services.deep_enrichment_service.deep_enrich_company",
            new_callable=AsyncMock,
        ),
    ):
        mock_settings.deep_enrichment_stale_days = 30
        from app.jobs.enrichment_jobs import _job_deep_enrichment

        asyncio.run(_job_deep_enrichment())
        assert mock_enrich_v.call_count >= 1


def test_deep_enrichment_error_handling(scheduler_db):
    """Deep enrichment handles errors gracefully."""
    card = VendorCard(
        normalized_name="error vendor",
        display_name="Error Vendor",
        emails=[],
        phones=[],
        deep_enrichment_at=None,
        sighting_count=5,
    )
    scheduler_db.add(card)
    scheduler_db.commit()

    with (
        patch("app.config.settings") as mock_settings,
        patch(
            "app.services.deep_enrichment_service.deep_enrich_vendor",
            new_callable=AsyncMock,
            side_effect=Exception("Enrichment API down"),
        ),
        patch(
            "app.services.deep_enrichment_service.deep_enrich_company",
            new_callable=AsyncMock,
        ),
    ):
        mock_settings.deep_enrichment_stale_days = 30
        from app.jobs.enrichment_jobs import _job_deep_enrichment

        asyncio.run(_job_deep_enrichment())


def test_deep_enrichment_per_vendor_error_with_savepoint(scheduler_db):
    """Per-vendor enrichment errors rollback to savepoint."""
    card = VendorCard(
        normalized_name="savepoint vendor",
        display_name="Savepoint Vendor",
        emails=[],
        phones=[],
        deep_enrichment_at=None,
        sighting_count=10,
        created_at=datetime.now(timezone.utc) - timedelta(days=30),
    )
    scheduler_db.add(card)
    scheduler_db.commit()

    with (
        patch("app.config.settings") as mock_settings,
        patch(
            "app.services.deep_enrichment_service.deep_enrich_vendor",
            new_callable=AsyncMock,
            side_effect=Exception("enrich failed"),
        ),
        patch(
            "app.services.deep_enrichment_service.deep_enrich_company",
            new_callable=AsyncMock,
        ),
    ):
        mock_settings.deep_enrichment_stale_days = 30
        from app.jobs.enrichment_jobs import _job_deep_enrichment

        asyncio.run(_job_deep_enrichment())


def test_deep_enrichment_per_company_error_with_savepoint(scheduler_db, test_company):
    """Per-company enrichment errors rollback to savepoint."""
    test_company.deep_enrichment_at = None
    scheduler_db.commit()

    with (
        patch("app.config.settings") as mock_settings,
        patch(
            "app.services.deep_enrichment_service.deep_enrich_vendor",
            new_callable=AsyncMock,
        ),
        patch(
            "app.services.deep_enrichment_service.deep_enrich_company",
            new_callable=AsyncMock,
            side_effect=Exception("company enrich failed"),
        ),
    ):
        mock_settings.deep_enrichment_stale_days = 30
        from app.jobs.enrichment_jobs import _job_deep_enrichment

        asyncio.run(_job_deep_enrichment())


def test_deep_enrichment_outer_exception(scheduler_db):
    """Outer exception in deep enrichment is caught."""
    with (
        patch.object(scheduler_db, "query", side_effect=Exception("DB crash")),
        patch("app.config.settings") as mock_settings,
    ):
        mock_settings.deep_enrichment_stale_days = 30
        from app.jobs.enrichment_jobs import _job_deep_enrichment

        asyncio.run(_job_deep_enrichment())


# ── _job_customer_enrichment_sweep() ──────────────────────────────────


def test_customer_enrichment_sweep_success(scheduler_db):
    """_job_customer_enrichment_sweep happy path."""
    mock_batch = AsyncMock(return_value={"processed": 10, "enriched": 5})
    with patch("app.services.customer_enrichment_batch.run_customer_enrichment_batch", mock_batch):
        from app.jobs.enrichment_jobs import _job_customer_enrichment_sweep

        asyncio.run(_job_customer_enrichment_sweep())
    mock_batch.assert_called_once()


def test_customer_enrichment_sweep_error(scheduler_db):
    """Exception rolls back."""
    mock_batch = AsyncMock(side_effect=Exception("Enrichment failed"))
    with patch("app.services.customer_enrichment_batch.run_customer_enrichment_batch", mock_batch):
        from app.jobs.enrichment_jobs import _job_customer_enrichment_sweep

        asyncio.run(_job_customer_enrichment_sweep())


# ── _job_monthly_enrichment_refresh() ─────────────────────────────────


def test_monthly_enrichment_refresh_success(scheduler_db):
    """_job_monthly_enrichment_refresh happy path."""
    mock_backfill = AsyncMock(return_value=42)
    mock_flush = MagicMock(return_value=10)

    with (
        patch("app.services.deep_enrichment_service.run_backfill_job", mock_backfill),
        patch("app.cache.intel_cache.flush_enrichment_cache", mock_flush),
    ):
        from app.jobs.enrichment_jobs import _job_monthly_enrichment_refresh

        asyncio.run(_job_monthly_enrichment_refresh())
    mock_backfill.assert_called_once()


def test_monthly_enrichment_refresh_already_running(scheduler_db):
    """Skip when a job is already running."""
    from app.models import EnrichmentJob

    running_job = EnrichmentJob(
        job_type="backfill",
        status="running",
    )
    scheduler_db.add(running_job)
    scheduler_db.commit()

    from app.jobs.enrichment_jobs import _job_monthly_enrichment_refresh

    asyncio.run(_job_monthly_enrichment_refresh())


def test_monthly_enrichment_refresh_error(scheduler_db):
    """Exception is caught."""
    mock_flush = MagicMock(side_effect=Exception("Cache flush error"))
    with patch("app.cache.intel_cache.flush_enrichment_cache", mock_flush):
        from app.jobs.enrichment_jobs import _job_monthly_enrichment_refresh

        asyncio.run(_job_monthly_enrichment_refresh())
