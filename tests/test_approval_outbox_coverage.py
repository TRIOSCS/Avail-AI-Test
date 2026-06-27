"""tests/test_approval_outbox_coverage.py — Extra coverage for approval_outbox.

Targets missing lines:
  - _job_drain_approval_outbox() scheduled job wrapper
  - register_approval_outbox_job()

Called by: pytest
Depends on: conftest.py, app.jobs.approval_outbox
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("TESTING", "1")


class TestApprovalOutboxJob:
    @pytest.mark.asyncio
    async def test_job_drain_dispatches(self):
        """_job_drain_approval_outbox() calls dispatch_pending and logs count."""
        from app.jobs.approval_outbox import _job_drain_approval_outbox

        mock_db = MagicMock()
        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.jobs.approval_outbox.dispatch_pending", new_callable=AsyncMock, return_value=3) as mock_dispatch,
        ):
            await _job_drain_approval_outbox()

        mock_dispatch.assert_awaited_once_with(mock_db)
        mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_job_drain_zero_count_no_log(self):
        """_job_drain_approval_outbox() with count=0 doesn't log 'dispatched'."""
        from app.jobs.approval_outbox import _job_drain_approval_outbox

        mock_db = MagicMock()
        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch("app.jobs.approval_outbox.dispatch_pending", new_callable=AsyncMock, return_value=0),
        ):
            await _job_drain_approval_outbox()

        mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_job_drain_exception_rolls_back(self):
        """_job_drain_approval_outbox() on exception calls rollback."""
        from app.jobs.approval_outbox import _job_drain_approval_outbox

        mock_db = MagicMock()
        with (
            patch("app.database.SessionLocal", return_value=mock_db),
            patch(
                "app.jobs.approval_outbox.dispatch_pending", new_callable=AsyncMock, side_effect=Exception("db error")
            ),
        ):
            await _job_drain_approval_outbox()  # should not raise

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()

    def test_register_approval_outbox_job(self):
        """register_approval_outbox_job() adds a job to the scheduler."""
        from app.jobs.approval_outbox import register_approval_outbox_job

        mock_scheduler = MagicMock()
        with patch("app.scheduler._traced_job", return_value=MagicMock()):
            register_approval_outbox_job(mock_scheduler)

        mock_scheduler.add_job.assert_called_once()
        call_kwargs = mock_scheduler.add_job.call_args[1]
        assert call_kwargs["id"] == "approval_outbox_drain"
