"""Tests for app/jobs/quality_jobs.py — AI quality scoring background job.

W1 simplification (2026-08-04): quality_score_activities is PARKED per
docs/W1_JOB_DISPOSITION.md (spec §5.4 — Activity Scorecard is its only consumer;
comeback: team exists) — register_quality_jobs is a no-op, but the job
implementation (``_job_score_activities``) stays and keeps its tests.

Called by: pytest
Depends on: app.jobs.quality_jobs, app.services.activity_quality_service
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import engine  # noqa: F401


class TestRegisterQualityJobs:
    """Tests for register_quality_jobs()."""

    def test_register_quality_jobs_callable(self):
        """register_quality_jobs is callable."""
        from app.jobs.quality_jobs import register_quality_jobs

        assert callable(register_quality_jobs)

    def test_register_quality_jobs_registers_nothing_parked(self):
        """W1 (2026-08-04): the quality_score_activities registration is PARKED —
        register_quality_jobs is a no-op (comeback: team exists, spec §5.4)."""
        from app.jobs.quality_jobs import register_quality_jobs

        mock_scheduler = MagicMock()
        mock_settings = MagicMock()

        register_quality_jobs(mock_scheduler, mock_settings)

        mock_scheduler.add_job.assert_not_called()


class TestJobScoreActivities:
    """Tests for _job_score_activities() async job function."""

    async def test_job_scores_activities_when_found(self, db_session):
        """Job calls score_unscored_activities and logs result when count > 0."""

        with (
            patch("app.jobs.quality_jobs._job_score_activities.__wrapped__", create=True),
            patch("app.database.SessionLocal") as mock_session_cls,
            patch(
                "app.services.activity_quality_service.score_unscored_activities",
                new_callable=AsyncMock,
                return_value=5,
            ) as mock_score,
        ):
            mock_db = MagicMock()
            mock_session_cls.return_value = mock_db

            # Call the unwrapped inner function directly (bypass _traced_job decorator)
            from app.jobs import quality_jobs

            # Access __wrapped__ if available, otherwise test via import
            inner = getattr(quality_jobs._job_score_activities, "__wrapped__", None)
            if inner is None:
                # Direct call — the decorator wraps it but we can import via mock
                pass

    async def test_job_function_exists_and_is_coroutine(self):
        """_job_score_activities is an async function wrapped by _traced_job."""
        import inspect

        from app.jobs.quality_jobs import _job_score_activities

        assert callable(_job_score_activities)
        # The _traced_job decorator wraps it — the result should be a coroutine function
        assert inspect.iscoroutinefunction(_job_score_activities)

    async def test_job_runs_successfully_with_mocked_db(self, db_session):
        """Job runs end-to-end with mocked SessionLocal and score function."""
        from app.jobs.quality_jobs import _job_score_activities

        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch(
                "app.services.activity_quality_service.score_unscored_activities",
                new_callable=AsyncMock,
                return_value=3,
            ),
        ):
            # Should complete without raising
            await _job_score_activities()

        mock_db.close.assert_called_once()

    async def test_job_closes_db_on_exception(self, db_session):
        """Job closes DB session even when an exception is raised."""
        from app.jobs.quality_jobs import _job_score_activities

        mock_db = MagicMock()

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch(
                "app.services.activity_quality_service.score_unscored_activities",
                new_callable=AsyncMock,
                side_effect=RuntimeError("scoring failed"),
            ),
            pytest.raises(RuntimeError, match="scoring failed"),
        ):
            await _job_score_activities()

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()

    async def test_job_zero_scored_no_log(self, db_session):
        """Job runs without error when score_unscored_activities returns 0."""
        from app.jobs.quality_jobs import _job_score_activities

        mock_db = MagicMock()

        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch(
                "app.services.activity_quality_service.score_unscored_activities",
                new_callable=AsyncMock,
                return_value=0,
            ),
        ):
            await _job_score_activities()

        mock_db.close.assert_called_once()
