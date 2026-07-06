"""Tests for app/jobs/knowledge_jobs.py — targeting missing coverage.

Covers register_knowledge_jobs and _job_expire_stale.

Called by: pytest
Depends on: conftest fixtures, knowledge_jobs
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import MagicMock, patch

import pytest


class TestRegisterKnowledgeJobs:
    def test_registers_exactly_one_job(self):
        from app.jobs.knowledge_jobs import register_knowledge_jobs

        mock_scheduler = MagicMock()
        mock_settings = MagicMock()
        register_knowledge_jobs(mock_scheduler, mock_settings)
        assert mock_scheduler.add_job.call_count == 1

    def test_registers_expire_job(self):
        from app.jobs.knowledge_jobs import register_knowledge_jobs

        mock_scheduler = MagicMock()
        register_knowledge_jobs(mock_scheduler, MagicMock())
        all_kwargs = [c[1] for c in mock_scheduler.add_job.call_args_list]
        ids = [kw.get("id") for kw in all_kwargs]
        assert "knowledge_expire_stale" in ids


class TestJobExpireStale:
    async def test_expire_stale_empty_db(self):
        """_job_expire_stale runs with a mocked session returning zero entries."""
        from app.jobs.knowledge_jobs import _job_expire_stale

        mock_session = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value.count.return_value = 0
        mock_query.count.return_value = 0
        mock_session.query.return_value = mock_query
        mock_session.close = MagicMock()

        with patch("app.database.SessionLocal", return_value=mock_session):
            await _job_expire_stale()

        mock_session.close.assert_called_once()

    async def test_expire_stale_with_entries(self):
        """Expire stale runs and logs count when entries exist."""
        from app.jobs.knowledge_jobs import _job_expire_stale

        mock_session = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value.count.return_value = 3
        mock_query.count.return_value = 10
        mock_session.query.return_value = mock_query
        mock_session.close = MagicMock()

        with patch("app.database.SessionLocal", return_value=mock_session):
            await _job_expire_stale()

    async def test_expire_stale_db_error_raises(self):
        """If DB query fails, exception is re-raised."""
        from app.jobs.knowledge_jobs import _job_expire_stale

        mock_session = MagicMock()
        mock_session.query.side_effect = RuntimeError("DB error")
        mock_session.close = MagicMock()

        with patch("app.database.SessionLocal", return_value=mock_session):
            with pytest.raises(RuntimeError):
                await _job_expire_stale()
