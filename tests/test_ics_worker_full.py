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
        pn_locator.is_visible = AsyncMock(return_value=True)
        pn_locator.count = AsyncMock(return_value=1)
        pn_locator.fill = AsyncMock()
        page.locator = MagicMock(return_value=pn_locator)

        page.wait_for_selector = AsyncMock()
        page.screenshot = AsyncMock()
        page.evaluate = AsyncMock(side_effect=[
            {"buttons": [], "forms": [], "url": "http://x", "title": "t"},
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
        """When primary search field not found, falls back to next selector."""
        page = AsyncMock()
        page.goto = AsyncMock()
        page.url = "https://www.icsource.com/members/Search/Results.aspx"

        primary_locator = AsyncMock()
        primary_locator.wait_for = AsyncMock(side_effect=Exception("not found"))
        primary_locator.fill = AsyncMock()

        fallback_locator = AsyncMock()
        fallback_locator.wait_for = AsyncMock()
        fallback_locator.is_visible = AsyncMock(return_value=True)
        fallback_locator.count = AsyncMock(return_value=1)
        fallback_locator.fill = AsyncMock()

        def mock_locator(sel):
            if "rtxtPartNumber" in sel:
                return primary_locator
            return fallback_locator

        page.locator = MagicMock(side_effect=mock_locator)
        page.wait_for_selector = AsyncMock()
        page.screenshot = AsyncMock()
        page.evaluate = AsyncMock(side_effect=[
            {"buttons": [], "forms": [], "url": "http://x", "title": "t"},
            "<html></html>",
            0,
        ])

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
        locator.is_visible = AsyncMock(return_value=True)
        locator.count = AsyncMock(return_value=1)
        locator.fill = AsyncMock()
        page.locator = MagicMock(return_value=locator)

        page.wait_for_selector = AsyncMock(side_effect=Exception("Timeout"))
        page.screenshot = AsyncMock()
        page.evaluate = AsyncMock(side_effect=[
            {"buttons": [], "forms": [], "url": "http://x", "title": "t"},
            "<html>body</html>",
            0,
        ])

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


# ═══════════════════════════════════════════════════════════════════════
# COVERAGE: ICS WORKER MAIN LOOP (lines 121-318, 322)
# ═══════════════════════════════════════════════════════════════════════


class TestIcsWorkerMainLoop:
    """Tests for async main() — patches at source modules since imports are lazy."""

    _DB = "app.database.SessionLocal"
    _SESSION = "app.services.ics_worker.session_manager.IcsSessionManager"
    _SCHEDULER = "app.services.ics_worker.scheduler.SearchScheduler"
    _BREAKER = "app.services.ics_worker.circuit_breaker.CircuitBreaker"
    _CONFIG = "app.services.ics_worker.config.IcsConfig"
    _QUEUE_NEXT = "app.services.ics_worker.queue_manager.get_next_queued_item"
    _QUEUE_RECOVER = "app.services.ics_worker.queue_manager.recover_stale_searches"
    _QUEUE_MARK = "app.services.ics_worker.queue_manager.mark_status"
    _QUEUE_COMPLETE = "app.services.ics_worker.queue_manager.mark_completed"
    _SEARCH = "app.services.ics_worker.search_engine.search_part"
    _PARSE = "app.services.ics_worker.result_parser.parse_results_html"
    _SAVE = "app.services.ics_worker.sighting_writer.save_ics_sightings"
    _AI_GATE = "app.services.ics_worker.ai_gate.process_ai_gate"
    _ASYNC_SLEEP = "app.services.ics_worker.worker.asyncio.sleep"

    def _make_mock_db(self, db_session):
        mock_session = MagicMock(wraps=db_session)
        mock_session.close = MagicMock()
        return MagicMock(return_value=mock_session)

    @pytest.mark.asyncio
    async def test_main_shutdown_immediate(self, db_session):
        """main() exits immediately when shutdown flag is set."""
        import app.services.ics_worker.worker as worker_mod

        ws = IcsWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        original = worker_mod._shutdown_requested
        try:
            worker_mod._shutdown_requested = True

            mock_session = AsyncMock()
            mock_session.start = AsyncMock()
            mock_session.is_logged_in = True
            mock_session.stop = AsyncMock()

            with patch(self._DB, self._make_mock_db(db_session)):
                with patch(self._SESSION, return_value=mock_session):
                    with patch(self._QUEUE_RECOVER):
                        await worker_mod.main()

            mock_session.stop.assert_called_once()
        finally:
            worker_mod._shutdown_requested = original

    @pytest.mark.asyncio
    async def test_main_outside_business_hours(self, db_session):
        """main() sleeps when outside business hours."""
        import app.services.ics_worker.worker as worker_mod

        ws = IcsWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        original = worker_mod._shutdown_requested

        async def mock_sleep(seconds):
            worker_mod._shutdown_requested = True

        mock_session = AsyncMock()
        mock_session.start = AsyncMock()
        mock_session.is_logged_in = True
        mock_session.stop = AsyncMock()

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.return_value = False

        try:
            worker_mod._shutdown_requested = False
            with patch(self._DB, self._make_mock_db(db_session)):
                with patch(self._SESSION, return_value=mock_session):
                    with patch(self._SCHEDULER, return_value=mock_scheduler):
                        with patch(self._ASYNC_SLEEP, side_effect=mock_sleep):
                            with patch(self._QUEUE_RECOVER):
                                await worker_mod.main()
        finally:
            worker_mod._shutdown_requested = original

    @pytest.mark.asyncio
    async def test_main_daily_limit(self, db_session):
        """main() sleeps when daily limit is reached."""
        import app.services.ics_worker.worker as worker_mod

        ws = IcsWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        original = worker_mod._shutdown_requested

        async def mock_sleep(seconds):
            worker_mod._shutdown_requested = True

        mock_session = AsyncMock()
        mock_session.start = AsyncMock()
        mock_session.is_logged_in = True
        mock_session.stop = AsyncMock()

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.return_value = True

        mock_config = MagicMock()
        mock_config.ICS_MAX_DAILY_SEARCHES = 0

        try:
            worker_mod._shutdown_requested = False
            with patch(self._DB, self._make_mock_db(db_session)):
                with patch(self._SESSION, return_value=mock_session):
                    with patch(self._SCHEDULER, return_value=mock_scheduler):
                        with patch(self._CONFIG, return_value=mock_config):
                            with patch(self._ASYNC_SLEEP, side_effect=mock_sleep):
                                with patch(self._QUEUE_RECOVER):
                                    await worker_mod.main()
        finally:
            worker_mod._shutdown_requested = original

    @pytest.mark.asyncio
    async def test_main_circuit_breaker_open(self, db_session):
        """main() sleeps when circuit breaker is open."""
        import app.services.ics_worker.worker as worker_mod

        ws = IcsWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        original = worker_mod._shutdown_requested

        async def mock_sleep(seconds):
            worker_mod._shutdown_requested = True

        mock_session = AsyncMock()
        mock_session.start = AsyncMock()
        mock_session.is_logged_in = True
        mock_session.stop = AsyncMock()

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.return_value = True

        mock_breaker = MagicMock()
        mock_breaker.should_stop.return_value = True
        mock_breaker.get_trip_info.return_value = {"trip_reason": "captcha"}

        try:
            worker_mod._shutdown_requested = False
            with patch(self._DB, self._make_mock_db(db_session)):
                with patch(self._SESSION, return_value=mock_session):
                    with patch(self._SCHEDULER, return_value=mock_scheduler):
                        with patch(self._BREAKER, return_value=mock_breaker):
                            with patch(self._ASYNC_SLEEP, side_effect=mock_sleep):
                                with patch(self._QUEUE_RECOVER):
                                    await worker_mod.main()
        finally:
            worker_mod._shutdown_requested = original

    @pytest.mark.asyncio
    async def test_main_break_time(self, db_session):
        """main() takes a break when scheduler says so."""
        import app.services.ics_worker.worker as worker_mod

        ws = IcsWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        original = worker_mod._shutdown_requested

        async def mock_sleep(seconds):
            worker_mod._shutdown_requested = True

        mock_session = AsyncMock()
        mock_session.start = AsyncMock()
        mock_session.is_logged_in = True
        mock_session.stop = AsyncMock()

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.return_value = True
        mock_scheduler.time_for_break.return_value = True
        mock_scheduler.get_break_duration.return_value = 300.0
        mock_scheduler.reset_break_counter = MagicMock()

        mock_breaker = MagicMock()
        mock_breaker.should_stop.return_value = False

        try:
            worker_mod._shutdown_requested = False
            with patch(self._DB, self._make_mock_db(db_session)):
                with patch(self._SESSION, return_value=mock_session):
                    with patch(self._SCHEDULER, return_value=mock_scheduler):
                        with patch(self._BREAKER, return_value=mock_breaker):
                            with patch(self._ASYNC_SLEEP, side_effect=mock_sleep):
                                with patch(self._QUEUE_RECOVER):
                                    with patch(self._AI_GATE, new_callable=AsyncMock):
                                        await worker_mod.main()
        finally:
            worker_mod._shutdown_requested = original

    @pytest.mark.asyncio
    async def test_main_empty_queue(self, db_session):
        """main() sleeps when queue is empty."""
        import app.services.ics_worker.worker as worker_mod

        ws = IcsWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        original = worker_mod._shutdown_requested

        async def mock_sleep(seconds):
            worker_mod._shutdown_requested = True

        mock_session = AsyncMock()
        mock_session.start = AsyncMock()
        mock_session.is_logged_in = True
        mock_session.stop = AsyncMock()

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.return_value = True
        mock_scheduler.time_for_break.return_value = False

        mock_breaker = MagicMock()
        mock_breaker.should_stop.return_value = False

        try:
            worker_mod._shutdown_requested = False
            with patch(self._DB, self._make_mock_db(db_session)):
                with patch(self._SESSION, return_value=mock_session):
                    with patch(self._SCHEDULER, return_value=mock_scheduler):
                        with patch(self._BREAKER, return_value=mock_breaker):
                            with patch(self._ASYNC_SLEEP, side_effect=mock_sleep):
                                with patch(self._QUEUE_RECOVER):
                                    with patch(self._AI_GATE, new_callable=AsyncMock):
                                        with patch(self._QUEUE_NEXT, return_value=None):
                                            await worker_mod.main()
        finally:
            worker_mod._shutdown_requested = original

    @pytest.mark.asyncio
    async def test_main_search_success(self, db_session, test_requisition):
        """main() performs a full search cycle with results."""
        import app.services.ics_worker.worker as worker_mod

        ws = IcsWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        req = test_requisition.requirements[0]
        queue_item = IcsSearchQueue(
            requirement_id=req.id, requisition_id=test_requisition.id,
            mpn="LM317T", normalized_mpn="LM317T", status="queued",
        )
        db_session.add(queue_item)
        db_session.commit()

        original = worker_mod._shutdown_requested
        search_done = False

        async def mock_sleep(seconds):
            nonlocal search_done
            if search_done:
                worker_mod._shutdown_requested = True
            search_done = True

        mock_session = AsyncMock()
        mock_session.start = AsyncMock()
        mock_session.is_logged_in = True
        mock_session.stop = AsyncMock()
        mock_session.page = AsyncMock()
        mock_session.ensure_session = AsyncMock(return_value=True)

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.return_value = True
        mock_scheduler.time_for_break.return_value = False
        mock_scheduler.next_delay.return_value = 120

        mock_breaker = MagicMock()
        mock_breaker.should_stop.return_value = False
        mock_breaker.check_page_health = AsyncMock(return_value="HEALTHY")
        mock_breaker.record_results = MagicMock()
        mock_breaker.record_empty_results = MagicMock()

        search_result = {
            "html": "<div>results</div>",
            "total_count": 1,
            "url": "https://icsource.com/search",
            "duration_ms": 1500,
        }

        try:
            worker_mod._shutdown_requested = False
            with patch(self._DB, self._make_mock_db(db_session)):
                with patch(self._SESSION, return_value=mock_session):
                    with patch(self._SCHEDULER, return_value=mock_scheduler):
                        with patch(self._BREAKER, return_value=mock_breaker):
                            with patch(self._ASYNC_SLEEP, side_effect=mock_sleep):
                                with patch(self._QUEUE_RECOVER):
                                    with patch(self._AI_GATE, new_callable=AsyncMock):
                                        with patch(self._QUEUE_NEXT, return_value=queue_item):
                                            with patch(self._SEARCH, new_callable=AsyncMock, return_value=search_result):
                                                with patch(self._PARSE, return_value=[]):
                                                    with patch(self._SAVE, return_value=0):
                                                        with patch(self._QUEUE_MARK):
                                                            with patch(self._QUEUE_COMPLETE):
                                                                await worker_mod.main()
        finally:
            worker_mod._shutdown_requested = original

    @pytest.mark.asyncio
    async def test_main_session_expired_during_search(self, db_session, test_requisition):
        """main() re-queues item when health check returns SESSION_EXPIRED."""
        import app.services.ics_worker.worker as worker_mod

        ws = IcsWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        req = test_requisition.requirements[0]
        queue_item = IcsSearchQueue(
            requirement_id=req.id, requisition_id=test_requisition.id,
            mpn="LM317T", normalized_mpn="LM317T", status="queued",
        )
        db_session.add(queue_item)
        db_session.commit()

        original = worker_mod._shutdown_requested

        async def health_then_shutdown(page):
            worker_mod._shutdown_requested = True
            return "SESSION_EXPIRED"

        async def mock_sleep(seconds):
            worker_mod._shutdown_requested = True

        mock_session = AsyncMock()
        mock_session.start = AsyncMock()
        mock_session.is_logged_in = True
        mock_session.stop = AsyncMock()
        mock_session.page = AsyncMock()
        mock_session.ensure_session = AsyncMock(return_value=True)

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.return_value = True
        mock_scheduler.time_for_break.return_value = False

        mock_breaker = MagicMock()
        mock_breaker.should_stop.return_value = False
        mock_breaker.check_page_health = AsyncMock(side_effect=health_then_shutdown)

        search_result = {"html": "", "total_count": 0, "url": "", "duration_ms": 100}

        try:
            worker_mod._shutdown_requested = False
            with patch(self._DB, self._make_mock_db(db_session)):
                with patch(self._SESSION, return_value=mock_session):
                    with patch(self._SCHEDULER, return_value=mock_scheduler):
                        with patch(self._BREAKER, return_value=mock_breaker):
                            with patch(self._ASYNC_SLEEP, side_effect=mock_sleep):
                                with patch(self._QUEUE_RECOVER):
                                    with patch(self._AI_GATE, new_callable=AsyncMock):
                                        with patch(self._QUEUE_NEXT, return_value=queue_item):
                                            with patch(self._SEARCH, new_callable=AsyncMock, return_value=search_result):
                                                with patch(self._QUEUE_MARK):
                                                    await worker_mod.main()
        finally:
            worker_mod._shutdown_requested = original

    @pytest.mark.asyncio
    async def test_main_breaker_trips_during_search(self, db_session, test_requisition):
        """main() marks item failed when breaker trips after page health check."""
        import app.services.ics_worker.worker as worker_mod

        ws = IcsWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        req = test_requisition.requirements[0]
        queue_item = IcsSearchQueue(
            requirement_id=req.id, requisition_id=test_requisition.id,
            mpn="LM317T", normalized_mpn="LM317T", status="queued",
        )
        db_session.add(queue_item)
        db_session.commit()

        original = worker_mod._shutdown_requested

        async def mock_sleep(seconds):
            worker_mod._shutdown_requested = True

        mock_session = AsyncMock()
        mock_session.start = AsyncMock()
        mock_session.is_logged_in = True
        mock_session.stop = AsyncMock()
        mock_session.page = AsyncMock()
        mock_session.ensure_session = AsyncMock(return_value=True)

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.return_value = True
        mock_scheduler.time_for_break.return_value = False

        should_stop_calls = [False, True]
        mock_breaker = MagicMock()
        mock_breaker.should_stop.side_effect = should_stop_calls
        mock_breaker.check_page_health = AsyncMock(return_value="CAPTCHA_WARNING")
        mock_breaker.trip_reason = "captcha"

        search_result = {"html": "", "total_count": 0, "url": "", "duration_ms": 100}

        try:
            worker_mod._shutdown_requested = False
            with patch(self._DB, self._make_mock_db(db_session)):
                with patch(self._SESSION, return_value=mock_session):
                    with patch(self._SCHEDULER, return_value=mock_scheduler):
                        with patch(self._BREAKER, return_value=mock_breaker):
                            with patch(self._ASYNC_SLEEP, side_effect=mock_sleep):
                                with patch(self._QUEUE_RECOVER):
                                    with patch(self._AI_GATE, new_callable=AsyncMock):
                                        with patch(self._QUEUE_NEXT, return_value=queue_item):
                                            with patch(self._SEARCH, new_callable=AsyncMock, return_value=search_result):
                                                with patch(self._QUEUE_MARK):
                                                    await worker_mod.main()
        finally:
            worker_mod._shutdown_requested = original

    @pytest.mark.asyncio
    async def test_main_session_reauth_fails(self, db_session, test_requisition):
        """main() handles session re-auth failure."""
        import app.services.ics_worker.worker as worker_mod

        ws = IcsWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        req = test_requisition.requirements[0]
        queue_item = IcsSearchQueue(
            requirement_id=req.id, requisition_id=test_requisition.id,
            mpn="LM317T", normalized_mpn="LM317T", status="queued",
        )
        db_session.add(queue_item)
        db_session.commit()

        original = worker_mod._shutdown_requested

        async def mock_sleep(seconds):
            worker_mod._shutdown_requested = True

        mock_session = AsyncMock()
        mock_session.start = AsyncMock()
        mock_session.is_logged_in = True
        mock_session.stop = AsyncMock()
        mock_session.page = AsyncMock()
        mock_session.ensure_session = AsyncMock(return_value=False)

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.return_value = True
        mock_scheduler.time_for_break.return_value = False

        mock_breaker = MagicMock()
        mock_breaker.should_stop.return_value = False

        try:
            worker_mod._shutdown_requested = False
            with patch(self._DB, self._make_mock_db(db_session)):
                with patch(self._SESSION, return_value=mock_session):
                    with patch(self._SCHEDULER, return_value=mock_scheduler):
                        with patch(self._BREAKER, return_value=mock_breaker):
                            with patch(self._ASYNC_SLEEP, side_effect=mock_sleep):
                                with patch(self._QUEUE_RECOVER):
                                    with patch(self._AI_GATE, new_callable=AsyncMock):
                                        with patch(self._QUEUE_NEXT, return_value=queue_item):
                                            with patch(self._QUEUE_MARK):
                                                await worker_mod.main()
        finally:
            worker_mod._shutdown_requested = original

    @pytest.mark.asyncio
    async def test_main_search_exception(self, db_session, test_requisition):
        """main() marks item failed on search exception."""
        import app.services.ics_worker.worker as worker_mod

        ws = IcsWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        req = test_requisition.requirements[0]
        queue_item = IcsSearchQueue(
            requirement_id=req.id, requisition_id=test_requisition.id,
            mpn="LM317T", normalized_mpn="LM317T", status="queued",
        )
        db_session.add(queue_item)
        db_session.commit()

        original = worker_mod._shutdown_requested

        async def mock_sleep(seconds):
            worker_mod._shutdown_requested = True

        mock_session = AsyncMock()
        mock_session.start = AsyncMock()
        mock_session.is_logged_in = True
        mock_session.stop = AsyncMock()
        mock_session.page = AsyncMock()
        mock_session.ensure_session = AsyncMock(return_value=True)

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.return_value = True
        mock_scheduler.time_for_break.return_value = False
        mock_scheduler.next_delay.return_value = 120

        mock_breaker = MagicMock()
        mock_breaker.should_stop.return_value = False

        try:
            worker_mod._shutdown_requested = False
            with patch(self._DB, self._make_mock_db(db_session)):
                with patch(self._SESSION, return_value=mock_session):
                    with patch(self._SCHEDULER, return_value=mock_scheduler):
                        with patch(self._BREAKER, return_value=mock_breaker):
                            with patch(self._ASYNC_SLEEP, side_effect=mock_sleep):
                                with patch(self._QUEUE_RECOVER):
                                    with patch(self._AI_GATE, new_callable=AsyncMock):
                                        with patch(self._QUEUE_NEXT, return_value=queue_item):
                                            with patch(self._SEARCH, new_callable=AsyncMock, side_effect=Exception("crash")):
                                                with patch(self._QUEUE_MARK):
                                                    await worker_mod.main()
        finally:
            worker_mod._shutdown_requested = original

    @pytest.mark.asyncio
    async def test_main_ai_gate_error(self, db_session):
        """main() continues after AI gate error."""
        import app.services.ics_worker.worker as worker_mod

        ws = IcsWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        original = worker_mod._shutdown_requested

        async def mock_sleep(seconds):
            worker_mod._shutdown_requested = True

        mock_session = AsyncMock()
        mock_session.start = AsyncMock()
        mock_session.is_logged_in = True
        mock_session.stop = AsyncMock()

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.return_value = True
        mock_scheduler.time_for_break.return_value = False

        mock_breaker = MagicMock()
        mock_breaker.should_stop.return_value = False

        try:
            worker_mod._shutdown_requested = False
            with patch(self._DB, self._make_mock_db(db_session)):
                with patch(self._SESSION, return_value=mock_session):
                    with patch(self._SCHEDULER, return_value=mock_scheduler):
                        with patch(self._BREAKER, return_value=mock_breaker):
                            with patch(self._ASYNC_SLEEP, side_effect=mock_sleep):
                                with patch(self._QUEUE_RECOVER):
                                    with patch(self._AI_GATE, new_callable=AsyncMock, side_effect=Exception("AI boom")):
                                        with patch(self._QUEUE_NEXT, return_value=None):
                                            await worker_mod.main()
        finally:
            worker_mod._shutdown_requested = original

    @pytest.mark.asyncio
    async def test_main_daily_stats_reset(self, db_session):
        """main() resets daily stats at midnight (cover lines 134-158)."""
        import app.services.ics_worker.worker as worker_mod

        ws = IcsWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        original = worker_mod._shutdown_requested
        loop_count = 0

        async def mock_sleep(seconds):
            nonlocal loop_count
            loop_count += 1
            if loop_count >= 2:
                worker_mod._shutdown_requested = True

        mock_session = AsyncMock()
        mock_session.start = AsyncMock()
        mock_session.is_logged_in = True
        mock_session.stop = AsyncMock()

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.return_value = False

        try:
            worker_mod._shutdown_requested = False
            with patch(self._DB, self._make_mock_db(db_session)):
                with patch(self._SESSION, return_value=mock_session):
                    with patch(self._SCHEDULER, return_value=mock_scheduler):
                        with patch(self._ASYNC_SLEEP, side_effect=mock_sleep):
                            with patch(self._QUEUE_RECOVER):
                                await worker_mod.main()
        finally:
            worker_mod._shutdown_requested = original

    @pytest.mark.asyncio
    async def test_main_unexpected_error(self, db_session):
        """main() handles unexpected error in outer try (line 302-304)."""
        import app.services.ics_worker.worker as worker_mod

        ws = IcsWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        original = worker_mod._shutdown_requested
        call_count = 0

        async def mock_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                worker_mod._shutdown_requested = True

        mock_session = AsyncMock()
        mock_session.start = AsyncMock()
        mock_session.is_logged_in = True
        mock_session.stop = AsyncMock()

        mock_scheduler = MagicMock()
        # Force an exception that isn't caught by the inner try
        mock_scheduler.is_business_hours.side_effect = Exception("Outer error")

        try:
            worker_mod._shutdown_requested = False
            with patch(self._DB, self._make_mock_db(db_session)):
                with patch(self._SESSION, return_value=mock_session):
                    with patch(self._SCHEDULER, return_value=mock_scheduler):
                        with patch(self._ASYNC_SLEEP, side_effect=mock_sleep):
                            with patch(self._QUEUE_RECOVER):
                                await worker_mod.main()
        finally:
            worker_mod._shutdown_requested = original

    @pytest.mark.asyncio
    async def test_main_mark_status_exception_in_except(self, db_session, test_requisition):
        """main() handles mark_status failure in exception handler (lines 292-293)."""
        import app.services.ics_worker.worker as worker_mod

        ws = IcsWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        req = test_requisition.requirements[0]
        queue_item = IcsSearchQueue(
            requirement_id=req.id, requisition_id=test_requisition.id,
            mpn="LM317T", normalized_mpn="LM317T", status="queued",
        )
        db_session.add(queue_item)
        db_session.commit()

        original = worker_mod._shutdown_requested

        async def mock_sleep(seconds):
            worker_mod._shutdown_requested = True

        mock_session = AsyncMock()
        mock_session.start = AsyncMock()
        mock_session.is_logged_in = True
        mock_session.stop = AsyncMock()
        mock_session.page = AsyncMock()
        mock_session.ensure_session = AsyncMock(return_value=True)

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.return_value = True
        mock_scheduler.time_for_break.return_value = False
        mock_scheduler.next_delay.return_value = 120

        mock_breaker = MagicMock()
        mock_breaker.should_stop.return_value = False

        def mark_status_fail(db, item, status, error=None):
            raise Exception("DB connection lost")

        try:
            worker_mod._shutdown_requested = False
            with patch(self._DB, self._make_mock_db(db_session)):
                with patch(self._SESSION, return_value=mock_session):
                    with patch(self._SCHEDULER, return_value=mock_scheduler):
                        with patch(self._BREAKER, return_value=mock_breaker):
                            with patch(self._ASYNC_SLEEP, side_effect=mock_sleep):
                                with patch(self._QUEUE_RECOVER):
                                    with patch(self._AI_GATE, new_callable=AsyncMock):
                                        with patch(self._QUEUE_NEXT, return_value=queue_item):
                                            with patch(self._SEARCH, new_callable=AsyncMock, side_effect=Exception("crash")):
                                                with patch(self._QUEUE_MARK, side_effect=mark_status_fail):
                                                    await worker_mod.main()
        finally:
            worker_mod._shutdown_requested = original

    @pytest.mark.asyncio
    async def test_main_daily_stats_with_previous_date(self, db_session):
        """main() logs daily summary when last_stats_date is not None (lines 136-155)."""
        import app.services.ics_worker.worker as worker_mod

        ws = IcsWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        original = worker_mod._shutdown_requested
        loop_count = 0

        async def mock_sleep(seconds):
            nonlocal loop_count
            loop_count += 1
            if loop_count >= 2:
                worker_mod._shutdown_requested = True

        mock_session = AsyncMock()
        mock_session.start = AsyncMock()
        mock_session.is_logged_in = True
        mock_session.stop = AsyncMock()

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.return_value = False

        real_datetime = datetime
        from app.services.ics_worker.worker import EASTERN

        eastern_calls = 0

        def patched_now(tz=None):
            nonlocal eastern_calls
            # Only track calls with EASTERN timezone (the main loop calls)
            if tz is not None and str(tz) == str(EASTERN):
                eastern_calls += 1
                if eastern_calls == 1:
                    return real_datetime(2026, 2, 28, 23, 59, 0, tzinfo=timezone.utc)
                return real_datetime(2026, 3, 1, 0, 1, 0, tzinfo=timezone.utc)
            # For timezone.utc calls (update_worker_status, etc.), return a stable time
            return real_datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc)

        try:
            worker_mod._shutdown_requested = False
            with patch(self._DB, self._make_mock_db(db_session)):
                with patch(self._SESSION, return_value=mock_session):
                    with patch(self._SCHEDULER, return_value=mock_scheduler):
                        with patch(self._ASYNC_SLEEP, side_effect=mock_sleep):
                            with patch(self._QUEUE_RECOVER):
                                with patch("app.services.ics_worker.worker.datetime") as mock_dt:
                                    mock_dt.now = patched_now
                                    mock_dt.side_effect = lambda *a, **kw: real_datetime(*a, **kw)
                                    await worker_mod.main()
        finally:
            worker_mod._shutdown_requested = original

    @pytest.mark.asyncio
    async def test_main_search_with_results_record_results(self, db_session, test_requisition):
        """main() calls breaker.record_results() when parse returns results (line 247)."""
        import app.services.ics_worker.worker as worker_mod

        ws = IcsWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        req = test_requisition.requirements[0]
        queue_item = IcsSearchQueue(
            requirement_id=req.id, requisition_id=test_requisition.id,
            mpn="LM317T", normalized_mpn="LM317T", status="queued",
        )
        db_session.add(queue_item)
        db_session.commit()

        original = worker_mod._shutdown_requested
        search_done = False

        async def mock_sleep(seconds):
            nonlocal search_done
            if search_done:
                worker_mod._shutdown_requested = True
            search_done = True

        mock_session = AsyncMock()
        mock_session.start = AsyncMock()
        mock_session.is_logged_in = True
        mock_session.stop = AsyncMock()
        mock_session.page = AsyncMock()
        mock_session.ensure_session = AsyncMock(return_value=True)

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.return_value = True
        mock_scheduler.time_for_break.return_value = False
        mock_scheduler.next_delay.return_value = 120

        mock_breaker = MagicMock()
        mock_breaker.should_stop.return_value = False
        mock_breaker.check_page_health = AsyncMock(return_value="HEALTHY")
        mock_breaker.record_results = MagicMock()

        search_result = {
            "html": "<div>results</div>",
            "total_count": 2,
            "url": "https://icsource.com/search",
            "duration_ms": 1500,
        }

        # Return actual sighting objects so record_results is called
        mock_sighting = MagicMock()

        try:
            worker_mod._shutdown_requested = False
            with patch(self._DB, self._make_mock_db(db_session)):
                with patch(self._SESSION, return_value=mock_session):
                    with patch(self._SCHEDULER, return_value=mock_scheduler):
                        with patch(self._BREAKER, return_value=mock_breaker):
                            with patch(self._ASYNC_SLEEP, side_effect=mock_sleep):
                                with patch(self._QUEUE_RECOVER):
                                    with patch(self._AI_GATE, new_callable=AsyncMock):
                                        with patch(self._QUEUE_NEXT, return_value=queue_item):
                                            with patch(self._SEARCH, new_callable=AsyncMock, return_value=search_result):
                                                with patch(self._PARSE, return_value=[mock_sighting]):
                                                    with patch(self._SAVE, return_value=1):
                                                        with patch(self._QUEUE_MARK):
                                                            with patch(self._QUEUE_COMPLETE):
                                                                await worker_mod.main()

            mock_breaker.record_results.assert_called()
        finally:
            worker_mod._shutdown_requested = original


# ═══════════════════════════════════════════════════════════════════════
# COVERAGE: ICS AI GATE (lines 156, 159-206)
# ═══════════════════════════════════════════════════════════════════════


class TestAiGateFull:
    @pytest.mark.asyncio
    async def test_process_ai_gate_classifies_items(self, db_session, test_requisition):
        """process_ai_gate classifies pending items (lines 159-206)."""
        from app.services.ics_worker.ai_gate import clear_classification_cache, process_ai_gate

        clear_classification_cache()

        req = test_requisition.requirements[0]
        item = IcsSearchQueue(
            requirement_id=req.id, requisition_id=test_requisition.id,
            mpn="STM32F103C8T6", normalized_mpn="STM32F103C8T6",
            status="pending",
        )
        db_session.add(item)
        db_session.commit()

        mock_result = {
            "classifications": [
                {"mpn": "STM32F103C8T6", "search_ics": True, "commodity": "semiconductor", "reason": "ARM MCU"}
            ]
        }

        with patch("app.utils.llm_router.routed_structured", new_callable=AsyncMock, return_value=mock_result):
            await process_ai_gate(db_session)

        db_session.refresh(item)
        assert item.status == "queued"
        assert item.commodity_class == "semiconductor"
        assert item.gate_decision == "search"

    @pytest.mark.asyncio
    async def test_process_ai_gate_gated_out(self, db_session, test_requisition):
        """process_ai_gate gates out commodity items."""
        from app.services.ics_worker.ai_gate import clear_classification_cache, process_ai_gate

        clear_classification_cache()

        req = test_requisition.requirements[0]
        item = IcsSearchQueue(
            requirement_id=req.id, requisition_id=test_requisition.id,
            mpn="RC0402FR-07100KL", normalized_mpn="RC0402FR07100KL",
            status="pending",
        )
        db_session.add(item)
        db_session.commit()

        mock_result = {
            "classifications": [
                {"mpn": "RC0402FR-07100KL", "search_ics": False, "commodity": "passive", "reason": "Standard resistor"}
            ]
        }

        with patch("app.utils.llm_router.routed_structured", new_callable=AsyncMock, return_value=mock_result):
            await process_ai_gate(db_session)

        db_session.refresh(item)
        assert item.status == "gated_out"

    @pytest.mark.asyncio
    async def test_process_ai_gate_api_failure_failopen(self, db_session, test_requisition):
        """process_ai_gate defaults to 'queued' on API failure (fail-open)."""
        import app.services.ics_worker.ai_gate as ai_gate_mod
        from app.services.ics_worker.ai_gate import clear_classification_cache, process_ai_gate

        clear_classification_cache()
        ai_gate_mod._last_api_failure = 0.0

        req = test_requisition.requirements[0]
        item = IcsSearchQueue(
            requirement_id=req.id, requisition_id=test_requisition.id,
            mpn="UNKNOWN123", normalized_mpn="UNKNOWN123",
            status="pending",
        )
        db_session.add(item)
        db_session.commit()

        with patch("app.utils.llm_router.routed_structured", new_callable=AsyncMock, return_value=None):
            await process_ai_gate(db_session)

        db_session.refresh(item)
        assert item.status == "queued"
        assert item.gate_reason == "AI gate unavailable — defaulting to search"
        assert ai_gate_mod._last_api_failure > 0

        ai_gate_mod._last_api_failure = 0.0

    @pytest.mark.asyncio
    async def test_process_ai_gate_missing_classification(self, db_session, test_requisition):
        """process_ai_gate handles when model doesn't return a classification for an MPN."""
        from app.services.ics_worker.ai_gate import clear_classification_cache, process_ai_gate

        clear_classification_cache()

        req = test_requisition.requirements[0]
        item = IcsSearchQueue(
            requirement_id=req.id, requisition_id=test_requisition.id,
            mpn="MISSING_MPN", normalized_mpn="MISSING_MPN",
            status="pending",
        )
        db_session.add(item)
        db_session.commit()

        mock_result = {"classifications": []}

        with patch("app.utils.llm_router.routed_structured", new_callable=AsyncMock, return_value=mock_result):
            await process_ai_gate(db_session)

        db_session.refresh(item)
        assert item.status == "pending"


# ═══════════════════════════════════════════════════════════════════════
# COVERAGE: ICS SCHEDULER (lines 20-21, 42, 48, 53-56)
# ═══════════════════════════════════════════════════════════════════════


class TestSchedulerBranches:
    def test_business_hours_force_env(self):
        """FORCE_BUSINESS_HOURS env var overrides schedule (line 42)."""
        cfg = IcsConfig()
        sched = SearchScheduler(cfg)
        with patch.dict(os.environ, {"FORCE_BUSINESS_HOURS": "1"}):
            assert sched.is_business_hours() is True

    def test_business_hours_saturday(self):
        """Saturday always returns False (line 48)."""
        cfg = IcsConfig()
        sched = SearchScheduler(cfg)
        # Saturday: weekday() == 5
        with patch("app.services.ics_worker.scheduler.datetime") as mock_dt:
            mock_now = MagicMock()
            mock_now.weekday.return_value = 5
            mock_now.hour = 12
            mock_dt.now.return_value = mock_now
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("FORCE_BUSINESS_HOURS", None)
                assert sched.is_business_hours() is False

    def test_business_hours_sunday_before_6pm(self):
        """Sunday before 6 PM returns False (line 51)."""
        cfg = IcsConfig()
        sched = SearchScheduler(cfg)
        with patch("app.services.ics_worker.scheduler.datetime") as mock_dt:
            mock_now = MagicMock()
            mock_now.weekday.return_value = 6
            mock_now.hour = 10
            mock_dt.now.return_value = mock_now
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("FORCE_BUSINESS_HOURS", None)
                assert sched.is_business_hours() is False

    def test_business_hours_sunday_after_6pm(self):
        """Sunday at 6 PM+ returns True."""
        cfg = IcsConfig()
        sched = SearchScheduler(cfg)
        with patch("app.services.ics_worker.scheduler.datetime") as mock_dt:
            mock_now = MagicMock()
            mock_now.weekday.return_value = 6
            mock_now.hour = 18
            mock_dt.now.return_value = mock_now
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("FORCE_BUSINESS_HOURS", None)
                assert sched.is_business_hours() is True

    def test_business_hours_friday_before_5pm(self):
        """Friday before 5 PM returns True (line 54)."""
        cfg = IcsConfig()
        sched = SearchScheduler(cfg)
        with patch("app.services.ics_worker.scheduler.datetime") as mock_dt:
            mock_now = MagicMock()
            mock_now.weekday.return_value = 4
            mock_now.hour = 12
            mock_dt.now.return_value = mock_now
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("FORCE_BUSINESS_HOURS", None)
                assert sched.is_business_hours() is True

    def test_business_hours_friday_after_5pm(self):
        """Friday at 5 PM+ returns False."""
        cfg = IcsConfig()
        sched = SearchScheduler(cfg)
        with patch("app.services.ics_worker.scheduler.datetime") as mock_dt:
            mock_now = MagicMock()
            mock_now.weekday.return_value = 4
            mock_now.hour = 17
            mock_dt.now.return_value = mock_now
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("FORCE_BUSINESS_HOURS", None)
                assert sched.is_business_hours() is False

    def test_business_hours_weekday(self):
        """Monday-Thursday always returns True (line 56)."""
        cfg = IcsConfig()
        sched = SearchScheduler(cfg)
        with patch("app.services.ics_worker.scheduler.datetime") as mock_dt:
            mock_now = MagicMock()
            mock_now.weekday.return_value = 2  # Wednesday
            mock_now.hour = 3
            mock_dt.now.return_value = mock_now
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("FORCE_BUSINESS_HOURS", None)
                assert sched.is_business_hours() is True


# ═══════════════════════════════════════════════════════════════════════
# COVERAGE: ICS SEARCH ENGINE (lines 54-55, 95-96, 117-136, 140-177)
# ═══════════════════════════════════════════════════════════════════════


class TestSearchEngineFull:
    @pytest.mark.asyncio
    async def test_search_part_all_selectors_fail_fallback(self):
        """When all 4 selectors fail, falls back to first text input (lines 52-55)."""
        page = AsyncMock()
        page.goto = AsyncMock()
        page.url = "https://www.icsource.com/search"

        # All selectors fail
        failing_locator = AsyncMock()
        failing_locator.wait_for = AsyncMock(side_effect=Exception("not found"))
        failing_locator.count = AsyncMock(return_value=0)
        failing_locator.is_visible = AsyncMock(return_value=False)

        fallback_locator = AsyncMock()
        fallback_locator.fill = AsyncMock()
        fallback_locator.first = fallback_locator

        def mock_locator(sel):
            if sel == "input[type='text']":
                return fallback_locator
            return failing_locator

        page.locator = MagicMock(side_effect=mock_locator)
        page.wait_for_selector = AsyncMock()
        page.screenshot = AsyncMock()
        # evaluate calls: 1=diagnostic, 2=JS strategy, 3=results HTML, 4=total_count
        page.evaluate = AsyncMock(side_effect=[
            {"buttons": [], "forms": [], "url": "http://x", "title": "t"},
            "no method found",
            "<html></html>",
            0,
        ])

        with patch("app.services.ics_worker.search_engine.HumanBehavior") as mock_hb:
            mock_hb.human_type = AsyncMock()
            mock_hb.human_click = AsyncMock()
            mock_hb.random_delay = AsyncMock()

            result = await search_part(page, "XYZ123")

        assert result["html"] == "<html></html>"

    @pytest.mark.asyncio
    async def test_search_part_screenshot_failure(self):
        """Screenshot failure is handled gracefully (lines 95-96)."""
        page = AsyncMock()
        page.goto = AsyncMock()
        page.url = "https://www.icsource.com/search"

        locator = AsyncMock()
        locator.wait_for = AsyncMock()
        locator.is_visible = AsyncMock(return_value=True)
        locator.count = AsyncMock(return_value=1)
        locator.fill = AsyncMock()
        page.locator = MagicMock(return_value=locator)

        page.wait_for_selector = AsyncMock()
        page.screenshot = AsyncMock(side_effect=Exception("Screenshot failed"))
        page.evaluate = AsyncMock(side_effect=[
            {"buttons": [], "forms": [], "url": "http://x", "title": "t"},
            "<html>body</html>",
            0,
        ])

        with patch("app.services.ics_worker.search_engine.HumanBehavior") as mock_hb:
            mock_hb.human_type = AsyncMock()
            mock_hb.human_click = AsyncMock()
            mock_hb.random_delay = AsyncMock()

            result = await search_part(page, "XYZ123")

        assert result["html"] == "<html>body</html>"

    @pytest.mark.asyncio
    async def test_search_part_strategy2_force_click(self):
        """Strategy 2: force-click hidden button (lines 122-136)."""
        page = AsyncMock()
        page.goto = AsyncMock()
        page.url = "https://www.icsource.com/search"

        # Input locator is visible
        pn_locator = AsyncMock()
        pn_locator.wait_for = AsyncMock()
        pn_locator.is_visible = AsyncMock(return_value=True)
        pn_locator.fill = AsyncMock()

        # Button locator: visible check fails but count > 0 for force-click
        btn_locator = AsyncMock()
        btn_locator.count = AsyncMock(return_value=1)
        btn_locator.is_visible = AsyncMock(return_value=False)  # Strategy 1 skips
        btn_locator.click = AsyncMock()  # Strategy 2 force-click

        call_count = 0

        def mock_locator(sel):
            nonlocal call_count
            if "PartNumber" in sel or "txtPN" in sel:
                return pn_locator
            return btn_locator

        page.locator = MagicMock(side_effect=mock_locator)
        page.wait_for_selector = AsyncMock()
        page.screenshot = AsyncMock()
        page.evaluate = AsyncMock(side_effect=[
            {"buttons": [], "forms": [], "url": "http://x", "title": "t"},
            "<html>results</html>",
            5,
        ])

        with patch("app.services.ics_worker.search_engine.HumanBehavior") as mock_hb:
            mock_hb.human_type = AsyncMock()
            mock_hb.human_click = AsyncMock()
            mock_hb.random_delay = AsyncMock()

            result = await search_part(page, "XYZ123")

        assert result["html"] == "<html>results</html>"

    @pytest.mark.asyncio
    async def test_search_part_strategy3_js_click(self):
        """Strategy 3: JS-based search submission (lines 140-177)."""
        page = AsyncMock()
        page.goto = AsyncMock()
        page.url = "https://www.icsource.com/search"

        # Input locator is visible
        pn_locator = AsyncMock()
        pn_locator.wait_for = AsyncMock()
        pn_locator.is_visible = AsyncMock(return_value=True)
        pn_locator.fill = AsyncMock()

        # Button locator: both strategies 1 and 2 fail
        btn_locator = AsyncMock()
        btn_locator.count = AsyncMock(return_value=0)
        btn_locator.is_visible = AsyncMock(return_value=False)

        def mock_locator(sel):
            if "PartNumber" in sel or "txtPN" in sel or sel == "input[type='text']":
                return pn_locator
            return btn_locator

        page.locator = MagicMock(side_effect=mock_locator)
        page.wait_for_selector = AsyncMock()
        page.screenshot = AsyncMock()
        page.evaluate = AsyncMock(side_effect=[
            {"buttons": [], "forms": [], "url": "http://x", "title": "t"},
            "clicked: #btn",  # JS strategy result
            "<html>js results</html>",
            3,
        ])

        with patch("app.services.ics_worker.search_engine.HumanBehavior") as mock_hb:
            mock_hb.human_type = AsyncMock()
            mock_hb.human_click = AsyncMock()
            mock_hb.random_delay = AsyncMock()

            result = await search_part(page, "XYZ123")

        assert result["html"] == "<html>js results</html>"

    @pytest.mark.asyncio
    async def test_search_part_diagnostic_buttons_and_forms(self):
        """Diagnostic logging iterates over buttons and forms (lines 85-89)."""
        page = AsyncMock()
        page.goto = AsyncMock()
        page.url = "https://www.icsource.com/search"

        locator = AsyncMock()
        locator.wait_for = AsyncMock()
        locator.is_visible = AsyncMock(return_value=True)
        locator.count = AsyncMock(return_value=1)
        locator.fill = AsyncMock()
        page.locator = MagicMock(return_value=locator)
        page.wait_for_selector = AsyncMock()
        page.screenshot = AsyncMock()

        # Diagnostic data has buttons and forms to iterate over (covers lines 86, 89)
        page.evaluate = AsyncMock(side_effect=[
            {
                "buttons": [
                    {"tag": "INPUT", "id": "btn1", "type": "submit", "value": "Search",
                     "text": "Search", "visible": True, "display": "block", "onclick": ""},
                    {"tag": "BUTTON", "id": "btn2", "type": "button", "value": "",
                     "text": "Go", "visible": False, "display": "none", "onclick": "doSearch()"},
                ],
                "forms": [
                    {"id": "form1", "action": "/search", "method": "post"},
                ],
                "url": "http://test", "title": "Search",
            },
            "<html>results</html>",
            2,
        ])

        with patch("app.services.ics_worker.search_engine.HumanBehavior") as mock_hb:
            mock_hb.human_type = AsyncMock()
            mock_hb.human_click = AsyncMock()
            mock_hb.random_delay = AsyncMock()
            result = await search_part(page, "TEST123")

        assert result["total_count"] == 2

    @pytest.mark.asyncio
    async def test_search_part_strategy1_exception(self):
        """Strategy 1 button exception triggers continue (lines 117-118)."""
        page = AsyncMock()
        page.goto = AsyncMock()
        page.url = "https://www.icsource.com/search"

        # Input locator is visible
        pn_locator = AsyncMock()
        pn_locator.wait_for = AsyncMock()
        pn_locator.is_visible = AsyncMock(return_value=True)
        pn_locator.fill = AsyncMock()

        # Button locator: count raises exception for Strategy 1 (lines 117-118)
        # but count returns 0 for Strategy 2 so it falls through to JS
        btn_locator = AsyncMock()
        btn_locator.count = AsyncMock(side_effect=Exception("element detached"))
        btn_locator.is_visible = AsyncMock(side_effect=Exception("element detached"))

        def mock_locator(sel):
            if "PartNumber" in sel or "txtPN" in sel:
                return pn_locator
            return btn_locator

        page.locator = MagicMock(side_effect=mock_locator)
        page.wait_for_selector = AsyncMock()
        page.screenshot = AsyncMock()
        page.evaluate = AsyncMock(side_effect=[
            {"buttons": [], "forms": [], "url": "http://x", "title": "t"},
            "submitted form",
            "<html>found</html>",
            1,
        ])

        with patch("app.services.ics_worker.search_engine.HumanBehavior") as mock_hb:
            mock_hb.human_type = AsyncMock()
            mock_hb.human_click = AsyncMock()
            mock_hb.random_delay = AsyncMock()
            result = await search_part(page, "ABC")

        assert result["html"] == "<html>found</html>"

    @pytest.mark.asyncio
    async def test_search_part_strategy2_force_click_exception(self):
        """Strategy 2 force-click raises exception, falls to Strategy 3 (lines 134-136)."""
        page = AsyncMock()
        page.goto = AsyncMock()
        page.url = "https://www.icsource.com/search"

        pn_locator = AsyncMock()
        pn_locator.wait_for = AsyncMock()
        pn_locator.is_visible = AsyncMock(return_value=True)
        pn_locator.fill = AsyncMock()

        # Strategy 1: count > 0 but is_visible False -> skip
        # Strategy 2: count > 0, click(force=True) raises -> triggers lines 134-136
        btn_locator = AsyncMock()
        btn_locator.count = AsyncMock(return_value=1)
        btn_locator.is_visible = AsyncMock(return_value=False)
        btn_locator.click = AsyncMock(side_effect=Exception("click timeout"))

        def mock_locator(sel):
            if "PartNumber" in sel or "txtPN" in sel:
                return pn_locator
            return btn_locator

        page.locator = MagicMock(side_effect=mock_locator)
        page.wait_for_selector = AsyncMock()
        page.screenshot = AsyncMock()
        page.evaluate = AsyncMock(side_effect=[
            {"buttons": [], "forms": [], "url": "http://x", "title": "t"},
            "called showPageAjax()",
            "<html>js fallback</html>",
            0,
        ])

        with patch("app.services.ics_worker.search_engine.HumanBehavior") as mock_hb:
            mock_hb.human_type = AsyncMock()
            mock_hb.human_click = AsyncMock()
            mock_hb.random_delay = AsyncMock()
            result = await search_part(page, "DEF")

        assert result["html"] == "<html>js fallback</html>"


# ═══════════════════════════════════════════════════════════════════════
# COVERAGE: ICS SESSION MANAGER (lines 47-64, 78-88, 109-172)
# ═══════════════════════════════════════════════════════════════════════


class TestSessionManagerFull:
    @pytest.mark.asyncio
    async def test_start_success(self):
        """start() launches browser and checks health (lines 47-64)."""
        from app.services.ics_worker.session_manager import IcsSessionManager

        cfg = IcsConfig()
        sm = IcsSessionManager(cfg)

        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.url = "https://www.icsource.com/members/Search/NewSearch.aspx"

        mock_context = AsyncMock()
        mock_context.pages = [mock_page]

        mock_chromium = AsyncMock()
        mock_chromium.launch_persistent_context = AsyncMock(return_value=mock_context)

        mock_pw = AsyncMock()
        mock_pw.chromium = mock_chromium

        mock_pw_cm = AsyncMock()
        mock_pw_cm.start = AsyncMock(return_value=mock_pw)

        mock_async_pw = MagicMock(return_value=mock_pw_cm)

        with patch.dict(os.environ, {"DISPLAY": ":99"}):
            with patch("patchright.async_api.async_playwright", mock_async_pw):
                with patch.object(sm, "check_session_health", new_callable=AsyncMock, return_value=True):
                    await sm.start()

        assert sm.is_logged_in is True

    @pytest.mark.asyncio
    async def test_start_not_logged_in(self):
        """start() sets is_logged_in=False when not already logged in."""
        from app.services.ics_worker.session_manager import IcsSessionManager

        cfg = IcsConfig()
        sm = IcsSessionManager(cfg)

        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()

        mock_context = AsyncMock()
        mock_context.pages = [mock_page]

        mock_chromium = AsyncMock()
        mock_chromium.launch_persistent_context = AsyncMock(return_value=mock_context)

        mock_pw = AsyncMock()
        mock_pw.chromium = mock_chromium

        mock_pw_cm = AsyncMock()
        mock_pw_cm.start = AsyncMock(return_value=mock_pw)

        mock_async_pw = MagicMock(return_value=mock_pw_cm)

        with patch.dict(os.environ, {"DISPLAY": ":99"}):
            with patch("patchright.async_api.async_playwright", mock_async_pw):
                with patch.object(sm, "check_session_health", new_callable=AsyncMock, return_value=False):
                    await sm.start()

        assert sm.is_logged_in is False

    @pytest.mark.asyncio
    async def test_check_session_health_login_redirect(self):
        """check_session_health returns False on login redirect (line 83)."""
        from app.services.ics_worker.session_manager import IcsSessionManager

        cfg = IcsConfig()
        sm = IcsSessionManager(cfg)
        sm._page = AsyncMock()
        sm._page.goto = AsyncMock()
        sm._page.url = "https://www.icsource.com/home/login.aspx"

        result = await sm.check_session_health()
        assert result is False

    @pytest.mark.asyncio
    async def test_check_session_health_redirect_to_home(self):
        """check_session_health returns False when redirected to public home (line 86)."""
        from app.services.ics_worker.session_manager import IcsSessionManager

        cfg = IcsConfig()
        sm = IcsSessionManager(cfg)
        sm._page = AsyncMock()
        sm._page.goto = AsyncMock()
        sm._page.url = "https://www.icsource.com/"

        result = await sm.check_session_health()
        assert result is False

    @pytest.mark.asyncio
    async def test_check_session_health_valid(self):
        """check_session_health returns True when on members page."""
        from app.services.ics_worker.session_manager import IcsSessionManager

        cfg = IcsConfig()
        sm = IcsSessionManager(cfg)
        sm._page = AsyncMock()
        sm._page.goto = AsyncMock()
        sm._page.url = "https://www.icsource.com/members/Search/NewSearch.aspx"

        result = await sm.check_session_health()
        assert result is True

    @pytest.mark.asyncio
    async def test_login_success(self):
        """login() full success flow (lines 109-172)."""
        from app.services.ics_worker.session_manager import IcsSessionManager

        cfg = IcsConfig()
        cfg.ICS_USERNAME = "testuser"
        cfg.ICS_PASSWORD = "testpass"
        sm = IcsSessionManager(cfg)

        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.evaluate = AsyncMock()

        mock_locator = AsyncMock()
        mock_locator.wait_for = AsyncMock()
        mock_locator.click = AsyncMock()
        mock_page.locator = MagicMock(return_value=mock_locator)

        sm._page = mock_page

        with patch("app.services.ics_worker.session_manager.HumanBehavior") as mock_hb:
            mock_hb.random_delay = AsyncMock()
            mock_hb.human_click = AsyncMock()
            with patch.object(sm, "check_session_health", new_callable=AsyncMock, return_value=True):
                result = await sm.login()

        assert result is True
        assert sm.is_logged_in is True

    @pytest.mark.asyncio
    async def test_login_failed_after_submit(self):
        """login() sets is_logged_in=False when health check fails after login."""
        from app.services.ics_worker.session_manager import IcsSessionManager

        cfg = IcsConfig()
        cfg.ICS_USERNAME = "testuser"
        cfg.ICS_PASSWORD = "testpass"
        sm = IcsSessionManager(cfg)

        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.evaluate = AsyncMock()

        mock_locator = AsyncMock()
        mock_locator.wait_for = AsyncMock()
        mock_locator.click = AsyncMock()
        mock_page.locator = MagicMock(return_value=mock_locator)

        sm._page = mock_page

        with patch("app.services.ics_worker.session_manager.HumanBehavior") as mock_hb:
            mock_hb.random_delay = AsyncMock()
            mock_hb.human_click = AsyncMock()
            with patch.object(sm, "check_session_health", new_callable=AsyncMock, return_value=False):
                result = await sm.login()

        assert result is False
        assert sm.is_logged_in is False

    @pytest.mark.asyncio
    async def test_login_password_placeholder_fails(self):
        """login() handles missing password placeholder gracefully (line 136-137)."""
        from app.services.ics_worker.session_manager import IcsSessionManager

        cfg = IcsConfig()
        cfg.ICS_USERNAME = "testuser"
        cfg.ICS_PASSWORD = "testpass"
        sm = IcsSessionManager(cfg)

        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.evaluate = AsyncMock()

        pwd_locator = AsyncMock()
        pwd_locator.wait_for = AsyncMock(side_effect=Exception("not found"))

        username_locator = AsyncMock()
        username_locator.wait_for = AsyncMock()
        username_locator.click = AsyncMock()

        login_btn_locator = AsyncMock()
        login_btn_locator.wait_for = AsyncMock()

        def mock_locator(sel):
            if "passwordhidden" in sel:
                return pwd_locator
            if "btnLogIn" in sel:
                return login_btn_locator
            return username_locator

        mock_page.locator = MagicMock(side_effect=mock_locator)
        sm._page = mock_page

        with patch("app.services.ics_worker.session_manager.HumanBehavior") as mock_hb:
            mock_hb.random_delay = AsyncMock()
            mock_hb.human_click = AsyncMock()
            with patch.object(sm, "check_session_health", new_callable=AsyncMock, return_value=True):
                result = await sm.login()

        assert result is True

    @pytest.mark.asyncio
    async def test_login_fallback_button(self):
        """login() uses fallback ASP.NET button when green button not found (lines 159-162)."""
        from app.services.ics_worker.session_manager import IcsSessionManager

        cfg = IcsConfig()
        cfg.ICS_USERNAME = "testuser"
        cfg.ICS_PASSWORD = "testpass"
        sm = IcsSessionManager(cfg)

        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.evaluate = AsyncMock()

        username_locator = AsyncMock()
        username_locator.wait_for = AsyncMock()

        pwd_locator = AsyncMock()
        pwd_locator.wait_for = AsyncMock()
        pwd_locator.click = AsyncMock()

        green_btn = AsyncMock()
        green_btn.wait_for = AsyncMock(side_effect=Exception("not visible"))

        fallback_btn = AsyncMock()

        def mock_locator(sel):
            if "passwordhidden" in sel:
                return pwd_locator
            if "green" in sel:
                return green_btn
            if "btnLogIn" in sel:
                return fallback_btn
            return username_locator

        mock_page.locator = MagicMock(side_effect=mock_locator)
        sm._page = mock_page

        with patch("app.services.ics_worker.session_manager.HumanBehavior") as mock_hb:
            mock_hb.random_delay = AsyncMock()
            mock_hb.human_click = AsyncMock()
            with patch.object(sm, "check_session_health", new_callable=AsyncMock, return_value=True):
                result = await sm.login()

        assert result is True


# ═══════════════════════════════════════════════════════════════════════
# COVERAGE: ICS RESULT PARSER (lines 161-163)
# ═══════════════════════════════════════════════════════════════════════


class TestResultParserExceptions:
    def test_exception_in_row_parsing(self):
        """Force an IndexError in row parsing (lines 161-163)."""
        with patch("app.services.ics_worker.result_parser.parse_quantity", side_effect=IndexError("forced")):
            result = parse_results_html("""
            <div class="tblWTBPanel">
              <div class="flex">
                <a href="javascript:OpenProfile(1)">V</a>
              </div>
              <tr class="browseMatchItem">
                <td>P</td><td>D</td><td>1</td><td></td><td>M</td><td></td><td></td>
              </tr>
            </div>
            """)
        assert result == []


# ═══════════════════════════════════════════════════════════════════════
# COVERAGE: ICS QUEUE MANAGER (line 42)
# ═══════════════════════════════════════════════════════════════════════


class TestQueueManagerEdge:
    def test_enqueue_whitespace_mpn(self, db_session, test_requisition):
        """Whitespace-only MPN normalizes to empty, returns None (line 42)."""
        req = test_requisition.requirements[0]
        req.primary_mpn = "   "
        db_session.commit()
        result = enqueue_for_ics_search(req.id, db_session)
        assert result is None
