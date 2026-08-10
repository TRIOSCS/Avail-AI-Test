"""tests/test_remediation_p4med.py — QC 2026-08-10 P4 (code-QC medium: tagging job).

CancelledError is a BaseException, not Exception, so `_run_threaded_db_job`'s
`except Exception` missed it — on scheduler shutdown the finally closed the DB
session while the asyncio.to_thread worker was still using it. On cancellation we
now skip the close (the pool tears down at process exit).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import engine  # noqa: F401


@pytest.mark.anyio
async def test_cancelled_shutdown_does_not_close_session_under_the_thread():
    from app.jobs import tagging_jobs

    fake_db = MagicMock()
    with (
        patch("app.database.SessionLocal", return_value=fake_db),
        patch("app.jobs.tagging_jobs.asyncio.to_thread", new=AsyncMock(side_effect=asyncio.CancelledError())),
    ):
        with pytest.raises(asyncio.CancelledError):
            await tagging_jobs._run_threaded_db_job("test", lambda db: None)
    # The session must NOT be closed out from under the still-running worker thread.
    fake_db.close.assert_not_called()
    fake_db.rollback.assert_not_called()  # rollback would also touch the busy session


@pytest.mark.anyio
async def test_normal_completion_still_closes_session():
    from app.jobs import tagging_jobs

    fake_db = MagicMock()
    with (
        patch("app.database.SessionLocal", return_value=fake_db),
        patch("app.jobs.tagging_jobs.asyncio.to_thread", new=AsyncMock(return_value="ok")),
    ):
        await tagging_jobs._run_threaded_db_job("test", lambda db: "ok")
    fake_db.close.assert_called_once()


@pytest.mark.anyio
async def test_real_error_rolls_back_and_closes():
    from app.jobs import tagging_jobs

    fake_db = MagicMock()
    with (
        patch("app.database.SessionLocal", return_value=fake_db),
        patch("app.jobs.tagging_jobs.asyncio.to_thread", new=AsyncMock(side_effect=ValueError("boom"))),
    ):
        with pytest.raises(ValueError):
            await tagging_jobs._run_threaded_db_job("test", lambda db: None)
    fake_db.rollback.assert_called_once()
    fake_db.close.assert_called_once()
