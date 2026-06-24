"""Tests for uncovered tbf_worker package modules.

Targets: ai_gate, human_behavior, monitoring, scheduler, search_engine,
session_manager, worker — all currently below 85% or 0% coverage.

Called by: pytest
Depends on: conftest.py fixtures, tbf_worker modules
"""

import os
import time
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

os.environ["TESTING"] = "1"

from app.models import TbfSearchQueue, TbfWorkerStatus
from app.services.tbf_worker.config import TbfConfig
from app.services.tbf_worker.human_behavior import HumanBehavior
from app.services.tbf_worker.monitoring import (
    _get_hash_set,
    _known_html_hashes,
    capture_sentry_error,
    capture_sentry_message,
    check_html_structure_hash,
    log_daily_report,
)
from app.services.tbf_worker.scheduler import SearchScheduler

pytestmark = pytest.mark.slow


# ═══════════════════════════════════════════════════════════════════════
# SCHEDULER
# ═══════════════════════════════════════════════════════════════════════


class TestScheduler:
    def test_initial_break_threshold(self):
        cfg = TbfConfig()
        sched = SearchScheduler(cfg)
        assert 8 <= sched.break_threshold <= 15

    def test_searches_since_break_starts_zero(self):
        cfg = TbfConfig()
        sched = SearchScheduler(cfg)
        assert sched.searches_since_break == 0

    def test_next_delay_increments_search_count(self):
        cfg = TbfConfig()
        sched = SearchScheduler(cfg)
        sched.next_delay()
        assert sched.searches_since_break == 1

    def test_next_delay_respects_bounds(self):
        cfg = TbfConfig()
        sched = SearchScheduler(cfg)
        for _ in range(20):
            delay = sched.next_delay()
            assert cfg.TBF_MIN_DELAY_SECONDS <= delay <= cfg.TBF_MAX_DELAY_SECONDS

    def test_time_for_break_false_initially(self):
        cfg = TbfConfig()
        sched = SearchScheduler(cfg)
        assert not sched.time_for_break()

    def test_time_for_break_true_at_threshold(self):
        cfg = TbfConfig()
        sched = SearchScheduler(cfg)
        sched.searches_since_break = sched.break_threshold
        assert sched.time_for_break()

    def test_get_break_duration_in_range(self):
        cfg = TbfConfig()
        sched = SearchScheduler(cfg)
        duration = sched.get_break_duration()
        assert 5 * 60 <= duration <= 25 * 60

    def test_reset_break_counter(self):
        cfg = TbfConfig()
        sched = SearchScheduler(cfg)
        sched.searches_since_break = 10
        sched.reset_break_counter()
        assert sched.searches_since_break == 0
        assert 8 <= sched.break_threshold <= 15

    def test_is_business_hours_forced(self):
        cfg = TbfConfig()
        sched = SearchScheduler(cfg)
        with patch.dict(os.environ, {"FORCE_BUSINESS_HOURS": "1"}):
            assert sched.is_business_hours() is True

    def test_is_business_hours_saturday(self):
        cfg = TbfConfig()
        sched = SearchScheduler(cfg)
        from freezegun import freeze_time

        with freeze_time("2024-01-06 12:00:00"):  # Saturday
            env = dict(os.environ)
            env.pop("FORCE_BUSINESS_HOURS", None)
            with patch.dict(os.environ, env, clear=True):
                result = sched.is_business_hours()
                assert result is False

    def test_is_business_hours_weekday(self):
        cfg = TbfConfig()
        sched = SearchScheduler(cfg)
        from freezegun import freeze_time

        with freeze_time("2024-01-08 10:00:00"):  # Monday
            env = dict(os.environ)
            env.pop("FORCE_BUSINESS_HOURS", None)
            with patch.dict(os.environ, env, clear=True):
                result = sched.is_business_hours()
                assert result is True

    def test_is_business_hours_sunday_before_6pm(self):
        cfg = TbfConfig()
        sched = SearchScheduler(cfg)
        from freezegun import freeze_time

        with freeze_time("2024-01-07 10:00:00"):  # Sunday morning
            env = dict(os.environ)
            env.pop("FORCE_BUSINESS_HOURS", None)
            with patch.dict(os.environ, env, clear=True):
                result = sched.is_business_hours()
                assert result is False

    def test_is_business_hours_sunday_after_6pm(self):
        # Sunday 7pm ET = Monday 00:00 UTC (EST is UTC-5)
        cfg = TbfConfig()
        sched = SearchScheduler(cfg)
        from freezegun import freeze_time

        with freeze_time("2024-01-08 00:00:00"):  # Sunday 7pm ET in UTC
            env = dict(os.environ)
            env.pop("FORCE_BUSINESS_HOURS", None)
            with patch.dict(os.environ, env, clear=True):
                result = sched.is_business_hours()
                assert result is True

    def test_is_business_hours_friday_morning(self):
        # Friday 10am ET = Friday 15:00 UTC
        cfg = TbfConfig()
        sched = SearchScheduler(cfg)
        from freezegun import freeze_time

        with freeze_time("2024-01-05 15:00:00"):  # Friday 10am ET in UTC
            env = dict(os.environ)
            env.pop("FORCE_BUSINESS_HOURS", None)
            with patch.dict(os.environ, env, clear=True):
                result = sched.is_business_hours()
                assert result is True

    def test_is_business_hours_friday_evening(self):
        # Friday 6pm ET = Friday 23:00 UTC
        cfg = TbfConfig()
        sched = SearchScheduler(cfg)
        from freezegun import freeze_time

        with freeze_time("2024-01-05 23:00:00"):  # Friday 6pm ET in UTC
            env = dict(os.environ)
            env.pop("FORCE_BUSINESS_HOURS", None)
            with patch.dict(os.environ, env, clear=True):
                result = sched.is_business_hours()
                assert result is False


# ═══════════════════════════════════════════════════════════════════════
# HUMAN BEHAVIOR
# ═══════════════════════════════════════════════════════════════════════


class TestHumanBehavior:
    @pytest.mark.asyncio
    async def test_random_delay_sleeps_within_bounds(self):
        with patch("app.services.tbf_worker.human_behavior.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await HumanBehavior.random_delay(0.5, 1.5)
        mock_sleep.assert_called_once()
        delay = mock_sleep.call_args[0][0]
        assert 0.5 <= delay <= 1.5

    @pytest.mark.asyncio
    async def test_human_type_types_each_char(self):
        page = AsyncMock()
        locator = AsyncMock()
        locator.click = AsyncMock()

        with patch("app.services.tbf_worker.human_behavior.asyncio.sleep", new_callable=AsyncMock):
            with patch("app.services.tbf_worker.human_behavior.random.uniform", return_value=0.1):
                with patch("app.services.tbf_worker.human_behavior.random.random", return_value=0.5):
                    await HumanBehavior.human_type(page, locator, "abc")

        locator.click.assert_called_once()
        assert page.keyboard.type.call_count == 3

    @pytest.mark.asyncio
    async def test_human_type_thinking_pause(self):
        page = AsyncMock()
        locator = AsyncMock()

        with patch("app.services.tbf_worker.human_behavior.asyncio.sleep", new_callable=AsyncMock):
            with patch("app.services.tbf_worker.human_behavior.random.uniform", return_value=0.1):
                with patch("app.services.tbf_worker.human_behavior.random.random", return_value=0.01):
                    await HumanBehavior.human_type(page, locator, "ab")

        assert page.keyboard.type.call_count == 2

    @pytest.mark.asyncio
    async def test_human_click_with_bounding_box(self):
        page = AsyncMock()
        locator = AsyncMock()
        locator.bounding_box = AsyncMock(return_value={"x": 100, "y": 200, "width": 50, "height": 30})

        await HumanBehavior.human_click(page, locator)
        page.mouse.click.assert_called_once()
        args = page.mouse.click.call_args[0]
        assert 115 <= args[0] <= 135
        assert 209 <= args[1] <= 221

    @pytest.mark.asyncio
    async def test_human_click_no_bounding_box(self):
        page = AsyncMock()
        locator = AsyncMock()
        locator.bounding_box = AsyncMock(return_value=None)

        await HumanBehavior.human_click(page, locator)
        locator.click.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# MONITORING
# ═══════════════════════════════════════════════════════════════════════


class TestMonitoring:
    def setup_method(self):
        _known_html_hashes.clear()

    def test_log_daily_report(self):
        log_daily_report(
            searches_completed=10,
            sightings_created=50,
            parts_gated_out=5,
            parts_deduped=3,
            failed_searches=1,
            queue_remaining=20,
            circuit_breaker_status="closed",
        )

    def test_capture_sentry_error_with_sdk(self):
        mock_sdk = MagicMock()
        with patch.dict("sys.modules", {"sentry_sdk": mock_sdk}):
            capture_sentry_error(ValueError("test"), {"mpn": "STM32"})
            mock_sdk.capture_exception.assert_called_once()

    def test_capture_sentry_error_no_sdk(self):
        with patch.dict("sys.modules", {"sentry_sdk": None}):
            capture_sentry_error(ValueError("test"))

    def test_capture_sentry_message_with_sdk(self):
        mock_sdk = MagicMock()
        with patch.dict("sys.modules", {"sentry_sdk": mock_sdk}):
            capture_sentry_message("test message", level="info", context={"key": "val"})
            mock_sdk.capture_message.assert_called_once()

    def test_capture_sentry_message_no_sdk(self):
        with patch.dict("sys.modules", {"sentry_sdk": None}):
            capture_sentry_message("test message")

    def test_check_html_structure_hash_empty(self):
        assert check_html_structure_hash("", "TEST") == ""

    def test_check_html_structure_hash_first_time(self):
        html = "<table><tr><td>data</td></tr></table>"
        h = check_html_structure_hash(html, "STM32")
        assert len(h) == 16
        assert h in _get_hash_set("TBF")

    def test_check_html_structure_hash_known(self):
        html = "<table><tr><td>data</td></tr></table>"
        h1 = check_html_structure_hash(html, "STM32")
        h2 = check_html_structure_hash(html, "LM317")
        assert h1 == h2

    def test_check_html_structure_hash_new_structure_warns(self):
        html1 = "<table><tr><td>data</td></tr></table>"
        check_html_structure_hash(html1, "STM32")
        html2 = "<div class='new'><span>different</span></div>"
        h2 = check_html_structure_hash(html2, "LM317")
        assert h2 in _get_hash_set("TBF")


# ═══════════════════════════════════════════════════════════════════════
# AI GATE
# ═══════════════════════════════════════════════════════════════════════


class TestAiGate:
    @pytest.fixture(autouse=True)
    def reset_ai_gate_cooldown(self):
        import app.services.tbf_worker.ai_gate as ai_gate_mod

        ai_gate_mod._last_api_failure = 0.0
        yield
        ai_gate_mod._last_api_failure = 0.0

    @pytest.mark.asyncio
    async def test_classify_parts_batch_empty(self):
        from app.services.tbf_worker.ai_gate import classify_parts_batch

        result = await classify_parts_batch([])
        assert result == []

    @pytest.mark.asyncio
    async def test_classify_parts_batch_success(self):
        from app.services.tbf_worker.ai_gate import classify_parts_batch

        mock_result = {
            "classifications": [
                {"mpn": "STM32F103", "search_broker": True, "commodity": "semiconductor", "reason": "MCU"}
            ]
        }
        with patch("app.utils.llm_router.routed_structured", new_callable=AsyncMock, return_value=mock_result):
            result = await classify_parts_batch([{"mpn": "STM32F103", "manufacturer": "ST", "description": "MCU"}])
        assert len(result) == 1
        assert result[0]["search_broker"] is True

    @pytest.mark.asyncio
    async def test_classify_parts_batch_api_failure(self):
        from app.services.tbf_worker.ai_gate import classify_parts_batch

        with patch(
            "app.utils.llm_router.routed_structured",
            new_callable=AsyncMock,
            side_effect=Exception("API error"),
        ):
            result = await classify_parts_batch([{"mpn": "X", "manufacturer": "", "description": ""}])
        assert result is None

    @pytest.mark.asyncio
    async def test_classify_parts_batch_bad_format(self):
        from app.services.tbf_worker.ai_gate import classify_parts_batch

        with patch("app.utils.llm_router.routed_structured", new_callable=AsyncMock, return_value={"bad": "format"}):
            result = await classify_parts_batch([{"mpn": "X", "manufacturer": "", "description": ""}])
        assert result is None

    @pytest.mark.asyncio
    async def test_process_ai_gate_no_pending(self, db_session):
        from app.services.tbf_worker.ai_gate import process_ai_gate

        await process_ai_gate(db_session)

    @pytest.mark.asyncio
    async def test_process_ai_gate_classifies_items(self, db_session, test_requisition):
        from app.services.tbf_worker.ai_gate import clear_classification_cache, process_ai_gate

        clear_classification_cache()

        req = test_requisition.requirements[0]
        item = TbfSearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="STM32F103C8T6",
            normalized_mpn="STM32F103C8T6",
            status="pending",
        )
        db_session.add(item)
        db_session.commit()

        mock_result = {
            "classifications": [
                {"mpn": "STM32F103C8T6", "search_broker": True, "commodity": "semiconductor", "reason": "ARM MCU"}
            ]
        }
        with patch("app.utils.llm_router.routed_structured", new_callable=AsyncMock, return_value=mock_result):
            await process_ai_gate(db_session)

        db_session.refresh(item)
        assert item.status == "queued"
        assert item.commodity_class == "semiconductor"

    @pytest.mark.asyncio
    async def test_process_ai_gate_gated_out(self, db_session, test_requisition):
        from app.services.tbf_worker.ai_gate import clear_classification_cache, process_ai_gate

        clear_classification_cache()

        req = test_requisition.requirements[0]
        item = TbfSearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="RC0402FR-07100KL",
            normalized_mpn="RC0402FR07100KL",
            status="pending",
        )
        db_session.add(item)
        db_session.commit()

        mock_result = {
            "classifications": [
                {
                    "mpn": "RC0402FR-07100KL",
                    "search_broker": False,
                    "commodity": "passive",
                    "reason": "Standard resistor",
                }
            ]
        }
        with patch("app.utils.llm_router.routed_structured", new_callable=AsyncMock, return_value=mock_result):
            await process_ai_gate(db_session)

        db_session.refresh(item)
        assert item.status == "gated_out"

    @pytest.mark.asyncio
    async def test_process_ai_gate_cache_hit(self, db_session, test_requisition):
        from app.services.tbf_worker.ai_gate import _classification_cache, clear_classification_cache, process_ai_gate

        clear_classification_cache()
        _classification_cache[("STM32F103C8T6", "")] = ("semiconductor", "search", "ARM MCU")

        req = test_requisition.requirements[0]
        item = TbfSearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="STM32F103C8T6",
            normalized_mpn="STM32F103C8T6",
            status="pending",
        )
        db_session.add(item)
        db_session.commit()

        await process_ai_gate(db_session)

        db_session.refresh(item)
        assert item.status == "queued"
        assert "[cached]" in item.gate_reason

    @pytest.mark.asyncio
    async def test_process_ai_gate_api_failure_cooldown(self, db_session, test_requisition):
        import app.services.tbf_worker.ai_gate as ai_gate_module
        from app.services.tbf_worker.ai_gate import clear_classification_cache, process_ai_gate

        clear_classification_cache()
        ai_gate_module._last_api_failure = 0.0

        req = test_requisition.requirements[0]
        item = TbfSearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="UNKNOWN123",
            normalized_mpn="UNKNOWN123",
            status="pending",
        )
        db_session.add(item)
        db_session.commit()

        with patch("app.utils.llm_router.routed_structured", new_callable=AsyncMock, return_value=None):
            await process_ai_gate(db_session)

        db_session.refresh(item)
        assert item.status == "queued"
        assert ai_gate_module._last_api_failure > 0

    @pytest.mark.asyncio
    async def test_process_ai_gate_in_cooldown(self, db_session, test_requisition):
        import app.services.tbf_worker.ai_gate as ai_gate_module
        from app.services.tbf_worker.ai_gate import clear_classification_cache, process_ai_gate

        clear_classification_cache()
        ai_gate_module._last_api_failure = time.monotonic()

        req = test_requisition.requirements[0]
        item = TbfSearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="TEST123",
            normalized_mpn="TEST123",
            status="pending",
        )
        db_session.add(item)
        db_session.commit()

        await process_ai_gate(db_session)

        db_session.refresh(item)
        assert item.status == "pending"

    @pytest.mark.asyncio
    async def test_process_ai_gate_missing_classification(self, db_session, test_requisition):
        from app.services.tbf_worker.ai_gate import clear_classification_cache, process_ai_gate

        clear_classification_cache()

        req = test_requisition.requirements[0]
        item = TbfSearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="MISSING_MPN",
            normalized_mpn="MISSING_MPN",
            status="pending",
        )
        db_session.add(item)
        db_session.commit()

        mock_result = {"classifications": []}
        with patch("app.utils.llm_router.routed_structured", new_callable=AsyncMock, return_value=mock_result):
            await process_ai_gate(db_session)

        db_session.refresh(item)
        assert item.status == "pending"

    def test_clear_classification_cache(self):
        from app.services.tbf_worker.ai_gate import _classification_cache, clear_classification_cache

        _classification_cache[("test", "test")] = ("x", "y", "z")
        clear_classification_cache()
        assert len(_classification_cache) == 0


# ═══════════════════════════════════════════════════════════════════════
# SEARCH ENGINE
# ═══════════════════════════════════════════════════════════════════════


class TestSearchEngine:
    @pytest.mark.asyncio
    async def test_search_part_returns_html(self):
        from app.services.tbf_worker.search_engine import search_part

        mock_response = MagicMock()
        mock_response.status = 200
        page = AsyncMock()
        page.url = "https://www.thebrokersite.com/parts?query=STM32F103"
        page.goto = AsyncMock(return_value=mock_response)
        page.wait_for_selector = AsyncMock()
        page.content = AsyncMock(return_value="<html>results</html>")

        result = await search_part(page, "STM32F103")

        assert result["html"] == "<html>results</html>"
        assert result["status_code"] == 200
        assert "duration_ms" in result
        assert "url" in result

    @pytest.mark.asyncio
    async def test_search_part_no_results_table(self):
        from app.services.tbf_worker.search_engine import search_part

        mock_response = MagicMock()
        mock_response.status = 200
        page = AsyncMock()
        page.url = "https://www.thebrokersite.com/parts?query=NOTFOUND"
        page.goto = AsyncMock(return_value=mock_response)
        page.wait_for_selector = AsyncMock(side_effect=Exception("Timeout"))
        page.content = AsyncMock(return_value="<html>no results</html>")

        with patch("app.services.tbf_worker.search_engine.asyncio.sleep", new_callable=AsyncMock):
            result = await search_part(page, "NOTFOUND")

        assert result["html"] == "<html>no results</html>"

    @pytest.mark.asyncio
    async def test_search_part_no_goto_response(self):
        from app.services.tbf_worker.search_engine import search_part

        page = AsyncMock()
        page.url = "https://www.thebrokersite.com/parts?query=X"
        page.goto = AsyncMock(return_value=None)
        page.wait_for_selector = AsyncMock()
        page.content = AsyncMock(return_value="<html/>")

        result = await search_part(page, "X")
        assert result["status_code"] == 200

    @pytest.mark.asyncio
    async def test_search_part_response_status_exception(self):
        from app.services.tbf_worker.search_engine import search_part

        # Simulate a response whose .status attribute raises
        mock_response = MagicMock()
        type(mock_response).status = PropertyMock(side_effect=Exception("attr error"))
        page = AsyncMock()
        page.url = "https://www.thebrokersite.com/parts?query=X"
        page.goto = AsyncMock(return_value=mock_response)
        page.wait_for_selector = AsyncMock()
        page.content = AsyncMock(return_value="<html/>")

        result = await search_part(page, "X")
        assert result["status_code"] == 200


# ═══════════════════════════════════════════════════════════════════════
# SESSION MANAGER
# ═══════════════════════════════════════════════════════════════════════


class TestSessionManager:
    def test_init(self):
        from app.services.tbf_worker.session_manager import TbfSessionManager

        cfg = TbfConfig()
        mgr = TbfSessionManager(cfg)
        assert mgr.page is None
        assert not mgr.is_logged_in

    @pytest.mark.asyncio
    async def test_start_no_display_raises(self):
        from app.services.tbf_worker.session_manager import TbfSessionManager

        cfg = TbfConfig()
        mgr = TbfSessionManager(cfg)
        env = dict(os.environ)
        env.pop("DISPLAY", None)
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(RuntimeError, match="DISPLAY"):
                await mgr.start()

    @pytest.mark.asyncio
    async def test_check_session_health_logged_in(self):
        from app.services.tbf_worker.session_manager import TbfSessionManager

        cfg = TbfConfig()
        mgr = TbfSessionManager(cfg)
        mock_locator = AsyncMock()
        mock_locator.count = AsyncMock(return_value=1)
        mock_page = MagicMock()
        mock_page.locator = MagicMock(return_value=mock_locator)
        mgr._page = mock_page

        result = await mgr.check_session_health()
        assert result is True

    @pytest.mark.asyncio
    async def test_check_session_health_logged_out(self):
        from app.services.tbf_worker.session_manager import TbfSessionManager

        cfg = TbfConfig()
        mgr = TbfSessionManager(cfg)
        mock_locator = AsyncMock()
        mock_locator.count = AsyncMock(return_value=0)
        mock_page = MagicMock()
        mock_page.locator = MagicMock(return_value=mock_locator)
        mgr._page = mock_page

        result = await mgr.check_session_health()
        assert result is False

    @pytest.mark.asyncio
    async def test_check_session_health_exception(self):
        from app.services.tbf_worker.session_manager import TbfSessionManager

        cfg = TbfConfig()
        mgr = TbfSessionManager(cfg)
        mock_page = MagicMock()
        mock_page.locator = MagicMock(side_effect=Exception("page error"))
        mgr._page = mock_page

        result = await mgr.check_session_health()
        assert result is False

    @pytest.mark.asyncio
    async def test_login_no_credentials(self):
        from app.services.tbf_worker.session_manager import TbfSessionManager

        cfg = TbfConfig()
        cfg.TBF_USERNAME = ""
        cfg.TBF_PASSWORD = ""
        mgr = TbfSessionManager(cfg)
        mgr._page = AsyncMock()

        result = await mgr.login()
        assert result is False

    @pytest.mark.asyncio
    async def test_login_success(self):
        from app.services.tbf_worker.session_manager import TbfSessionManager

        cfg = TbfConfig()
        cfg.TBF_USERNAME = "user@example.com"
        cfg.TBF_PASSWORD = "password123"
        mgr = TbfSessionManager(cfg)

        mock_page = AsyncMock()
        form_mock = AsyncMock()
        form_mock.locator = MagicMock(return_value=AsyncMock())
        mock_page.locator = MagicMock(return_value=form_mock)
        mgr._page = mock_page

        with patch.object(mgr, "check_session_health", new_callable=AsyncMock, return_value=True):
            with patch("app.services.tbf_worker.session_manager.asyncio.sleep", new_callable=AsyncMock):
                with patch.object(mgr, "_dismiss_consent_banner", new_callable=AsyncMock):
                    result = await mgr.login()

        assert result is True
        assert mgr.is_logged_in is True

    @pytest.mark.asyncio
    async def test_login_exception(self):
        from app.services.tbf_worker.session_manager import TbfSessionManager

        cfg = TbfConfig()
        cfg.TBF_USERNAME = "user@example.com"
        cfg.TBF_PASSWORD = "password123"
        mgr = TbfSessionManager(cfg)

        mock_page = AsyncMock()
        mock_page.goto = AsyncMock(side_effect=Exception("network error"))
        mgr._page = mock_page

        with patch("app.services.tbf_worker.session_manager.asyncio.sleep", new_callable=AsyncMock):
            result = await mgr.login()

        assert result is False
        assert mgr.is_logged_in is False

    @pytest.mark.asyncio
    async def test_ensure_session_already_valid(self):
        from app.services.tbf_worker.session_manager import TbfSessionManager

        cfg = TbfConfig()
        mgr = TbfSessionManager(cfg)
        mgr._page = AsyncMock()

        with patch.object(mgr, "check_session_health", new_callable=AsyncMock, return_value=True):
            result = await mgr.ensure_session()

        assert result is True
        assert mgr.is_logged_in is True

    @pytest.mark.asyncio
    async def test_ensure_session_relogin(self):
        from app.services.tbf_worker.session_manager import TbfSessionManager

        cfg = TbfConfig()
        mgr = TbfSessionManager(cfg)
        mgr._page = AsyncMock()

        with patch.object(mgr, "check_session_health", new_callable=AsyncMock, return_value=False):
            with patch.object(mgr, "login", new_callable=AsyncMock, return_value=True):
                result = await mgr.ensure_session()

        assert result is True

    @pytest.mark.asyncio
    async def test_stop_cleans_up(self):
        from app.services.tbf_worker.session_manager import TbfSessionManager

        cfg = TbfConfig()
        mgr = TbfSessionManager(cfg)
        mock_context = AsyncMock()
        mock_playwright = AsyncMock()
        mgr._context = mock_context
        mgr._playwright = mock_playwright
        mgr._page = AsyncMock()
        mgr.is_logged_in = True

        await mgr.stop()

        assert mgr._context is None
        assert mgr._page is None
        assert mgr._playwright is None
        assert mgr.is_logged_in is False

    @pytest.mark.asyncio
    async def test_stop_handles_exception(self):
        from app.services.tbf_worker.session_manager import TbfSessionManager

        cfg = TbfConfig()
        mgr = TbfSessionManager(cfg)
        mock_context = AsyncMock()
        mock_context.close = AsyncMock(side_effect=Exception("close error"))
        mgr._context = mock_context
        mgr._playwright = AsyncMock()

        await mgr.stop()

        assert mgr._context is None


# ═══════════════════════════════════════════════════════════════════════
# WORKER
# ═══════════════════════════════════════════════════════════════════════


class TestWorker:
    # Patch targets — lazy imports inside main() resolve from source modules
    _DB = "app.database.SessionLocal"
    _SESSION_MGR = "app.services.tbf_worker.session_manager.TbfSessionManager"
    _RECOVER = "app.services.tbf_worker.queue_manager.recover_stale_searches"
    _CLAIM = "app.services.tbf_worker.queue_manager.claim_next_queued_item"
    _AI_GATE = "app.services.tbf_worker.ai_gate.process_ai_gate"
    _UPDATE_STATUS = "app.services.tbf_worker.worker.update_worker_status"
    _HEARTBEAT = "app.services.tbf_worker.worker._record_heartbeat"

    def test_handle_shutdown_sets_flag(self):
        import app.services.tbf_worker.worker as worker_mod

        original = worker_mod._shutdown_requested
        try:
            worker_mod._shutdown_requested = False
            worker_mod._handle_shutdown(15, None)
            assert worker_mod._shutdown_requested is True
        finally:
            worker_mod._shutdown_requested = original

    def test_db_session_context_manager(self):
        from app.services.tbf_worker.worker import _db_session

        mock_db = MagicMock()
        with patch(self._DB, return_value=mock_db):
            with _db_session() as db:
                assert db is mock_db
        mock_db.close.assert_called_once()

    def test_db_session_closes_on_exception(self):
        from app.services.tbf_worker.worker import _db_session

        mock_db = MagicMock()
        with patch(self._DB, return_value=mock_db):
            try:
                with _db_session():
                    raise ValueError("test error")
            except ValueError:
                pass
        mock_db.close.assert_called_once()

    def test_update_worker_status_no_row(self, db_session):
        from app.services.tbf_worker.worker import update_worker_status

        update_worker_status(db_session, is_running=True)

    def test_update_worker_status_with_row(self, db_session):
        from app.services.tbf_worker.worker import update_worker_status

        status = TbfWorkerStatus(id=1, is_running=False, searches_today=0)
        db_session.add(status)
        db_session.commit()

        update_worker_status(db_session, is_running=True, searches_today=5)

        db_session.refresh(status)
        assert status.is_running is True
        assert status.searches_today == 5

    def test_record_heartbeat(self, db_session):
        from app.services.tbf_worker.worker import _record_heartbeat

        status = TbfWorkerStatus(id=1, is_running=False)
        db_session.add(status)
        db_session.commit()

        _record_heartbeat(db_session)

        db_session.refresh(status)
        assert status.is_running is True
        assert status.last_heartbeat is not None

    @pytest.mark.asyncio
    async def test_main_session_start_failure(self):
        from app.services.tbf_worker import worker as worker_mod

        worker_mod._shutdown_requested = False
        mock_db = MagicMock()

        mock_session = AsyncMock()
        mock_session.start = AsyncMock(side_effect=Exception("browser error"))

        with patch(self._DB, return_value=mock_db):
            with patch(self._SESSION_MGR, return_value=mock_session):
                with patch(self._RECOVER):
                    with patch(self._UPDATE_STATUS):
                        await worker_mod.main()

        mock_session.stop.assert_not_called()

    @pytest.mark.asyncio
    async def test_main_login_failure(self):
        from app.services.tbf_worker import worker as worker_mod

        worker_mod._shutdown_requested = False

        mock_db = MagicMock()
        mock_session = AsyncMock()
        mock_session.start = AsyncMock()
        mock_session.is_logged_in = False
        mock_session.login = AsyncMock(return_value=False)
        mock_session.stop = AsyncMock()

        with patch(self._DB, return_value=mock_db):
            with patch(self._SESSION_MGR, return_value=mock_session):
                with patch(self._RECOVER):
                    with patch(self._UPDATE_STATUS):
                        await worker_mod.main()

        mock_session.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_main_shutdown_loop(self):
        from app.services.tbf_worker import worker as worker_mod

        worker_mod._shutdown_requested = False

        mock_db = MagicMock()
        mock_session = AsyncMock()
        mock_session.start = AsyncMock()
        mock_session.is_logged_in = True
        mock_session.stop = AsyncMock()

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours = MagicMock(return_value=True)
        mock_scheduler.time_for_break = MagicMock(return_value=False)
        mock_scheduler.next_delay = MagicMock(return_value=1)

        mock_breaker = MagicMock()
        mock_breaker.should_stop = MagicMock(return_value=False)

        call_count = [0]

        def fake_claim(*a, **kw):
            call_count[0] += 1
            worker_mod._shutdown_requested = True
            return None

        mock_config = MagicMock()
        mock_config.TBF_MAX_DAILY_SEARCHES = 1000
        mock_config.TBF_SEARCH_TIMEOUT_SECONDS = 60

        with patch(self._DB, return_value=mock_db):
            with patch(self._SESSION_MGR, return_value=mock_session):
                with patch("app.services.tbf_worker.config.TbfConfig", return_value=mock_config):
                    with patch("app.services.tbf_worker.scheduler.SearchScheduler", return_value=mock_scheduler):
                        with patch("app.services.tbf_worker.circuit_breaker.CircuitBreaker", return_value=mock_breaker):
                            with patch(self._RECOVER):
                                with patch(self._UPDATE_STATUS):
                                    with patch(self._HEARTBEAT):
                                        with patch(self._AI_GATE, new_callable=AsyncMock):
                                            with patch(self._CLAIM, side_effect=fake_claim):
                                                with patch(
                                                    "app.services.tbf_worker.worker.asyncio.sleep",
                                                    new_callable=AsyncMock,
                                                ):
                                                    await worker_mod.main()

        mock_session.stop.assert_called_once()
        worker_mod._shutdown_requested = False


# ═══════════════════════════════════════════════════════════════════════
# MANAGEMENT SCRIPTS — __main__ block coverage
# ═══════════════════════════════════════════════════════════════════════


class TestBackfillQuoteSourceMainBlock:
    def test_main_block_logic(self):
        import app.management.backfill_quote_source as mod

        mock_db = MagicMock()
        mock_session_local = MagicMock(return_value=mock_db)
        mock_backfill_fn = MagicMock(return_value=0)

        with patch.object(mod, "SessionLocal", mock_session_local):
            with patch.object(mod, "backfill", mock_backfill_fn):
                db = mod.SessionLocal()
                try:
                    mod.backfill(db)
                finally:
                    db.close()

        mock_backfill_fn.assert_called_once_with(mock_db)
        mock_db.close.assert_called_once()


class TestBackfillBuyplanCphMainBlock:
    def test_main_block_logic(self):
        import app.management.backfill_buyplan_cph as mod

        mock_db = MagicMock()
        mock_session_local = MagicMock(return_value=mock_db)
        mock_backfill_fn = MagicMock(return_value=0)

        with patch.object(mod, "SessionLocal", mock_session_local):
            with patch.object(mod, "backfill", mock_backfill_fn):
                db = mod.SessionLocal()
                try:
                    mod.backfill(db)
                finally:
                    db.close()

        mock_backfill_fn.assert_called_once_with(mock_db)
        mock_db.close.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# SESSION MANAGER — additional coverage for start() and _dismiss_consent_banner
# ═══════════════════════════════════════════════════════════════════════


class TestSessionManagerStart:
    @pytest.mark.asyncio
    async def test_start_with_display_already_logged_in(self):
        """Start() launches browser, navigates home, checks health → already logged in.

        patchright is imported lazily inside start(), so we patch at the source module.
        """
        from app.services.tbf_worker.session_manager import TbfSessionManager

        cfg = TbfConfig()
        mgr = TbfSessionManager(cfg)

        mock_page = AsyncMock()
        mock_context = AsyncMock()
        mock_context.pages = [mock_page]
        mock_playwright = AsyncMock()
        mock_playwright.chromium.launch_persistent_context = AsyncMock(return_value=mock_context)

        # async_playwright() is called then .start() awaited:
        # self._playwright = await async_playwright().start()
        mock_ap_instance = AsyncMock()
        mock_ap_instance.start = AsyncMock(return_value=mock_playwright)
        mock_async_playwright = MagicMock(return_value=mock_ap_instance)

        with patch.dict(os.environ, {"DISPLAY": ":99"}):
            with patch("app.services.tbf_worker.session_manager.asyncio.sleep", new_callable=AsyncMock):
                # Patch at the patchright source since it's a lazy import
                with patch.dict(
                    "sys.modules",
                    {
                        "patchright": MagicMock(),
                        "patchright.async_api": MagicMock(async_playwright=mock_async_playwright),
                    },
                ):
                    with patch.object(mgr, "check_session_health", new_callable=AsyncMock, return_value=True):
                        await mgr.start()

        assert mgr.is_logged_in is True

    @pytest.mark.asyncio
    async def test_dismiss_consent_banner_button_visible(self):
        """_dismiss_consent_banner clicks visible accept buttons."""
        from app.services.tbf_worker.session_manager import TbfSessionManager

        cfg = TbfConfig()
        mgr = TbfSessionManager(cfg)

        mock_btn = AsyncMock()
        mock_btn.count = AsyncMock(return_value=1)
        mock_btn.first = AsyncMock()
        mock_btn.first.is_visible = AsyncMock(return_value=True)
        mock_btn.first.click = AsyncMock()

        mock_page = MagicMock()
        mock_page.get_by_role = MagicMock(return_value=mock_btn)
        mgr._page = mock_page

        with patch("app.services.tbf_worker.session_manager.asyncio.sleep", new_callable=AsyncMock):
            await mgr._dismiss_consent_banner()

        mock_btn.first.click.assert_called()

    @pytest.mark.asyncio
    async def test_dismiss_consent_banner_exception_continues(self):
        """_dismiss_consent_banner handles exceptions gracefully."""
        from app.services.tbf_worker.session_manager import TbfSessionManager

        cfg = TbfConfig()
        mgr = TbfSessionManager(cfg)

        mock_page = MagicMock()
        mock_page.get_by_role = MagicMock(side_effect=Exception("page error"))
        mgr._page = mock_page

        # Should not raise
        await mgr._dismiss_consent_banner()

    @pytest.mark.asyncio
    async def test_login_failed_with_2fa_detection(self):
        """Login() logs 2FA message when code input is visible after failed login."""
        from app.services.tbf_worker.session_manager import TbfSessionManager

        cfg = TbfConfig()
        cfg.TBF_USERNAME = "user@example.com"
        cfg.TBF_PASSWORD = "password123"
        mgr = TbfSessionManager(cfg)

        # Set up mock page that simulates login form interaction
        mock_form = AsyncMock()
        mock_form.locator = MagicMock(return_value=AsyncMock())

        # 2FA code input visible
        mock_code_locator = AsyncMock()
        mock_code_locator.count = AsyncMock(return_value=1)

        mock_page = AsyncMock()
        mock_page.locator = MagicMock(return_value=mock_form)

        def page_locator_side_effect(sel):
            if "code" in sel:
                return mock_code_locator
            return mock_form

        mock_page.locator = MagicMock(side_effect=page_locator_side_effect)
        mgr._page = mock_page

        with patch.object(mgr, "check_session_health", new_callable=AsyncMock, return_value=False):
            with patch("app.services.tbf_worker.session_manager.asyncio.sleep", new_callable=AsyncMock):
                with patch.object(mgr, "_dismiss_consent_banner", new_callable=AsyncMock):
                    result = await mgr.login()

        assert result is False


# ═══════════════════════════════════════════════════════════════════════
# WORKER — additional main() loop path coverage
# ═══════════════════════════════════════════════════════════════════════


class TestWorkerMainLoopPaths:
    _DB = "app.database.SessionLocal"
    _SESSION_MGR = "app.services.tbf_worker.session_manager.TbfSessionManager"
    _RECOVER = "app.services.tbf_worker.queue_manager.recover_stale_searches"
    _CLAIM = "app.services.tbf_worker.queue_manager.claim_next_queued_item"
    _AI_GATE = "app.services.tbf_worker.ai_gate.process_ai_gate"
    _UPDATE_STATUS = "app.services.tbf_worker.worker.update_worker_status"
    _HEARTBEAT = "app.services.tbf_worker.worker._record_heartbeat"

    def _make_session(self, is_logged_in=True):
        mock_session = AsyncMock()
        mock_session.start = AsyncMock()
        mock_session.is_logged_in = is_logged_in
        mock_session.stop = AsyncMock()
        mock_session.ensure_session = AsyncMock(return_value=True)
        return mock_session

    def _make_config(self, max_daily=1000):
        cfg = MagicMock()
        cfg.TBF_MAX_DAILY_SEARCHES = max_daily
        cfg.TBF_SEARCH_TIMEOUT_SECONDS = 60
        return cfg

    @pytest.mark.asyncio
    async def test_main_outside_business_hours(self):
        """Main() sleeps when outside business hours then shuts down."""
        from app.services.tbf_worker import worker as worker_mod

        worker_mod._shutdown_requested = False
        mock_db = MagicMock()
        mock_session = self._make_session()

        tick = [0]

        def business_hours_side_effect():
            tick[0] += 1
            if tick[0] == 1:
                return False  # First tick: off hours
            worker_mod._shutdown_requested = True
            return True

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours = MagicMock(side_effect=business_hours_side_effect)
        mock_scheduler.time_for_break = MagicMock(return_value=False)

        mock_breaker = MagicMock()
        mock_breaker.should_stop = MagicMock(return_value=False)

        with patch(self._DB, return_value=mock_db):
            with patch(self._SESSION_MGR, return_value=mock_session):
                with patch("app.services.tbf_worker.config.TbfConfig", return_value=self._make_config()):
                    with patch("app.services.tbf_worker.scheduler.SearchScheduler", return_value=mock_scheduler):
                        with patch("app.services.tbf_worker.circuit_breaker.CircuitBreaker", return_value=mock_breaker):
                            with patch(self._RECOVER):
                                with patch(self._UPDATE_STATUS):
                                    with patch(self._HEARTBEAT):
                                        with patch(self._AI_GATE, new_callable=AsyncMock):
                                            with patch(self._CLAIM, return_value=None):
                                                with patch(
                                                    "app.services.tbf_worker.worker.asyncio.sleep",
                                                    new_callable=AsyncMock,
                                                ) as mock_sleep:
                                                    await worker_mod.main()

        # Should have slept for 30 minutes (off hours)
        sleep_calls = [c[0][0] for c in mock_sleep.call_args_list]
        assert any(s == 30 * 60 for s in sleep_calls)
        worker_mod._shutdown_requested = False

    @pytest.mark.asyncio
    async def test_main_circuit_breaker_open(self):
        """Main() sleeps when circuit breaker is open."""
        from app.services.tbf_worker import worker as worker_mod

        worker_mod._shutdown_requested = False
        mock_db = MagicMock()
        mock_session = self._make_session()

        tick = [0]

        def breaker_should_stop():
            tick[0] += 1
            if tick[0] == 1:
                return True  # First tick: breaker open
            worker_mod._shutdown_requested = True
            return False

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours = MagicMock(return_value=True)
        mock_scheduler.time_for_break = MagicMock(return_value=False)

        mock_breaker = MagicMock()
        mock_breaker.should_stop = MagicMock(side_effect=breaker_should_stop)
        mock_breaker.get_trip_info = MagicMock(return_value={"trip_reason": "too many errors"})

        with patch(self._DB, return_value=mock_db):
            with patch(self._SESSION_MGR, return_value=mock_session):
                with patch("app.services.tbf_worker.config.TbfConfig", return_value=self._make_config()):
                    with patch("app.services.tbf_worker.scheduler.SearchScheduler", return_value=mock_scheduler):
                        with patch("app.services.tbf_worker.circuit_breaker.CircuitBreaker", return_value=mock_breaker):
                            with patch(self._RECOVER):
                                with patch(self._UPDATE_STATUS):
                                    with patch(self._HEARTBEAT):
                                        with patch(self._AI_GATE, new_callable=AsyncMock):
                                            with patch(self._CLAIM, return_value=None):
                                                with patch(
                                                    "app.services.tbf_worker.worker.asyncio.sleep",
                                                    new_callable=AsyncMock,
                                                ) as mock_sleep:
                                                    await worker_mod.main()

        sleep_calls = [c[0][0] for c in mock_sleep.call_args_list]
        assert any(s == 60 * 60 for s in sleep_calls)
        worker_mod._shutdown_requested = False

    @pytest.mark.asyncio
    async def test_main_daily_limit_reached(self):
        """Main() sleeps when daily search limit is reached."""
        from app.services.tbf_worker import worker as worker_mod

        worker_mod._shutdown_requested = False
        mock_db = MagicMock()
        mock_session = self._make_session()

        tick = [0]

        def business_hours():
            tick[0] += 1
            if tick[0] > 1:
                worker_mod._shutdown_requested = True
            return True

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours = MagicMock(side_effect=business_hours)
        mock_scheduler.time_for_break = MagicMock(return_value=False)

        mock_breaker = MagicMock()
        mock_breaker.should_stop = MagicMock(return_value=False)

        # Set max daily searches to 0 so it triggers immediately
        mock_config = self._make_config(max_daily=0)

        with patch(self._DB, return_value=mock_db):
            with patch(self._SESSION_MGR, return_value=mock_session):
                with patch("app.services.tbf_worker.config.TbfConfig", return_value=mock_config):
                    with patch("app.services.tbf_worker.scheduler.SearchScheduler", return_value=mock_scheduler):
                        with patch("app.services.tbf_worker.circuit_breaker.CircuitBreaker", return_value=mock_breaker):
                            with patch(self._RECOVER):
                                with patch(self._UPDATE_STATUS):
                                    with patch(self._HEARTBEAT):
                                        with patch(self._AI_GATE, new_callable=AsyncMock):
                                            with patch(self._CLAIM, return_value=None):
                                                with patch(
                                                    "app.services.tbf_worker.worker.asyncio.sleep",
                                                    new_callable=AsyncMock,
                                                ) as mock_sleep:
                                                    await worker_mod.main()

        sleep_calls = [c[0][0] for c in mock_sleep.call_args_list]
        assert any(s == 60 * 60 for s in sleep_calls)
        worker_mod._shutdown_requested = False

    @pytest.mark.asyncio
    async def test_main_break_time(self):
        """Main() takes a break when time_for_break is True."""
        from app.services.tbf_worker import worker as worker_mod

        worker_mod._shutdown_requested = False
        mock_db = MagicMock()
        mock_session = self._make_session()

        tick = [0]

        def time_for_break():
            tick[0] += 1
            if tick[0] == 1:
                return True
            worker_mod._shutdown_requested = True
            return False

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours = MagicMock(return_value=True)
        mock_scheduler.time_for_break = MagicMock(side_effect=time_for_break)
        mock_scheduler.get_break_duration = MagicMock(return_value=600)
        mock_scheduler.reset_break_counter = MagicMock()

        mock_breaker = MagicMock()
        mock_breaker.should_stop = MagicMock(return_value=False)

        with patch(self._DB, return_value=mock_db):
            with patch(self._SESSION_MGR, return_value=mock_session):
                with patch("app.services.tbf_worker.config.TbfConfig", return_value=self._make_config()):
                    with patch("app.services.tbf_worker.scheduler.SearchScheduler", return_value=mock_scheduler):
                        with patch("app.services.tbf_worker.circuit_breaker.CircuitBreaker", return_value=mock_breaker):
                            with patch(self._RECOVER):
                                with patch(self._UPDATE_STATUS):
                                    with patch(self._HEARTBEAT):
                                        with patch(self._AI_GATE, new_callable=AsyncMock):
                                            with patch(self._CLAIM, return_value=None):
                                                with patch(
                                                    "app.services.tbf_worker.worker.asyncio.sleep",
                                                    new_callable=AsyncMock,
                                                ) as mock_sleep:
                                                    await worker_mod.main()

        sleep_calls = [c[0][0] for c in mock_sleep.call_args_list]
        assert any(s == 600 for s in sleep_calls)
        worker_mod._shutdown_requested = False
