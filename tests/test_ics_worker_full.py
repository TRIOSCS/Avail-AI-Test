"""100% coverage tests for the entire ics_worker package.

Covers every module: worker, search_engine, session_manager, result_parser,
queue_manager, sighting_writer, ai_gate, circuit_breaker, scheduler,
human_behavior, config, monitoring, mpn_normalizer, __main__.

Called by: pytest
Depends on: conftest.py, ics_worker modules
"""

import asyncio
import hashlib
import math
import os
import random
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import IcsSearchLog, IcsSearchQueue, IcsWorkerStatus, Requirement, Sighting
from app.services.ics_worker.circuit_breaker import CircuitBreaker
from app.services.ics_worker.config import IcsConfig
from app.services.ics_worker.human_behavior import HumanBehavior
from app.services.ics_worker.monitoring import (
    _known_html_hashes,
    capture_sentry_error,
    capture_sentry_message,
    check_html_structure_hash,
    log_daily_report,
)
from app.services.ics_worker.mpn_normalizer import normalize_mpn
from app.services.ics_worker.queue_manager import (
    enqueue_for_ics_search,
    get_next_queued_item,
    get_queue_stats,
    mark_completed,
    mark_status,
    recover_stale_searches,
)
from app.services.ics_worker.result_parser import IcsSighting, parse_quantity, parse_results_html
from app.services.ics_worker.scheduler import SearchScheduler
from app.services.ics_worker.search_engine import SEARCH_URL, search_part
from app.services.ics_worker.sighting_writer import save_ics_sightings


# ═══════════════════════════════════════════════════════════════════════
# MPN NORMALIZER
# ═══════════════════════════════════════════════════════════════════════


class TestMpnNormalizer:
    def test_empty_string(self):
        assert normalize_mpn("") == ""

    def test_none_input(self):
        assert normalize_mpn(None) == ""

    def test_whitespace_only(self):
        assert normalize_mpn("   ") == ""

    def test_basic_uppercase(self):
        assert normalize_mpn("stm32f103c8t6") == "STM32F103C8T6"

    def test_strip_whitespace_internal(self):
        assert normalize_mpn("LM 317 T") == "LM317T"

    def test_strip_tape_and_reel_slash(self):
        assert normalize_mpn("STM32F103C8T6/TR") == "STM32F103C8T6"

    def test_strip_tape_and_reel_dash(self):
        assert normalize_mpn("STM32F103C8T6-TR") == "STM32F103C8T6"

    def test_strip_cut_tape_slash(self):
        assert normalize_mpn("LM317T/CT") == "LM317T"

    def test_strip_cut_tape_dash(self):
        assert normalize_mpn("LM317T-CT") == "LM317T"

    def test_strip_nd_suffix(self):
        assert normalize_mpn("LM358DR-ND") == "LM358DR"

    def test_strip_dkr_suffix(self):
        assert normalize_mpn("AD8232ACPZ-DKR") == "AD8232ACPZ"

    def test_strip_pbf_hash(self):
        assert normalize_mpn("IRF3205#PBF") == "IRF3205"

    def test_strip_pbf_dash(self):
        assert normalize_mpn("IRF3205-PBF") == "IRF3205"

    def test_strip_nopb_slash(self):
        assert normalize_mpn("TPS54302DDCR/NOPB") == "TPS54302DDCR"

    def test_strip_nopb_dash(self):
        assert normalize_mpn("TPS54302DDCR-NOPB") == "TPS54302DDCR"

    def test_strip_reel_suffix(self):
        assert normalize_mpn("ADP3338AKCZ-3.3-RL") == "ADP3338AKCZ-3.3"

    def test_strip_reel_with_number(self):
        assert normalize_mpn("ADP3338AKCZ-RL7") == "ADP3338AKCZ"

    def test_case_insensitive_suffix(self):
        assert normalize_mpn("lm317t/tr") == "LM317T"


# ═══════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════


class TestIcsConfig:
    def test_defaults(self):
        cfg = IcsConfig()
        assert cfg.ICS_MAX_DAILY_SEARCHES == 50
        assert cfg.ICS_MAX_HOURLY_SEARCHES == 10
        assert cfg.ICS_MIN_DELAY_SECONDS == 150
        assert cfg.ICS_MAX_DELAY_SECONDS == 420
        assert cfg.ICS_TYPICAL_DELAY_SECONDS == 270
        assert cfg.ICS_DEDUP_WINDOW_DAYS == 7
        assert cfg.ICS_BUSINESS_HOURS_START == 8
        assert cfg.ICS_BUSINESS_HOURS_END == 18

    def test_env_override(self):
        with patch.dict(os.environ, {"ICS_MAX_DAILY_SEARCHES": "30", "ICS_USERNAME": "testuser"}):
            cfg = IcsConfig()
            assert cfg.ICS_MAX_DAILY_SEARCHES == 30
            assert cfg.ICS_USERNAME == "testuser"


# ═══════════════════════════════════════════════════════════════════════
# RESULT PARSER
# ═══════════════════════════════════════════════════════════════════════


class TestResultParser:
    def test_none_html(self):
        assert parse_results_html(None) == []

    def test_empty_html(self):
        assert parse_results_html("") == []

    def test_whitespace_html(self):
        assert parse_results_html("   ") == []

    def test_no_results(self):
        html = "<div>No results found</div>"
        assert parse_results_html(html) == []

    def test_parse_quantity_normal(self):
        assert parse_quantity("1,000") == 1000

    def test_parse_quantity_plus(self):
        assert parse_quantity("500+") == 500

    def test_parse_quantity_empty(self):
        assert parse_quantity("") is None

    def test_parse_quantity_invalid(self):
        assert parse_quantity("N/A") is None

    def test_parse_quantity_none(self):
        assert parse_quantity(None) is None

    def test_browse_match_item_parsing(self):
        """Parse a typical ICsource results page structure."""
        html = """
        <div class="tblWTBPanel tblWrapper">
          <div class="divDateGroup">Feb 25, 2026</div>
          <div class="flex">
            <a href="javascript:OpenProfile(12345)">Acme Electronics</a>
            <a href="mailto:sales@acme.com">sales@acme.com</a>
            <span class="clicktocall">+1-555-0123</span>
          </div>
          <tr class="browseMatchItem">
            <td>STM32F103C8T6</td>
            <td>ARM Cortex-M3</td>
            <td>5,000</td>
            <td>$2.50</td>
            <td>ST</td>
            <td>2024+</td>
            <td><img src="check.gif" /></td>
          </tr>
        </div>
        """
        sightings = parse_results_html(html)
        assert len(sightings) == 1
        s = sightings[0]
        assert s.part_number == "STM32F103C8T6"
        assert s.description == "ARM Cortex-M3"
        assert s.quantity == 5000
        assert s.price == "$2.50"
        assert s.manufacturer == "ST"
        assert s.date_code == "2024+"
        assert s.in_stock is True
        assert s.vendor_name == "Acme Electronics"
        assert s.vendor_email == "sales@acme.com"
        assert s.vendor_phone == "+1-555-0123"
        assert s.vendor_company_id == "12345"
        assert s.uploaded_date == "Feb 25, 2026"

    def test_multiple_rows(self):
        """Multiple browseMatchItem rows within one company block."""
        html = """
        <div class="tblWTBPanel">
          <div class="flex">
            <a href="javascript:OpenProfile(99)">BigCo</a>
          </div>
          <tr class="browseMatchItem">
            <td>PART1</td><td>Desc1</td><td>100</td><td></td><td>MFR1</td><td></td><td></td>
          </tr>
          <tr class="browseMatchItem">
            <td>PART2</td><td>Desc2</td><td>200</td><td></td><td>MFR2</td><td></td><td></td>
          </tr>
        </div>
        """
        sightings = parse_results_html(html)
        assert len(sightings) == 2
        assert sightings[0].part_number == "PART1"
        assert sightings[1].part_number == "PART2"
        assert sightings[0].vendor_name == "BigCo"
        assert sightings[1].vendor_name == "BigCo"

    def test_no_stock_image(self):
        """Stock column without image means not in stock."""
        html = """
        <div class="tblWTBPanel">
          <div class="flex">
            <a href="javascript:OpenProfile(1)">Vendor</a>
          </div>
          <tr class="browseMatchItem">
            <td>PART</td><td></td><td>50</td><td></td><td>MFR</td><td></td><td></td>
          </tr>
        </div>
        """
        sightings = parse_results_html(html)
        assert len(sightings) == 1
        assert sightings[0].in_stock is False

    def test_checkmark_text_stock(self):
        """Stock column with checkmark character."""
        html = """
        <div class="tblWTBPanel">
          <div class="flex">
            <a href="javascript:OpenProfile(1)">V</a>
          </div>
          <tr class="browseMatchItem">
            <td>PART</td><td></td><td>50</td><td></td><td>MFR</td><td></td><td>✓</td>
          </tr>
        </div>
        """
        sightings = parse_results_html(html)
        assert len(sightings) == 1
        assert sightings[0].in_stock is True

    def test_malformed_row_too_few_cells(self):
        """Rows with fewer than 5 cells are skipped."""
        html = """
        <div class="tblWTBPanel">
          <tr class="browseMatchItem"><td>Only</td><td>Two</td></tr>
        </div>
        """
        assert parse_results_html(html) == []

    def test_email_with_query_params(self):
        """mailto links with ?subject= params are cleaned."""
        html = """
        <div class="tblWTBPanel">
          <div class="flex">
            <a href="javascript:OpenProfile(1)">Vendor</a>
            <a href="mailto:info@vendor.com?subject=Inquiry">Contact</a>
          </div>
          <tr class="browseMatchItem">
            <td>P</td><td>D</td><td>1</td><td></td><td>M</td><td></td><td></td>
          </tr>
        </div>
        """
        sightings = parse_results_html(html)
        assert len(sightings) == 1
        assert sightings[0].vendor_email == "info@vendor.com"

    def test_company_without_open_profile(self):
        """Company block without OpenProfile link yields empty company info."""
        html = """
        <div class="tblWTBPanel">
          <div class="flex">
            <span>No Link Here</span>
          </div>
          <tr class="browseMatchItem">
            <td>P</td><td>D</td><td>1</td><td></td><td>M</td><td></td><td></td>
          </tr>
        </div>
        """
        sightings = parse_results_html(html)
        assert len(sightings) == 1
        assert sightings[0].vendor_name == ""
        assert sightings[0].vendor_company_id == ""


# ═══════════════════════════════════════════════════════════════════════
# SEARCH ENGINE
# ═══════════════════════════════════════════════════════════════════════


class TestSearchEngine:
    def test_search_url_constant(self):
        assert "icsource.com" in SEARCH_URL
        assert "NewSearch" in SEARCH_URL

    @pytest.mark.asyncio
    async def test_search_part(self):
        """Exercise the search_part async function with a mocked page."""
        page = AsyncMock()
        page.goto = AsyncMock()
        page.url = "https://www.icsource.com/members/Search/Results.aspx"

        pn_locator = AsyncMock()
        pn_locator.wait_for = AsyncMock()
        pn_locator.fill = AsyncMock()
        page.locator = MagicMock(return_value=pn_locator)

        page.wait_for_selector = AsyncMock()
        page.evaluate = AsyncMock(side_effect=[
            "<div class='tblWTBPanel'>results</div>",  # HTML content
            5,  # total_count
        ])

        with patch("app.services.ics_worker.search_engine.HumanBehavior") as mock_hb:
            mock_hb.human_type = AsyncMock()
            mock_hb.human_click = AsyncMock()
            mock_hb.random_delay = AsyncMock()

            result = await search_part(page, "STM32F103C8T6")

        assert "results" in result["html"]
        assert result["total_count"] == 5
        assert result["duration_ms"] >= 0

    @pytest.mark.asyncio
    async def test_search_part_fallback_field(self):
        """When primary search field not found, falls back to multi-search field."""
        page = AsyncMock()
        page.goto = AsyncMock()
        page.url = "https://www.icsource.com/members/Search/Results.aspx"

        primary_locator = AsyncMock()
        primary_locator.wait_for = AsyncMock(side_effect=Exception("not found"))
        primary_locator.fill = AsyncMock()

        fallback_locator = AsyncMock()
        fallback_locator.wait_for = AsyncMock()
        fallback_locator.fill = AsyncMock()

        def mock_locator(sel):
            if "rtxtPartNumber" in sel:
                return primary_locator
            return fallback_locator

        page.locator = MagicMock(side_effect=mock_locator)
        page.wait_for_selector = AsyncMock()
        page.evaluate = AsyncMock(side_effect=["<html></html>", 0])

        with patch("app.services.ics_worker.search_engine.HumanBehavior") as mock_hb:
            mock_hb.human_type = AsyncMock()
            mock_hb.human_click = AsyncMock()
            mock_hb.random_delay = AsyncMock()

            result = await search_part(page, "XYZ123")

        assert result["html"] == "<html></html>"

    @pytest.mark.asyncio
    async def test_search_part_selector_timeout(self):
        """Cover the timeout warning branch when results selector not found."""
        page = AsyncMock()
        page.goto = AsyncMock()
        page.url = "https://www.icsource.com/search"

        locator = AsyncMock()
        locator.wait_for = AsyncMock()
        locator.fill = AsyncMock()
        page.locator = MagicMock(return_value=locator)

        page.wait_for_selector = AsyncMock(side_effect=Exception("Timeout"))
        page.evaluate = AsyncMock(side_effect=["<html>body</html>", 0])

        with patch("app.services.ics_worker.search_engine.HumanBehavior") as mock_hb:
            mock_hb.human_type = AsyncMock()
            mock_hb.human_click = AsyncMock()
            mock_hb.random_delay = AsyncMock()

            result = await search_part(page, "XYZ123")

        assert result["html"] == "<html>body</html>"
        assert result["total_count"] == 0


# ═══════════════════════════════════════════════════════════════════════
# CIRCUIT BREAKER
# ═══════════════════════════════════════════════════════════════════════


class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_healthy_page(self):
        breaker = CircuitBreaker()
        page = AsyncMock()
        page.url = "https://www.icsource.com/members/Search/Results.aspx"
        page.evaluate = AsyncMock(return_value="search results page content here")

        result = await breaker.check_page_health(page)
        assert result == "HEALTHY"
        assert not breaker.is_open

    @pytest.mark.asyncio
    async def test_unexpected_redirect(self):
        breaker = CircuitBreaker()
        page = AsyncMock()
        page.url = "https://www.google.com"
        page.evaluate = AsyncMock(return_value="google page")

        result = await breaker.check_page_health(page)
        assert result == "UNEXPECTED_REDIRECT"
        assert breaker.is_open

    @pytest.mark.asyncio
    async def test_session_expired_login_url(self):
        breaker = CircuitBreaker()
        page = AsyncMock()
        page.url = "https://www.icsource.com/home/LogIn.aspx"
        page.evaluate = AsyncMock(return_value="login page")

        result = await breaker.check_page_health(page)
        assert result == "SESSION_EXPIRED"
        assert not breaker.is_open

    @pytest.mark.asyncio
    async def test_captcha_warning(self):
        breaker = CircuitBreaker()
        page = AsyncMock()
        page.url = "https://www.icsource.com/members/Search"
        page.evaluate = AsyncMock(return_value="please verify you are human captcha")

        result = await breaker.check_page_health(page)
        assert result == "CAPTCHA_WARNING"
        assert breaker.captcha_count == 1
        assert not breaker.is_open  # Not tripped yet (needs 2)

    @pytest.mark.asyncio
    async def test_captcha_trips_on_second(self):
        breaker = CircuitBreaker()
        page = AsyncMock()
        page.url = "https://www.icsource.com/search"
        page.evaluate = AsyncMock(return_value="captcha verification required")

        await breaker.check_page_health(page)
        result = await breaker.check_page_health(page)
        assert result == "CAPTCHA_WARNING"
        assert breaker.is_open
        assert "Captcha" in breaker.trip_reason

    @pytest.mark.asyncio
    async def test_rate_limited(self):
        breaker = CircuitBreaker()
        page = AsyncMock()
        page.url = "https://www.icsource.com/error"
        page.evaluate = AsyncMock(return_value="too many requests please slow down")

        result = await breaker.check_page_health(page)
        assert result == "RATE_LIMITED"
        assert breaker.is_open

    @pytest.mark.asyncio
    async def test_access_denied(self):
        breaker = CircuitBreaker()
        page = AsyncMock()
        page.url = "https://www.icsource.com/error"
        page.evaluate = AsyncMock(return_value="access denied - your account has been blocked")

        result = await breaker.check_page_health(page)
        assert result == "ACCESS_DENIED"
        assert breaker.is_open

    @pytest.mark.asyncio
    async def test_unusual_activity(self):
        breaker = CircuitBreaker()
        page = AsyncMock()
        page.url = "https://www.icsource.com/warning"
        page.evaluate = AsyncMock(return_value="we detected unusual activity on your account")

        result = await breaker.check_page_health(page)
        assert result == "ACCESS_DENIED"
        assert breaker.is_open

    @pytest.mark.asyncio
    async def test_consecutive_failures_trip(self):
        breaker = CircuitBreaker()
        page = AsyncMock()
        page.url = "https://www.icsource.com/search"
        page.evaluate = AsyncMock(side_effect=Exception("page crashed"))

        for i in range(2):
            result = await breaker.check_page_health(page)
            assert result == "CHECK_FAILED"
            assert not breaker.is_open

        result = await breaker.check_page_health(page)
        assert result == "CHECK_FAILED"
        assert breaker.is_open
        assert "3 consecutive" in breaker.trip_reason

    def test_empty_results_streak(self):
        breaker = CircuitBreaker()
        for _ in range(9):
            breaker.record_empty_results()
            assert not breaker.is_open
        breaker.record_empty_results()
        assert breaker.is_open
        assert "shadow-block" in breaker.trip_reason

    def test_record_results_resets_streak(self):
        breaker = CircuitBreaker()
        breaker.empty_results_streak = 5
        breaker.record_results()
        assert breaker.empty_results_streak == 0

    def test_should_stop(self):
        breaker = CircuitBreaker()
        assert not breaker.should_stop()
        breaker.is_open = True
        assert breaker.should_stop()

    def test_get_trip_info(self):
        breaker = CircuitBreaker()
        info = breaker.get_trip_info()
        assert "is_open" in info
        assert "trip_reason" in info
        assert "captcha_count" in info

    def test_reset(self):
        breaker = CircuitBreaker()
        breaker.is_open = True
        breaker.trip_reason = "test"
        breaker.captcha_count = 5
        breaker.consecutive_failures = 3
        breaker.empty_results_streak = 8
        breaker.reset()
        assert not breaker.is_open
        assert breaker.trip_reason == ""
        assert breaker.captcha_count == 0
        assert breaker.consecutive_failures == 0
        assert breaker.empty_results_streak == 0


# ═══════════════════════════════════════════════════════════════════════
# SCHEDULER
# ═══════════════════════════════════════════════════════════════════════


class TestScheduler:
    def test_break_threshold_random_range(self):
        cfg = IcsConfig()
        sched = SearchScheduler(cfg)
        assert 8 <= sched.break_threshold <= 15

    def test_next_delay_increments_search_count(self):
        cfg = IcsConfig()
        sched = SearchScheduler(cfg)
        assert sched.searches_since_break == 0
        sched.next_delay()
        assert sched.searches_since_break == 1

    def test_next_delay_within_bounds(self):
        cfg = IcsConfig()
        sched = SearchScheduler(cfg)
        for _ in range(20):
            delay = sched.next_delay()
            assert cfg.ICS_MIN_DELAY_SECONDS <= delay <= cfg.ICS_MAX_DELAY_SECONDS

    def test_time_for_break(self):
        cfg = IcsConfig()
        sched = SearchScheduler(cfg)
        sched.break_threshold = 3
        assert not sched.time_for_break()
        sched.searches_since_break = 3
        assert sched.time_for_break()

    def test_get_break_duration(self):
        cfg = IcsConfig()
        sched = SearchScheduler(cfg)
        duration = sched.get_break_duration()
        assert 5 * 60 <= duration <= 25 * 60

    def test_reset_break_counter(self):
        cfg = IcsConfig()
        sched = SearchScheduler(cfg)
        sched.searches_since_break = 10
        sched.reset_break_counter()
        assert sched.searches_since_break == 0
        assert 8 <= sched.break_threshold <= 15

    def test_is_business_hours(self):
        cfg = IcsConfig()
        sched = SearchScheduler(cfg)
        # Just verify it returns a bool (actual result depends on time of day)
        assert isinstance(sched.is_business_hours(), bool)


# ═══════════════════════════════════════════════════════════════════════
# HUMAN BEHAVIOR
# ═══════════════════════════════════════════════════════════════════════


class TestHumanBehavior:
    @pytest.mark.asyncio
    async def test_random_delay(self):
        with patch("app.services.ics_worker.human_behavior.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await HumanBehavior.random_delay(0.5, 1.5)
            mock_sleep.assert_called_once()
            delay = mock_sleep.call_args[0][0]
            assert 0.5 <= delay <= 1.5

    @pytest.mark.asyncio
    async def test_human_type(self):
        page = AsyncMock()
        locator = AsyncMock()
        locator.click = AsyncMock()

        with patch("app.services.ics_worker.human_behavior.asyncio.sleep", new_callable=AsyncMock):
            with patch("app.services.ics_worker.human_behavior.random.uniform", return_value=0.1):
                with patch("app.services.ics_worker.human_behavior.random.random", return_value=0.5):
                    await HumanBehavior.human_type(page, locator, "abc")

        locator.click.assert_called_once()
        assert page.keyboard.type.call_count == 3

    @pytest.mark.asyncio
    async def test_human_type_thinking_pause(self):
        page = AsyncMock()
        locator = AsyncMock()

        with patch("app.services.ics_worker.human_behavior.asyncio.sleep", new_callable=AsyncMock):
            with patch("app.services.ics_worker.human_behavior.random.uniform", return_value=0.1):
                with patch("app.services.ics_worker.human_behavior.random.random", return_value=0.01):
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
        assert h in _known_html_hashes

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
        assert h2 in _known_html_hashes


# ═══════════════════════════════════════════════════════════════════════
# QUEUE MANAGER
# ═══════════════════════════════════════════════════════════════════════


class TestQueueManager:
    def test_enqueue_no_requirement(self, db_session):
        result = enqueue_for_ics_search(99999, db_session)
        assert result is None

    def test_enqueue_no_mpn(self, db_session, test_requisition):
        req = test_requisition.requirements[0]
        req.primary_mpn = None
        db_session.commit()
        result = enqueue_for_ics_search(req.id, db_session)
        assert result is None

    def test_enqueue_empty_mpn(self, db_session, test_requisition):
        req = test_requisition.requirements[0]
        req.primary_mpn = ""
        db_session.commit()
        result = enqueue_for_ics_search(req.id, db_session)
        assert result is None

    def test_enqueue_success(self, db_session, test_requisition):
        req = test_requisition.requirements[0]
        item = enqueue_for_ics_search(req.id, db_session)
        assert item is not None
        assert item.mpn == "LM317T"
        assert item.status == "pending"
        assert item.priority == 3  # "open" is not an active sourcing status

    def test_enqueue_active_requisition_gets_priority(self, db_session, test_requisition):
        test_requisition.status = "sourcing"
        db_session.commit()
        req = test_requisition.requirements[0]
        item = enqueue_for_ics_search(req.id, db_session)
        assert item is not None
        assert item.priority == 1

    def test_enqueue_already_queued(self, db_session, test_requisition):
        req = test_requisition.requirements[0]
        item1 = enqueue_for_ics_search(req.id, db_session)
        item2 = enqueue_for_ics_search(req.id, db_session)
        assert item1.id == item2.id

    def test_enqueue_dedup_links_sightings(self, db_session, test_user):
        from app.models import MaterialCard, Requisition

        mc = MaterialCard(
            normalized_mpn="lm317t", display_mpn="LM317T", manufacturer="TI",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(mc)
        db_session.flush()

        req1 = Requisition(
            name="REQ-1", customer_name="Acme", status="open",
            created_by=test_user.id, created_at=datetime.now(timezone.utc),
        )
        db_session.add(req1)
        db_session.flush()

        item1_req = Requirement(
            requisition_id=req1.id, primary_mpn="LM317T", target_qty=100,
            material_card_id=mc.id, created_at=datetime.now(timezone.utc),
        )
        db_session.add(item1_req)
        db_session.flush()

        queue1 = IcsSearchQueue(
            requirement_id=item1_req.id, requisition_id=req1.id,
            mpn="LM317T", normalized_mpn="LM317T",
            status="completed", last_searched_at=datetime.now(timezone.utc),
        )
        db_session.add(queue1)
        db_session.flush()

        sighting = Sighting(
            requirement_id=item1_req.id, vendor_name="Arrow",
            vendor_name_normalized="arrow", mpn_matched="LM317T",
            normalized_mpn="LM317T", source_type="icsource",
            qty_available=500, created_at=datetime.now(timezone.utc),
        )
        db_session.add(sighting)
        db_session.commit()

        req2 = Requisition(
            name="REQ-2", customer_name="Beta Corp", status="open",
            created_by=test_user.id, created_at=datetime.now(timezone.utc),
        )
        db_session.add(req2)
        db_session.flush()

        item2_req = Requirement(
            requisition_id=req2.id, primary_mpn="LM317T", target_qty=200,
            material_card_id=mc.id, created_at=datetime.now(timezone.utc),
        )
        db_session.add(item2_req)
        db_session.commit()

        result = enqueue_for_ics_search(item2_req.id, db_session)
        assert result is None  # Deduped

        linked = (
            db_session.query(Sighting)
            .filter(Sighting.requirement_id == item2_req.id, Sighting.source_type == "icsource")
            .all()
        )
        assert len(linked) == 1
        assert linked[0].vendor_name == "Arrow"

    def test_enqueue_dedup_no_material_card(self, db_session, test_user):
        from app.models import Requisition

        req1 = Requisition(
            name="REQ-D1", customer_name="X", status="open",
            created_by=test_user.id, created_at=datetime.now(timezone.utc),
        )
        db_session.add(req1)
        db_session.flush()

        item1 = Requirement(
            requisition_id=req1.id, primary_mpn="LM317T",
            target_qty=100, created_at=datetime.now(timezone.utc),
        )
        db_session.add(item1)
        db_session.flush()

        queue1 = IcsSearchQueue(
            requirement_id=item1.id, requisition_id=req1.id,
            mpn="LM317T", normalized_mpn="LM317T",
            status="completed", last_searched_at=datetime.now(timezone.utc),
        )
        db_session.add(queue1)
        db_session.commit()

        req2 = Requisition(
            name="REQ-D2", customer_name="Y", status="open",
            created_by=test_user.id, created_at=datetime.now(timezone.utc),
        )
        db_session.add(req2)
        db_session.flush()

        item2 = Requirement(
            requisition_id=req2.id, primary_mpn="LM317T",
            target_qty=50, created_at=datetime.now(timezone.utc),
        )
        db_session.add(item2)
        db_session.commit()

        result = enqueue_for_ics_search(item2.id, db_session)
        assert result is None  # Deduped but no link

    def test_recover_stale_searches(self, db_session, test_requisition):
        req = test_requisition.requirements[0]
        item = IcsSearchQueue(
            requirement_id=req.id, requisition_id=test_requisition.id,
            mpn="LM317T", normalized_mpn="LM317T", status="searching",
        )
        db_session.add(item)
        db_session.commit()

        count = recover_stale_searches(db_session)
        assert count == 1
        db_session.refresh(item)
        assert item.status == "queued"

    def test_recover_stale_none(self, db_session):
        assert recover_stale_searches(db_session) == 0

    def test_get_next_queued_item(self, db_session, test_requisition):
        req = test_requisition.requirements[0]
        item = IcsSearchQueue(
            requirement_id=req.id, requisition_id=test_requisition.id,
            mpn="LM317T", normalized_mpn="LM317T", status="queued",
        )
        db_session.add(item)
        db_session.commit()

        result = get_next_queued_item(db_session)
        assert result.id == item.id

    def test_get_next_queued_prefers_high_priority_then_newest(self, db_session, test_user):
        """Active-sourcing items (priority=1) come first, then newest-first within same priority."""
        from app.models import Requisition

        # Create two requisitions
        r1 = Requisition(name="OLD", customer_name="A", status="open",
                         created_by=test_user.id, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        r2 = Requisition(name="NEW", customer_name="B", status="open",
                         created_by=test_user.id, created_at=datetime(2026, 2, 1, tzinfo=timezone.utc))
        db_session.add_all([r1, r2])
        db_session.flush()

        req1 = Requirement(requisition_id=r1.id, primary_mpn="OLD-PART", target_qty=1,
                           created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        req2 = Requirement(requisition_id=r2.id, primary_mpn="NEW-PART", target_qty=1,
                           created_at=datetime(2026, 2, 1, tzinfo=timezone.utc))
        db_session.add_all([req1, req2])
        db_session.flush()

        # Both same priority — newer should come first
        old_item = IcsSearchQueue(
            requirement_id=req1.id, requisition_id=r1.id,
            mpn="OLD-PART", normalized_mpn="old-part", status="queued",
            priority=3, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        new_item = IcsSearchQueue(
            requirement_id=req2.id, requisition_id=r2.id,
            mpn="NEW-PART", normalized_mpn="new-part", status="queued",
            priority=3, created_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
        )
        db_session.add_all([old_item, new_item])
        db_session.commit()

        result = get_next_queued_item(db_session)
        assert result.mpn == "NEW-PART", "Newest part should come first"

        # Now set old_item to high priority — it should come first despite being older
        old_item.priority = 1
        db_session.commit()

        result = get_next_queued_item(db_session)
        assert result.mpn == "OLD-PART", "High priority should beat recency"

    def test_get_next_queued_none(self, db_session):
        assert get_next_queued_item(db_session) is None

    def test_mark_status(self, db_session, test_requisition):
        req = test_requisition.requirements[0]
        item = IcsSearchQueue(
            requirement_id=req.id, requisition_id=test_requisition.id,
            mpn="LM317T", normalized_mpn="LM317T", status="queued",
        )
        db_session.add(item)
        db_session.commit()

        mark_status(db_session, item, "searching")
        db_session.refresh(item)
        assert item.status == "searching"

    def test_mark_status_with_error(self, db_session, test_requisition):
        req = test_requisition.requirements[0]
        item = IcsSearchQueue(
            requirement_id=req.id, requisition_id=test_requisition.id,
            mpn="LM317T", normalized_mpn="LM317T", status="searching",
        )
        db_session.add(item)
        db_session.commit()

        mark_status(db_session, item, "failed", error="Timeout")
        db_session.refresh(item)
        assert item.status == "failed"
        assert item.error_message == "Timeout"

    def test_mark_completed(self, db_session, test_requisition):
        req = test_requisition.requirements[0]
        item = IcsSearchQueue(
            requirement_id=req.id, requisition_id=test_requisition.id,
            mpn="LM317T", normalized_mpn="LM317T", status="searching",
        )
        db_session.add(item)
        db_session.commit()

        mark_completed(db_session, item, results_found=10, sightings_created=5)
        db_session.refresh(item)
        assert item.status == "completed"
        assert item.results_count == 10
        assert item.search_count == 1
        assert item.last_searched_at is not None

    def test_mark_completed_increments_search_count(self, db_session, test_requisition):
        req = test_requisition.requirements[0]
        item = IcsSearchQueue(
            requirement_id=req.id, requisition_id=test_requisition.id,
            mpn="LM317T", normalized_mpn="LM317T",
            status="searching", search_count=2,
        )
        db_session.add(item)
        db_session.commit()

        mark_completed(db_session, item, results_found=5, sightings_created=3)
        assert item.search_count == 3

    def test_get_queue_stats(self, db_session, test_requisition):
        stats = get_queue_stats(db_session)
        assert "pending" in stats
        assert "queued" in stats
        assert "completed" in stats
        assert "total_today" in stats
        assert "remaining" in stats


# ═══════════════════════════════════════════════════════════════════════
# SIGHTING WRITER
# ═══════════════════════════════════════════════════════════════════════


class TestSightingWriter:
    def test_save_no_requirement(self, db_session):
        """Returns 0 when requirement not found."""
        queue_item = MagicMock()
        queue_item.requirement_id = 99999
        assert save_ics_sightings(db_session, queue_item, []) == 0

    def test_save_empty_sightings(self, db_session, test_requisition):
        req = test_requisition.requirements[0]
        queue_item = MagicMock()
        queue_item.requirement_id = req.id
        assert save_ics_sightings(db_session, queue_item, []) == 0

    def test_save_skips_no_vendor(self, db_session, test_requisition):
        req = test_requisition.requirements[0]
        queue_item = MagicMock()
        queue_item.requirement_id = req.id

        sighting = IcsSighting(part_number="LM317T", vendor_name="")
        assert save_ics_sightings(db_session, queue_item, [sighting]) == 0

    def test_save_creates_sightings(self, db_session, test_requisition):
        req = test_requisition.requirements[0]
        queue_item = MagicMock()
        queue_item.requirement_id = req.id

        sighting = IcsSighting(
            part_number="LM317T",
            manufacturer="TI",
            vendor_name="TestVendor",
            vendor_email="test@vendor.com",
            vendor_phone="+1-555-0123",
            vendor_company_id="123",
            quantity=1000,
            price="$1.50",
            in_stock=True,
            date_code="2024+",
            description="Voltage Regulator",
            uploaded_date="Feb 25, 2026",
        )
        count = save_ics_sightings(db_session, queue_item, [sighting])
        assert count == 1

        saved = db_session.query(Sighting).filter(
            Sighting.requirement_id == req.id,
            Sighting.source_type == "icsource",
        ).first()
        assert saved is not None
        assert saved.vendor_name == "TestVendor"
        assert saved.vendor_email == "test@vendor.com"
        assert saved.vendor_phone == "+1-555-0123"
        assert saved.confidence == 0.6  # in_stock
        assert saved.qty_available == 1000

    def test_save_dedup(self, db_session, test_requisition):
        """Duplicate sightings are not created."""
        req = test_requisition.requirements[0]
        queue_item = MagicMock()
        queue_item.requirement_id = req.id

        sighting = IcsSighting(
            part_number="LM317T", vendor_name="Vendor1", quantity=100,
        )
        count1 = save_ics_sightings(db_session, queue_item, [sighting])
        count2 = save_ics_sightings(db_session, queue_item, [sighting])
        assert count1 == 1
        assert count2 == 0

    def test_save_not_in_stock_confidence(self, db_session, test_requisition):
        """Not-in-stock sightings get lower confidence."""
        req = test_requisition.requirements[0]
        queue_item = MagicMock()
        queue_item.requirement_id = req.id

        sighting = IcsSighting(
            part_number="LM317T", vendor_name="Vendor1", quantity=100, in_stock=False,
        )
        save_ics_sightings(db_session, queue_item, [sighting])

        saved = db_session.query(Sighting).filter(
            Sighting.requirement_id == req.id, Sighting.source_type == "icsource",
        ).first()
        assert saved.confidence == 0.3


# ═══════════════════════════════════════════════════════════════════════
# AI GATE
# ═══════════════════════════════════════════════════════════════════════


class TestAiGate:
    @pytest.mark.asyncio
    async def test_classify_parts_batch_empty(self):
        from app.services.ics_worker.ai_gate import classify_parts_batch
        result = await classify_parts_batch([])
        assert result == []

    @pytest.mark.asyncio
    async def test_classify_parts_batch_success(self):
        from app.services.ics_worker.ai_gate import classify_parts_batch

        mock_response = {
            "classifications": [
                {"mpn": "STM32F103", "search_ics": True, "commodity": "semiconductor", "reason": "MCU"}
            ]
        }
        with patch("app.utils.llm_router.routed_structured", new_callable=AsyncMock, return_value=mock_response):
            result = await classify_parts_batch([{"mpn": "STM32F103", "manufacturer": "ST", "description": "MCU"}])

        assert len(result) == 1
        assert result[0]["search_ics"] is True

    @pytest.mark.asyncio
    async def test_classify_parts_batch_api_failure(self):
        from app.services.ics_worker.ai_gate import classify_parts_batch

        with patch("app.utils.llm_router.routed_structured", new_callable=AsyncMock, side_effect=Exception("API error")):
            result = await classify_parts_batch([{"mpn": "STM32F103", "manufacturer": "ST", "description": ""}])

        assert result is None

    @pytest.mark.asyncio
    async def test_classify_parts_batch_bad_format(self):
        from app.services.ics_worker.ai_gate import classify_parts_batch

        with patch("app.utils.llm_router.routed_structured", new_callable=AsyncMock, return_value={"bad": "format"}):
            result = await classify_parts_batch([{"mpn": "X", "manufacturer": "", "description": ""}])

        assert result is None

    @pytest.mark.asyncio
    async def test_process_ai_gate_no_pending(self, db_session):
        from app.services.ics_worker.ai_gate import process_ai_gate
        # No pending items — should return without doing anything
        await process_ai_gate(db_session)

    @pytest.mark.asyncio
    async def test_process_ai_gate_with_cache(self, db_session, test_requisition):
        from app.services.ics_worker.ai_gate import _classification_cache, clear_classification_cache, process_ai_gate
        clear_classification_cache()

        req = test_requisition.requirements[0]
        item = IcsSearchQueue(
            requirement_id=req.id, requisition_id=test_requisition.id,
            mpn="LM317T", normalized_mpn="LM317T",
            manufacturer="TI", status="pending",
        )
        db_session.add(item)
        db_session.commit()

        # Pre-populate cache
        _classification_cache[("LM317T", "ti")] = ("semiconductor", "search", "Voltage regulator IC")

        await process_ai_gate(db_session)

        db_session.refresh(item)
        assert item.status == "queued"
        assert "[cached]" in item.gate_reason
        clear_classification_cache()

    @pytest.mark.asyncio
    async def test_process_ai_gate_cooldown(self, db_session):
        import app.services.ics_worker.ai_gate as ai_gate_mod
        original = ai_gate_mod._last_api_failure
        ai_gate_mod._last_api_failure = time.monotonic()
        try:
            await ai_gate_mod.process_ai_gate(db_session)
            # Should return early due to cooldown
        finally:
            ai_gate_mod._last_api_failure = original

    def test_clear_classification_cache(self):
        from app.services.ics_worker.ai_gate import _classification_cache, clear_classification_cache
        _classification_cache[("test", "test")] = ("x", "y", "z")
        clear_classification_cache()
        assert len(_classification_cache) == 0


# ═══════════════════════════════════════════════════════════════════════
# SESSION MANAGER
# ═══════════════════════════════════════════════════════════════════════


class TestSessionManager:
    @pytest.mark.asyncio
    async def test_start_no_display(self):
        from app.services.ics_worker.session_manager import IcsSessionManager
        cfg = IcsConfig()
        sm = IcsSessionManager(cfg)
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("DISPLAY", None)
            with pytest.raises(RuntimeError, match="DISPLAY"):
                await sm.start()

    @pytest.mark.asyncio
    async def test_login_no_credentials(self):
        from app.services.ics_worker.session_manager import IcsSessionManager
        cfg = IcsConfig()
        cfg.ICS_USERNAME = ""
        cfg.ICS_PASSWORD = ""
        sm = IcsSessionManager(cfg)
        result = await sm.login()
        assert result is False

    @pytest.mark.asyncio
    async def test_login_exception(self):
        from app.services.ics_worker.session_manager import IcsSessionManager
        cfg = IcsConfig()
        cfg.ICS_USERNAME = "user"
        cfg.ICS_PASSWORD = "pass"
        sm = IcsSessionManager(cfg)
        sm._page = MagicMock()
        sm._page.goto = AsyncMock(side_effect=Exception("Browser error"))

        result = await sm.login()
        assert result is False
        assert sm.is_logged_in is False

    @pytest.mark.asyncio
    async def test_ensure_session_already_valid(self):
        from app.services.ics_worker.session_manager import IcsSessionManager
        cfg = IcsConfig()
        sm = IcsSessionManager(cfg)
        sm.check_session_health = AsyncMock(return_value=True)

        result = await sm.ensure_session()
        assert result is True
        assert sm.is_logged_in is True

    @pytest.mark.asyncio
    async def test_ensure_session_needs_login(self):
        from app.services.ics_worker.session_manager import IcsSessionManager
        cfg = IcsConfig()
        sm = IcsSessionManager(cfg)
        sm.check_session_health = AsyncMock(return_value=False)
        sm.login = AsyncMock(return_value=True)

        result = await sm.ensure_session()
        assert result is True
        sm.login.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop(self):
        from app.services.ics_worker.session_manager import IcsSessionManager
        cfg = IcsConfig()
        sm = IcsSessionManager(cfg)
        sm._context = AsyncMock()
        sm._playwright = AsyncMock()

        await sm.stop()
        assert sm._context is None
        assert sm._page is None
        assert sm._playwright is None
        assert sm.is_logged_in is False

    @pytest.mark.asyncio
    async def test_stop_with_error(self):
        from app.services.ics_worker.session_manager import IcsSessionManager
        cfg = IcsConfig()
        sm = IcsSessionManager(cfg)
        sm._context = AsyncMock()
        sm._context.close = AsyncMock(side_effect=Exception("Close error"))
        sm._playwright = AsyncMock()

        await sm.stop()
        assert sm._context is None

    def test_page_property(self):
        from app.services.ics_worker.session_manager import IcsSessionManager
        cfg = IcsConfig()
        sm = IcsSessionManager(cfg)
        sm._page = "test_page"
        assert sm.page == "test_page"

    @pytest.mark.asyncio
    async def test_check_session_health_exception(self):
        from app.services.ics_worker.session_manager import IcsSessionManager
        cfg = IcsConfig()
        sm = IcsSessionManager(cfg)
        sm._page = MagicMock()
        sm._page.goto = AsyncMock(side_effect=Exception("Network error"))

        result = await sm.check_session_health()
        assert result is False


# ═══════════════════════════════════════════════════════════════════════
# WORKER — update_worker_status + main loop
# ═══════════════════════════════════════════════════════════════════════


class TestWorker:
    def test_update_worker_status(self, db_session):
        from app.services.ics_worker.worker import update_worker_status

        # Create the singleton row
        ws = IcsWorkerStatus(id=1, is_running=False, searches_today=0)
        db_session.add(ws)
        db_session.commit()

        update_worker_status(db_session, is_running=True, searches_today=5)
        db_session.refresh(ws)
        assert ws.is_running is True
        assert ws.searches_today == 5

    def test_update_worker_status_no_row(self, db_session):
        from app.services.ics_worker.worker import update_worker_status
        # No status row — should not raise
        update_worker_status(db_session, is_running=True)

    def test_update_worker_status_ignores_bad_key(self, db_session):
        from app.services.ics_worker.worker import update_worker_status

        ws = IcsWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        # nonexistent_field should be ignored
        update_worker_status(db_session, nonexistent_field="should_be_ignored")
        db_session.refresh(ws)

    # Patch targets: lazy imports inside main() resolve from source modules
    _DB = "app.database.SessionLocal"
    _SESSION = "app.services.ics_worker.session_manager.IcsSessionManager"
    _CONFIG = "app.services.ics_worker.config.IcsConfig"
    _RECOVER = "app.services.ics_worker.queue_manager.recover_stale_searches"

    def _make_mock_db(self, db_session):
        """Create a mock SessionLocal that returns a proxy session that won't actually close."""
        mock_session = MagicMock(wraps=db_session)
        mock_session.close = MagicMock()
        return MagicMock(return_value=mock_session)

    @pytest.mark.asyncio
    async def test_main_browser_start_failure(self, db_session):
        """Worker exits gracefully when browser fails to start."""
        import app.services.ics_worker.worker as worker_mod

        ws = IcsWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        mock_session = AsyncMock()
        mock_session.start = AsyncMock(side_effect=Exception("No DISPLAY"))

        original_shutdown = worker_mod._shutdown_requested
        try:
            worker_mod._shutdown_requested = False

            with patch(self._DB, self._make_mock_db(db_session)):
                with patch(self._SESSION, return_value=mock_session):
                    with patch(self._RECOVER):
                        await worker_mod.main()
        finally:
            worker_mod._shutdown_requested = original_shutdown

    @pytest.mark.asyncio
    async def test_main_login_failure(self, db_session):
        """Worker exits gracefully when login fails."""
        import app.services.ics_worker.worker as worker_mod

        ws = IcsWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        mock_session = AsyncMock()
        mock_session.start = AsyncMock()
        mock_session.is_logged_in = False
        mock_session.login = AsyncMock(return_value=False)
        mock_session.stop = AsyncMock()

        original_shutdown = worker_mod._shutdown_requested
        try:
            worker_mod._shutdown_requested = False

            with patch(self._DB, self._make_mock_db(db_session)):
                with patch(self._SESSION, return_value=mock_session):
                    with patch(self._RECOVER):
                        await worker_mod.main()
        finally:
            worker_mod._shutdown_requested = original_shutdown

        mock_session.stop.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# __main__ MODULE
# ═══════════════════════════════════════════════════════════════════════


class TestMainModule:
    def test_main_module_import(self):
        """Verify the __main__ module imports correctly."""
        import importlib
        spec = importlib.util.find_spec("app.services.ics_worker.__main__")
        assert spec is not None


# ═══════════════════════════════════════════════════════════════════════
# SIGNAL HANDLER
# ═══════════════════════════════════════════════════════════════════════


class TestSignalHandler:
    def test_handle_shutdown(self):
        from app.services.ics_worker import worker as worker_mod
        original = worker_mod._shutdown_requested
        try:
            worker_mod._handle_shutdown(15, None)
            assert worker_mod._shutdown_requested is True
        finally:
            worker_mod._shutdown_requested = original
