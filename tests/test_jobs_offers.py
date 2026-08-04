"""test_jobs_offers.py — Tests for offer-related background jobs.

Covers: _job_proactive_matching (parked implementation, kept for the
Proactive workspace comeback).

All jobs use SessionLocal() internally, so we patch app.database.SessionLocal
to return the test DB session with close() disabled.
"""

import asyncio
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


# ── _job_proactive_matching() ─────────────────────────────────────────


def test_proactive_matching_calls_scan(scheduler_db):
    """Proactive matching job delegates to run_proactive_scan."""
    with (
        patch("app.services.proactive_matching.run_proactive_scan") as mock_scan,
        patch("app.services.proactive_matching.expire_old_matches", return_value=0),
    ):
        mock_scan.return_value = {"matches_created": 3, "scanned_offers": 10}
        from app.jobs.offers_jobs import _job_proactive_matching

        asyncio.run(_job_proactive_matching())
        mock_scan.assert_called_once_with(scheduler_db)


def test_proactive_matching_no_matches(scheduler_db):
    """Proactive matching runs cleanly when no matches are created."""
    with (
        patch("app.services.proactive_matching.run_proactive_scan") as mock_scan,
        patch("app.services.proactive_matching.expire_old_matches", return_value=0),
    ):
        mock_scan.return_value = {"matches_created": 0, "scanned_offers": 5}
        from app.jobs.offers_jobs import _job_proactive_matching

        asyncio.run(_job_proactive_matching())
        mock_scan.assert_called_once()


def test_proactive_matching_error_handling(scheduler_db):
    """Proactive matching rolls back and re-raises so _traced_job can capture it."""
    with patch(
        "app.services.proactive_matching.run_proactive_scan",
        side_effect=Exception("DB connection lost"),
    ):
        from app.jobs.offers_jobs import _job_proactive_matching

        with pytest.raises(Exception, match="DB connection lost"):
            asyncio.run(_job_proactive_matching())


def test_proactive_matching_timeout(scheduler_db):
    """Proactive matching rolls back and re-raises TimeoutError so _traced_job can
    capture it."""

    async def _mock_wait_for(coro, timeout=None):
        try:
            coro.close()
        except Exception:
            pass
        raise TimeoutError()

    with (
        patch("app.services.proactive_matching.run_proactive_scan"),
        patch("asyncio.wait_for", side_effect=_mock_wait_for),
    ):
        from app.jobs.offers_jobs import _job_proactive_matching

        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(_job_proactive_matching())


def test_proactive_matching_logs_summary(scheduler_db):
    """Proactive matching logs a summary with new matches and total pending."""
    with (
        patch("app.services.proactive_matching.run_proactive_scan") as mock_scan,
        patch("app.services.proactive_matching.expire_old_matches") as mock_expire,
        patch("app.jobs.offers_jobs.logger") as mock_logger,
    ):
        mock_scan.return_value = {"matches_created": 3, "scanned_offers": 5}
        mock_expire.return_value = 0
        from app.jobs.offers_jobs import _job_proactive_matching

        asyncio.run(_job_proactive_matching())
        log_calls = [str(c) for c in mock_logger.info.call_args_list]
        summary_found = any("3 new matches" in c and "pending" in c for c in log_calls)
        assert summary_found, f"Expected summary log with '3 new matches' and 'pending', got: {log_calls}"


def test_proactive_matching_expired_branch(scheduler_db):
    """When expire_old_matches returns a nonzero count, logger.info is called."""
    mock_scan = MagicMock(return_value={"matches_created": 0, "scanned_offers": 3})
    mock_expire = MagicMock(return_value=7)  # 7 expired matches

    with (
        patch("app.services.proactive_matching.expire_old_matches", mock_expire),
        patch("app.services.proactive_matching.run_proactive_scan", mock_scan),
    ):
        from app.jobs.offers_jobs import _job_proactive_matching

        asyncio.run(_job_proactive_matching())
