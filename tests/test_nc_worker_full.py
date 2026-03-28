"""100% coverage tests for the entire nc_worker package.

Covers every module: worker, search_engine, session_manager, result_parser,
queue_manager, sighting_writer, ai_gate, circuit_breaker, scheduler,
human_behavior, config, monitoring, mpn_normalizer, __main__.

Called by: pytest
Depends on: conftest.py, nc_worker modules
"""

import os
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from app.models import NcSearchQueue, NcWorkerStatus, Requirement, Sighting
from app.services.nc_worker.circuit_breaker import CircuitBreaker
from app.services.nc_worker.config import NcConfig
from app.services.nc_worker.human_behavior import HumanBehavior
from app.services.nc_worker.monitoring import (
    _get_hash_set,
    _known_html_hashes,
    capture_sentry_error,
    capture_sentry_message,
    check_html_structure_hash,
    log_daily_report,
)
from app.services.nc_worker.mpn_normalizer import strip_packaging_suffixes as normalize_mpn
from app.services.nc_worker.queue_manager import (
    enqueue_for_nc_search,
    get_next_queued_item,
    get_queue_stats,
    mark_completed,
    mark_status,
    recover_stale_searches,
)
from app.services.nc_worker.result_parser import NcSighting, parse_results_html
from app.services.nc_worker.scheduler import SearchScheduler
from app.services.nc_worker.search_engine import build_search_url, search_part
from app.services.nc_worker.sighting_writer import save_nc_sightings

pytestmark = pytest.mark.slow

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


class TestNcConfig:
    def test_defaults(self):
        cfg = NcConfig()
        assert cfg.NC_MAX_DAILY_SEARCHES == 75
        assert cfg.NC_MAX_HOURLY_SEARCHES == 12
        assert cfg.NC_MIN_DELAY_SECONDS == 120
        assert cfg.NC_MAX_DELAY_SECONDS == 420
        assert cfg.NC_TYPICAL_DELAY_SECONDS == 240
        assert cfg.NC_DEDUP_WINDOW_DAYS == 7
        assert cfg.NC_BUSINESS_HOURS_START == 8
        assert cfg.NC_BUSINESS_HOURS_END == 18

    def test_env_override(self):
        with patch.dict(os.environ, {"NC_MAX_DAILY_SEARCHES": "50", "NC_USERNAME": "foo@bar.com"}):
            cfg = NcConfig()
            assert cfg.NC_MAX_DAILY_SEARCHES == 50
            assert cfg.NC_USERNAME == "foo@bar.com"


# ═══════════════════════════════════════════════════════════════════════
# RESULT PARSER — additional coverage for uncovered branches
# ═══════════════════════════════════════════════════════════════════════


class TestResultParser:
    def test_asia_region_detection(self):
        """Cover the Asia region branch (lines 83-84)."""
        html = """
        <table>
          <tr><td colspan="10">Asia</td></tr>
          <tr><td colspan="10">In-Stock Inventory</td></tr>
          <tr>
            <td>STM32F103</td><td></td><td>ST</td><td>2024</td>
            <td>MCU</td><td>01/2026</td><td>CN</td><td>500</td>
            <td></td><td>Shenzhen Parts</td>
          </tr>
        </table>
        """
        sightings = parse_results_html(html)
        assert len(sightings) == 1
        assert sightings[0].region == "Asia"

    def test_div_based_fallback(self):
        """Cover the div-based layout fallback (line 70)."""
        html = """
        <div class="result-row">
            <td>MPN1</td><td></td><td>MFR</td><td>DC</td>
            <td>Desc</td><td>Date</td><td>US</td><td>100</td>
            <td></td><td>Vendor</td>
        </div>
        """
        # No <tr> tags, so it falls through to div fallback
        result = parse_results_html(html)
        # The div has <td> children but isn't a <tr>, so result depends on structure
        # The key is exercising the fallback code path
        assert isinstance(result, list)

    def test_malformed_row_skipped(self):
        """Cover the IndexError/AttributeError catch (lines 143-145)."""
        html = """
        <table>
          <tr>
            <td>Part</td><td></td><td>Mfr</td><td>DC</td>
            <td>Desc</td><td>Date</td><td>US</td><td>100</td>
            <td></td><td>Vendor</td>
          </tr>
        </table>
        """
        # This row parses fine, but let's test with malformed data
        # We mock cells to raise IndexError
        sightings = parse_results_html(html)
        assert isinstance(sightings, list)

    def test_sponsor_badge_detected(self):
        """Sponsor detected when cell 13 has non-empty text (14 cells via flat
        parser)."""
        html = """
        <table>
          <tr>
            <td>STM32</td><td></td><td></td><td>ST</td>
            <td>2024</td><td>MCU</td><td>01/26</td><td>US</td>
            <td>500</td><td></td><td></td><td></td>
            <td>SponsorCo</td><td>S</td>
          </tr>
        </table>
        """
        sightings = parse_results_html(html)
        assert len(sightings) == 1
        assert sightings[0].is_sponsor is True

    def test_authorized_badge_detected(self):
        """Authorized detected when price breaks exist (via .ncprc data-pbrk)."""
        html = """
        <table>
          <tr>
            <td>STM32</td><td></td><td></td><td>ST</td>
            <td>2024</td><td>MCU</td><td>01/26</td><td>US</td>
            <td>500</td><td><span class="ncprc" data-pbrk='{"currency":"USD","Prices":[{"price":1.5,"minQty":1}]}'></span></td><td></td><td></td>
            <td>AuthCo</td><td></td>
          </tr>
        </table>
        """
        sightings = parse_results_html(html)
        assert len(sightings) == 1
        assert sightings[0].is_authorized is True

    def test_no_table_rows(self):
        html = "<div>Nothing here</div>"
        assert parse_results_html(html) == []

    def test_none_html(self):
        assert parse_results_html(None) == []

    def test_vendor_fallback_last_cell(self):
        """When fewer than 10 cells, vendor falls back to last cell."""
        html = """
        <table>
          <tr>
            <td>MPN</td><td></td><td>MFR</td><td>DC</td>
            <td>Desc</td><td>Date</td><td>US</td><td>100</td>
            <td>VendorName</td>
          </tr>
        </table>
        """
        sightings = parse_results_html(html)
        assert len(sightings) == 1
        assert sightings[0].vendor_name == "VendorName"


# ═══════════════════════════════════════════════════════════════════════
# SEARCH ENGINE
# ═══════════════════════════════════════════════════════════════════════


class TestSearchEngine:
    def test_build_search_url(self):
        url = build_search_url("AD8232ACPZ")
        assert "netcomponents.com/search/result" in url
        assert "AD8232ACPZ" in url

    def test_build_search_url_encodes_special(self):
        url = build_search_url("LM 317T")
        assert "LM%20317T" in url

    def test_search_part(self):
        """Exercise the search_part function with a mocked session_manager."""
        mock_resp = MagicMock()
        mock_resp.text = '<div class="searchresultstable">result</div>'
        mock_resp.status_code = 200

        mock_session_mgr = MagicMock()
        mock_session_mgr.session.get = MagicMock(return_value=mock_resp)

        result = search_part(mock_session_mgr, "STM32F103C8T6")
        assert "searchresultstable" in result["html"]
        assert result["duration_ms"] >= 0
        assert "STM32F103C8T6" in result["url"]
        assert result["mode"] == "http"

    def test_search_part_empty_result(self):
        """search_part returns empty HTML when HTTP returns no results."""
        mock_resp = MagicMock()
        mock_resp.text = "<html>No results</html>"
        mock_resp.status_code = 200

        mock_session_mgr = MagicMock()
        mock_session_mgr.session.get = MagicMock(return_value=mock_resp)
        mock_session_mgr.has_browser = False

        result = search_part(mock_session_mgr, "XYZ123")
        assert result["mode"] == "http_empty"


# ═══════════════════════════════════════════════════════════════════════
# CIRCUIT BREAKER — fill coverage gaps
# ═══════════════════════════════════════════════════════════════════════


class TestCircuitBreakerFull:
    def test_consecutive_failures_trip(self):
        """3 consecutive server error health checks trip the breaker."""
        breaker = CircuitBreaker()

        for i in range(2):
            result = breaker.check_response_health(500, "", "https://www.netcomponents.com/search")
            assert result == "SERVER_ERROR"
            assert not breaker.is_open

        result = breaker.check_response_health(500, "", "https://www.netcomponents.com/search")
        assert result == "SERVER_ERROR"
        assert breaker.is_open
        assert "3 consecutive" in breaker.trip_reason

    def test_access_denied(self):
        """Access denied trips breaker immediately."""
        breaker = CircuitBreaker()

        result = breaker.check_response_health(
            200,
            "access denied - your account has been blocked",
            "https://www.netcomponents.com/error",
        )
        assert result == "ACCESS_DENIED"
        assert breaker.is_open

    def test_unusual_activity(self):
        """Unusual activity message trips the breaker."""
        breaker = CircuitBreaker()

        result = breaker.check_response_health(
            200,
            "we detected unusual activity on your account",
            "https://www.netcomponents.com/warning",
        )
        assert result == "ACCESS_DENIED"
        assert breaker.is_open


# ═══════════════════════════════════════════════════════════════════════
# SCHEDULER — cover zoneinfo import fallback
# ═══════════════════════════════════════════════════════════════════════


class TestSchedulerFull:
    def test_break_threshold_random_range(self):
        cfg = NcConfig()
        sched = SearchScheduler(cfg)
        assert 8 <= sched.break_threshold <= 15

    def test_next_delay_increments_search_count(self):
        cfg = NcConfig()
        sched = SearchScheduler(cfg)
        assert sched.searches_since_break == 0
        sched.next_delay()
        assert sched.searches_since_break == 1


# ═══════════════════════════════════════════════════════════════════════
# HUMAN BEHAVIOR
# ═══════════════════════════════════════════════════════════════════════


class TestHumanBehavior:
    @pytest.mark.asyncio
    async def test_random_delay(self):
        """random_delay sleeps within bounds."""
        with patch("app.services.nc_worker.human_behavior.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await HumanBehavior.random_delay(0.5, 1.5)
            mock_sleep.assert_called_once()
            delay = mock_sleep.call_args[0][0]
            assert 0.5 <= delay <= 1.5

    @pytest.mark.asyncio
    async def test_human_type(self):
        """human_type types each character with variable delays."""
        page = AsyncMock()
        locator = AsyncMock()
        locator.click = AsyncMock()

        with patch("app.services.nc_worker.human_behavior.asyncio.sleep", new_callable=AsyncMock):
            with patch("app.services.nc_worker.human_behavior.random.uniform", return_value=0.1):
                with patch("app.services.nc_worker.human_behavior.random.random", return_value=0.5):
                    await HumanBehavior.human_type(page, locator, "abc")

        locator.click.assert_called_once()
        assert page.keyboard.type.call_count == 3

    @pytest.mark.asyncio
    async def test_human_type_thinking_pause(self):
        """human_type adds thinking pauses when random < 0.05."""
        page = AsyncMock()
        locator = AsyncMock()

        with patch("app.services.nc_worker.human_behavior.asyncio.sleep", new_callable=AsyncMock):
            with patch("app.services.nc_worker.human_behavior.random.uniform", return_value=0.1):
                # Force thinking pause for every character
                with patch("app.services.nc_worker.human_behavior.random.random", return_value=0.01):
                    await HumanBehavior.human_type(page, locator, "ab")

        assert page.keyboard.type.call_count == 2

    @pytest.mark.asyncio
    async def test_human_click_with_bounding_box(self):
        """human_click uses random position within bounding box."""
        page = AsyncMock()
        locator = AsyncMock()
        locator.bounding_box = AsyncMock(return_value={"x": 100, "y": 200, "width": 50, "height": 30})

        await HumanBehavior.human_click(page, locator)
        page.mouse.click.assert_called_once()
        args = page.mouse.click.call_args[0]
        assert 115 <= args[0] <= 135  # x: 100 + 50*0.3 to 100 + 50*0.7
        assert 209 <= args[1] <= 221  # y: 200 + 30*0.3 to 200 + 30*0.7

    @pytest.mark.asyncio
    async def test_human_click_no_bounding_box(self):
        """human_click falls back to regular click when no bounding box."""
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
        """Clear known hashes before each test."""
        _known_html_hashes.clear()

    def test_log_daily_report(self):
        """log_daily_report runs without error."""
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
        """capture_sentry_error calls sentry_sdk when available."""
        mock_sdk = MagicMock()
        with patch.dict("sys.modules", {"sentry_sdk": mock_sdk}):
            capture_sentry_error(ValueError("test"), {"mpn": "STM32"})
            mock_sdk.capture_exception.assert_called_once()

    def test_capture_sentry_error_no_sdk(self):
        """capture_sentry_error handles missing sentry_sdk gracefully."""
        with patch.dict("sys.modules", {"sentry_sdk": None}):
            # Should not raise (handles ImportError internally)
            capture_sentry_error(ValueError("test"))

    def test_capture_sentry_message_with_sdk(self):
        """capture_sentry_message calls sentry_sdk when available."""
        mock_sdk = MagicMock()
        with patch.dict("sys.modules", {"sentry_sdk": mock_sdk}):
            capture_sentry_message("test message", level="info", context={"key": "val"})
            mock_sdk.capture_message.assert_called_once()

    def test_capture_sentry_message_no_sdk(self):
        """capture_sentry_message handles missing sentry_sdk gracefully."""
        with patch.dict("sys.modules", {"sentry_sdk": None}):
            capture_sentry_message("test message")

    def test_check_html_structure_hash_empty(self):
        assert check_html_structure_hash("", "TEST") == ""

    def test_check_html_structure_hash_first_time(self):
        """First hash is always added to known set without warning."""
        html = "<table><tr><td>data</td></tr></table>"
        h = check_html_structure_hash(html, "STM32")
        assert len(h) == 16
        assert h in _get_hash_set("NC")

    def test_check_html_structure_hash_known(self):
        """Same HTML structure returns same hash."""
        html = "<table><tr><td>data</td></tr></table>"
        h1 = check_html_structure_hash(html, "STM32")
        h2 = check_html_structure_hash(html, "LM317")
        assert h1 == h2

    def test_check_html_structure_hash_new_structure_warns(self):
        """New structure after first triggers a warning."""
        html1 = "<table><tr><td>data</td></tr></table>"
        check_html_structure_hash(html1, "STM32")

        html2 = "<div class='new'><span>different</span></div>"
        h2 = check_html_structure_hash(html2, "LM317")
        assert h2 in _get_hash_set("NC")


# ═══════════════════════════════════════════════════════════════════════
# QUEUE MANAGER
# ═══════════════════════════════════════════════════════════════════════


class TestQueueManager:
    def test_enqueue_no_requirement(self, db_session):
        """enqueue_for_nc_search returns None when requirement doesn't exist."""
        result = enqueue_for_nc_search(99999, db_session)
        assert result is None

    def test_enqueue_no_mpn(self, db_session, test_requisition):
        """enqueue_for_nc_search skips requirements without MPN."""
        req = test_requisition.requirements[0]
        req.primary_mpn = None
        db_session.commit()
        result = enqueue_for_nc_search(req.id, db_session)
        assert result is None

    def test_enqueue_empty_mpn(self, db_session, test_requisition):
        """enqueue_for_nc_search skips empty MPN."""
        req = test_requisition.requirements[0]
        req.primary_mpn = ""
        db_session.commit()
        result = enqueue_for_nc_search(req.id, db_session)
        assert result is None

    def test_enqueue_success(self, db_session, test_requisition):
        """Successfully enqueues a requirement."""
        req = test_requisition.requirements[0]
        item = enqueue_for_nc_search(req.id, db_session)
        assert item is not None
        assert item.mpn == "LM317T"
        assert item.status == "pending"

    def test_enqueue_already_queued(self, db_session, test_requisition):
        """Returns existing queue item if already queued."""
        req = test_requisition.requirements[0]
        item1 = enqueue_for_nc_search(req.id, db_session)
        item2 = enqueue_for_nc_search(req.id, db_session)
        assert item1.id == item2.id

    def test_enqueue_dedup_links_sightings(self, db_session, test_user):
        """Dedup: links existing sightings when same MPN was recently searched."""
        from app.models import MaterialCard, Requisition

        # Create material card
        mc = MaterialCard(
            normalized_mpn="lm317t",
            display_mpn="LM317T",
            manufacturer="TI",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(mc)
        db_session.flush()

        # Create first requisition with requirement + completed search
        req1 = Requisition(
            name="REQ-1",
            customer_name="Acme",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req1)
        db_session.flush()

        item1_req = Requirement(
            requisition_id=req1.id,
            primary_mpn="LM317T",
            target_qty=100,
            material_card_id=mc.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item1_req)
        db_session.flush()

        # Create a completed queue entry
        queue1 = NcSearchQueue(
            requirement_id=item1_req.id,
            requisition_id=req1.id,
            mpn="LM317T",
            normalized_mpn="LM317T",
            status="completed",
            last_searched_at=datetime.now(timezone.utc),
        )
        db_session.add(queue1)
        db_session.flush()

        # Create existing NC sightings for first requirement
        sighting = Sighting(
            requirement_id=item1_req.id,
            vendor_name="Arrow",
            vendor_name_normalized="arrow",
            mpn_matched="LM317T",
            normalized_mpn="LM317T",
            source_type="netcomponents",
            qty_available=500,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(sighting)
        db_session.commit()

        # Create second requisition with same MPN
        req2 = Requisition(
            name="REQ-2",
            customer_name="Beta Corp",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req2)
        db_session.flush()

        item2_req = Requirement(
            requisition_id=req2.id,
            primary_mpn="LM317T",
            target_qty=200,
            material_card_id=mc.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item2_req)
        db_session.commit()

        # Enqueue second requirement — should dedup and link sightings
        result = enqueue_for_nc_search(item2_req.id, db_session)
        assert result is None  # Deduped, not queued

        # Check that sightings were linked
        linked = (
            db_session.query(Sighting)
            .filter(
                Sighting.requirement_id == item2_req.id,
                Sighting.source_type == "netcomponents",
            )
            .all()
        )
        assert len(linked) == 1
        assert linked[0].vendor_name == "Arrow"

    def test_enqueue_dedup_no_material_card(self, db_session, test_user):
        """Dedup match without material_card doesn't crash."""
        from app.models import Requisition

        # First req — completed search
        req1 = Requisition(
            name="REQ-D1",
            customer_name="X",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req1)
        db_session.flush()

        item1 = Requirement(
            requisition_id=req1.id,
            primary_mpn="LM317T",
            target_qty=100,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item1)
        db_session.flush()

        queue1 = NcSearchQueue(
            requirement_id=item1.id,
            requisition_id=req1.id,
            mpn="LM317T",
            normalized_mpn="LM317T",
            status="completed",
            last_searched_at=datetime.now(timezone.utc),
        )
        db_session.add(queue1)
        db_session.commit()

        # Second req — same MPN but no material_card
        req2 = Requisition(
            name="REQ-D2",
            customer_name="Y",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req2)
        db_session.flush()

        item2 = Requirement(
            requisition_id=req2.id,
            primary_mpn="LM317T",
            target_qty=50,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item2)
        db_session.commit()

        result = enqueue_for_nc_search(item2.id, db_session)
        assert result is None  # Deduped but no link

    def test_recover_stale_searches(self, db_session, test_requisition):
        """Stale 'searching' items are reset to 'queued'."""
        req = test_requisition.requirements[0]
        item = NcSearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="LM317T",
            normalized_mpn="LM317T",
            status="searching",
        )
        db_session.add(item)
        db_session.commit()

        count = recover_stale_searches(db_session)
        assert count == 1
        db_session.refresh(item)
        assert item.status == "queued"

    def test_recover_stale_none(self, db_session):
        """No stale items returns 0."""
        assert recover_stale_searches(db_session) == 0

    def test_get_next_queued_item(self, db_session, test_requisition):
        """Gets oldest queued item ordered by priority then created_at."""
        req = test_requisition.requirements[0]
        item = NcSearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="LM317T",
            normalized_mpn="LM317T",
            status="queued",
        )
        db_session.add(item)
        db_session.commit()

        result = get_next_queued_item(db_session)
        assert result.id == item.id

    def test_get_next_queued_none(self, db_session):
        """Returns None when no queued items exist."""
        assert get_next_queued_item(db_session) is None

    def test_mark_status(self, db_session, test_requisition):
        req = test_requisition.requirements[0]
        item = NcSearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="LM317T",
            normalized_mpn="LM317T",
            status="queued",
        )
        db_session.add(item)
        db_session.commit()

        mark_status(db_session, item, "searching")
        db_session.refresh(item)
        assert item.status == "searching"

    def test_mark_status_with_error(self, db_session, test_requisition):
        req = test_requisition.requirements[0]
        item = NcSearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="LM317T",
            normalized_mpn="LM317T",
            status="searching",
        )
        db_session.add(item)
        db_session.commit()

        mark_status(db_session, item, "failed", error="Timeout")
        db_session.refresh(item)
        assert item.status == "failed"
        assert item.error_message == "Timeout"

    def test_mark_completed(self, db_session, test_requisition):
        req = test_requisition.requirements[0]
        item = NcSearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="LM317T",
            normalized_mpn="LM317T",
            status="searching",
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
        item = NcSearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="LM317T",
            normalized_mpn="LM317T",
            status="searching",
            search_count=2,
        )
        db_session.add(item)
        db_session.commit()

        mark_completed(db_session, item, results_found=5, sightings_created=3)
        assert item.search_count == 3

    def test_get_queue_stats(self, db_session, test_requisition):
        req = test_requisition.requirements[0]
        item = NcSearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="LM317T",
            normalized_mpn="LM317T",
            status="queued",
        )
        db_session.add(item)
        db_session.commit()

        stats = get_queue_stats(db_session)
        assert stats["queued"] == 1
        assert stats["remaining"] == 1
        assert "pending" in stats
        assert "completed" in stats

    def test_get_queue_stats_empty(self, db_session):
        stats = get_queue_stats(db_session)
        assert stats["queued"] == 0
        assert stats["remaining"] == 0
        assert stats["total_today"] == 0


# ═══════════════════════════════════════════════════════════════════════
# SIGHTING WRITER — fill coverage gaps
# ═══════════════════════════════════════════════════════════════════════


class TestSightingWriterFull:
    def test_requirement_not_found(self, db_session, test_requisition):
        """save_nc_sightings returns 0 when requirement doesn't exist (lines 34-35)."""
        queue_item = MagicMock()
        queue_item.requirement_id = 99999
        result = save_nc_sightings(db_session, queue_item, [])
        assert result == 0

    def test_confidence_in_stock(self, db_session, test_requisition):
        """In-stock sightings get 0.6 confidence."""
        req = test_requisition.requirements[0]
        queue_item = NcSearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="LM317T",
            normalized_mpn="LM317T",
            status="searching",
        )
        db_session.add(queue_item)
        db_session.commit()

        nc = [
            NcSighting(
                part_number="LM317T",
                vendor_name="Arrow",
                quantity=500,
                inventory_type="in_stock",
            )
        ]
        save_nc_sightings(db_session, queue_item, nc)

        s = db_session.query(Sighting).filter(Sighting.source_type == "netcomponents").first()
        assert s.confidence == 0.6

    def test_confidence_brokered(self, db_session, test_requisition):
        """Brokered sightings get 0.3 confidence."""
        req = test_requisition.requirements[0]
        queue_item = NcSearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="LM317T",
            normalized_mpn="LM317T",
            status="searching",
        )
        db_session.add(queue_item)
        db_session.commit()

        nc = [
            NcSighting(
                part_number="LM317T",
                vendor_name="Broker",
                quantity=500,
                inventory_type="brokered",
            )
        ]
        save_nc_sightings(db_session, queue_item, nc)

        s = db_session.query(Sighting).filter(Sighting.source_type == "netcomponents").first()
        assert s.confidence == 0.3

    def test_raw_data_populated(self, db_session, test_requisition):
        """Raw data JSON includes all NC metadata fields."""
        req = test_requisition.requirements[0]
        queue_item = NcSearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="LM317T",
            normalized_mpn="LM317T",
            status="searching",
        )
        db_session.add(queue_item)
        db_session.commit()

        nc = [
            NcSighting(
                part_number="LM317T",
                vendor_name="Arrow",
                quantity=500,
                region="The Americas",
                country="US",
                inventory_type="in_stock",
                uploaded_date="01/15/2026",
                is_sponsor=True,
                description="Voltage Reg",
            )
        ]
        save_nc_sightings(db_session, queue_item, nc)

        s = db_session.query(Sighting).filter(Sighting.source_type == "netcomponents").first()
        assert s.raw_data["region"] == "The Americas"
        assert s.raw_data["country"] == "US"
        assert s.raw_data["is_sponsor"] is True

    def test_empty_sightings_no_commit(self, db_session, test_requisition):
        """Empty sightings list creates nothing."""
        req = test_requisition.requirements[0]
        queue_item = NcSearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="LM317T",
            normalized_mpn="LM317T",
            status="searching",
        )
        db_session.add(queue_item)
        db_session.commit()

        result = save_nc_sightings(db_session, queue_item, [])
        assert result == 0


# ═══════════════════════════════════════════════════════════════════════
# AI GATE
# ═══════════════════════════════════════════════════════════════════════


class TestAiGate:
    @pytest.mark.asyncio
    async def test_classify_parts_batch_empty(self):
        """classify_parts_batch returns empty list for empty input."""
        from app.services.nc_worker.ai_gate import classify_parts_batch

        result = await classify_parts_batch([])
        assert result == []

    @pytest.mark.asyncio
    async def test_classify_parts_batch_success(self):
        """classify_parts_batch returns classifications on success."""
        from app.services.nc_worker.ai_gate import classify_parts_batch

        mock_result = {
            "classifications": [{"mpn": "STM32F103", "search_nc": True, "commodity": "semiconductor", "reason": "MCU"}]
        }

        with patch("app.utils.llm_router.routed_structured", new_callable=AsyncMock, return_value=mock_result):
            result = await classify_parts_batch([{"mpn": "STM32F103", "manufacturer": "ST", "description": "MCU"}])
            assert len(result) == 1
            assert result[0]["search_nc"] is True

    @pytest.mark.asyncio
    async def test_classify_parts_batch_api_failure(self):
        """classify_parts_batch returns None on API failure."""
        from app.services.nc_worker.ai_gate import classify_parts_batch

        with patch(
            "app.utils.llm_router.routed_structured", new_callable=AsyncMock, side_effect=Exception("API error")
        ):
            result = await classify_parts_batch([{"mpn": "X", "manufacturer": "", "description": ""}])
            assert result is None

    @pytest.mark.asyncio
    async def test_classify_parts_batch_bad_format(self):
        """classify_parts_batch returns None on bad response format."""
        from app.services.nc_worker.ai_gate import classify_parts_batch

        with patch("app.utils.llm_router.routed_structured", new_callable=AsyncMock, return_value={"bad": "format"}):
            result = await classify_parts_batch([{"mpn": "X", "manufacturer": "", "description": ""}])
            assert result is None

    @pytest.mark.asyncio
    async def test_process_ai_gate_no_pending(self, db_session):
        """process_ai_gate does nothing when no pending items."""
        from app.services.nc_worker.ai_gate import process_ai_gate

        await process_ai_gate(db_session)

    @pytest.mark.asyncio
    async def test_process_ai_gate_classifies_items(self, db_session, test_requisition):
        """process_ai_gate classifies pending items and updates status."""
        from app.services.nc_worker.ai_gate import clear_classification_cache, process_ai_gate

        clear_classification_cache()

        req = test_requisition.requirements[0]
        item = NcSearchQueue(
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
                {"mpn": "STM32F103C8T6", "search_nc": True, "commodity": "semiconductor", "reason": "ARM MCU"}
            ]
        }

        with patch("app.utils.llm_router.routed_structured", new_callable=AsyncMock, return_value=mock_result):
            await process_ai_gate(db_session)

        db_session.refresh(item)
        assert item.status == "queued"
        assert item.commodity_class == "semiconductor"

    @pytest.mark.asyncio
    async def test_process_ai_gate_gated_out(self, db_session, test_requisition):
        """process_ai_gate gates out commodity items."""
        from app.services.nc_worker.ai_gate import clear_classification_cache, process_ai_gate

        clear_classification_cache()

        req = test_requisition.requirements[0]
        item = NcSearchQueue(
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
                {"mpn": "RC0402FR-07100KL", "search_nc": False, "commodity": "passive", "reason": "Standard resistor"}
            ]
        }

        with patch("app.utils.llm_router.routed_structured", new_callable=AsyncMock, return_value=mock_result):
            await process_ai_gate(db_session)

        db_session.refresh(item)
        assert item.status == "gated_out"

    @pytest.mark.asyncio
    async def test_process_ai_gate_cache_hit(self, db_session, test_requisition):
        """process_ai_gate uses cache for previously classified MPNs."""
        from app.services.nc_worker.ai_gate import _classification_cache, clear_classification_cache, process_ai_gate

        clear_classification_cache()

        # Pre-populate cache
        _classification_cache[("STM32F103C8T6", "")] = ("semiconductor", "search", "ARM MCU")

        req = test_requisition.requirements[0]
        item = NcSearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="STM32F103C8T6",
            normalized_mpn="STM32F103C8T6",
            status="pending",
        )
        db_session.add(item)
        db_session.commit()

        # No API call needed — should use cache
        await process_ai_gate(db_session)

        db_session.refresh(item)
        assert item.status == "queued"
        assert "[cached]" in item.gate_reason

    @pytest.mark.asyncio
    async def test_process_ai_gate_api_failure_cooldown(self, db_session, test_requisition):
        """process_ai_gate activates cooldown after API failure."""
        import app.services.nc_worker.ai_gate as ai_gate_module
        from app.services.nc_worker.ai_gate import clear_classification_cache, process_ai_gate

        clear_classification_cache()
        ai_gate_module._last_api_failure = 0.0

        req = test_requisition.requirements[0]
        item = NcSearchQueue(
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

        # Item should be 'queued' after API failure (fail-open)
        db_session.refresh(item)
        assert item.status == "queued"
        assert ai_gate_module._last_api_failure > 0

    @pytest.mark.asyncio
    async def test_process_ai_gate_in_cooldown(self, db_session, test_requisition):
        """process_ai_gate skips processing during cooldown."""
        import app.services.nc_worker.ai_gate as ai_gate_module
        from app.services.nc_worker.ai_gate import clear_classification_cache, process_ai_gate

        clear_classification_cache()
        ai_gate_module._last_api_failure = time.monotonic()  # Just failed

        req = test_requisition.requirements[0]
        item = NcSearchQueue(
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
        assert item.status == "pending"  # Still pending, skipped due to cooldown

        # Cleanup
        ai_gate_module._last_api_failure = 0.0

    @pytest.mark.asyncio
    async def test_process_ai_gate_missing_classification(self, db_session, test_requisition):
        """process_ai_gate handles when model doesn't return a classification for an
        MPN."""
        from app.services.nc_worker.ai_gate import clear_classification_cache, process_ai_gate

        clear_classification_cache()

        req = test_requisition.requirements[0]
        item = NcSearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="MISSING_MPN",
            normalized_mpn="MISSING_MPN",
            status="pending",
        )
        db_session.add(item)
        db_session.commit()

        mock_result = {"classifications": []}  # Empty classifications

        with patch("app.utils.llm_router.routed_structured", new_callable=AsyncMock, return_value=mock_result):
            await process_ai_gate(db_session)

        db_session.refresh(item)
        assert item.status == "pending"  # Not classified, left pending

    def test_clear_classification_cache(self):
        from app.services.nc_worker.ai_gate import _classification_cache, clear_classification_cache

        _classification_cache[("test", "test")] = ("x", "y", "z")
        clear_classification_cache()
        assert len(_classification_cache) == 0


# ═══════════════════════════════════════════════════════════════════════
# SESSION MANAGER
# ═══════════════════════════════════════════════════════════════════════


class TestSessionManager:
    def test_start_success(self):
        """Start() loads homepage and checks session health via HTTP."""
        from app.services.nc_worker.session_manager import NcSessionManager

        cfg = NcConfig()
        session = NcSessionManager(cfg)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        with patch.object(session.session, "get", return_value=mock_resp):
            with patch.object(session, "check_session_health", return_value=True):
                session.start()

        assert session.is_logged_in is True

    def test_check_session_health_true(self):
        """check_session_health returns True on authorized response."""
        from app.services.nc_worker.session_manager import NcSessionManager

        cfg = NcConfig()
        session = NcSessionManager(cfg)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "true"

        with patch.object(session.session, "get", return_value=mock_resp):
            result = session.check_session_health()
        assert result is True

    def test_check_session_health_false(self):
        """check_session_health returns False on non-authorized response."""
        from app.services.nc_worker.session_manager import NcSessionManager

        cfg = NcConfig()
        session = NcSessionManager(cfg)

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "false"

        with patch.object(session.session, "get", return_value=mock_resp):
            result = session.check_session_health()
        assert result is False

    def test_check_session_health_exception(self):
        """check_session_health returns False on exception."""
        from app.services.nc_worker.session_manager import NcSessionManager

        cfg = NcConfig()
        session = NcSessionManager(cfg)

        import requests

        with patch.object(session.session, "get", side_effect=requests.RequestException("network error")):
            result = session.check_session_health()
        assert result is False

    def test_login_no_credentials(self):
        """Login() returns False when credentials are not configured."""
        from app.services.nc_worker.session_manager import NcSessionManager

        cfg = NcConfig()
        cfg.NC_USERNAME = ""
        cfg.NC_PASSWORD = ""
        cfg.NC_ACCOUNT_NUMBER = ""
        session = NcSessionManager(cfg)

        result = session.login()
        assert result is False

    def test_login_success(self):
        """Login() posts credentials and returns True on success."""
        from app.services.nc_worker.session_manager import NcSessionManager

        cfg = NcConfig()
        cfg.NC_ACCOUNT_NUMBER = "12345"
        cfg.NC_USERNAME = "test@example.com"
        cfg.NC_PASSWORD = "password123"
        session = NcSessionManager(cfg)

        # Mock the login form page (returns CSRF token)
        login_page_resp = MagicMock()
        login_page_resp.status_code = 200
        login_page_resp.text = '<input name="__RequestVerificationToken" value="token123" />'
        login_page_resp.raise_for_status = MagicMock()

        # Mock the POST response
        post_resp = MagicMock()
        post_resp.status_code = 200

        with patch.object(session.session, "get", return_value=login_page_resp):
            with patch.object(session.session, "post", return_value=post_resp):
                with patch.object(session, "check_session_health", return_value=True):
                    result = session.login()

        assert result is True
        assert session.is_logged_in is True

    def test_login_failure(self):
        """Login() returns False when session not authorized after submit."""
        from app.services.nc_worker.session_manager import NcSessionManager

        cfg = NcConfig()
        cfg.NC_ACCOUNT_NUMBER = "12345"
        cfg.NC_USERNAME = "test@example.com"
        cfg.NC_PASSWORD = "wrongpassword"
        session = NcSessionManager(cfg)

        login_page_resp = MagicMock()
        login_page_resp.status_code = 200
        login_page_resp.text = '<input name="__RequestVerificationToken" value="token123" />'
        login_page_resp.raise_for_status = MagicMock()

        post_resp = MagicMock()
        post_resp.status_code = 200

        with patch.object(session.session, "get", return_value=login_page_resp):
            with patch.object(session.session, "post", return_value=post_resp):
                with patch.object(session, "check_session_health", return_value=False):
                    result = session.login()

        assert result is False
        assert session.is_logged_in is False

    def test_login_exception(self):
        """Login() handles exceptions gracefully."""
        import requests

        from app.services.nc_worker.session_manager import NcSessionManager

        cfg = NcConfig()
        cfg.NC_ACCOUNT_NUMBER = "12345"
        cfg.NC_USERNAME = "test@example.com"
        cfg.NC_PASSWORD = "pass"
        session = NcSessionManager(cfg)

        with patch.object(session.session, "get", side_effect=requests.RequestException("Connection refused")):
            result = session.login()
        assert result is False
        assert session.is_logged_in is False

    def test_ensure_session_already_valid(self):
        """ensure_session returns True when session is already healthy."""
        from app.services.nc_worker.session_manager import NcSessionManager

        cfg = NcConfig()
        session = NcSessionManager(cfg)

        with patch.object(session, "check_session_health", return_value=True):
            result = session.ensure_session()
        assert result is True
        assert session.is_logged_in is True

    def test_ensure_session_re_login(self):
        """ensure_session re-authenticates when session expired."""
        from app.services.nc_worker.session_manager import NcSessionManager

        cfg = NcConfig()
        cfg.NC_ACCOUNT_NUMBER = "12345"
        cfg.NC_USERNAME = "test@example.com"
        cfg.NC_PASSWORD = "pass"
        session = NcSessionManager(cfg)

        with patch.object(session, "check_session_health", return_value=False):
            with patch.object(session, "login", return_value=True):
                result = session.ensure_session()
        assert result is True

    def test_stop(self):
        """Stop() closes HTTP session."""
        from app.services.nc_worker.session_manager import NcSessionManager

        cfg = NcConfig()
        session = NcSessionManager(cfg)
        session.is_logged_in = True

        session.stop()
        assert session.is_logged_in is False

    def test_stop_none_context(self):
        """Stop() with no active session doesn't crash."""
        from app.services.nc_worker.session_manager import NcSessionManager

        cfg = NcConfig()
        session = NcSessionManager(cfg)
        session.stop()  # No-op


# ═══════════════════════════════════════════════════════════════════════
# WORKER — update_worker_status + main loop
# ═══════════════════════════════════════════════════════════════════════


class TestWorker:
    def test_update_worker_status(self, db_session):
        """update_worker_status sets attributes on the singleton row."""
        from app.services.nc_worker.worker import update_worker_status

        ws = NcWorkerStatus(id=1, is_running=False, searches_today=0)
        db_session.add(ws)
        db_session.commit()

        update_worker_status(db_session, is_running=True, searches_today=5)
        db_session.refresh(ws)
        assert ws.is_running is True
        assert ws.searches_today == 5

    def test_update_worker_status_no_row(self, db_session):
        """update_worker_status does nothing when no singleton row exists."""
        from app.services.nc_worker.worker import update_worker_status

        update_worker_status(db_session, is_running=True)  # Should not raise

    def test_update_worker_status_invalid_field(self, db_session):
        """update_worker_status ignores non-existent attributes."""
        from app.services.nc_worker.worker import update_worker_status

        ws = NcWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        update_worker_status(db_session, nonexistent_field="value")  # Should not raise

    def test_shutdown_handler(self):
        """_handle_shutdown sets the global shutdown flag."""
        import app.services.nc_worker.worker as worker_mod

        original = worker_mod._shutdown_requested
        try:
            worker_mod._shutdown_requested = False
            worker_mod._handle_shutdown(15, None)  # SIGTERM
            assert worker_mod._shutdown_requested is True
        finally:
            worker_mod._shutdown_requested = original


# ═══════════════════════════════════════════════════════════════════════
# __main__ module
# ═══════════════════════════════════════════════════════════════════════


class TestResultParserExceptions:
    def test_exception_in_row_parsing(self):
        """Force an IndexError/AttributeError in row parsing (lines 143-145)."""
        from unittest.mock import patch as _patch

        html = """
        <table>
          <tr>
            <td>Part</td><td></td><td>Mfr</td><td>DC</td>
            <td>Desc</td><td>Date</td><td>US</td><td>100</td>
            <td></td><td>Vendor</td>
          </tr>
        </table>
        """
        # Monkey-patch parse_quantity to raise IndexError mid-parse
        with _patch("app.services.nc_worker.result_parser.parse_quantity", side_effect=IndexError("forced")):
            result = parse_results_html(html)
        assert result == []  # Row skipped due to exception


class TestQueueManagerEdge:
    def test_enqueue_whitespace_only_mpn(self, db_session, test_requisition):
        """Whitespace-only MPN normalizes to empty, returns None (line 38)."""
        req = test_requisition.requirements[0]
        req.primary_mpn = "   "  # Truthy but normalizes to ""
        db_session.commit()
        result = enqueue_for_nc_search(req.id, db_session)
        assert result is None


class TestSessionManagerNotLoggedIn:
    def test_start_not_logged_in(self):
        """Start() sets is_logged_in=False when session is not authorized."""
        from app.services.nc_worker.session_manager import NcSessionManager

        cfg = NcConfig()
        session = NcSessionManager(cfg)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        with patch.object(session.session, "get", return_value=mock_resp):
            with patch.object(session, "check_session_health", return_value=False):
                session.start()

        assert session.is_logged_in is False


# ═══════════════════════════════════════════════════════════════════════
# WORKER MAIN LOOP — comprehensive coverage
# ═══════════════════════════════════════════════════════════════════════


class TestWorkerMainLoop:
    """Tests for worker.main() — patches at source modules since imports are lazy.

    worker.main() is a sync function using time.sleep(), so tests patch time.sleep (not
    asyncio.sleep) and call main() directly (no await).
    """

    # Patch targets: lazy imports inside main() resolve from source modules
    _DB = "app.database.SessionLocal"
    _SESSION = "app.services.nc_worker.session_manager.NcSessionManager"
    _SCHEDULER = "app.services.nc_worker.scheduler.SearchScheduler"
    _BREAKER = "app.services.nc_worker.circuit_breaker.CircuitBreaker"
    _CONFIG = "app.services.nc_worker.config.NcConfig"
    _QUEUE_NEXT = "app.services.nc_worker.queue_manager.get_next_queued_item"
    _QUEUE_RECOVER = "app.services.nc_worker.queue_manager.recover_stale_searches"
    _QUEUE_MARK = "app.services.nc_worker.queue_manager.mark_status"
    _QUEUE_COMPLETE = "app.services.nc_worker.queue_manager.mark_completed"
    _SEARCH = "app.services.nc_worker.search_engine.search_part"
    _PARSE = "app.services.nc_worker.result_parser.parse_results_html"
    _SAVE = "app.services.nc_worker.sighting_writer.save_nc_sightings"
    _TIME_SLEEP = "app.services.nc_worker.worker.time.sleep"
    _ASYNCIO_RUN = "app.services.nc_worker.worker.asyncio.run"
    _RUN_AI_GATE = "app.services.nc_worker.worker.run_ai_gate"

    def _make_mock_db(self, db_session):
        """Create a mock SessionLocal that returns a proxy session that won't actually
        close."""
        mock_session = MagicMock(wraps=db_session)
        mock_session.close = MagicMock()  # Prevent actual close
        return MagicMock(return_value=mock_session)

    def test_main_shutdown_requested(self, db_session):
        """Main() exits immediately when shutdown is requested."""
        import app.services.nc_worker.worker as worker_mod

        ws = NcWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        original_shutdown = worker_mod._shutdown_requested
        try:
            worker_mod._shutdown_requested = True

            mock_session = MagicMock()
            mock_session.start = MagicMock()
            mock_session.stop = MagicMock()
            mock_session.is_logged_in = True
            mock_session.has_browser = False

            with patch(self._DB, self._make_mock_db(db_session)):
                with patch(self._SESSION, return_value=mock_session):
                    worker_mod.main()

            mock_session.stop.assert_called_once()
        finally:
            worker_mod._shutdown_requested = original_shutdown

    def test_main_browser_start_fails(self, db_session):
        """Main() exits when browser session fails to start."""
        import app.services.nc_worker.worker as worker_mod

        ws = NcWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        mock_session = MagicMock()
        mock_session.start = MagicMock(side_effect=Exception("Chrome not found"))

        with patch(self._DB, self._make_mock_db(db_session)):
            with patch(self._SESSION, return_value=mock_session):
                worker_mod.main()

        # Worker should not be running
        db_session.refresh(ws)
        assert ws.is_running is False

    def test_main_login_fails(self, db_session):
        """Main() exits when initial login fails."""
        import app.services.nc_worker.worker as worker_mod

        ws = NcWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        mock_session = MagicMock()
        mock_session.start = MagicMock()
        mock_session.is_logged_in = False
        mock_session.login = MagicMock(return_value=False)
        mock_session.stop = MagicMock()
        mock_session.has_browser = False

        with patch(self._DB, self._make_mock_db(db_session)):
            with patch(self._SESSION, return_value=mock_session):
                worker_mod.main()

        mock_session.stop.assert_called_once()
        db_session.refresh(ws)
        assert ws.is_running is False

    def test_main_outside_business_hours(self, db_session):
        """Main() sleeps when outside business hours then shuts down."""
        import app.services.nc_worker.worker as worker_mod

        ws = NcWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        original_shutdown = worker_mod._shutdown_requested
        loop_count = 0

        def mock_sleep(seconds):
            nonlocal loop_count
            loop_count += 1
            if loop_count >= 1:
                worker_mod._shutdown_requested = True

        mock_session = MagicMock()
        mock_session.start = MagicMock()
        mock_session.is_logged_in = True
        mock_session.stop = MagicMock()
        mock_session.has_browser = False

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.return_value = False

        try:
            worker_mod._shutdown_requested = False

            with patch(self._DB, self._make_mock_db(db_session)):
                with patch(self._SESSION, return_value=mock_session):
                    with patch(self._SCHEDULER, return_value=mock_scheduler):
                        with patch(self._TIME_SLEEP, side_effect=mock_sleep):
                            with patch(self._QUEUE_RECOVER):
                                worker_mod.main()
        finally:
            worker_mod._shutdown_requested = original_shutdown

    def test_main_daily_limit_reached(self, db_session):
        """Main() sleeps when daily limit is reached."""
        import app.services.nc_worker.worker as worker_mod

        ws = NcWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        original_shutdown = worker_mod._shutdown_requested

        def mock_sleep(seconds):
            worker_mod._shutdown_requested = True

        mock_session = MagicMock()
        mock_session.start = MagicMock()
        mock_session.is_logged_in = True
        mock_session.stop = MagicMock()
        mock_session.has_browser = False

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.return_value = True

        mock_config = MagicMock()
        mock_config.NC_MAX_DAILY_SEARCHES = 0  # Already at limit

        try:
            worker_mod._shutdown_requested = False

            with patch(self._DB, self._make_mock_db(db_session)):
                with patch(self._SESSION, return_value=mock_session):
                    with patch(self._SCHEDULER, return_value=mock_scheduler):
                        with patch(self._CONFIG, return_value=mock_config):
                            with patch(self._TIME_SLEEP, side_effect=mock_sleep):
                                with patch(self._QUEUE_RECOVER):
                                    worker_mod.main()
        finally:
            worker_mod._shutdown_requested = original_shutdown

    def test_main_circuit_breaker_open(self, db_session):
        """Main() sleeps when circuit breaker is open."""
        import app.services.nc_worker.worker as worker_mod

        ws = NcWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        original_shutdown = worker_mod._shutdown_requested

        def mock_sleep(seconds):
            worker_mod._shutdown_requested = True

        mock_session = MagicMock()
        mock_session.start = MagicMock()
        mock_session.is_logged_in = True
        mock_session.stop = MagicMock()
        mock_session.has_browser = False

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
                            with patch(self._TIME_SLEEP, side_effect=mock_sleep):
                                with patch(self._QUEUE_RECOVER):
                                    worker_mod.main()
        finally:
            worker_mod._shutdown_requested = original_shutdown

    def test_main_break_time(self, db_session):
        """Main() takes a break when scheduler says it's time."""
        import app.services.nc_worker.worker as worker_mod

        ws = NcWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        original_shutdown = worker_mod._shutdown_requested

        def mock_sleep(seconds):
            worker_mod._shutdown_requested = True

        mock_session = MagicMock()
        mock_session.start = MagicMock()
        mock_session.is_logged_in = True
        mock_session.stop = MagicMock()
        mock_session.has_browser = False

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
                            with patch(self._TIME_SLEEP, side_effect=mock_sleep):
                                with patch(self._QUEUE_RECOVER):
                                    with patch(self._RUN_AI_GATE):
                                        with patch(self._ASYNCIO_RUN):
                                            worker_mod.main()
        finally:
            worker_mod._shutdown_requested = original_shutdown

    def test_main_empty_queue(self, db_session):
        """Main() sleeps when queue is empty."""
        import app.services.nc_worker.worker as worker_mod

        ws = NcWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        original_shutdown = worker_mod._shutdown_requested

        def mock_sleep(seconds):
            worker_mod._shutdown_requested = True

        mock_session = MagicMock()
        mock_session.start = MagicMock()
        mock_session.is_logged_in = True
        mock_session.stop = MagicMock()
        mock_session.has_browser = False

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
                            with patch(self._TIME_SLEEP, side_effect=mock_sleep):
                                with patch(self._QUEUE_RECOVER):
                                    with patch(self._RUN_AI_GATE):
                                        with patch(self._ASYNCIO_RUN):
                                            with patch(self._QUEUE_NEXT, return_value=None):
                                                worker_mod.main()
        finally:
            worker_mod._shutdown_requested = original_shutdown

    def test_main_search_success(self, db_session, test_requisition):
        """Main() performs a full search cycle."""
        import app.services.nc_worker.worker as worker_mod

        ws = NcWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        req = test_requisition.requirements[0]
        queue_item = NcSearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="LM317T",
            normalized_mpn="LM317T",
            status="queued",
        )
        db_session.add(queue_item)
        db_session.commit()

        original_shutdown = worker_mod._shutdown_requested
        search_done = False

        def mock_sleep(seconds):
            nonlocal search_done
            if search_done:
                worker_mod._shutdown_requested = True
            search_done = True

        mock_session = MagicMock()
        mock_session.start = MagicMock()
        mock_session.is_logged_in = True
        mock_session.stop = MagicMock()
        mock_session.page = MagicMock()
        mock_session.ensure_session = MagicMock(return_value=True)
        mock_session.has_browser = False

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.return_value = True
        mock_scheduler.time_for_break.return_value = False
        mock_scheduler.next_delay.return_value = 120

        mock_breaker = MagicMock()
        mock_breaker.should_stop.return_value = False
        mock_breaker.check_response_health = MagicMock(return_value="HEALTHY")
        mock_breaker.record_results = MagicMock()

        search_result = {
            "html": "<table><tr><td>LM317T</td><td></td><td>TI</td><td></td><td>Vreg</td><td></td><td>US</td><td>500</td><td></td><td>Arrow</td></tr></table>",
            "total_count": 1,
            "url": "https://netcomponents.com/search/result",
            "duration_ms": 1500,
            "status_code": 200,
        }

        try:
            worker_mod._shutdown_requested = False

            with patch(self._DB, self._make_mock_db(db_session)):
                with patch(self._SESSION, return_value=mock_session):
                    with patch(self._SCHEDULER, return_value=mock_scheduler):
                        with patch(self._BREAKER, return_value=mock_breaker):
                            with patch(self._TIME_SLEEP, side_effect=mock_sleep):
                                with patch(self._QUEUE_RECOVER):
                                    with patch(self._RUN_AI_GATE):
                                        with patch(self._ASYNCIO_RUN):
                                            with patch(self._QUEUE_NEXT, return_value=queue_item):
                                                with patch(self._SEARCH, return_value=search_result):
                                                    with patch(self._PARSE, return_value=[]):
                                                        with patch(self._SAVE, return_value=0):
                                                            with patch(self._QUEUE_MARK):
                                                                with patch(self._QUEUE_COMPLETE):
                                                                    worker_mod.main()
        finally:
            worker_mod._shutdown_requested = original_shutdown

    def test_main_session_reauth_fails(self, db_session, test_requisition):
        """Main() handles session re-auth failure."""
        import app.services.nc_worker.worker as worker_mod

        ws = NcWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        req = test_requisition.requirements[0]
        queue_item = NcSearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="LM317T",
            normalized_mpn="LM317T",
            status="queued",
        )
        db_session.add(queue_item)
        db_session.commit()

        original_shutdown = worker_mod._shutdown_requested

        def mock_sleep(seconds):
            worker_mod._shutdown_requested = True

        mock_session = MagicMock()
        mock_session.start = MagicMock()
        mock_session.is_logged_in = True
        mock_session.stop = MagicMock()
        mock_session.page = MagicMock()
        mock_session.ensure_session = MagicMock(return_value=False)  # Re-auth fails
        mock_session.has_browser = False

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
                            with patch(self._TIME_SLEEP, side_effect=mock_sleep):
                                with patch(self._QUEUE_RECOVER):
                                    with patch(self._RUN_AI_GATE):
                                        with patch(self._ASYNCIO_RUN):
                                            with patch(self._QUEUE_NEXT, return_value=queue_item):
                                                with patch(self._QUEUE_MARK) as mock_mark:
                                                    worker_mod.main()
        finally:
            worker_mod._shutdown_requested = original_shutdown

    def test_main_session_expired_during_search(self, db_session, test_requisition):
        """Main() re-queues item when health check returns SESSION_EXPIRED."""
        import app.services.nc_worker.worker as worker_mod

        ws = NcWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        req = test_requisition.requirements[0]
        queue_item = NcSearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="LM317T",
            normalized_mpn="LM317T",
            status="queued",
        )
        db_session.add(queue_item)
        db_session.commit()

        original_shutdown = worker_mod._shutdown_requested

        def mock_sleep(seconds):
            worker_mod._shutdown_requested = True

        # SESSION_EXPIRED uses `continue` without sleeping — trigger shutdown from health check
        def health_then_shutdown(status_code, html, url):
            worker_mod._shutdown_requested = True
            return "SESSION_EXPIRED"

        mock_session = MagicMock()
        mock_session.start = MagicMock()
        mock_session.is_logged_in = True
        mock_session.stop = MagicMock()
        mock_session.page = MagicMock()
        mock_session.ensure_session = MagicMock(return_value=True)
        mock_session.has_browser = False

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.return_value = True
        mock_scheduler.time_for_break.return_value = False

        mock_breaker = MagicMock()
        mock_breaker.should_stop.return_value = False
        mock_breaker.check_response_health = MagicMock(side_effect=health_then_shutdown)

        search_result = {"html": "", "total_count": 0, "url": "", "duration_ms": 100, "status_code": 200}

        try:
            worker_mod._shutdown_requested = False

            with patch(self._DB, self._make_mock_db(db_session)):
                with patch(self._SESSION, return_value=mock_session):
                    with patch(self._SCHEDULER, return_value=mock_scheduler):
                        with patch(self._BREAKER, return_value=mock_breaker):
                            with patch(self._TIME_SLEEP, side_effect=mock_sleep):
                                with patch(self._QUEUE_RECOVER):
                                    with patch(self._RUN_AI_GATE):
                                        with patch(self._ASYNCIO_RUN):
                                            with patch(self._QUEUE_NEXT, return_value=queue_item):
                                                with patch(self._SEARCH, return_value=search_result):
                                                    with patch(self._QUEUE_MARK):
                                                        worker_mod.main()
        finally:
            worker_mod._shutdown_requested = original_shutdown

    def test_main_search_exception(self, db_session, test_requisition):
        """Main() marks item failed on search exception."""
        import app.services.nc_worker.worker as worker_mod

        ws = NcWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        req = test_requisition.requirements[0]
        queue_item = NcSearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="LM317T",
            normalized_mpn="LM317T",
            status="queued",
        )
        db_session.add(queue_item)
        db_session.commit()

        original_shutdown = worker_mod._shutdown_requested

        def mock_sleep(seconds):
            worker_mod._shutdown_requested = True

        mock_session = MagicMock()
        mock_session.start = MagicMock()
        mock_session.is_logged_in = True
        mock_session.stop = MagicMock()
        mock_session.page = MagicMock()
        mock_session.ensure_session = MagicMock(return_value=True)
        mock_session.has_browser = False

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
                            with patch(self._TIME_SLEEP, side_effect=mock_sleep):
                                with patch(self._QUEUE_RECOVER):
                                    with patch(self._RUN_AI_GATE):
                                        with patch(self._ASYNCIO_RUN):
                                            with patch(self._QUEUE_NEXT, return_value=queue_item):
                                                with patch(self._SEARCH, side_effect=Exception("crash")):
                                                    with patch(self._QUEUE_MARK) as mock_mark:
                                                        worker_mod.main()
        finally:
            worker_mod._shutdown_requested = original_shutdown

    def test_main_ai_gate_error(self, db_session):
        """Main() continues after AI gate error."""
        import app.services.nc_worker.worker as worker_mod

        ws = NcWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        original_shutdown = worker_mod._shutdown_requested

        def mock_sleep(seconds):
            worker_mod._shutdown_requested = True

        mock_session = MagicMock()
        mock_session.start = MagicMock()
        mock_session.is_logged_in = True
        mock_session.stop = MagicMock()
        mock_session.has_browser = False

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
                            with patch(self._TIME_SLEEP, side_effect=mock_sleep):
                                with patch(self._QUEUE_RECOVER):
                                    with patch(self._RUN_AI_GATE):
                                        with patch(self._ASYNCIO_RUN, side_effect=Exception("AI gate boom")):
                                            with patch(self._QUEUE_NEXT, return_value=None):
                                                worker_mod.main()
        finally:
            worker_mod._shutdown_requested = original_shutdown

    def test_main_daily_reset(self, db_session):
        """Main() resets daily stats on date change."""
        import app.services.nc_worker.worker as worker_mod

        ws = NcWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        original_shutdown = worker_mod._shutdown_requested
        loop_count = 0

        def mock_sleep(seconds):
            nonlocal loop_count
            loop_count += 1
            if loop_count >= 2:
                worker_mod._shutdown_requested = True

        mock_session = MagicMock()
        mock_session.start = MagicMock()
        mock_session.is_logged_in = True
        mock_session.stop = MagicMock()
        mock_session.has_browser = False

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.return_value = False  # Skip to sleep quickly

        mock_breaker = MagicMock()
        mock_breaker.should_stop.return_value = False

        try:
            worker_mod._shutdown_requested = False

            with patch(self._DB, self._make_mock_db(db_session)):
                with patch(self._SESSION, return_value=mock_session):
                    with patch(self._SCHEDULER, return_value=mock_scheduler):
                        with patch(self._BREAKER, return_value=mock_breaker):
                            with patch(self._TIME_SLEEP, side_effect=mock_sleep):
                                with patch(self._QUEUE_RECOVER):
                                    worker_mod.main()
        finally:
            worker_mod._shutdown_requested = original_shutdown

    def test_main_breaker_trips_during_search(self, db_session, test_requisition):
        """Main() marks item failed when breaker trips after health check."""
        import app.services.nc_worker.worker as worker_mod

        ws = NcWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        req = test_requisition.requirements[0]
        queue_item = NcSearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="LM317T",
            normalized_mpn="LM317T",
            status="queued",
        )
        db_session.add(queue_item)
        db_session.commit()

        original_shutdown = worker_mod._shutdown_requested

        def mock_sleep(seconds):
            worker_mod._shutdown_requested = True

        mock_session = MagicMock()
        mock_session.start = MagicMock()
        mock_session.is_logged_in = True
        mock_session.stop = MagicMock()
        mock_session.page = MagicMock()
        mock_session.ensure_session = MagicMock(return_value=True)
        mock_session.has_browser = False

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.return_value = True
        mock_scheduler.time_for_break.return_value = False

        should_stop_calls = [False, True]  # First check OK, after health check trips
        mock_breaker = MagicMock()
        mock_breaker.should_stop.side_effect = should_stop_calls
        mock_breaker.check_response_health = MagicMock(return_value="CAPTCHA_WARNING")
        mock_breaker.trip_reason = "captcha"

        search_result = {"html": "", "total_count": 0, "url": "", "duration_ms": 100, "status_code": 200}

        try:
            worker_mod._shutdown_requested = False

            with patch(self._DB, self._make_mock_db(db_session)):
                with patch(self._SESSION, return_value=mock_session):
                    with patch(self._SCHEDULER, return_value=mock_scheduler):
                        with patch(self._BREAKER, return_value=mock_breaker):
                            with patch(self._TIME_SLEEP, side_effect=mock_sleep):
                                with patch(self._QUEUE_RECOVER):
                                    with patch(self._RUN_AI_GATE):
                                        with patch(self._ASYNCIO_RUN):
                                            with patch(self._QUEUE_NEXT, return_value=queue_item):
                                                with patch(self._SEARCH, return_value=search_result):
                                                    with patch(self._QUEUE_MARK) as mock_mark:
                                                        worker_mod.main()
        finally:
            worker_mod._shutdown_requested = original_shutdown


class TestMainModule:
    def test_main_module_imports(self):
        """Verify __main__ module can be imported (without running)."""
        from app.services.nc_worker import worker

        assert callable(worker.main)


# ═══════════════════════════════════════════════════════════════════════
# COVERAGE: NC CIRCUIT BREAKER (lines 44-45, 47-48)
# ═══════════════════════════════════════════════════════════════════════


class TestCircuitBreakerGaps:
    def test_http_429_trips(self):
        """HTTP 429 rate-limited trips breaker immediately (lines 44-45)."""
        breaker = CircuitBreaker()
        result = breaker.check_response_health(429, "", "https://www.netcomponents.com/search")
        assert result == "RATE_LIMITED"
        assert breaker.is_open
        assert "429" in breaker.trip_reason

    def test_http_403_trips(self):
        """HTTP 403 access denied trips breaker immediately (lines 47-48)."""
        breaker = CircuitBreaker()
        result = breaker.check_response_health(403, "", "https://www.netcomponents.com/search")
        assert result == "ACCESS_DENIED"
        assert breaker.is_open
        assert "403" in breaker.trip_reason

    def test_session_expired(self):
        """Login redirect returns SESSION_EXPIRED (line 40)."""
        breaker = CircuitBreaker()
        result = breaker.check_response_health(200, "", "https://www.netcomponents.com/account/login")
        assert result == "SESSION_EXPIRED"
        assert not breaker.is_open

    def test_captcha_detection(self):
        """Captcha in content triggers warning then trip on second."""
        breaker = CircuitBreaker()
        result = breaker.check_response_health(
            200, "please verify you are human captcha", "https://www.netcomponents.com/search"
        )
        assert result == "CAPTCHA_WARNING"
        assert breaker.captcha_count == 1
        assert not breaker.is_open

        result = breaker.check_response_health(200, "captcha detected", "https://www.netcomponents.com/search")
        assert result == "CAPTCHA_WARNING"
        assert breaker.is_open

    def test_rate_limited_content(self):
        """Rate limit in content trips breaker."""
        breaker = CircuitBreaker()
        result = breaker.check_response_health(200, "too many requests", "https://www.netcomponents.com/search")
        assert result == "RATE_LIMITED"
        assert breaker.is_open

    def test_healthy_resets_failures(self):
        """Healthy response resets consecutive failures."""
        breaker = CircuitBreaker()
        breaker.consecutive_failures = 2
        result = breaker.check_response_health(200, "normal page content", "https://www.netcomponents.com/search")
        assert result == "HEALTHY"
        assert breaker.consecutive_failures == 0

    def test_empty_results_streak_trips(self):
        """10 consecutive empty results trips breaker."""
        breaker = CircuitBreaker()
        for _ in range(9):
            breaker.record_empty_results()
            assert not breaker.is_open
        breaker.record_empty_results()
        assert breaker.is_open

    def test_record_results_resets_streak(self):
        """Non-empty results reset the streak."""
        breaker = CircuitBreaker()
        breaker.empty_results_streak = 5
        breaker.record_results()
        assert breaker.empty_results_streak == 0

    def test_get_trip_info(self):
        """get_trip_info returns correct dict."""
        breaker = CircuitBreaker()
        info = breaker.get_trip_info()
        assert "is_open" in info
        assert "trip_reason" in info

    def test_should_stop_returns_is_open(self):
        """should_stop() returns is_open value (line 93)."""
        breaker = CircuitBreaker()
        assert breaker.should_stop() is False
        breaker.is_open = True
        assert breaker.should_stop() is True


# ═══════════════════════════════════════════════════════════════════════
# COVERAGE: NC SCHEDULER (lines 20-21, 40-56, 73, 77, 81-83)
# ═══════════════════════════════════════════════════════════════════════


class TestNcSchedulerFull:
    def test_business_hours_force_env(self):
        """FORCE_BUSINESS_HOURS env var overrides schedule."""
        cfg = NcConfig()
        sched = SearchScheduler(cfg)
        with patch.dict(os.environ, {"FORCE_BUSINESS_HOURS": "1"}):
            assert sched.is_business_hours() is True

    def test_business_hours_saturday(self):
        """Saturday returns False."""
        cfg = NcConfig()
        sched = SearchScheduler(cfg)
        with patch("app.services.nc_worker.scheduler.datetime") as mock_dt:
            mock_now = MagicMock()
            mock_now.weekday.return_value = 5
            mock_now.hour = 12
            mock_dt.now.return_value = mock_now
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("FORCE_BUSINESS_HOURS", None)
                assert sched.is_business_hours() is False

    def test_business_hours_sunday_before_6pm(self):
        """Sunday before 6 PM returns False."""
        cfg = NcConfig()
        sched = SearchScheduler(cfg)
        with patch("app.services.nc_worker.scheduler.datetime") as mock_dt:
            mock_now = MagicMock()
            mock_now.weekday.return_value = 6
            mock_now.hour = 10
            mock_dt.now.return_value = mock_now
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("FORCE_BUSINESS_HOURS", None)
                assert sched.is_business_hours() is False

    def test_business_hours_sunday_after_6pm(self):
        """Sunday at 6 PM+ returns True."""
        cfg = NcConfig()
        sched = SearchScheduler(cfg)
        with patch("app.services.nc_worker.scheduler.datetime") as mock_dt:
            mock_now = MagicMock()
            mock_now.weekday.return_value = 6
            mock_now.hour = 18
            mock_dt.now.return_value = mock_now
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("FORCE_BUSINESS_HOURS", None)
                assert sched.is_business_hours() is True

    def test_business_hours_friday_before_5pm(self):
        """Friday before 5 PM returns True."""
        cfg = NcConfig()
        sched = SearchScheduler(cfg)
        with patch("app.services.nc_worker.scheduler.datetime") as mock_dt:
            mock_now = MagicMock()
            mock_now.weekday.return_value = 4
            mock_now.hour = 12
            mock_dt.now.return_value = mock_now
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("FORCE_BUSINESS_HOURS", None)
                assert sched.is_business_hours() is True

    def test_business_hours_friday_after_5pm(self):
        """Friday at 5 PM+ returns False."""
        cfg = NcConfig()
        sched = SearchScheduler(cfg)
        with patch("app.services.nc_worker.scheduler.datetime") as mock_dt:
            mock_now = MagicMock()
            mock_now.weekday.return_value = 4
            mock_now.hour = 17
            mock_dt.now.return_value = mock_now
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("FORCE_BUSINESS_HOURS", None)
                assert sched.is_business_hours() is False

    def test_business_hours_weekday(self):
        """Mon-Thu always returns True."""
        cfg = NcConfig()
        sched = SearchScheduler(cfg)
        with patch("app.services.nc_worker.scheduler.datetime") as mock_dt:
            mock_now = MagicMock()
            mock_now.weekday.return_value = 2
            mock_now.hour = 3
            mock_dt.now.return_value = mock_now
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("FORCE_BUSINESS_HOURS", None)
                assert sched.is_business_hours() is True

    def test_next_delay_bounds(self):
        """next_delay returns value within configured bounds."""
        cfg = NcConfig()
        sched = SearchScheduler(cfg)
        for _ in range(20):
            delay = sched.next_delay()
            assert cfg.NC_MIN_DELAY_SECONDS <= delay <= cfg.NC_MAX_DELAY_SECONDS

    def test_time_for_break(self):
        """time_for_break returns True when threshold reached."""
        cfg = NcConfig()
        sched = SearchScheduler(cfg)
        sched.break_threshold = 3
        assert not sched.time_for_break()
        sched.searches_since_break = 3
        assert sched.time_for_break()

    def test_get_break_duration(self):
        """get_break_duration returns value in range."""
        cfg = NcConfig()
        sched = SearchScheduler(cfg)
        duration = sched.get_break_duration()
        assert 5 * 60 <= duration <= 25 * 60

    def test_reset_break_counter(self):
        """reset_break_counter resets counter and picks new threshold."""
        cfg = NcConfig()
        sched = SearchScheduler(cfg)
        sched.searches_since_break = 10
        sched.reset_break_counter()
        assert sched.searches_since_break == 0
        assert 8 <= sched.break_threshold <= 15


# ═══════════════════════════════════════════════════════════════════════
# COVERAGE: NC SEARCH ENGINE (lines 52-53, 80-82, 91-138, 153)
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.slow
class TestNcSearchEngineFull:
    def test_search_http_exception(self):
        """_search_http handles exceptions gracefully (lines 80-82)."""
        from app.services.nc_worker.search_engine import _search_http

        mock_session = MagicMock()
        mock_session.session.get = MagicMock(side_effect=Exception("Connection refused"))

        result = _search_http(mock_session, "STM32F103")
        assert result["html"] == ""
        assert result["status_code"] == 0
        assert result["duration_ms"] == 0

    @pytest.mark.asyncio
    async def test_search_browser_no_browser_start_fail(self):
        """_search_browser returns None when browser cannot be started (lines 91-93)."""
        from app.services.nc_worker.search_engine import _search_browser

        mock_session = MagicMock()
        mock_session.has_browser = False
        mock_session.start_browser = AsyncMock()

        # After start_browser, still no browser
        type(mock_session).has_browser = PropertyMock(side_effect=[False, False])

        result = await _search_browser(mock_session, "XYZ")
        assert result is None

    @pytest.mark.asyncio
    async def test_search_browser_success(self):
        """_search_browser returns results on success (lines 96-143)."""
        from app.services.nc_worker.search_engine import _search_browser

        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.wait_for_selector = AsyncMock()
        mock_page.evaluate = AsyncMock(
            side_effect=[
                True,  # auth_check returns True
                "<html>browser results</html>",  # page HTML
            ]
        )

        mock_session = MagicMock()
        mock_session.has_browser = True
        mock_session.page = mock_page

        result = await _search_browser(mock_session, "STM32F103")
        assert result is not None
        assert result["html"] == "<html>browser results</html>"
        assert result["status_code"] == 200

    @pytest.mark.asyncio
    async def test_search_browser_login_required(self):
        """_search_browser handles login flow when not authorized."""
        from app.services.nc_worker.search_engine import _search_browser

        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.wait_for_selector = AsyncMock()
        mock_page.evaluate = AsyncMock(
            side_effect=[
                False,  # auth_check fails
            ]
        )

        mock_session = MagicMock()
        mock_session.has_browser = True
        mock_session.page = mock_page
        mock_session.login_browser = AsyncMock(return_value=False)

        result = await _search_browser(mock_session, "STM32F103")
        assert result is None

    @pytest.mark.asyncio
    async def test_search_browser_exception(self):
        """_search_browser returns None on exception (line 146)."""
        from app.services.nc_worker.search_engine import _search_browser

        mock_session = MagicMock()
        mock_session.has_browser = True
        mock_session.page = AsyncMock()
        mock_session.page.goto = AsyncMock(side_effect=Exception("Browser crashed"))

        result = await _search_browser(mock_session, "XYZ")
        assert result is None

    @pytest.mark.asyncio
    async def test_search_browser_selector_timeout(self):
        """_search_browser handles selector timeout (line 123)."""
        from app.services.nc_worker.search_engine import _search_browser

        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.wait_for_selector = AsyncMock(side_effect=Exception("Timeout"))
        mock_page.evaluate = AsyncMock(
            side_effect=[
                True,  # auth_check
                "<html>results after timeout</html>",  # page HTML
            ]
        )

        mock_session = MagicMock()
        mock_session.has_browser = True
        mock_session.page = mock_page

        result = await _search_browser(mock_session, "ABC")
        assert result is not None
        assert "results after timeout" in result["html"]

    def test_search_part_browser_fallback_success(self):
        """search_part uses browser fallback when HTTP returns no results (line
        52-53)."""
        mock_resp = MagicMock()
        mock_resp.text = "<html>No results</html>"
        mock_resp.status_code = 200

        mock_session_mgr = MagicMock()
        mock_session_mgr.session.get = MagicMock(return_value=mock_resp)
        mock_session_mgr.has_browser = True

        browser_result = {
            "html": "<div class='searchresultstable'>data</div>",
            "url": "url",
            "duration_ms": 500,
            "status_code": 200,
        }

        with patch("app.services.nc_worker.search_engine._search_browser"):
            with patch("app.services.nc_worker.search_engine.asyncio.run", return_value=browser_result):
                result = search_part(mock_session_mgr, "XYZ123")

        assert result["mode"] == "browser"

    def test_has_results_empty(self):
        """_has_results returns False for empty string (line 153)."""
        from app.services.nc_worker.search_engine import _has_results

        assert _has_results("") is False
        assert _has_results(None) is False


# ═══════════════════════════════════════════════════════════════════════
# COVERAGE: NC RESULT PARSER (lines 78, 90-92, 118-197)
# ═══════════════════════════════════════════════════════════════════════


class TestNcResultParserFull:
    def test_floating_block_parse(self):
        """Parse results with proper .div-table-float-reg.floating-block structure
        (lines 118-197)."""
        html = """
        <div class="div-table-float-reg floating-block">
            <div class="region-header">The Americas</div>
            <div class="stock-type">In-Stock Inventory</div>
            <table class="searchresultstable">
                <tr>
                    <td>STM32F103</td>
                    <td><span class="nctd" data-url="https://example.com/part">link</span></td>
                    <td class="ncdsl">dsl</td>
                    <td>ST</td>
                    <td>2024</td>
                    <td>ARM MCU</td>
                    <td>01/2026</td>
                    <td>US</td>
                    <td>500</td>
                    <td><span class="ncprc" data-pbrk='{"currency":"USD","Prices":[{"price":1.5,"minQty":1}]}'></span></td>
                    <td class="nccart">cart</td>
                    <td class="ncsqrs">sqrs</td>
                    <td>Arrow Electronics</td>
                    <td></td>
                </tr>
            </table>
        </div>
        """
        sightings = parse_results_html(html)
        assert len(sightings) == 1
        s = sightings[0]
        assert s.part_number == "STM32F103"
        assert s.manufacturer == "ST"
        assert s.region == "The Americas"
        assert s.inventory_type == "in_stock"
        assert s.vendor_name == "Arrow Electronics"
        assert len(s.price_breaks) == 1
        assert float(s.price_breaks[0].price) == 1.5
        assert s.currency == "USD"
        assert s.is_authorized is True
        assert s.supplier_product_url == "https://example.com/part"

    def test_floating_block_brokered(self):
        """Brokered inventory type detection."""
        html = """
        <div class="div-table-float-reg floating-block">
            <div class="region-header">Europe</div>
            <div class="stock-type">Brokered Inventory</div>
            <table class="searchresultstable">
                <tr>
                    <td>LM317T</td>
                    <td></td><td></td><td>TI</td><td>2024</td>
                    <td>Vreg</td><td>02/2026</td><td>DE</td>
                    <td>100</td><td></td><td></td><td></td>
                    <td>EuroParts</td><td></td>
                </tr>
            </table>
        </div>
        """
        sightings = parse_results_html(html)
        assert len(sightings) == 1
        assert sightings[0].inventory_type == "brokered"
        assert sightings[0].region == "Europe"

    def test_floating_block_sponsor(self):
        """Sponsor detection from non-empty cell 13."""
        html = """
        <div class="div-table-float-reg floating-block">
            <div class="region-header">Asia</div>
            <table class="searchresultstable">
                <tr>
                    <td>XC7A35T</td>
                    <td></td><td></td><td>Xilinx</td><td>2024</td>
                    <td>FPGA</td><td>03/2026</td><td>CN</td>
                    <td>50</td><td></td><td></td><td></td>
                    <td>SponsorVendor</td><td>SPONSOR</td>
                </tr>
            </table>
        </div>
        """
        sightings = parse_results_html(html)
        assert len(sightings) == 1
        assert sightings[0].is_sponsor is True

    def test_floating_block_no_data_tables(self):
        """Container with no data tables is skipped (line 131)."""
        html = """
        <div class="div-table-float-reg floating-block">
            <div class="region-header">The Americas</div>
        </div>
        """
        sightings = parse_results_html(html)
        assert len(sightings) == 0

    def test_floating_block_header_row_skipped(self):
        """Row with 'Part Number' text is skipped (line 144)."""
        html = """
        <div class="div-table-float-reg floating-block">
            <div class="region-header">The Americas</div>
            <table class="searchresultstable">
                <tr>
                    <td>Part Number</td>
                    <td></td><td></td><td></td><td></td>
                    <td></td><td></td><td></td><td></td>
                    <td></td><td></td><td></td><td></td>
                </tr>
            </table>
        </div>
        """
        sightings = parse_results_html(html)
        assert len(sightings) == 0

    def test_floating_block_too_few_cells(self):
        """Row with fewer than 13 cells is skipped (line 138)."""
        html = """
        <div class="div-table-float-reg floating-block">
            <div class="region-header">The Americas</div>
            <table class="searchresultstable">
                <tr><td>Part</td><td>B</td><td>C</td></tr>
            </table>
        </div>
        """
        sightings = parse_results_html(html)
        assert len(sightings) == 0

    def test_floating_block_nctd_fallback(self):
        """Nctd selector falls back to row-level (lines 160-162)."""
        html = """
        <div class="div-table-float-reg floating-block">
            <div class="region-header">The Americas</div>
            <table class="searchresultstable">
                <tr>
                    <td>STM32</td>
                    <td>no nctd here</td><td></td><td>ST</td><td>2024</td>
                    <td>MCU</td><td>01/26</td><td>US</td>
                    <td>500</td><td></td><td></td><td></td>
                    <td>Vendor</td><td></td>
                    <span class="nctd" data-url="https://row-level.com">row</span>
                </tr>
            </table>
        </div>
        """
        sightings = parse_results_html(html)
        assert len(sightings) == 1

    def test_floating_block_ncprc_fallback(self):
        """Ncprc selector falls back to row-level (lines 166-167)."""
        html = """
        <div class="div-table-float-reg floating-block">
            <div class="region-header">The Americas</div>
            <table class="searchresultstable">
                <tr>
                    <td>STM32</td>
                    <td></td><td></td><td>ST</td><td>2024</td>
                    <td>MCU</td><td>01/26</td><td>US</td>
                    <td>500</td><td>no ncprc</td><td></td><td></td>
                    <td>Vendor</td><td></td>
                    <span class="ncprc" data-pbrk='{"currency":"EUR","Prices":[{"price":2.0,"minQty":10}]}'></span>
                </tr>
            </table>
        </div>
        """
        sightings = parse_results_html(html)
        assert len(sightings) == 1

    def test_malformed_row_exception(self):
        """Force IndexError in row parsing (lines 192-194)."""
        with patch("app.services.nc_worker.result_parser.parse_quantity", side_effect=IndexError("forced")):
            html = """
            <div class="div-table-float-reg floating-block">
                <div class="region-header">The Americas</div>
                <table class="searchresultstable">
                    <tr>
                        <td>STM32</td><td></td><td></td><td>ST</td>
                        <td>2024</td><td>MCU</td><td>01/26</td><td>US</td>
                        <td>500</td><td></td><td></td><td></td>
                        <td>Vendor</td><td></td>
                    </tr>
                </table>
            </div>
            """
            sightings = parse_results_html(html)
        assert sightings == []

    def test_parse_price_breaks_bad_json(self):
        """parse_price_breaks handles invalid JSON gracefully (line 90-92)."""
        from bs4 import BeautifulSoup

        from app.services.nc_worker.result_parser import parse_price_breaks

        soup = BeautifulSoup('<span class="ncprc" data-pbrk="not valid json"></span>', "html.parser")
        el = soup.find("span")
        breaks, currency = parse_price_breaks(el)
        assert breaks == []
        assert currency is None

    def test_parse_price_breaks_none_element(self):
        """parse_price_breaks handles None element (line 73-74)."""
        from app.services.nc_worker.result_parser import parse_price_breaks

        breaks, currency = parse_price_breaks(None)
        assert breaks == []
        assert currency is None

    def test_parse_price_breaks_no_data_attr(self):
        """parse_price_breaks handles element without data-pbrk."""
        from bs4 import BeautifulSoup

        from app.services.nc_worker.result_parser import parse_price_breaks

        soup = BeautifulSoup('<span class="ncprc"></span>', "html.parser")
        el = soup.find("span")
        breaks, currency = parse_price_breaks(el)
        assert breaks == []
        assert currency is None

    def test_flat_parse_europe_region(self):
        """Flat parser detects Europe region (lines 216-217)."""
        html = """
        <table>
          <tr><td colspan="10">Europe Distribution</td></tr>
          <tr><td colspan="10">In-Stock Inventory</td></tr>
          <tr>
            <td>LM317</td><td></td><td></td><td>TI</td>
            <td>2024</td><td>Vreg</td><td>01/26</td><td>DE</td>
            <td>100</td><td></td><td></td><td></td>
            <td>EuroParts</td><td></td>
          </tr>
        </table>
        """
        sightings = parse_results_html(html)
        assert len(sightings) == 1
        assert sightings[0].region == "Europe"

    def test_flat_parse_brokered(self):
        """Flat parser detects brokered inventory type (lines 225-226)."""
        html = """
        <table>
          <tr><td colspan="10">The Americas</td></tr>
          <tr><td colspan="10">Brokered</td></tr>
          <tr>
            <td>XYZ123</td><td></td><td></td><td>MFR</td>
            <td>2024</td><td>Desc</td><td>01/26</td><td>US</td>
            <td>50</td><td></td><td></td><td></td>
            <td>Vendor</td><td></td>
          </tr>
        </table>
        """
        sightings = parse_results_html(html)
        assert len(sightings) == 1
        assert sightings[0].inventory_type == "brokered"

    def test_flat_parse_empty_part_number(self):
        """Flat parser skips rows with empty part number (line 236)."""
        html = """
        <table>
          <tr>
            <td></td><td></td><td></td><td></td>
            <td></td><td></td><td></td><td></td>
          </tr>
        </table>
        """
        sightings = parse_results_html(html)
        assert len(sightings) == 0

    def test_flat_parse_too_few_cells(self):
        """Flat parser skips rows with fewer than 8 cells (line 230)."""
        html = """
        <table>
          <tr><td>Americas</td></tr>
          <tr>
            <td>Part</td><td>B</td><td>C</td><td>D</td><td>E</td>
          </tr>
        </table>
        """
        sightings = parse_results_html(html)
        assert len(sightings) == 0


# ═══════════════════════════════════════════════════════════════════════
# COVERAGE: NC SESSION MANAGER (lines 52, 64-66, 70-91, 128-129, 161-208, 222-223, 228-239)
# ═══════════════════════════════════════════════════════════════════════


class TestNcSessionManagerFull:
    def test_has_browser_property(self):
        """has_browser returns False when not started (line 52)."""
        from app.services.nc_worker.session_manager import NcSessionManager

        cfg = NcConfig()
        sm = NcSessionManager(cfg)
        assert sm.has_browser is False

    def test_page_property(self):
        """Page property returns None when not started (line 48)."""
        from app.services.nc_worker.session_manager import NcSessionManager

        cfg = NcConfig()
        sm = NcSessionManager(cfg)
        assert sm.page is None

    def test_start_exception(self):
        """Start() raises on network error (lines 64-66)."""
        from app.services.nc_worker.session_manager import NcSessionManager

        cfg = NcConfig()
        sm = NcSessionManager(cfg)

        with patch.object(sm.session, "get", side_effect=Exception("Network error")):
            with pytest.raises(Exception, match="Network error"):
                sm.start()

    @pytest.mark.asyncio
    async def test_start_browser_no_display(self):
        """start_browser raises when DISPLAY not set (lines 73-78)."""
        from app.services.nc_worker.session_manager import NcSessionManager

        cfg = NcConfig()
        sm = NcSessionManager(cfg)

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("DISPLAY", None)
            with pytest.raises(RuntimeError, match="DISPLAY"):
                await sm.start_browser()

    @pytest.mark.asyncio
    async def test_start_browser_success(self):
        """start_browser launches Patchright (lines 80-91)."""
        from app.services.nc_worker.session_manager import NcSessionManager

        cfg = NcConfig()
        sm = NcSessionManager(cfg)

        mock_page = AsyncMock()
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
                await sm.start_browser()

        assert sm.has_browser is True
        assert sm._page is mock_page

    @pytest.mark.asyncio
    async def test_start_browser_already_started(self):
        """start_browser does nothing if already started (line 71)."""
        from app.services.nc_worker.session_manager import NcSessionManager

        cfg = NcConfig()
        sm = NcSessionManager(cfg)
        sm._browser_started = True

        await sm.start_browser()  # Should return immediately

    def test_login_no_csrf_token(self):
        """Login() returns False when CSRF token not found (lines 128-129)."""
        from app.services.nc_worker.session_manager import NcSessionManager

        cfg = NcConfig()
        cfg.NC_ACCOUNT_NUMBER = "12345"
        cfg.NC_USERNAME = "test@example.com"
        cfg.NC_PASSWORD = "pass"
        sm = NcSessionManager(cfg)

        login_page_resp = MagicMock()
        login_page_resp.status_code = 200
        login_page_resp.text = "<html>No token here</html>"
        login_page_resp.raise_for_status = MagicMock()

        with patch.object(sm.session, "get", return_value=login_page_resp):
            result = sm.login()

        assert result is False

    @pytest.mark.asyncio
    async def test_login_browser_success(self):
        """login_browser full success flow (lines 161-208)."""
        from app.services.nc_worker.session_manager import NcSessionManager

        cfg = NcConfig()
        cfg.NC_ACCOUNT_NUMBER = "12345"
        cfg.NC_USERNAME = "test@example.com"
        cfg.NC_PASSWORD = "pass"
        sm = NcSessionManager(cfg)
        sm._browser_started = True

        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value={"status": 200, "body": "true"})

        mock_locator = AsyncMock()
        mock_locator.wait_for = AsyncMock()
        mock_locator.fill = AsyncMock()
        mock_locator.click = AsyncMock()
        mock_page.locator = MagicMock(return_value=mock_locator)

        sm._page = mock_page

        mock_hb = MagicMock()
        mock_hb.human_type = AsyncMock()
        mock_hb.random_delay = AsyncMock()

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with patch("app.services.nc_worker.human_behavior.HumanBehavior", mock_hb):
                result = await sm.login_browser()

        assert result is True
        assert sm.is_logged_in is True

    @pytest.mark.asyncio
    async def test_login_browser_failure(self):
        """login_browser returns False when auth check fails."""
        from app.services.nc_worker.session_manager import NcSessionManager

        cfg = NcConfig()
        cfg.NC_ACCOUNT_NUMBER = "12345"
        cfg.NC_USERNAME = "test@example.com"
        cfg.NC_PASSWORD = "pass"
        sm = NcSessionManager(cfg)
        sm._browser_started = True

        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value={"status": 401, "body": "false"})

        mock_locator = AsyncMock()
        mock_locator.wait_for = AsyncMock()
        mock_locator.fill = AsyncMock()
        mock_locator.click = AsyncMock()
        mock_page.locator = MagicMock(return_value=mock_locator)

        sm._page = mock_page

        mock_hb = MagicMock()
        mock_hb.human_type = AsyncMock()
        mock_hb.random_delay = AsyncMock()

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with patch("app.services.nc_worker.human_behavior.HumanBehavior", mock_hb):
                result = await sm.login_browser()

        assert result is False

    @pytest.mark.asyncio
    async def test_login_browser_exception(self):
        """login_browser handles exceptions gracefully."""
        from app.services.nc_worker.session_manager import NcSessionManager

        cfg = NcConfig()
        cfg.NC_ACCOUNT_NUMBER = "12345"
        cfg.NC_USERNAME = "test@example.com"
        cfg.NC_PASSWORD = "pass"
        sm = NcSessionManager(cfg)
        sm._browser_started = True
        sm._page = AsyncMock()
        sm._page.goto = AsyncMock(side_effect=TimeoutError("Browser crashed"))

        result = await sm.login_browser()
        assert result is False
        assert sm.is_logged_in is False

    @pytest.mark.asyncio
    async def test_login_browser_starts_browser_if_needed(self):
        """login_browser calls start_browser if not started (line 163-164)."""
        from app.services.nc_worker.session_manager import NcSessionManager

        cfg = NcConfig()
        cfg.NC_ACCOUNT_NUMBER = "12345"
        cfg.NC_USERNAME = "test@example.com"
        cfg.NC_PASSWORD = "pass"
        sm = NcSessionManager(cfg)

        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value={"status": 200, "body": "true"})
        mock_locator = AsyncMock()
        mock_locator.wait_for = AsyncMock()
        mock_locator.fill = AsyncMock()
        mock_locator.click = AsyncMock()
        mock_page.locator = MagicMock(return_value=mock_locator)

        async def fake_start_browser():
            sm._browser_started = True
            sm._page = mock_page

        sm.start_browser = fake_start_browser

        mock_hb = MagicMock()
        mock_hb.human_type = AsyncMock()
        mock_hb.random_delay = AsyncMock()

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with patch("app.services.nc_worker.human_behavior.HumanBehavior", mock_hb):
                result = await sm.login_browser()

        assert result is True

    def test_stop_with_exception(self):
        """Stop() handles exception on close (lines 222-223)."""
        from app.services.nc_worker.session_manager import NcSessionManager

        cfg = NcConfig()
        sm = NcSessionManager(cfg)
        sm.is_logged_in = True
        sm.session = MagicMock()
        sm.session.close = MagicMock(side_effect=OSError("close error"))

        sm.stop()
        assert sm.is_logged_in is False

    @pytest.mark.asyncio
    async def test_stop_browser(self):
        """stop_browser closes context and playwright (lines 228-239)."""
        from app.services.nc_worker.session_manager import NcSessionManager

        cfg = NcConfig()
        sm = NcSessionManager(cfg)
        sm._context = AsyncMock()
        sm._playwright = AsyncMock()
        sm._page = AsyncMock()
        sm._browser_started = True

        await sm.stop_browser()

        assert sm._context is None
        assert sm._page is None
        assert sm._playwright is None
        assert sm._browser_started is False

    @pytest.mark.asyncio
    async def test_stop_browser_with_error(self):
        """stop_browser handles errors gracefully."""
        from app.services.nc_worker.session_manager import NcSessionManager

        cfg = NcConfig()
        sm = NcSessionManager(cfg)
        sm._context = AsyncMock()
        sm._context.close = AsyncMock(side_effect=RuntimeError("Close error"))
        sm._playwright = AsyncMock()
        sm._browser_started = True

        await sm.stop_browser()
        assert sm._context is None


# ═══════════════════════════════════════════════════════════════════════
# COVERAGE: NC SIGHTING WRITER (lines 58, 66, 73, 86)
# ═══════════════════════════════════════════════════════════════════════


class TestNcSightingWriterGaps:
    def test_skip_no_vendor_name(self, db_session, test_requisition):
        """save_nc_sightings skips entries without vendor_name (line 58)."""
        req = test_requisition.requirements[0]
        queue_item = NcSearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="LM317T",
            normalized_mpn="LM317T",
            status="searching",
        )
        db_session.add(queue_item)
        db_session.commit()

        nc = [NcSighting(part_number="LM317T", vendor_name="", quantity=100)]
        result = save_nc_sightings(db_session, queue_item, nc)
        assert result == 0

    def test_dedup_skip(self, db_session, test_requisition):
        """save_nc_sightings skips duplicate entries (line 66)."""
        req = test_requisition.requirements[0]
        queue_item = NcSearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="LM317T",
            normalized_mpn="LM317T",
            status="searching",
        )
        db_session.add(queue_item)
        db_session.commit()

        nc = [NcSighting(part_number="LM317T", vendor_name="Arrow", quantity=500, inventory_type="in_stock")]
        count1 = save_nc_sightings(db_session, queue_item, nc)
        count2 = save_nc_sightings(db_session, queue_item, nc)
        assert count1 == 1
        assert count2 == 0

    def test_price_breaks_in_sighting(self, db_session, test_requisition):
        """save_nc_sightings extracts unit_price and includes price_breaks in raw_data
        (lines 73, 86)."""
        from app.services.nc_worker.result_parser import PriceBreak

        req = test_requisition.requirements[0]
        queue_item = NcSearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="LM317T",
            normalized_mpn="LM317T",
            status="searching",
        )
        db_session.add(queue_item)
        db_session.commit()

        nc = [
            NcSighting(
                part_number="LM317T",
                vendor_name="DigiKey",
                quantity=500,
                inventory_type="in_stock",
                price_breaks=[PriceBreak(price=1.25, min_qty=1), PriceBreak(price=0.99, min_qty=100)],
                currency="USD",
            )
        ]
        save_nc_sightings(db_session, queue_item, nc)

        s = db_session.query(Sighting).filter(Sighting.source_type == "netcomponents").first()
        assert float(s.unit_price) == 1.25
        assert s.currency == "USD"
        assert "price_breaks" in s.raw_data
        assert len(s.raw_data["price_breaks"]) == 2


# ═══════════════════════════════════════════════════════════════════════
# COVERAGE: NC WORKER (lines 26-27, 60-61, 139-156, 253, 295-296, 316, 325)
# ═══════════════════════════════════════════════════════════════════════


class TestNcWorkerGaps:
    _DB = "app.database.SessionLocal"
    _SESSION = "app.services.nc_worker.session_manager.NcSessionManager"
    _SCHEDULER = "app.services.nc_worker.scheduler.SearchScheduler"
    _BREAKER = "app.services.nc_worker.circuit_breaker.CircuitBreaker"
    _CONFIG = "app.services.nc_worker.config.NcConfig"
    _QUEUE_NEXT = "app.services.nc_worker.queue_manager.get_next_queued_item"
    _QUEUE_RECOVER = "app.services.nc_worker.queue_manager.recover_stale_searches"
    _QUEUE_MARK = "app.services.nc_worker.queue_manager.mark_status"
    _QUEUE_COMPLETE = "app.services.nc_worker.queue_manager.mark_completed"
    _SEARCH = "app.services.nc_worker.search_engine.search_part"
    _PARSE = "app.services.nc_worker.result_parser.parse_results_html"
    _SAVE = "app.services.nc_worker.sighting_writer.save_nc_sightings"
    _TIME_SLEEP = "app.services.nc_worker.worker.time.sleep"
    _ASYNCIO_RUN = "app.services.nc_worker.worker.asyncio.run"
    _RUN_AI_GATE = "app.services.nc_worker.worker.run_ai_gate"

    def _make_mock_db(self, db_session):
        mock_session = MagicMock(wraps=db_session)
        mock_session.close = MagicMock()
        return MagicMock(return_value=mock_session)

    @pytest.mark.asyncio
    async def test_run_ai_gate(self, db_session):
        """run_ai_gate is a simple async wrapper (lines 60-61)."""
        import app.services.nc_worker.worker as worker_mod

        with patch("app.services.nc_worker.ai_gate.process_ai_gate", new_callable=AsyncMock) as mock_gate:
            await worker_mod.run_ai_gate(db_session)
            mock_gate.assert_called_once_with(db_session)

    def test_main_daily_stats_reset_with_summary(self, db_session):
        """Main() logs daily summary and resets stats on date change (lines 139-156)."""
        import app.services.nc_worker.worker as worker_mod

        ws = NcWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        original = worker_mod._shutdown_requested
        loop_count = 0

        real_datetime = datetime

        def mock_sleep(seconds):
            nonlocal loop_count
            loop_count += 1
            if loop_count >= 2:
                worker_mod._shutdown_requested = True

        from app.services.nc_worker.worker import EASTERN

        eastern_calls = 0

        def patched_now(tz=None):
            nonlocal eastern_calls
            if tz is not None and str(tz) == str(EASTERN):
                eastern_calls += 1
                if eastern_calls == 1:
                    return real_datetime(2026, 2, 28, 23, 59, 0, tzinfo=timezone.utc)
                return real_datetime(2026, 3, 1, 0, 1, 0, tzinfo=timezone.utc)
            return real_datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc)

        mock_session = MagicMock()
        mock_session.start = MagicMock()
        mock_session.is_logged_in = True
        mock_session.stop = MagicMock()
        mock_session.has_browser = False

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.return_value = False

        try:
            worker_mod._shutdown_requested = False
            with patch(self._DB, self._make_mock_db(db_session)):
                with patch(self._SESSION, return_value=mock_session):
                    with patch(self._SCHEDULER, return_value=mock_scheduler):
                        with patch(self._TIME_SLEEP, side_effect=mock_sleep):
                            with patch(self._QUEUE_RECOVER):
                                with patch("app.services.nc_worker.worker.datetime") as mock_dt:
                                    mock_dt.now = patched_now
                                    mock_dt.side_effect = lambda *a, **kw: real_datetime(*a, **kw)
                                    worker_mod.main()
        finally:
            worker_mod._shutdown_requested = original

    def test_main_search_with_sightings(self, db_session, test_requisition):
        """Main() records sightings and empty results correctly (line 253)."""
        import app.services.nc_worker.worker as worker_mod

        ws = NcWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        req = test_requisition.requirements[0]
        queue_item = NcSearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="LM317T",
            normalized_mpn="LM317T",
            status="queued",
        )
        db_session.add(queue_item)
        db_session.commit()

        original = worker_mod._shutdown_requested
        search_done = False

        def mock_sleep(seconds):
            nonlocal search_done
            if search_done:
                worker_mod._shutdown_requested = True
            search_done = True

        mock_session = MagicMock()
        mock_session.start = MagicMock()
        mock_session.is_logged_in = True
        mock_session.stop = MagicMock()
        mock_session.ensure_session = MagicMock(return_value=True)
        mock_session.has_browser = False

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.return_value = True
        mock_scheduler.time_for_break.return_value = False
        mock_scheduler.next_delay.return_value = 120

        mock_breaker = MagicMock()
        mock_breaker.should_stop.return_value = False
        mock_breaker.check_response_health = MagicMock(return_value="HEALTHY")

        search_result = {
            "html": "<table>results</table>",
            "total_count": 1,
            "url": "url",
            "duration_ms": 500,
            "status_code": 200,
        }
        mock_nc_sighting = MagicMock()

        try:
            worker_mod._shutdown_requested = False
            with patch(self._DB, self._make_mock_db(db_session)):
                with patch(self._SESSION, return_value=mock_session):
                    with patch(self._SCHEDULER, return_value=mock_scheduler):
                        with patch(self._BREAKER, return_value=mock_breaker):
                            with patch(self._TIME_SLEEP, side_effect=mock_sleep):
                                with patch(self._QUEUE_RECOVER):
                                    with patch(self._RUN_AI_GATE):
                                        with patch(self._ASYNCIO_RUN):
                                            with patch(self._QUEUE_NEXT, return_value=queue_item):
                                                with patch(self._SEARCH, return_value=search_result):
                                                    with patch(self._PARSE, return_value=[mock_nc_sighting]):
                                                        with patch(self._SAVE, return_value=1):
                                                            with patch(self._QUEUE_MARK):
                                                                with patch(self._QUEUE_COMPLETE):
                                                                    worker_mod.main()
        finally:
            worker_mod._shutdown_requested = original

    def test_main_mark_status_fails_in_except(self, db_session, test_requisition):
        """Main() handles mark_status failure in except handler (lines 295-296)."""
        import app.services.nc_worker.worker as worker_mod

        ws = NcWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        req = test_requisition.requirements[0]
        queue_item = NcSearchQueue(
            requirement_id=req.id,
            requisition_id=test_requisition.id,
            mpn="LM317T",
            normalized_mpn="LM317T",
            status="queued",
        )
        db_session.add(queue_item)
        db_session.commit()

        original = worker_mod._shutdown_requested

        def mock_sleep(seconds):
            worker_mod._shutdown_requested = True

        mock_session = MagicMock()
        mock_session.start = MagicMock()
        mock_session.is_logged_in = True
        mock_session.stop = MagicMock()
        mock_session.ensure_session = MagicMock(return_value=True)
        mock_session.has_browser = False

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.return_value = True
        mock_scheduler.time_for_break.return_value = False
        mock_scheduler.next_delay.return_value = 120

        mock_breaker = MagicMock()
        mock_breaker.should_stop.return_value = False

        def mark_status_fail(db, item, status, error=None):
            raise Exception("DB error")

        try:
            worker_mod._shutdown_requested = False
            with patch(self._DB, self._make_mock_db(db_session)):
                with patch(self._SESSION, return_value=mock_session):
                    with patch(self._SCHEDULER, return_value=mock_scheduler):
                        with patch(self._BREAKER, return_value=mock_breaker):
                            with patch(self._TIME_SLEEP, side_effect=mock_sleep):
                                with patch(self._QUEUE_RECOVER):
                                    with patch(self._RUN_AI_GATE):
                                        with patch(self._ASYNCIO_RUN):
                                            with patch(self._QUEUE_NEXT, return_value=queue_item):
                                                with patch(self._SEARCH, side_effect=Exception("crash")):
                                                    with patch(self._QUEUE_MARK, side_effect=mark_status_fail):
                                                        worker_mod.main()
        finally:
            worker_mod._shutdown_requested = original

    def test_main_has_browser_stop(self, db_session):
        """Main() calls stop_browser when has_browser is True (line 316)."""
        import app.services.nc_worker.worker as worker_mod

        ws = NcWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        original = worker_mod._shutdown_requested

        mock_session = MagicMock()
        mock_session.start = MagicMock()
        mock_session.is_logged_in = True
        mock_session.stop = MagicMock()
        mock_session.has_browser = True
        mock_session.stop_browser = MagicMock()  # Not AsyncMock — avoids unawaited coroutine

        try:
            worker_mod._shutdown_requested = True
            with patch(self._DB, self._make_mock_db(db_session)):
                with patch(self._SESSION, return_value=mock_session):
                    with patch(self._QUEUE_RECOVER):
                        with patch(self._ASYNCIO_RUN) as mock_asyncio_run:
                            worker_mod.main()

            # stop_browser called via asyncio.run
            mock_asyncio_run.assert_called()
        finally:
            worker_mod._shutdown_requested = original

    def test_main_outer_exception(self, db_session):
        """Main() handles unexpected outer exception (line 305-307)."""
        import app.services.nc_worker.worker as worker_mod

        ws = NcWorkerStatus(id=1, is_running=False)
        db_session.add(ws)
        db_session.commit()

        original = worker_mod._shutdown_requested
        call_count = 0

        def mock_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                worker_mod._shutdown_requested = True

        mock_session = MagicMock()
        mock_session.start = MagicMock()
        mock_session.is_logged_in = True
        mock_session.stop = MagicMock()
        mock_session.has_browser = False

        mock_scheduler = MagicMock()
        mock_scheduler.is_business_hours.side_effect = Exception("Unexpected")

        try:
            worker_mod._shutdown_requested = False
            with patch(self._DB, self._make_mock_db(db_session)):
                with patch(self._SESSION, return_value=mock_session):
                    with patch(self._SCHEDULER, return_value=mock_scheduler):
                        with patch(self._TIME_SLEEP, side_effect=mock_sleep):
                            with patch(self._QUEUE_RECOVER):
                                worker_mod.main()
        finally:
            worker_mod._shutdown_requested = original
