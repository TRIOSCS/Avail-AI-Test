"""Tests for app/jobs/knowledge_jobs.py — targeting missing coverage.

Covers register_knowledge_jobs, _job_refresh_insights, _job_expire_stale.

Called by: pytest
Depends on: conftest fixtures, knowledge_jobs
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import User
from app.models.sourcing import Requisition


@pytest.fixture()
def active_req(db_session: Session, test_user: User) -> Requisition:
    req = Requisition(
        name="KJ-TEST-REQ",
        customer_name="Test Co",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.commit()
    db_session.refresh(req)
    return req


class TestRegisterKnowledgeJobs:
    def test_registers_at_least_one_job(self):
        from app.jobs.knowledge_jobs import register_knowledge_jobs

        mock_scheduler = MagicMock()
        mock_settings = MagicMock()
        register_knowledge_jobs(mock_scheduler, mock_settings)
        assert mock_scheduler.add_job.call_count >= 1

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


def _make_mock_session(req_ids=None, vendor_ids=None, company_ids=None, mpns=None):
    """Build a mock session returning configurable query results."""
    mock_session = MagicMock()
    mock_session.close = MagicMock()
    mock_session.rollback = MagicMock()

    req_rows = [(rid,) for rid in (req_ids or [])]
    vendor_rows = [(vid,) for vid in (vendor_ids or [])]
    company_rows = [(cid,) for cid in (company_ids or [])]
    mpn_rows = [(mpn,) for mpn in (mpns or [])]

    call_count = [0]

    def query_side_effect(*args, **kwargs):
        mock_q = MagicMock()
        # Chain: .filter(...).order_by(...).limit(...).all() -> rows
        # or .filter(...).group_by(...).order_by(...).limit(...).all() -> rows
        results = [req_rows, vendor_rows, company_rows, mpn_rows]
        idx = call_count[0] % len(results)
        call_count[0] += 1
        mock_q.filter.return_value.order_by.return_value.limit.return_value.all.return_value = results[idx]
        mock_q.filter.return_value.group_by.return_value.order_by.return_value.limit.return_value.all.return_value = (
            results[idx]
        )
        return mock_q

    mock_session.query.side_effect = query_side_effect
    return mock_session


class TestJobRefreshInsights:
    async def test_refresh_insights_empty_db(self):
        """With empty DB (no rows), job runs without errors."""
        from app.jobs.knowledge_jobs import _job_refresh_insights

        mock_session = _make_mock_session()
        with patch("app.database.SessionLocal", return_value=mock_session):
            with patch("app.services.knowledge_service.generate_insights", new=AsyncMock(return_value=[])):
                with patch("app.services.knowledge_service.generate_pipeline_insights", new=AsyncMock(return_value=[])):
                    with patch(
                        "app.services.knowledge_service.generate_vendor_insights", new=AsyncMock(return_value=[])
                    ):
                        with patch(
                            "app.services.knowledge_service.generate_company_insights", new=AsyncMock(return_value=[])
                        ):
                            with patch(
                                "app.services.knowledge_service.generate_mpn_insights", new=AsyncMock(return_value=[])
                            ):
                                await _job_refresh_insights()

        mock_session.close.assert_called_once()

    async def test_refresh_insights_with_req_ids(self):
        """generate_insights is called for each returned req id."""
        from app.jobs.knowledge_jobs import _job_refresh_insights

        mock_generate = AsyncMock(return_value=[MagicMock()])
        mock_session = _make_mock_session(req_ids=[1, 2, 3])

        with patch("app.database.SessionLocal", return_value=mock_session):
            with patch("app.services.knowledge_service.generate_insights", new=mock_generate):
                with patch("app.services.knowledge_service.generate_pipeline_insights", new=AsyncMock(return_value=[])):
                    with patch(
                        "app.services.knowledge_service.generate_vendor_insights", new=AsyncMock(return_value=[])
                    ):
                        with patch(
                            "app.services.knowledge_service.generate_company_insights", new=AsyncMock(return_value=[])
                        ):
                            with patch(
                                "app.services.knowledge_service.generate_mpn_insights", new=AsyncMock(return_value=[])
                            ):
                                await _job_refresh_insights()

        assert mock_generate.call_count == 3

    async def test_refresh_insights_req_exception_continues(self):
        """If generate_insights raises for a req, job continues."""
        from app.jobs.knowledge_jobs import _job_refresh_insights

        mock_generate = AsyncMock(side_effect=Exception("AI failed"))
        mock_session = _make_mock_session(req_ids=[1])

        with patch("app.database.SessionLocal", return_value=mock_session):
            with patch("app.services.knowledge_service.generate_insights", new=mock_generate):
                with patch("app.services.knowledge_service.generate_pipeline_insights", new=AsyncMock(return_value=[])):
                    with patch(
                        "app.services.knowledge_service.generate_vendor_insights", new=AsyncMock(return_value=[])
                    ):
                        with patch(
                            "app.services.knowledge_service.generate_company_insights", new=AsyncMock(return_value=[])
                        ):
                            with patch(
                                "app.services.knowledge_service.generate_mpn_insights", new=AsyncMock(return_value=[])
                            ):
                                await _job_refresh_insights()  # Should not raise

    async def test_refresh_pipeline_exception_continues(self):
        """If pipeline insights fail, rest of job continues."""
        from app.jobs.knowledge_jobs import _job_refresh_insights

        mock_session = _make_mock_session()
        with patch("app.database.SessionLocal", return_value=mock_session):
            with patch("app.services.knowledge_service.generate_insights", new=AsyncMock(return_value=[])):
                with patch(
                    "app.services.knowledge_service.generate_pipeline_insights",
                    new=AsyncMock(side_effect=Exception("pipeline fail")),
                ):
                    with patch(
                        "app.services.knowledge_service.generate_vendor_insights", new=AsyncMock(return_value=[])
                    ):
                        with patch(
                            "app.services.knowledge_service.generate_company_insights", new=AsyncMock(return_value=[])
                        ):
                            with patch(
                                "app.services.knowledge_service.generate_mpn_insights", new=AsyncMock(return_value=[])
                            ):
                                await _job_refresh_insights()  # Should not raise

    async def test_refresh_insights_db_error_logs_and_continues(self):
        """DB errors within each section are caught and logged; job completes."""
        from app.jobs.knowledge_jobs import _job_refresh_insights

        mock_session = MagicMock()
        mock_session.query.side_effect = RuntimeError("Section DB failure")
        mock_session.close = MagicMock()
        mock_session.rollback = MagicMock()

        with patch("app.database.SessionLocal", return_value=mock_session):
            # Each section catches its own error, so the job should complete without raising
            await _job_refresh_insights()

        mock_session.close.assert_called_once()

    async def test_refresh_vendor_insights_called(self):
        """generate_vendor_insights is called for each vendor id returned."""
        from app.jobs.knowledge_jobs import _job_refresh_insights

        mock_vendor = AsyncMock(return_value=[MagicMock()])
        mock_session = _make_mock_session(vendor_ids=[10, 20])

        with patch("app.database.SessionLocal", return_value=mock_session):
            with patch("app.services.knowledge_service.generate_insights", new=AsyncMock(return_value=[])):
                with patch("app.services.knowledge_service.generate_pipeline_insights", new=AsyncMock(return_value=[])):
                    with patch("app.services.knowledge_service.generate_vendor_insights", new=mock_vendor):
                        with patch(
                            "app.services.knowledge_service.generate_company_insights", new=AsyncMock(return_value=[])
                        ):
                            with patch(
                                "app.services.knowledge_service.generate_mpn_insights", new=AsyncMock(return_value=[])
                            ):
                                await _job_refresh_insights()

        assert mock_vendor.call_count == 2
