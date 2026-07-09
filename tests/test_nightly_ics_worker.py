"""test_nightly_ics_worker.py — Coverage for ics_worker modules.

Targets:
- app/services/ics_worker/circuit_breaker.py (12%, 28 miss) - lines 26-65
- app/services/ics_worker/queue_manager.py (72%, 5 miss) - lines 35, 40, 45, 50, 55
- app/services/ics_worker/result_parser.py (24%, 63 miss) - lines 46-169
- app/services/ics_worker/sighting_writer.py (23%, 27 miss) - lines 33-105
- app/services/ics_worker/scheduler.py (36%, 27 miss) - lines 30-84

Called by: pytest
Depends on: tests/conftest.py, unittest.mock
"""

import asyncio
import os

os.environ["TESTING"] = "1"

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from tests.conftest import engine


def _run(coro):
    """Run a coroutine to completion on a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ══════════════════════════════════════════════════════════════════════════════
# circuit_breaker.py
# ══════════════════════════════════════════════════════════════════════════════


class TestIcsCircuitBreaker:
    def _make_cb(self):
        from app.services.ics_worker.circuit_breaker import CircuitBreaker

        return CircuitBreaker()

    def _page(self, url, content):
        """A mock page whose evaluate() resolves to the given content."""
        page = MagicMock()
        page.url = url

        async def fake_evaluate(js):
            return content

        page.evaluate = fake_evaluate
        return page

    def test_check_page_health_exception_increments_failures(self):
        cb = self._make_cb()
        page = MagicMock()
        page.url = "https://icsource.com/search"
        page.evaluate = MagicMock(side_effect=RuntimeError("evaluate failed"))

        result = _run(cb.check_page_health(page))

        assert result == "CHECK_FAILED"
        assert cb.consecutive_failures == 1

    def test_check_page_health_three_failures_trips_breaker(self):
        cb = self._make_cb()
        page = MagicMock()
        page.url = "https://icsource.com/search"
        page.evaluate = MagicMock(side_effect=RuntimeError("fail"))

        for _ in range(3):
            _run(cb.check_page_health(page))

        assert cb.is_open is True
        assert "3 consecutive" in cb.trip_reason

    @pytest.mark.parametrize(
        ("url", "content", "expected_result", "expected_attrs"),
        [
            pytest.param(
                "https://evil.com/redirect",
                "page content here",
                "UNEXPECTED_REDIRECT",
                {"is_open": True},
                id="unexpected_redirect_trips",
            ),
            pytest.param(
                "https://icsource.com/login.aspx",
                "please login",
                "SESSION_EXPIRED",
                {"is_open": False},
                id="session_expired_from_url",
            ),
            pytest.param(
                "https://icsource.com/browse",
                "please verify you are human to continue",
                "CAPTCHA_WARNING",
                {"captcha_count": 1, "is_open": False},
                id="captcha_warning",
            ),
            pytest.param(
                "https://icsource.com/browse",
                "too many requests from your ip",
                "RATE_LIMITED",
                {"is_open": True},
                id="rate_limited",
            ),
            pytest.param(
                "https://icsource.com/browse",
                "access denied by server policy",
                "ACCESS_DENIED",
                {"is_open": True},
                id="access_denied",
            ),
            pytest.param(
                "https://icsource.com/account/login",
                "please sign in to continue",
                "SESSION_EXPIRED",
                {},
                id="login_in_path_segment",
            ),
        ],
    )
    def test_check_page_health(self, url, content, expected_result, expected_attrs):
        cb = self._make_cb()
        result = _run(cb.check_page_health(self._page(url, content)))

        assert result == expected_result
        for attr, value in expected_attrs.items():
            assert getattr(cb, attr) == value

    def test_check_page_health_captcha_twice_trips(self):
        cb = self._make_cb()
        cb.captcha_count = 1  # Pre-set so second triggers trip

        page = self._page("https://icsource.com/browse", "verify you are human")
        result = _run(cb.check_page_health(page))

        assert result == "CAPTCHA_WARNING"
        assert cb.is_open is True

    def test_check_page_health_healthy_resets_failures(self):
        cb = self._make_cb()
        cb.consecutive_failures = 2  # Prior failures

        page = self._page("https://icsource.com/browse", "normal component listing results here")
        result = _run(cb.check_page_health(page))

        assert result == "HEALTHY"
        assert cb.consecutive_failures == 0


# ══════════════════════════════════════════════════════════════════════════════
# result_parser.py (ICS)
# ══════════════════════════════════════════════════════════════════════════════


class TestIcsResultParser:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("1000", 1000, id="plain"),
            pytest.param("1,500", 1500, id="comma_separated"),
            pytest.param("500+", 500, id="trailing_plus"),
            pytest.param("", None, id="empty_string"),
            pytest.param(None, None, id="none"),
            pytest.param("N/A", None, id="not_available"),
            pytest.param("--", None, id="dashes"),
        ],
    )
    def test_parse_quantity(self, raw, expected):
        from app.services.ics_worker.result_parser import parse_quantity

        assert parse_quantity(raw) == expected  # type: ignore[arg-type]

    def test_extract_company_info_with_open_profile(self):
        from bs4 import BeautifulSoup

        from app.services.ics_worker.result_parser import _extract_company_info

        html = """
        <div class="flex">
            <a href="javascript:OpenProfile(42)">Acme Electronics</a>
            <a href="mailto:sales@acme.com?subject=RFQ">sales@acme.com</a>
            <span class="clicktocall">+1-800-555-1234</span>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        block = soup.find("div")
        info = _extract_company_info(block)

        assert info["name"] == "Acme Electronics"
        assert info["company_id"] == "42"
        assert info["email"] == "sales@acme.com"
        assert info["phone"] == "+1-800-555-1234"

    def test_extract_company_info_no_elements(self):
        from bs4 import BeautifulSoup

        from app.services.ics_worker.result_parser import _extract_company_info

        html = "<div><p>Nothing here</p></div>"
        soup = BeautifulSoup(html, "html.parser")
        block = soup.find("div")
        info = _extract_company_info(block)

        assert info["name"] == ""
        assert info["email"] == ""
        assert info["phone"] == ""
        assert info["company_id"] == ""

    def test_parse_results_html_empty(self):
        from app.services.ics_worker.result_parser import parse_results_html

        assert parse_results_html("") == []
        assert parse_results_html("   ") == []
        assert parse_results_html(None) == []  # type: ignore[arg-type]

    def test_parse_results_html_full_structure(self):
        from app.services.ics_worker.result_parser import parse_results_html

        html = """
        <html><body>
        <div class="divDateGroup">2024-01-15</div>
        <div class="flex">
            <a href="javascript:OpenProfile(101)">TestCo Supply</a>
            <a href="mailto:info@testco.com">info@testco.com</a>
            <span class="clicktocall">555-1234</span>
        </div>
        <tr class="browseMatchItem">
            <td>LM317T</td>
            <td>Voltage Regulator</td>
            <td>1,000+</td>
            <td>$0.45</td>
            <td>Texas Instruments</td>
            <td>2023</td>
            <td><img src="check.png"/></td>
        </tr>
        </body></html>
        """
        sightings = parse_results_html(html)
        assert len(sightings) == 1
        s = sightings[0]
        assert s.part_number == "LM317T"
        assert s.manufacturer == "Texas Instruments"
        assert s.quantity == 1000
        assert s.price == "$0.45"
        assert s.description == "Voltage Regulator"
        assert s.date_code == "2023"
        assert s.uploaded_date == "2024-01-15"
        assert s.vendor_name == "TestCo Supply"
        assert s.vendor_email == "info@testco.com"
        assert s.vendor_phone == "555-1234"
        assert s.vendor_company_id == "101"
        assert s.in_stock is True

    def test_parse_results_html_no_stock_checkmark(self):
        from app.services.ics_worker.result_parser import parse_results_html

        html = """
        <html><body>
        <div class="flex">
            <a href="javascript:OpenProfile(5)">Vendor B</a>
        </div>
        <tr class="browseMatchItem">
            <td>ABC123</td>
            <td></td>
            <td>500</td>
            <td>$1.00</td>
            <td>Mfr</td>
            <td>2022</td>
            <td></td>
        </tr>
        </body></html>
        """
        sightings = parse_results_html(html)
        assert len(sightings) == 1
        assert sightings[0].in_stock is False

    def test_parse_results_html_skips_short_rows(self):
        from app.services.ics_worker.result_parser import parse_results_html

        html = """
        <html><body>
        <div class="flex">
            <a href="javascript:OpenProfile(1)">VendorX</a>
        </div>
        <tr class="browseMatchItem">
            <td>PART1</td>
            <td>Desc</td>
        </tr>
        </body></html>
        """
        sightings = parse_results_html(html)
        assert sightings == []

    def test_parse_results_html_unicode_checkmark(self):
        from app.services.ics_worker.result_parser import parse_results_html

        html = """
        <html><body>
        <div class="flex">
            <a href="javascript:OpenProfile(3)">VendorC</a>
        </div>
        <tr class="browseMatchItem">
            <td>PART2</td>
            <td></td>
            <td>100</td>
            <td>$2.00</td>
            <td>MFR</td>
            <td>2023</td>
            <td>✓</td>
        </tr>
        </body></html>
        """
        sightings = parse_results_html(html)
        assert len(sightings) == 1
        assert sightings[0].in_stock is True

    def test_ics_sighting_dataclass_defaults(self):
        from app.services.ics_worker.result_parser import IcsSighting

        s = IcsSighting()
        assert s.part_number == ""
        assert s.quantity is None
        assert s.in_stock is False


# ══════════════════════════════════════════════════════════════════════════════
# scheduler.py (ICS)
# ══════════════════════════════════════════════════════════════════════════════


class TestIcsScheduler:
    def _make_scheduler(self):
        from app.services.ics_worker.config import IcsConfig
        from app.services.ics_worker.scheduler import SearchScheduler

        config = IcsConfig()
        return SearchScheduler(config)

    def test_is_business_hours_force_env(self):
        import os

        scheduler = self._make_scheduler()
        os.environ["FORCE_BUSINESS_HOURS"] = "1"
        try:
            result = scheduler.is_business_hours()
            assert result is True
        finally:
            del os.environ["FORCE_BUSINESS_HOURS"]

    @pytest.mark.parametrize(
        ("weekday", "hour", "expected"),
        [
            pytest.param(5, 12, False, id="saturday"),  # Saturday always off
            pytest.param(6, 15, False, id="sunday_before_6pm"),  # 3 PM, before threshold
            pytest.param(6, 19, True, id="sunday_after_6pm"),  # 7 PM, after threshold
            pytest.param(4, 14, True, id="friday_before_5pm"),  # 2 PM
            pytest.param(4, 17, False, id="friday_after_5pm"),  # 5 PM, off
            pytest.param(0, 10, True, id="monday"),
        ],
    )
    def test_is_business_hours(self, weekday, hour, expected):
        scheduler = self._make_scheduler()

        with patch("app.services.ics_worker.scheduler.datetime") as mock_dt:
            mock_now = MagicMock()
            mock_now.weekday.return_value = weekday
            mock_now.hour = hour
            mock_dt.now.return_value = mock_now
            result = scheduler.is_business_hours()

        assert result is expected

    def test_next_delay_returns_float_in_range(self):
        scheduler = self._make_scheduler()
        delay = scheduler.next_delay()
        config = scheduler.config
        assert config.ICS_MIN_DELAY_SECONDS <= delay <= config.ICS_MAX_DELAY_SECONDS
        assert scheduler.searches_since_break == 1

    def test_time_for_break_below_threshold(self):
        scheduler = self._make_scheduler()
        scheduler.searches_since_break = 3
        scheduler.break_threshold = 10
        assert scheduler.time_for_break() is False

    def test_time_for_break_at_threshold(self):
        scheduler = self._make_scheduler()
        scheduler.searches_since_break = 10
        scheduler.break_threshold = 10
        assert scheduler.time_for_break() is True

    def test_get_break_duration_in_range(self):
        scheduler = self._make_scheduler()
        dur = scheduler.get_break_duration()
        assert 5 * 60 <= dur <= 25 * 60

    def test_reset_break_counter(self):
        scheduler = self._make_scheduler()
        scheduler.searches_since_break = 12
        scheduler.reset_break_counter()
        assert scheduler.searches_since_break == 0
        assert 8 <= scheduler.break_threshold <= 15


# ══════════════════════════════════════════════════════════════════════════════
# queue_manager.py (ICS) — delegation tests
# ══════════════════════════════════════════════════════════════════════════════


class TestIcsQueueManager:
    """Tests the thin wrapper functions that delegate to QueueManager."""

    @patch("app.services.ics_worker.queue_manager._qm")
    def test_enqueue_for_ics_search_delegates(self, mock_qm):
        from app.services.ics_worker.queue_manager import enqueue_for_ics_search

        mock_db = MagicMock()
        mock_qm.enqueue_search.return_value = MagicMock(id=1)
        result = enqueue_for_ics_search(42, mock_db)
        mock_qm.enqueue_search.assert_called_once_with(42, mock_db, override_mpn=None, resolved_via_spec_code=None)
        assert result is not None

    @patch("app.services.ics_worker.queue_manager._qm")
    def test_recover_stale_searches_delegates(self, mock_qm):
        from app.services.ics_worker.queue_manager import recover_stale_searches

        mock_db = MagicMock()
        mock_qm.recover_stale_searches.return_value = 3
        result = recover_stale_searches(mock_db)
        mock_qm.recover_stale_searches.assert_called_once_with(mock_db)
        assert result == 3

    @patch("app.services.ics_worker.queue_manager._qm")
    def test_get_next_queued_item_delegates(self, mock_qm):
        from app.services.ics_worker.queue_manager import get_next_queued_item

        mock_db = MagicMock()
        mock_qm.get_next_queued_item.return_value = None
        result = get_next_queued_item(mock_db)
        mock_qm.get_next_queued_item.assert_called_once_with(mock_db)
        assert result is None

    @patch("app.services.ics_worker.queue_manager._qm")
    def test_mark_status_delegates(self, mock_qm):
        from app.services.ics_worker.queue_manager import mark_status

        mock_db = MagicMock()
        mock_item = MagicMock()
        mark_status(mock_db, mock_item, "searching", error=None)
        mock_qm.mark_status.assert_called_once_with(mock_db, mock_item, "searching", None)

    @patch("app.services.ics_worker.queue_manager._qm")
    def test_mark_completed_delegates(self, mock_qm):
        from app.services.ics_worker.queue_manager import mark_completed

        mock_db = MagicMock()
        mock_item = MagicMock()
        mark_completed(mock_db, mock_item, results_found=5, sightings_created=3)
        mock_qm.mark_completed.assert_called_once_with(mock_db, mock_item, 5, 3)

    @patch("app.services.ics_worker.queue_manager._qm")
    def test_get_queue_stats_delegates(self, mock_qm):
        from app.services.ics_worker.queue_manager import get_queue_stats

        mock_db = MagicMock()
        mock_qm.get_queue_stats.return_value = {"pending": 0}
        result = get_queue_stats(mock_db)
        mock_qm.get_queue_stats.assert_called_once_with(mock_db)
        assert result == {"pending": 0}


# ══════════════════════════════════════════════════════════════════════════════
# sighting_writer.py (ICS)
# ══════════════════════════════════════════════════════════════════════════════


class TestIcsSightingWriter:
    @pytest.fixture
    def db(self):

        with Session(engine) as session:
            yield session
            session.rollback()

    def _make_queue_item(self, db, mpn, normalized_mpn, target_qty):
        """Create a Requisition/MaterialCard/Requirement chain and return a queue_item
        MagicMock pointing at the new requirement."""
        from app.models import MaterialCard, Requirement, Requisition

        req = Requisition(name=f"Test Req {normalized_mpn}", status="open")
        db.add(req)
        db.flush()

        mc = MaterialCard(display_mpn=mpn, normalized_mpn=normalized_mpn)
        db.add(mc)
        db.flush()

        requirement = Requirement(
            requisition_id=req.id,
            primary_mpn=mpn,
            target_qty=target_qty,
            material_card_id=mc.id,
        )
        db.add(requirement)
        db.flush()

        return MagicMock(requirement_id=requirement.id)

    @patch("app.services.sighting_aggregation.rebuild_vendor_summaries_from_sightings")
    def test_save_ics_sightings_returns_zero_if_no_requirement(self, mock_rebuild, db):
        from app.services.ics_worker.sighting_writer import save_ics_sightings

        queue_item = MagicMock(requirement_id=99999)
        result = save_ics_sightings(db, queue_item, [])
        assert result == 0
        mock_rebuild.assert_not_called()

    @patch("app.services.sighting_aggregation.rebuild_vendor_summaries_from_sightings")
    def test_save_ics_sightings_empty_list(self, mock_rebuild, db):
        from app.services.ics_worker.sighting_writer import save_ics_sightings

        queue_item = self._make_queue_item(db, "LM317T", "lm317t_empty", 100)
        result = save_ics_sightings(db, queue_item, [])
        assert result == 0
        mock_rebuild.assert_not_called()

    @patch("app.services.sighting_aggregation.rebuild_vendor_summaries_from_sightings")
    def test_save_ics_sightings_skips_no_vendor_name(self, mock_rebuild, db):
        from app.services.ics_worker.result_parser import IcsSighting
        from app.services.ics_worker.sighting_writer import save_ics_sightings

        queue_item = self._make_queue_item(db, "ABC123", "abc123_skip", 50)

        # Sighting with no vendor_name should be skipped
        sighting = IcsSighting(
            part_number="ABC123",
            vendor_name="",
            quantity=100,
        )
        result = save_ics_sightings(db, queue_item, [sighting])
        assert result == 0

    @patch("app.services.sighting_aggregation.rebuild_vendor_summaries_from_sightings")
    def test_save_ics_sightings_creates_sighting(self, mock_rebuild, db):
        from app.services.ics_worker.result_parser import IcsSighting
        from app.services.ics_worker.sighting_writer import save_ics_sightings

        queue_item = self._make_queue_item(db, "XYZ789", "xyz789_create", 200)

        sighting = IcsSighting(
            part_number="XYZ789",
            vendor_name="Test Vendor Co",
            vendor_email="test@vendor.com",
            vendor_phone="555-9999",
            vendor_company_id="77",
            quantity=500,
            price="$2.50",
            manufacturer="MFR",
            date_code="2023",
            in_stock=True,
            description="Component",
            uploaded_date="2024-01-01",
        )
        result = save_ics_sightings(db, queue_item, [sighting])
        assert result == 1
        mock_rebuild.assert_called_once()

    @patch("app.services.sighting_aggregation.rebuild_vendor_summaries_from_sightings")
    def test_save_ics_sightings_deduplicates(self, mock_rebuild, db):
        from app.services.ics_worker.result_parser import IcsSighting
        from app.services.ics_worker.sighting_writer import save_ics_sightings

        queue_item = self._make_queue_item(db, "DUP001", "dup001_dedup", 10)

        sighting1 = IcsSighting(
            part_number="DUP001",
            vendor_name="DupVendor",
            quantity=100,
            in_stock=True,
        )
        sighting2 = IcsSighting(
            part_number="DUP001",
            vendor_name="DupVendor",
            quantity=100,
            in_stock=True,
        )
        result = save_ics_sightings(db, queue_item, [sighting1, sighting2])
        # Only 1 unique sighting should be created
        assert result == 1


class TestIcsResultParserExceptionPaths:
    """Cover exception-handling branches (lines 164-166)."""

    def test_parse_results_html_malformed_row_skipped(self):
        """Row with getattr raising AttributeError is skipped gracefully."""
        from app.services.ics_worker.result_parser import parse_results_html

        # A browseMatchItem row that has no proper cells — causes IndexError on access
        html = """
        <html><body>
        <div class="flex">
            <a href="javascript:OpenProfile(9)">GoodVendor</a>
        </div>
        <tr class="browseMatchItem"></tr>
        </body></html>
        """
        # Should not raise; malformed row is skipped
        sightings = parse_results_html(html)
        assert sightings == []
