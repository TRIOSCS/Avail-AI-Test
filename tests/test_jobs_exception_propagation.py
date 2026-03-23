"""Tests verifying that top-level job functions re-raise exceptions after cleanup.

Background jobs are wrapped by _traced_job (app/scheduler.py) which captures
exceptions for Sentry. If a job swallows exceptions internally, _traced_job
never sees them and Sentry gets zero alerts.

These tests verify that each top-level job function properly re-raises after
performing cleanup (logging + db.rollback).

Called by: pytest
Depends on: app.jobs.core_jobs, app.jobs.email_jobs, app.jobs.knowledge_jobs
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_db(fail_on_query=True):
    """Return a mock SessionLocal instance that raises on query() if requested."""
    db = MagicMock()
    if fail_on_query:
        db.query.side_effect = RuntimeError("DB connection lost")
    db.get.side_effect = RuntimeError("DB connection lost") if fail_on_query else None
    return db


def _make_session_local_factory(db):
    """Return a callable that returns the pre-built mock db."""
    return lambda: db


# ---------------------------------------------------------------------------
# core_jobs — top-level exception propagation
# ---------------------------------------------------------------------------


class TestCoreJobsPropagation:
    """Verify core_jobs top-level handlers re-raise."""

    @pytest.mark.asyncio
    async def test_auto_archive_reraises(self):
        db = _make_fake_db()
        with patch("app.database.SessionLocal", _make_session_local_factory(db)):
            from app.jobs.core_jobs import _job_auto_archive

            fn = _job_auto_archive.__wrapped__
            with pytest.raises(RuntimeError, match="DB connection lost"):
                await fn()
            db.rollback.assert_called_once()
            db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_token_refresh_reraises(self):
        db = _make_fake_db()
        with patch("app.database.SessionLocal", _make_session_local_factory(db)):
            from app.jobs.core_jobs import _job_token_refresh

            fn = _job_token_refresh.__wrapped__
            with pytest.raises(RuntimeError, match="DB connection lost"):
                await fn()
            db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_inbox_scan_reraises(self):
        db = _make_fake_db()
        with patch("app.database.SessionLocal", _make_session_local_factory(db)):
            from app.jobs.core_jobs import _job_inbox_scan

            fn = _job_inbox_scan.__wrapped__
            with pytest.raises(RuntimeError, match="DB connection lost"):
                await fn()
            db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_batch_results_reraises(self):
        db = _make_fake_db(fail_on_query=False)
        with (
            patch("app.database.SessionLocal", _make_session_local_factory(db)),
            patch("app.email_service.process_batch_results", side_effect=RuntimeError("batch boom")),
        ):
            from app.jobs.core_jobs import _job_batch_results

            fn = _job_batch_results.__wrapped__
            with pytest.raises(RuntimeError, match="batch boom"):
                await fn()
            db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_webhook_subscriptions_reraises(self):
        db = _make_fake_db(fail_on_query=False)
        with (
            patch("app.database.SessionLocal", _make_session_local_factory(db)),
            patch(
                "app.services.webhook_service.renew_expiring_subscriptions",
                new_callable=AsyncMock,
                side_effect=RuntimeError("webhook boom"),
            ),
        ):
            from app.jobs.core_jobs import _job_webhook_subscriptions

            fn = _job_webhook_subscriptions.__wrapped__
            with pytest.raises(RuntimeError, match="webhook boom"):
                await fn()
            db.rollback.assert_called_once()
            db.close.assert_called_once()


# ---------------------------------------------------------------------------
# email_jobs — top-level exception propagation
# ---------------------------------------------------------------------------


class TestEmailJobsPropagation:
    """Verify email_jobs top-level handlers re-raise."""

    @pytest.mark.asyncio
    async def test_contacts_sync_reraises(self):
        db = _make_fake_db()
        with patch("app.database.SessionLocal", _make_session_local_factory(db)):
            from app.jobs.email_jobs import _job_contacts_sync

            fn = _job_contacts_sync.__wrapped__
            with pytest.raises(RuntimeError, match="DB connection lost"):
                await fn()
            db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_ownership_sweep_reraises(self):
        db = _make_fake_db(fail_on_query=False)
        with (
            patch("app.database.SessionLocal", _make_session_local_factory(db)),
            patch(
                "app.services.ownership_service.run_ownership_sweep",
                new_callable=AsyncMock,
                side_effect=RuntimeError("ownership boom"),
            ),
        ):
            from app.jobs.email_jobs import _job_ownership_sweep

            fn = _job_ownership_sweep.__wrapped__
            with pytest.raises(RuntimeError, match="ownership boom"):
                await fn()
            db.rollback.assert_called_once()
            db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_contact_status_compute_reraises(self):
        db = _make_fake_db()
        with patch("app.database.SessionLocal", _make_session_local_factory(db)):
            from app.jobs.email_jobs import _job_contact_status_compute

            fn = _job_contact_status_compute.__wrapped__
            with pytest.raises(RuntimeError, match="DB connection lost"):
                await fn()
            db.rollback.assert_called_once()
            db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_email_health_update_reraises(self):
        db = _make_fake_db(fail_on_query=False)
        with (
            patch("app.database.SessionLocal", _make_session_local_factory(db)),
            patch(
                "app.services.response_analytics.batch_update_email_health",
                side_effect=RuntimeError("health boom"),
            ),
        ):
            from app.jobs.email_jobs import _job_email_health_update

            fn = _job_email_health_update.__wrapped__
            with pytest.raises(RuntimeError, match="health boom"):
                await fn()
            db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_email_reverification_reraises(self):
        db = _make_fake_db(fail_on_query=False)
        with (
            patch("app.database.SessionLocal", _make_session_local_factory(db)),
            patch(
                "app.services.customer_enrichment_batch.run_email_reverification",
                new_callable=AsyncMock,
                side_effect=RuntimeError("reverify boom"),
            ),
        ):
            from app.jobs.email_jobs import _job_email_reverification

            fn = _job_email_reverification.__wrapped__
            with pytest.raises(RuntimeError, match="reverify boom"):
                await fn()
            db.rollback.assert_called_once()
            db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_calendar_scan_reraises(self):
        db = _make_fake_db()
        with patch("app.database.SessionLocal", _make_session_local_factory(db)):
            from app.jobs.email_jobs import _job_calendar_scan

            fn = _job_calendar_scan.__wrapped__
            with pytest.raises(RuntimeError, match="DB connection lost"):
                await fn()
            db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_scan_sent_folders_reraises(self):
        db = _make_fake_db()
        with patch("app.database.SessionLocal", _make_session_local_factory(db)):
            from app.jobs.email_jobs import _job_scan_sent_folders

            fn = _job_scan_sent_folders.__wrapped__
            with pytest.raises(RuntimeError, match="DB connection lost"):
                await fn()
            db.close.assert_called_once()


# ---------------------------------------------------------------------------
# knowledge_jobs — top-level exception propagation
# ---------------------------------------------------------------------------


class TestKnowledgeJobsPropagation:
    """Verify knowledge_jobs top-level handlers re-raise."""

    def test_refresh_insights_has_raise_in_outer_handler(self):
        """Verify _job_refresh_insights outer except has a raise statement.

        The inner sub-operation try/except blocks (req insights, vendor insights, etc.)
        intentionally catch and continue — similar to _safe_* wrappers. The outer
        handler is hard to trigger in a unit test, so we verify the source code
        directly.
        """
        import ast
        import inspect

        from app.jobs.knowledge_jobs import _job_refresh_insights

        source = inspect.getsource(_job_refresh_insights)
        tree = ast.parse(source)

        # Find the outermost try/except in the function body
        func_def = tree.body[0]
        assert isinstance(func_def, (ast.FunctionDef, ast.AsyncFunctionDef))

        # The outer try is in the function body (after db = SessionLocal())
        outer_tries = [n for n in ast.walk(func_def) if isinstance(n, ast.Try)]
        assert outer_tries, "Expected at least one try block"

        # The outermost try is the first one at the top nesting level
        outer_try = outer_tries[0]
        # Check its except handlers for a Raise node
        found_raise = False
        for handler in outer_try.handlers:
            for node in ast.walk(handler):
                if isinstance(node, ast.Raise):
                    found_raise = True
                    break
        assert found_raise, "Outer except handler in _job_refresh_insights must re-raise"

    @pytest.mark.asyncio
    async def test_expire_stale_reraises(self):
        db = _make_fake_db()
        with patch("app.database.SessionLocal", _make_session_local_factory(db)):
            from app.jobs.knowledge_jobs import _job_expire_stale

            with pytest.raises(RuntimeError, match="DB connection lost"):
                await _job_expire_stale()
            db.close.assert_called_once()


# ---------------------------------------------------------------------------
# _traced_job integration — verify decorator captures re-raised exceptions
# ---------------------------------------------------------------------------


class TestTracedJobIntegration:
    """Verify that _traced_job sees exceptions that bubble up from jobs."""

    @pytest.mark.asyncio
    async def test_traced_job_sees_reraised_exception(self):
        """When a job re-raises, _traced_job should log and re-raise."""
        from app.scheduler import _traced_job

        @_traced_job
        async def _failing_job():
            try:
                raise ValueError("something broke")
            except Exception:
                raise

        with pytest.raises(ValueError, match="something broke"):
            await _failing_job()

    @pytest.mark.asyncio
    async def test_traced_job_does_not_see_swallowed_exception(self):
        """Demonstrate the old broken pattern: swallowed = _traced_job sees nothing."""
        from app.scheduler import _traced_job

        @_traced_job
        async def _swallowing_job():
            try:
                raise ValueError("silently lost")
            except Exception:
                pass  # Swallowed — _traced_job never knows

        # This completes without error — the bug we fixed
        result = await _swallowing_job()
        assert result is None
