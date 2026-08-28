"""Tests for the RUN_SCHEDULER flag (Phase-4 infra: dedicated scheduler service).

Verifies app/main.py's lifespan gates the APScheduler start/shutdown, the
source/browser-worker seeds, and the deferred startup backfills behind
``os.environ.get("RUN_SCHEDULER", "1") == "1"``. Default "1" preserves
today's single-process behavior (instant rollback knob). docker-compose.yml
sets RUN_SCHEDULER=0 on the `app` service and RUN_SCHEDULER=1 on the new
`scheduler` service -- exactly one process must run the scheduler, since the
approval-notification outbox has no cross-process locking and two schedulers
would double-send emails.

Mirrors the lifespan-test technique tests/test_main.py already uses
(no_testing_env() + run_lifespan(), draining the P2.7 background task) rather
than importing it, so this file stays self-contained.
"""

import asyncio
import os
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch


@contextmanager
def no_testing_env():
    """Temporarily unset TESTING, restoring it to "1" on exit.

    The lifespan's RUN_SCHEDULER gate only matters on the `if not _is_testing:`
    branch -- under TESTING=1 (the suite default, set in conftest.py) the whole
    branch is skipped regardless of RUN_SCHEDULER.
    """
    original = os.environ.pop("TESTING", None)
    try:
        yield
    finally:
        os.environ["TESTING"] = original if original is not None else "1"


@contextmanager
def run_scheduler_env(value):
    """Temporarily set RUN_SCHEDULER to `value` (None = leave unset, to exercise the
    default), restoring the prior value/absence on exit."""
    original = os.environ.pop("RUN_SCHEDULER", None)
    try:
        if value is not None:
            os.environ["RUN_SCHEDULER"] = value
        yield
    finally:
        os.environ.pop("RUN_SCHEDULER", None)
        if original is not None:
            os.environ["RUN_SCHEDULER"] = original


def run_lifespan(mock_app):
    """Run the app lifespan async context (enter + exit) on a fresh event loop.

    Also drains any fire-and-forget background tasks the lifespan scheduled (P2.7's
    deferred startup-backfill task via safe_background_task) before closing the loop --
    mirrors tests/test_main.py's helper of the same name.
    """
    from app.main import lifespan

    async def _run():
        async with lifespan(mock_app):
            pass
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_run())
    finally:
        loop.close()


@contextmanager
def _lifespan_mocks():
    """Patch every lifespan dependency so a real (non-TESTING) boot can run end to end
    without touching a real DB/scheduler/Sentry, yielding the mocks relevant to the
    RUN_SCHEDULER assertions."""
    with (
        patch("app.main.settings") as mock_settings,
        patch("app.startup.run_startup_migrations"),
        patch("app.startup.ensure_screenshot_storage"),
        patch("app.startup.ensure_avatar_storage"),
        patch("app.startup.seed_api_sources") as mock_seed_api,
        patch("app.startup.seed_browser_workers") as mock_seed_browser,
        patch("app.startup.mark_deferred_backfills_pending") as mock_mark,
        patch("app.startup.run_deferred_startup_backfills") as mock_deferred,
        patch("app.connector_status.log_connector_status", return_value={}) as mock_connector,
        patch("app.scheduler.configure_scheduler") as mock_configure,
        patch("app.scheduler.scheduler") as mock_scheduler,
        patch("app.http_client.close_clients", new_callable=AsyncMock),
    ):
        mock_settings.secret_key = "a-real-secret-key"
        mock_settings.sentry_dsn = ""
        mock_settings.azure_client_id = "cid"
        mock_settings.azure_client_secret = "csecret"
        mock_settings.azure_tenant_id = "tid"
        mock_settings.acs_connection_string = ""
        mock_settings.acs_webhook_secret = ""
        yield {
            "seed_api": mock_seed_api,
            "seed_browser": mock_seed_browser,
            "mark": mock_mark,
            "deferred": mock_deferred,
            "connector": mock_connector,
            "configure": mock_configure,
            "scheduler": mock_scheduler,
        }


class TestRunSchedulerFlagDefaultOn:
    """RUN_SCHEDULER unset or "1" preserves today's single-process behavior:

    seeds, APScheduler start/shutdown, and the deferred backfills all run.
    """

    def test_default_unset_runs_scheduler_and_seeds(self):
        mock_app = MagicMock()
        with no_testing_env(), run_scheduler_env(None), _lifespan_mocks() as mocks:
            run_lifespan(mock_app)

            mocks["seed_api"].assert_called_once()
            mocks["seed_browser"].assert_called_once()
            mocks["configure"].assert_called_once()
            mocks["scheduler"].start.assert_called_once()
            mocks["scheduler"].shutdown.assert_called_once_with(wait=True)
            mocks["mark"].assert_called_once()
            mocks["deferred"].assert_called_once()
            # Connector status logging is NOT gated by RUN_SCHEDULER -- runs
            # on every real (non-TESTING) boot regardless.
            mocks["connector"].assert_called_once()

    def test_explicit_run_scheduler_1_runs_scheduler_and_seeds(self):
        mock_app = MagicMock()
        with no_testing_env(), run_scheduler_env("1"), _lifespan_mocks() as mocks:
            run_lifespan(mock_app)

            mocks["seed_api"].assert_called_once()
            mocks["seed_browser"].assert_called_once()
            mocks["configure"].assert_called_once()
            mocks["scheduler"].start.assert_called_once()
            mocks["scheduler"].shutdown.assert_called_once_with(wait=True)
            mocks["mark"].assert_called_once()
            mocks["deferred"].assert_called_once()


class TestRunSchedulerFlagOff:
    """RUN_SCHEDULER=0 skips seeds, APScheduler start/shutdown, and the deferred
    backfills entirely -- the `app` service's setting in docker-compose.yml, once the
    scheduler moves to its own service."""

    def test_zero_skips_scheduler_and_seeds(self):
        mock_app = MagicMock()
        with no_testing_env(), run_scheduler_env("0"), _lifespan_mocks() as mocks:
            run_lifespan(mock_app)

            mocks["seed_api"].assert_not_called()
            mocks["seed_browser"].assert_not_called()
            mocks["configure"].assert_not_called()
            mocks["scheduler"].start.assert_not_called()
            mocks["scheduler"].shutdown.assert_not_called()
            mocks["mark"].assert_not_called()
            mocks["deferred"].assert_not_called()
            # Connector status logging is NOT gated by RUN_SCHEDULER -- still
            # runs on the app process even with the scheduler disabled.
            mocks["connector"].assert_called_once()

    def test_zero_never_raises_on_shutdown(self):
        """RUN_SCHEDULER=0 never imports `scheduler` during startup -- the shutdown
        guard MUST be gated by the same flag rather than calling `scheduler.shutdown()`
        unconditionally, which would NameError since the name was never bound in this
        run of the lifespan.

        A regression here would surface as an unhandled exception from run_lifespan(),
        failing this test even without an explicit assertion below.
        """
        mock_app = MagicMock()
        with no_testing_env(), run_scheduler_env("0"), _lifespan_mocks():
            run_lifespan(mock_app)  # must not raise NameError on shutdown
