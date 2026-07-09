"""tests/test_nightly_tbf_worker_main.py — Coverage tests for tbf_worker/worker.py main
loop.

Tests the main() coroutine's various execution branches by mocking all browser/DB/API
dependencies and letting the loop take one tick before receiving a shutdown signal.

All symbols imported *inside* main() are patched at their source modules, not on the
worker module (they're not module-level attributes in worker.py).

Called by: pytest (nightly coverage run) Depends on: conftest (db_session)
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("TESTING", "1")


def _make_mock_db():
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    db.close = MagicMock()
    db.add = MagicMock()
    db.commit = MagicMock()
    return db


def _make_mock_ctx(mock_db=None):
    if mock_db is None:
        mock_db = _make_mock_db()
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=mock_db)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


def _make_sm(*, is_logged_in=True, start_raises=None, login_result=True):
    sm = AsyncMock()
    sm.is_logged_in = is_logged_in
    sm.page = MagicMock()
    sm.start = AsyncMock(side_effect=start_raises) if start_raises else AsyncMock()
    sm.login = AsyncMock(return_value=login_result)
    sm.stop = AsyncMock()
    sm.ensure_session = AsyncMock(return_value=True)
    return sm


def _make_normal_scheduler(tick_limit=1, *, on_tick=None):
    """Scheduler that permits work for `tick_limit` ticks then triggers shutdown."""
    tick = {"n": 0}

    class _S:
        def is_business_hours(self_inner):
            tick["n"] += 1
            if on_tick:
                on_tick(tick["n"])
            return True

        def time_for_break(self_inner):
            return False

        def next_delay(self_inner):
            return 0.001

    return _S()


# The standard patches used by every test that reaches the main loop body.
# Symbol → its actual module path (all are imported *inside* main(), not at module level).
_QUEUE_RECOVER = "app.services.tbf_worker.queue_manager.recover_stale_searches"
_QUEUE_CLAIM = "app.services.tbf_worker.queue_manager.claim_next_queued_item"
_QUEUE_COMPLETE = "app.services.tbf_worker.queue_manager.mark_completed"
_QUEUE_STATUS = "app.services.tbf_worker.queue_manager.mark_status"
_AI_GATE = "app.services.tbf_worker.ai_gate.process_ai_gate"
_SESSION_MGR = "app.services.tbf_worker.session_manager.TbfSessionManager"
_SCHEDULER = "app.services.tbf_worker.scheduler.SearchScheduler"
_BREAKER = "app.services.tbf_worker.circuit_breaker.CircuitBreaker"
_CONFIG = "app.services.tbf_worker.config.TbfConfig"
_SEARCH_PART = "app.services.tbf_worker.search_engine.search_part"
_PARSE_HTML = "app.services.tbf_worker.result_parser.parse_results_html"
_SAVE_SIGHTINGS = "app.services.tbf_worker.sighting_writer.save_tbf_sightings"
_SESSION_LOCAL = "app.database.SessionLocal"
_SLEEP = "app.services.tbf_worker.worker._async_sleep"
_UPDATE_STATUS = "app.services.tbf_worker.worker.update_worker_status"
_DB_SESSION = "app.services.tbf_worker.worker._db_session"


class TestTbfWorkerMain:
    """Exercises the main() loop paths via mocked dependencies."""

    @pytest.mark.asyncio
    async def test_shutdown_on_flag_before_loop(self):
        """Setting _shutdown_requested before the loop causes immediate clean exit."""
        import app.services.tbf_worker.worker as wmod

        sm = _make_sm(is_logged_in=True)
        mock_db = _make_mock_db()
        ctx = _make_mock_ctx(mock_db)

        wmod._shutdown_requested = True
        try:
            with (
                patch(_DB_SESSION, return_value=ctx),
                patch(_UPDATE_STATUS),
                patch(_QUEUE_RECOVER),
                patch(_SESSION_MGR, return_value=sm),
                patch(_SCHEDULER, return_value=_make_normal_scheduler()),
                patch(_BREAKER, return_value=MagicMock(should_stop=lambda: False)),
            ):
                await wmod.main()
        finally:
            wmod._shutdown_requested = False

        sm.stop.assert_awaited()

    @pytest.mark.asyncio
    async def test_browser_start_failure_stops_session(self):
        """Start() raising tears down the session (leak guard) before exiting.

        start() can launch Playwright/Chromium before failing; without stop() the
        browser subprocess would leak.
        """
        import app.services.tbf_worker.worker as wmod

        sm = _make_sm(start_raises=Exception("No DISPLAY"))
        ctx = _make_mock_ctx()

        wmod._shutdown_requested = False
        try:
            with (
                patch(_DB_SESSION, return_value=ctx),
                patch(_UPDATE_STATUS),
                patch(_QUEUE_RECOVER),
                patch(_SESSION_MGR, return_value=sm),
            ):
                await wmod.main()
        finally:
            wmod._shutdown_requested = False

        sm.stop.assert_awaited()

    @pytest.mark.asyncio
    async def test_off_hours_sleeps(self):
        """Off-hours path: loop calls asyncio.sleep(30*60)."""
        import app.services.tbf_worker.worker as wmod

        sm = _make_sm(is_logged_in=True)
        mock_db = _make_mock_db()
        ctx = _make_mock_ctx(mock_db)
        sleep_mock = AsyncMock()
        tick = {"n": 0}

        class _OffHoursScheduler:
            def is_business_hours(self_inner):
                tick["n"] += 1
                if tick["n"] >= 1:
                    wmod._shutdown_requested = True
                return False

            def time_for_break(self_inner):
                return False

            def next_delay(self_inner):
                return 0.001

        wmod._shutdown_requested = False
        try:
            with (
                patch(_DB_SESSION, return_value=ctx),
                patch(_UPDATE_STATUS),
                patch(_QUEUE_RECOVER),
                patch(_SESSION_MGR, return_value=sm),
                patch(_SCHEDULER, return_value=_OffHoursScheduler()),
                patch(_BREAKER, return_value=MagicMock(should_stop=lambda: False)),
                patch(_SLEEP, sleep_mock),
            ):
                await wmod.main()
        finally:
            wmod._shutdown_requested = False

        sleep_mock.assert_any_await(30 * 60)

    @pytest.mark.asyncio
    async def test_daily_limit_sleeps(self):
        """Daily-limit path: loop calls asyncio.sleep(60*60)."""
        import app.services.tbf_worker.worker as wmod
        from app.services.tbf_worker.config import TbfConfig

        sm = _make_sm(is_logged_in=True)
        mock_db = _make_mock_db()
        ctx = _make_mock_ctx(mock_db)
        sleep_mock = AsyncMock()
        tick = {"n": 0}

        class _LimitScheduler:
            def is_business_hours(self_inner):
                tick["n"] += 1
                if tick["n"] >= 1:
                    wmod._shutdown_requested = True
                return True

            def time_for_break(self_inner):
                return False

            def next_delay(self_inner):
                return 0.001

        cfg = TbfConfig()
        patched_cfg = MagicMock()
        patched_cfg.TBF_MAX_DAILY_SEARCHES = 0  # triggers limit immediately
        patched_cfg.TBF_BREAKER_COOLDOWN_MINUTES = cfg.TBF_BREAKER_COOLDOWN_MINUTES
        patched_cfg.TBF_SEARCH_TIMEOUT_SECONDS = 30
        patched_cfg.TBF_MIN_DELAY_SECONDS = 0.001
        patched_cfg.TBF_MAX_DELAY_SECONDS = 0.001

        wmod._shutdown_requested = False
        try:
            with (
                patch(_DB_SESSION, return_value=ctx),
                patch(_UPDATE_STATUS),
                patch(_QUEUE_RECOVER),
                patch(_SESSION_MGR, return_value=sm),
                patch(_SCHEDULER, return_value=_LimitScheduler()),
                patch(_BREAKER, return_value=MagicMock(should_stop=lambda: False)),
                patch(_CONFIG, return_value=patched_cfg),
                patch(_SLEEP, sleep_mock),
            ):
                await wmod.main()
        finally:
            wmod._shutdown_requested = False

        sleep_mock.assert_any_await(60 * 60)

    @pytest.mark.asyncio
    async def test_circuit_breaker_open_sleeps(self):
        """Open circuit breaker: loop calls asyncio.sleep(60*60)."""
        import app.services.tbf_worker.worker as wmod

        sm = _make_sm(is_logged_in=True)
        mock_db = _make_mock_db()
        ctx = _make_mock_ctx(mock_db)
        sleep_mock = AsyncMock()
        tick = {"n": 0}

        class _Scheduler:
            def is_business_hours(self_inner):
                tick["n"] += 1
                if tick["n"] >= 1:
                    wmod._shutdown_requested = True
                return True

            def time_for_break(self_inner):
                return False

            def next_delay(self_inner):
                return 0.001

        breaker = MagicMock()
        breaker.should_stop.return_value = True
        breaker.get_trip_info.return_value = {"trip_reason": "test-error"}
        breaker.trip_reason = "test-error"

        wmod._shutdown_requested = False
        try:
            with (
                patch(_DB_SESSION, return_value=ctx),
                patch(_UPDATE_STATUS),
                patch(_QUEUE_RECOVER),
                patch(_SESSION_MGR, return_value=sm),
                patch(_SCHEDULER, return_value=_Scheduler()),
                patch(_BREAKER, return_value=breaker),
                patch(_SLEEP, sleep_mock),
            ):
                await wmod.main()
        finally:
            wmod._shutdown_requested = False

        sleep_mock.assert_any_await(60 * 60)

    @pytest.mark.asyncio
    async def test_break_time_sleeps(self):
        """Break-time path: loop sleeps for the break duration."""
        import app.services.tbf_worker.worker as wmod

        sm = _make_sm(is_logged_in=True)
        mock_db = _make_mock_db()
        ctx = _make_mock_ctx(mock_db)
        sleep_mock = AsyncMock()
        tick = {"n": 0}

        class _BreakScheduler:
            def is_business_hours(self_inner):
                return True

            def time_for_break(self_inner):
                tick["n"] += 1
                if tick["n"] >= 1:
                    wmod._shutdown_requested = True
                return True

            def get_break_duration(self_inner):
                return 7

            def reset_break_counter(self_inner):
                pass

            def next_delay(self_inner):
                return 0.001

        breaker = MagicMock()
        breaker.should_stop.return_value = False

        wmod._shutdown_requested = False
        try:
            with (
                patch(_DB_SESSION, return_value=ctx),
                patch(_UPDATE_STATUS),
                patch(_QUEUE_RECOVER),
                patch(_SESSION_MGR, return_value=sm),
                patch(_SCHEDULER, return_value=_BreakScheduler()),
                patch(_BREAKER, return_value=breaker),
                patch(_SLEEP, sleep_mock),
            ):
                await wmod.main()
        finally:
            wmod._shutdown_requested = False

        sleep_mock.assert_any_await(7)

    @pytest.mark.asyncio
    async def test_empty_queue_sleeps(self):
        """Empty queue: loop sleeps 60s then shuts down."""
        import app.services.tbf_worker.worker as wmod

        sm = _make_sm(is_logged_in=True)
        mock_db = _make_mock_db()
        ctx = _make_mock_ctx(mock_db)
        sleep_mock = AsyncMock()
        tick = {"n": 0}

        class _Scheduler:
            def is_business_hours(self_inner):
                return True

            def time_for_break(self_inner):
                return False

            def next_delay(self_inner):
                return 0.001

        breaker = MagicMock()
        breaker.should_stop.return_value = False

        def _claim(db):
            tick["n"] += 1
            if tick["n"] >= 1:
                wmod._shutdown_requested = True
            return None

        wmod._shutdown_requested = False
        try:
            with (
                patch(_DB_SESSION, return_value=ctx),
                patch(_UPDATE_STATUS),
                patch(_QUEUE_RECOVER),
                patch(_AI_GATE, new_callable=AsyncMock),
                patch(_QUEUE_CLAIM, side_effect=_claim),
                patch(_SESSION_MGR, return_value=sm),
                patch(_SCHEDULER, return_value=_Scheduler()),
                patch(_BREAKER, return_value=breaker),
                patch(_SLEEP, sleep_mock),
                patch(_SESSION_LOCAL, return_value=mock_db),
            ):
                await wmod.main()
        finally:
            wmod._shutdown_requested = False

        sleep_mock.assert_any_await(60)

    @pytest.mark.asyncio
    async def test_session_auth_failure_sleeps_5min(self):
        """ensure_session() returning False marks item failed and sleeps 5min."""
        import app.services.tbf_worker.worker as wmod

        sm = _make_sm(is_logged_in=True)
        sm.ensure_session = AsyncMock(return_value=False)
        mock_db = _make_mock_db()
        ctx = _make_mock_ctx(mock_db)
        sleep_mock = AsyncMock()
        tick = {"n": 0}
        statuses = []

        class _Scheduler:
            def is_business_hours(self_inner):
                return True

            def time_for_break(self_inner):
                return False

            def next_delay(self_inner):
                return 0.001

        breaker = MagicMock()
        breaker.should_stop.return_value = False

        mock_item = MagicMock()
        mock_item.mpn = "LM317T"
        mock_item.id = 42

        def _claim(db):
            tick["n"] += 1
            if tick["n"] >= 1:
                wmod._shutdown_requested = True
            return mock_item

        wmod._shutdown_requested = False
        try:
            with (
                patch(_DB_SESSION, return_value=ctx),
                patch(_UPDATE_STATUS),
                patch(_QUEUE_RECOVER),
                patch(_AI_GATE, new_callable=AsyncMock),
                patch(_QUEUE_CLAIM, side_effect=_claim),
                patch(_QUEUE_STATUS, side_effect=lambda db, item, s, **kw: statuses.append(s)),
                patch(_SESSION_MGR, return_value=sm),
                patch(_SCHEDULER, return_value=_Scheduler()),
                patch(_BREAKER, return_value=breaker),
                patch(_SLEEP, sleep_mock),
                patch(_SESSION_LOCAL, return_value=mock_db),
            ):
                await wmod.main()
        finally:
            wmod._shutdown_requested = False

        sleep_mock.assert_any_await(5 * 60)
        assert "failed" in statuses

    @pytest.mark.asyncio
    async def test_search_timeout_fails_item(self):
        """Search timeout marks item failed and continues."""
        import app.services.tbf_worker.worker as wmod

        sm = _make_sm(is_logged_in=True)
        mock_db = _make_mock_db()
        ctx = _make_mock_ctx(mock_db)
        sleep_mock = AsyncMock()
        tick = {"n": 0}
        statuses = []

        class _Scheduler:
            def is_business_hours(self_inner):
                return True

            def time_for_break(self_inner):
                return False

            def next_delay(self_inner):
                return 0.001

        breaker = MagicMock()
        breaker.should_stop.return_value = False

        mock_item = MagicMock()
        mock_item.mpn = "XC7A35T"
        mock_item.id = 99

        def _claim(db):
            tick["n"] += 1
            if tick["n"] >= 1:
                wmod._shutdown_requested = True
            return mock_item

        async def _timed_out(*args, **kwargs):
            raise TimeoutError

        wmod._shutdown_requested = False
        try:
            with (
                patch(_DB_SESSION, return_value=ctx),
                patch(_UPDATE_STATUS),
                patch(_QUEUE_RECOVER),
                patch(_AI_GATE, new_callable=AsyncMock),
                patch(_QUEUE_CLAIM, side_effect=_claim),
                patch(_QUEUE_STATUS, side_effect=lambda db, item, s, **kw: statuses.append(s)),
                patch(_SESSION_MGR, return_value=sm),
                patch(_SCHEDULER, return_value=_Scheduler()),
                patch(_BREAKER, return_value=breaker),
                patch(_SLEEP, sleep_mock),
                patch(_SESSION_LOCAL, return_value=mock_db),
                patch("asyncio.wait_for", side_effect=_timed_out),
            ):
                await wmod.main()
        finally:
            wmod._shutdown_requested = False

        assert "failed" in statuses

    @pytest.mark.asyncio
    async def test_successful_search_marks_completed(self):
        """Happy path: search returns HTML, sightings written, item marked completed."""
        import app.services.tbf_worker.worker as wmod

        sm = _make_sm(is_logged_in=True)
        mock_db = _make_mock_db()
        ctx = _make_mock_ctx(mock_db)
        sleep_mock = AsyncMock()
        tick = {"n": 0}
        completed_calls = []

        class _Scheduler:
            def is_business_hours(self_inner):
                return True

            def time_for_break(self_inner):
                return False

            def next_delay(self_inner):
                return 0.001

        breaker = MagicMock()
        breaker.should_stop.return_value = False
        breaker.check_page_health = AsyncMock(return_value="OK")
        breaker.record_results = MagicMock()
        breaker.record_empty_results = MagicMock()

        mock_item = MagicMock()
        mock_item.mpn = "LM317T"
        mock_item.id = 77

        def _claim(db):
            tick["n"] += 1
            if tick["n"] >= 1:
                wmod._shutdown_requested = True
            return mock_item

        async def _search(page, mpn):
            return {"html": "<html>results</html>", "duration_ms": 50}

        sighting = MagicMock()

        wmod._shutdown_requested = False
        try:
            with (
                patch(_DB_SESSION, return_value=ctx),
                patch(_UPDATE_STATUS),
                patch(_QUEUE_RECOVER),
                patch(_AI_GATE, new_callable=AsyncMock),
                patch(_QUEUE_CLAIM, side_effect=_claim),
                patch(_QUEUE_COMPLETE, side_effect=lambda *a, **kw: completed_calls.append(kw)),
                patch(_QUEUE_STATUS),
                patch(_SESSION_MGR, return_value=sm),
                patch(_SCHEDULER, return_value=_Scheduler()),
                patch(_BREAKER, return_value=breaker),
                patch(_SLEEP, sleep_mock),
                patch(_SESSION_LOCAL, return_value=mock_db),
                patch(_SEARCH_PART, side_effect=_search),
                patch(_PARSE_HTML, return_value=[sighting]),
                patch(_SAVE_SIGHTINGS, return_value=1),
            ):
                await wmod.main()
        finally:
            wmod._shutdown_requested = False

        assert completed_calls, "mark_completed should have been called"

    @pytest.mark.asyncio
    async def test_session_expired_health_requeues_item(self):
        """SESSION_EXPIRED page health re-queues the item (mark_status with
        'queued')."""
        import app.services.tbf_worker.worker as wmod

        sm = _make_sm(is_logged_in=True)
        mock_db = _make_mock_db()
        ctx = _make_mock_ctx(mock_db)
        sleep_mock = AsyncMock()
        tick = {"n": 0}
        statuses = []

        class _Scheduler:
            def is_business_hours(self_inner):
                return True

            def time_for_break(self_inner):
                return False

            def next_delay(self_inner):
                return 0.001

        breaker = MagicMock()
        breaker.should_stop.return_value = False
        breaker.check_page_health = AsyncMock(return_value="SESSION_EXPIRED")

        mock_item = MagicMock()
        mock_item.mpn = "ATmega328P"
        mock_item.id = 55

        def _claim(db):
            tick["n"] += 1
            if tick["n"] >= 1:
                wmod._shutdown_requested = True
            return mock_item

        async def _search(page, mpn):
            return {"html": "<html/>", "duration_ms": 10}

        wmod._shutdown_requested = False
        try:
            with (
                patch(_DB_SESSION, return_value=ctx),
                patch(_UPDATE_STATUS),
                patch(_QUEUE_RECOVER),
                patch(_AI_GATE, new_callable=AsyncMock),
                patch(_QUEUE_CLAIM, side_effect=_claim),
                patch(_QUEUE_STATUS, side_effect=lambda db, item, s, **kw: statuses.append(s)),
                patch(_SESSION_MGR, return_value=sm),
                patch(_SCHEDULER, return_value=_Scheduler()),
                patch(_BREAKER, return_value=breaker),
                patch(_SLEEP, sleep_mock),
                patch(_SESSION_LOCAL, return_value=mock_db),
                patch(_SEARCH_PART, side_effect=_search),
            ):
                await wmod.main()
        finally:
            wmod._shutdown_requested = False

        assert "queued" in statuses
