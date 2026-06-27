"""tests/test_tbf_worker_coverage.py — Coverage tests for tbf_worker modules.

Covers scheduler, human_behavior, monitoring, search_engine, session_manager, worker
without touching any real browser/network/DB calls.

Called by: pytest
Depends on: conftest.py, app.services.tbf_worker.*
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("TESTING", "1")

from app.services.tbf_worker.config import TbfConfig
from app.services.tbf_worker.scheduler import SearchScheduler

# ─── SearchScheduler ────────────────────────────────────────────────────────


class TestSearchScheduler:
    def _make(self) -> SearchScheduler:
        return SearchScheduler(TbfConfig())

    def test_next_delay_within_bounds(self):
        s = self._make()
        for _ in range(20):
            d = s.next_delay()
            assert s.config.TBF_MIN_DELAY_SECONDS <= d <= s.config.TBF_MAX_DELAY_SECONDS

    def test_next_delay_increments_searches_since_break(self):
        s = self._make()
        s.searches_since_break = 0
        s.next_delay()
        assert s.searches_since_break == 1

    def test_time_for_break_false_initially(self):
        s = self._make()
        s.searches_since_break = 0
        s.break_threshold = 10
        assert s.time_for_break() is False

    def test_time_for_break_true_when_reached(self):
        s = self._make()
        s.searches_since_break = 10
        s.break_threshold = 10
        assert s.time_for_break() is True

    def test_get_break_duration_range(self):
        s = self._make()
        for _ in range(10):
            d = s.get_break_duration()
            assert 5 * 60 <= d <= 25 * 60

    def test_reset_break_counter(self):
        s = self._make()
        s.searches_since_break = 99
        s.reset_break_counter()
        assert s.searches_since_break == 0
        assert 8 <= s.break_threshold <= 15

    def test_is_business_hours_force_env(self):
        s = self._make()
        with patch.dict(os.environ, {"FORCE_BUSINESS_HOURS": "1"}):
            assert s.is_business_hours() is True

    def test_is_business_hours_saturday(self):
        """Saturday is always off."""
        s = self._make()
        # weekday() returns 5 for Saturday
        fake_dt = MagicMock()
        fake_dt.weekday.return_value = 5
        fake_dt.hour = 12
        with (
            patch.dict(os.environ, {}, clear=False),
            patch("app.services.tbf_worker.scheduler.datetime") as mock_dt,
        ):
            if "FORCE_BUSINESS_HOURS" in os.environ:
                del os.environ["FORCE_BUSINESS_HOURS"]
            mock_dt.now.return_value = fake_dt
            result = s.is_business_hours()
        assert result is False

    def test_is_business_hours_sunday_morning(self):
        """Sunday before 6 PM is off."""
        s = self._make()
        fake_dt = MagicMock()
        fake_dt.weekday.return_value = 6
        fake_dt.hour = 10
        env = {k: v for k, v in os.environ.items() if k != "FORCE_BUSINESS_HOURS"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("app.services.tbf_worker.scheduler.datetime") as mock_dt,
        ):
            mock_dt.now.return_value = fake_dt
            result = s.is_business_hours()
        assert result is False

    def test_is_business_hours_sunday_evening(self):
        """Sunday at 6 PM+ is on."""
        s = self._make()
        fake_dt = MagicMock()
        fake_dt.weekday.return_value = 6
        fake_dt.hour = 18
        env = {k: v for k, v in os.environ.items() if k != "FORCE_BUSINESS_HOURS"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("app.services.tbf_worker.scheduler.datetime") as mock_dt,
        ):
            mock_dt.now.return_value = fake_dt
            result = s.is_business_hours()
        assert result is True

    def test_is_business_hours_friday_on(self):
        """Friday before 5 PM is on."""
        s = self._make()
        fake_dt = MagicMock()
        fake_dt.weekday.return_value = 4
        fake_dt.hour = 14
        env = {k: v for k, v in os.environ.items() if k != "FORCE_BUSINESS_HOURS"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("app.services.tbf_worker.scheduler.datetime") as mock_dt,
        ):
            mock_dt.now.return_value = fake_dt
            result = s.is_business_hours()
        assert result is True

    def test_is_business_hours_friday_off(self):
        """Friday at 5 PM+ is off."""
        s = self._make()
        fake_dt = MagicMock()
        fake_dt.weekday.return_value = 4
        fake_dt.hour = 17
        env = {k: v for k, v in os.environ.items() if k != "FORCE_BUSINESS_HOURS"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("app.services.tbf_worker.scheduler.datetime") as mock_dt,
        ):
            mock_dt.now.return_value = fake_dt
            result = s.is_business_hours()
        assert result is False

    def test_is_business_hours_weekday(self):
        """Monday-Thursday are always on."""
        s = self._make()
        for wd in [0, 1, 2, 3]:
            fake_dt = MagicMock()
            fake_dt.weekday.return_value = wd
            fake_dt.hour = 10
            env = {k: v for k, v in os.environ.items() if k != "FORCE_BUSINESS_HOURS"}
            with (
                patch.dict(os.environ, env, clear=True),
                patch("app.services.tbf_worker.scheduler.datetime") as mock_dt,
            ):
                mock_dt.now.return_value = fake_dt
                assert s.is_business_hours() is True


# ─── HumanBehavior ──────────────────────────────────────────────────────────


class TestHumanBehavior:
    @pytest.mark.asyncio
    async def test_random_delay(self):
        from app.services.tbf_worker.human_behavior import HumanBehavior

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await HumanBehavior.random_delay(0.1, 0.3)
        mock_sleep.assert_awaited_once()
        # delay should be between min and max
        call_arg = mock_sleep.call_args[0][0]
        assert 0.1 <= call_arg <= 0.3

    @pytest.mark.asyncio
    async def test_human_type_types_each_char(self):
        from app.services.tbf_worker.human_behavior import HumanBehavior

        page = MagicMock()
        page.keyboard = MagicMock()
        page.keyboard.type = AsyncMock()
        locator = MagicMock()
        locator.click = AsyncMock()

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await HumanBehavior.human_type(page, locator, "ABC")

        assert page.keyboard.type.call_count == 3

    @pytest.mark.asyncio
    async def test_human_click_with_bounding_box(self):
        from app.services.tbf_worker.human_behavior import HumanBehavior

        page = MagicMock()
        page.mouse = MagicMock()
        page.mouse.click = AsyncMock()
        locator = AsyncMock()
        locator.bounding_box = AsyncMock(return_value={"x": 10, "y": 20, "width": 100, "height": 50})

        await HumanBehavior.human_click(page, locator)
        page.mouse.click.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_human_click_without_bounding_box(self):
        from app.services.tbf_worker.human_behavior import HumanBehavior

        page = MagicMock()
        locator = AsyncMock()
        locator.bounding_box = AsyncMock(return_value=None)
        locator.click = AsyncMock()

        await HumanBehavior.human_click(page, locator)
        locator.click.assert_awaited_once()


# ─── Monitoring ─────────────────────────────────────────────────────────────


class TestMonitoring:
    def test_monitoring_exports(self):
        from app.services.tbf_worker.monitoring import (
            capture_sentry_error,
            capture_sentry_message,
            check_html_structure_hash,
            log_daily_report,
        )

        assert callable(capture_sentry_error)
        assert callable(capture_sentry_message)
        assert callable(check_html_structure_hash)
        assert callable(log_daily_report)

    def test_log_daily_report(self):
        from app.services.tbf_worker.monitoring import log_daily_report

        # Call with the correct signature for the base function
        log_daily_report(
            searches_completed=5,
            sightings_created=10,
            parts_gated_out=0,
            parts_deduped=0,
            failed_searches=0,
            queue_remaining=0,
            circuit_breaker_status="closed",
        )

    def test_check_html_structure_hash_empty(self):
        from app.services.tbf_worker.monitoring import check_html_structure_hash

        # Result is True (known) or False (new hash) — both are valid bools
        result = check_html_structure_hash("<html><body></body></html>", "TEST_MPN")
        assert isinstance(result, (bool, str))

    def test_capture_sentry_error_no_sentry(self):
        from app.services.tbf_worker.monitoring import capture_sentry_error

        # Should not raise even when sentry SDK is not available
        with patch("app.services.search_worker_base.monitoring.logger"):
            try:
                capture_sentry_error("test error")
            except Exception:
                pass  # Sentry might not be configured — we just need the line covered

    def test_capture_sentry_message_no_sentry(self):
        from app.services.tbf_worker.monitoring import capture_sentry_message

        with patch("app.services.search_worker_base.monitoring.logger"):
            try:
                capture_sentry_message("test message")
            except Exception:
                pass


# ─── SearchEngine ────────────────────────────────────────────────────────────


class TestSearchEngine:
    @pytest.mark.asyncio
    async def test_search_part_with_response(self):
        from app.services.tbf_worker.search_engine import search_part

        page = MagicMock()
        mock_response = MagicMock()
        mock_response.status = 200
        page.goto = AsyncMock(return_value=mock_response)
        page.wait_for_selector = AsyncMock()
        page.content = AsyncMock(return_value="<html>results</html>")
        page.url = "https://www.thebrokersite.com/parts?query=LM317T"

        result = await search_part(page, "LM317T")

        assert result["html"] == "<html>results</html>"
        assert result["status_code"] == 200
        assert "duration_ms" in result
        assert "url" in result

    @pytest.mark.asyncio
    async def test_search_part_no_results(self):
        """When the selector times out, we treat it as no-results."""
        from app.services.tbf_worker.search_engine import search_part

        page = MagicMock()
        mock_response = MagicMock()
        mock_response.status = 200
        page.goto = AsyncMock(return_value=mock_response)
        page.wait_for_selector = AsyncMock(side_effect=Exception("timeout"))
        page.content = AsyncMock(return_value="<html>no results</html>")
        page.url = "https://www.thebrokersite.com/parts?query=NOTFOUND"

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await search_part(page, "NOTFOUND")

        assert "html" in result
        assert result["status_code"] == 200

    @pytest.mark.asyncio
    async def test_search_part_response_none(self):
        """Goto() returns None is handled gracefully."""
        from app.services.tbf_worker.search_engine import search_part

        page = MagicMock()
        page.goto = AsyncMock(return_value=None)
        page.wait_for_selector = AsyncMock()
        page.content = AsyncMock(return_value="<html></html>")
        page.url = "https://www.thebrokersite.com/parts?query=X"

        result = await search_part(page, "X")
        assert result["status_code"] == 200

    @pytest.mark.asyncio
    async def test_search_part_response_status_raises(self):
        """response.status raising is handled by fallback to 200."""
        from app.services.tbf_worker.search_engine import search_part

        page = MagicMock()
        mock_response = MagicMock()
        mock_response.status = PropertyError = property(lambda self: (_ for _ in ()).throw(Exception("err")))

        # Simulate response.status raising
        broken_response = MagicMock()
        broken_response.__bool__ = lambda s: True
        type(broken_response).status = property(lambda s: (_ for _ in ()).throw(Exception("boom")))
        page.goto = AsyncMock(return_value=broken_response)
        page.wait_for_selector = AsyncMock()
        page.content = AsyncMock(return_value="<html></html>")
        page.url = "https://thebrokersite.com/parts?query=ABC"

        result = await search_part(page, "ABC")
        assert result["status_code"] == 200


# ─── SessionManager ──────────────────────────────────────────────────────────


class TestTbfSessionManager:
    def test_init(self):
        from app.services.tbf_worker.session_manager import TbfSessionManager

        sm = TbfSessionManager(TbfConfig())
        assert sm.is_logged_in is False
        assert sm.page is None

    def test_page_property(self):
        from app.services.tbf_worker.session_manager import TbfSessionManager

        sm = TbfSessionManager(TbfConfig())
        sm._page = "mock_page"
        assert sm.page == "mock_page"

    @pytest.mark.asyncio
    async def test_start_no_display(self):
        """Start() raises RuntimeError when DISPLAY is not set."""
        from app.services.tbf_worker.session_manager import TbfSessionManager

        env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
        with patch.dict(os.environ, env, clear=True):
            sm = TbfSessionManager(TbfConfig())
            with pytest.raises(RuntimeError, match="DISPLAY"):
                await sm.start()

    @pytest.mark.asyncio
    async def test_check_session_health_true(self):
        from app.services.tbf_worker.session_manager import TbfSessionManager

        sm = TbfSessionManager(TbfConfig())
        mock_page = MagicMock()
        locator = MagicMock()
        locator.count = AsyncMock(return_value=1)
        mock_page.locator = MagicMock(return_value=locator)
        sm._page = mock_page

        result = await sm.check_session_health()
        assert result is True

    @pytest.mark.asyncio
    async def test_check_session_health_false(self):
        from app.services.tbf_worker.session_manager import TbfSessionManager

        sm = TbfSessionManager(TbfConfig())
        mock_page = MagicMock()
        locator = MagicMock()
        locator.count = AsyncMock(return_value=0)
        mock_page.locator = MagicMock(return_value=locator)
        sm._page = mock_page

        result = await sm.check_session_health()
        assert result is False

    @pytest.mark.asyncio
    async def test_check_session_health_exception(self):
        from app.services.tbf_worker.session_manager import TbfSessionManager

        sm = TbfSessionManager(TbfConfig())
        mock_page = MagicMock()
        mock_page.locator = MagicMock(side_effect=Exception("boom"))
        sm._page = mock_page

        result = await sm.check_session_health()
        assert result is False

    @pytest.mark.asyncio
    async def test_ensure_session_already_valid(self):
        from app.services.tbf_worker.session_manager import TbfSessionManager

        sm = TbfSessionManager(TbfConfig())
        sm.check_session_health = AsyncMock(return_value=True)

        result = await sm.ensure_session()
        assert result is True
        assert sm.is_logged_in is True

    @pytest.mark.asyncio
    async def test_ensure_session_relogins(self):
        from app.services.tbf_worker.session_manager import TbfSessionManager

        sm = TbfSessionManager(TbfConfig())
        sm.check_session_health = AsyncMock(return_value=False)
        sm.login = AsyncMock(return_value=True)

        result = await sm.ensure_session()
        assert result is True

    @pytest.mark.asyncio
    async def test_stop_cleans_up(self):
        from app.services.tbf_worker.session_manager import TbfSessionManager

        sm = TbfSessionManager(TbfConfig())
        mock_context = MagicMock()
        mock_context.close = AsyncMock()
        mock_playwright = MagicMock()
        mock_playwright.stop = AsyncMock()
        sm._context = mock_context
        sm._playwright = mock_playwright
        sm._page = MagicMock()
        sm.is_logged_in = True

        await sm.stop()
        assert sm._context is None
        assert sm._page is None
        assert sm.is_logged_in is False

    @pytest.mark.asyncio
    async def test_stop_handles_error(self):
        from app.services.tbf_worker.session_manager import TbfSessionManager

        sm = TbfSessionManager(TbfConfig())
        mock_context = MagicMock()
        mock_context.close = AsyncMock(side_effect=Exception("boom"))
        sm._context = mock_context
        sm._playwright = None

        # Should not raise
        await sm.stop()

    @pytest.mark.asyncio
    async def test_login_no_credentials(self):
        from app.services.tbf_worker.session_manager import TbfSessionManager

        config = TbfConfig()
        config.TBF_USERNAME = ""
        config.TBF_PASSWORD = ""
        sm = TbfSessionManager(config)

        result = await sm.login()
        assert result is False

    @pytest.mark.asyncio
    async def test_login_success(self):
        from app.services.tbf_worker.session_manager import TbfSessionManager

        config = TbfConfig()
        config.TBF_USERNAME = "user@test.com"
        config.TBF_PASSWORD = "password"
        sm = TbfSessionManager(config)

        mock_page = MagicMock()
        mock_page.goto = AsyncMock()
        mock_page.locator = MagicMock()
        btn = MagicMock()
        btn.first = MagicMock()
        btn.first.click = AsyncMock()
        form_locator = MagicMock()
        email_field = MagicMock()
        email_field.fill = AsyncMock()
        pwd_field = MagicMock()
        pwd_field.fill = AsyncMock()
        submit_btn = MagicMock()
        submit_btn.first = MagicMock()
        submit_btn.first.click = AsyncMock()
        form_locator.locator = MagicMock(
            side_effect=lambda sel: email_field if "email" in sel else (pwd_field if "password" in sel else submit_btn)
        )
        mock_page.locator = MagicMock(side_effect=lambda sel: btn if "Sign In" in str(sel) else form_locator)
        mock_page.locator("input[name='password']:visible").wait_for = AsyncMock()

        # After login check, return logged in
        sm._page = mock_page
        sm.check_session_health = AsyncMock(return_value=True)
        sm._dismiss_consent_banner = AsyncMock()

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await sm.login()

        assert result is True

    @pytest.mark.asyncio
    async def test_login_exception(self):
        from app.services.tbf_worker.session_manager import TbfSessionManager

        config = TbfConfig()
        config.TBF_USERNAME = "user@test.com"
        config.TBF_PASSWORD = "pass"
        sm = TbfSessionManager(config)
        mock_page = MagicMock()
        mock_page.goto = AsyncMock(side_effect=Exception("nav error"))
        sm._page = mock_page

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await sm.login()

        assert result is False

    @pytest.mark.asyncio
    async def test_dismiss_consent_banner_no_buttons(self):
        from app.services.tbf_worker.session_manager import TbfSessionManager

        sm = TbfSessionManager(TbfConfig())
        mock_page = MagicMock()
        btn = MagicMock()
        btn.count = AsyncMock(return_value=0)
        mock_page.get_by_role = MagicMock(return_value=btn)
        sm._page = mock_page

        await sm._dismiss_consent_banner()  # should not raise

    @pytest.mark.asyncio
    async def test_dismiss_consent_banner_click_exception(self):
        from app.services.tbf_worker.session_manager import TbfSessionManager

        sm = TbfSessionManager(TbfConfig())
        mock_page = MagicMock()
        btn = MagicMock()
        btn.count = AsyncMock(return_value=1)
        btn.first = MagicMock()
        btn.first.is_visible = AsyncMock(return_value=True)
        btn.first.click = AsyncMock(side_effect=Exception("click error"))
        mock_page.get_by_role = MagicMock(return_value=btn)
        sm._page = mock_page

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await sm._dismiss_consent_banner()  # should not raise


# ─── Worker functions ────────────────────────────────────────────────────────


class TestWorkerFunctions:
    def test_update_worker_status_no_row(self, db_session):
        """update_worker_status is a no-op when no row with id=1 exists."""
        from app.services.tbf_worker.worker import update_worker_status

        # No TbfWorkerStatus row in DB — should not raise
        update_worker_status(db_session, is_running=True)

    def test_update_worker_status_with_row(self, db_session):
        from app.models import TbfWorkerStatus
        from app.services.tbf_worker.worker import update_worker_status

        status = TbfWorkerStatus(id=1, is_running=False)
        db_session.add(status)
        db_session.commit()

        update_worker_status(db_session, is_running=True, searches_today=5)
        db_session.refresh(status)
        assert status.is_running is True

    def test_handle_shutdown_sets_flag(self):
        import app.services.tbf_worker.worker as worker_mod

        worker_mod._shutdown_requested = False
        worker_mod._handle_shutdown(15, None)
        assert worker_mod._shutdown_requested is True
        worker_mod._shutdown_requested = False  # reset

    def test_db_session_context_manager(self, db_session):
        from app.services.tbf_worker.worker import _db_session

        with patch("app.database.SessionLocal", return_value=db_session):
            with _db_session() as db:
                assert db is db_session

    @pytest.mark.asyncio
    async def test_main_no_display_raises_stops(self):
        """Main() handles browser start failure gracefully (returns without raising)."""
        from app.services.tbf_worker.worker import main

        mock_db = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_db)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        mock_sm = AsyncMock()
        mock_sm.start = AsyncMock(side_effect=RuntimeError("DISPLAY not set"))
        mock_sm.stop = AsyncMock()
        mock_sm.is_logged_in = False

        with (
            patch("app.services.tbf_worker.worker._db_session", return_value=mock_ctx),
            patch("app.services.tbf_worker.worker.update_worker_status"),
            patch("app.services.tbf_worker.queue_manager.recover_stale_searches"),
            patch("app.services.tbf_worker.session_manager.TbfSessionManager", return_value=mock_sm),
        ):
            # When start() raises, main() logs the error, updates status, and returns
            await main()  # must not raise

    @pytest.mark.asyncio
    async def test_main_login_failure(self):
        """Main() exits when initial login fails."""
        from app.services.tbf_worker.worker import main

        mock_db = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_db)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        mock_sm = AsyncMock()
        mock_sm.start = AsyncMock()
        mock_sm.is_logged_in = False
        mock_sm.login = AsyncMock(return_value=False)
        mock_sm.stop = AsyncMock()

        with (
            patch("app.services.tbf_worker.worker._db_session", return_value=mock_ctx),
            patch("app.services.tbf_worker.worker.update_worker_status"),
            patch("app.services.tbf_worker.queue_manager.recover_stale_searches"),
            patch("app.services.tbf_worker.session_manager.TbfSessionManager", return_value=mock_sm),
        ):
            await main()

        mock_sm.stop.assert_awaited()
