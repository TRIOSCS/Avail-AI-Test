"""tests/test_knowledge_jobs_coverage2.py — Additional coverage for knowledge_jobs.py.

Targets uncovered branches:
- Vendor insight exception handler (lines 96-97)
- Company insight exception handler (lines 115-120)
- MPN insight exception handler (lines 137-142)
- Outer exception re-raise path (lines 147-150)
- Company/MPN insight success paths

Called by: pytest
Depends on: conftest.py, app/jobs/knowledge_jobs.py
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_mock_session_full(req_ids=None, vendor_ids=None, company_ids=None, mpns=None):
    """Build a mock session returning configurable results for all query types."""
    mock_session = MagicMock()
    mock_session.close = MagicMock()
    mock_session.rollback = MagicMock()

    req_rows = [(rid,) for rid in (req_ids or [])]
    vendor_rows = [(vid,) for vid in (vendor_ids or [])]
    company_rows = [(cid,) for cid in (company_ids or [])]
    mpn_rows = [(mpn,) for mpn in (mpns or [])]

    call_count = [0]
    results = [req_rows, vendor_rows, company_rows, mpn_rows]

    def query_side_effect(*args, **kwargs):
        mock_q = MagicMock()
        idx = call_count[0] % len(results)
        call_count[0] += 1
        row_set = results[idx]
        mock_q.filter.return_value.order_by.return_value.limit.return_value.all.return_value = row_set
        mock_q.filter.return_value.group_by.return_value.order_by.return_value.limit.return_value.all.return_value = (
            row_set
        )
        mock_q.join.return_value.filter.return_value.group_by.return_value.order_by.return_value.limit.return_value.all.return_value = row_set
        return mock_q

    mock_session.query.side_effect = query_side_effect
    return mock_session


class TestJobRefreshInsightsMissingBranches:
    async def test_vendor_insight_exception_continues(self):
        """If generate_vendor_insights raises for a vendor, job continues to next."""
        from app.jobs.knowledge_jobs import _job_refresh_insights

        mock_vendor = AsyncMock(side_effect=Exception("Vendor AI failed"))
        mock_session = _make_mock_session_full(vendor_ids=[10, 20])

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
                                await _job_refresh_insights()  # Should not raise

        assert mock_vendor.call_count == 2

    async def test_company_insight_exception_continues(self):
        """If generate_company_insights raises, job continues."""
        from app.jobs.knowledge_jobs import _job_refresh_insights

        mock_company = AsyncMock(side_effect=Exception("Company AI failed"))
        mock_session = _make_mock_session_full(company_ids=[100, 200])

        with patch("app.database.SessionLocal", return_value=mock_session):
            with patch("app.services.knowledge_service.generate_insights", new=AsyncMock(return_value=[])):
                with patch("app.services.knowledge_service.generate_pipeline_insights", new=AsyncMock(return_value=[])):
                    with patch(
                        "app.services.knowledge_service.generate_vendor_insights", new=AsyncMock(return_value=[])
                    ):
                        with patch("app.services.knowledge_service.generate_company_insights", new=mock_company):
                            with patch(
                                "app.services.knowledge_service.generate_mpn_insights", new=AsyncMock(return_value=[])
                            ):
                                await _job_refresh_insights()  # Should not raise

        assert mock_company.call_count == 2

    async def test_mpn_insight_exception_continues(self):
        """If generate_mpn_insights raises for an MPN, job continues."""
        from app.jobs.knowledge_jobs import _job_refresh_insights

        mock_mpn = AsyncMock(side_effect=Exception("MPN AI failed"))
        mock_session = _make_mock_session_full(mpns=["LM317T", "TL431"])

        with patch("app.database.SessionLocal", return_value=mock_session):
            with patch("app.services.knowledge_service.generate_insights", new=AsyncMock(return_value=[])):
                with patch("app.services.knowledge_service.generate_pipeline_insights", new=AsyncMock(return_value=[])):
                    with patch(
                        "app.services.knowledge_service.generate_vendor_insights", new=AsyncMock(return_value=[])
                    ):
                        with patch(
                            "app.services.knowledge_service.generate_company_insights", new=AsyncMock(return_value=[])
                        ):
                            with patch("app.services.knowledge_service.generate_mpn_insights", new=mock_mpn):
                                await _job_refresh_insights()  # Should not raise

        assert mock_mpn.call_count == 2

    async def test_company_insights_called_for_each_company(self):
        """generate_company_insights is called for each company id returned."""
        from app.jobs.knowledge_jobs import _job_refresh_insights

        mock_company = AsyncMock(return_value=[MagicMock()])
        mock_session = _make_mock_session_full(company_ids=[30, 40, 50])

        with patch("app.database.SessionLocal", return_value=mock_session):
            with patch("app.services.knowledge_service.generate_insights", new=AsyncMock(return_value=[])):
                with patch("app.services.knowledge_service.generate_pipeline_insights", new=AsyncMock(return_value=[])):
                    with patch(
                        "app.services.knowledge_service.generate_vendor_insights", new=AsyncMock(return_value=[])
                    ):
                        with patch("app.services.knowledge_service.generate_company_insights", new=mock_company):
                            with patch(
                                "app.services.knowledge_service.generate_mpn_insights", new=AsyncMock(return_value=[])
                            ):
                                await _job_refresh_insights()

        assert mock_company.call_count == 3

    async def test_mpn_insights_called_for_each_mpn(self):
        """generate_mpn_insights is called for each MPN returned."""
        from app.jobs.knowledge_jobs import _job_refresh_insights

        mock_mpn = AsyncMock(return_value=[MagicMock()])
        mock_session = _make_mock_session_full(mpns=["ABC123", "DEF456", "GHI789"])

        with patch("app.database.SessionLocal", return_value=mock_session):
            with patch("app.services.knowledge_service.generate_insights", new=AsyncMock(return_value=[])):
                with patch("app.services.knowledge_service.generate_pipeline_insights", new=AsyncMock(return_value=[])):
                    with patch(
                        "app.services.knowledge_service.generate_vendor_insights", new=AsyncMock(return_value=[])
                    ):
                        with patch(
                            "app.services.knowledge_service.generate_company_insights", new=AsyncMock(return_value=[])
                        ):
                            with patch("app.services.knowledge_service.generate_mpn_insights", new=mock_mpn):
                                await _job_refresh_insights()

        assert mock_mpn.call_count == 3

    async def test_outer_exception_reraises_and_rollbacks(self):
        """Outer exception causes rollback and re-raise.

        Patches the datetime class in the knowledge_jobs module to raise
        at `cutoff = datetime.now(...)`, triggering lines 147-150.
        """
        import app.jobs.knowledge_jobs as kjobs
        from app.jobs.knowledge_jobs import _job_refresh_insights

        mock_session = MagicMock()
        mock_session.close = MagicMock()
        mock_session.rollback = MagicMock()

        # Create a mock datetime class where .now() raises
        mock_dt = MagicMock()
        mock_dt.now.side_effect = RuntimeError("Critical datetime failure")

        with patch("app.database.SessionLocal", return_value=mock_session):
            with patch.object(kjobs, "datetime", mock_dt):
                with pytest.raises(RuntimeError, match="Critical datetime failure"):
                    await _job_refresh_insights()

        mock_session.rollback.assert_called_once()
        mock_session.close.assert_called_once()

    async def test_vendor_section_db_error_caught(self):
        """DB error in vendor section is caught by section handler."""
        from app.jobs.knowledge_jobs import _job_refresh_insights

        mock_session = MagicMock()
        mock_session.close = MagicMock()
        mock_session.rollback = MagicMock()

        call_count = [0]

        def query_side_effect(*args, **kwargs):
            call_count[0] += 1
            mock_q = MagicMock()
            if call_count[0] == 1:
                # Req query - returns empty
                mock_q.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
            elif call_count[0] == 2:
                # Vendor query - raises section-level error
                mock_q.filter.return_value.group_by.return_value.order_by.return_value.limit.return_value.all.side_effect = RuntimeError(
                    "Vendor section DB failure"
                )
            else:
                mock_q.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
                mock_q.filter.return_value.group_by.return_value.order_by.return_value.limit.return_value.all.return_value = []
                mock_q.join.return_value.filter.return_value.group_by.return_value.order_by.return_value.limit.return_value.all.return_value = []
            return mock_q

        mock_session.query.side_effect = query_side_effect

        with patch("app.database.SessionLocal", return_value=mock_session):
            with patch("app.services.knowledge_service.generate_insights", new=AsyncMock(return_value=[])):
                with patch("app.services.knowledge_service.generate_pipeline_insights", new=AsyncMock(return_value=[])):
                    await _job_refresh_insights()  # Should not raise

        mock_session.close.assert_called_once()
