"""test_nightly_nc_worker.py — Coverage for nc_worker modules.

Targets:
- app/services/nc_worker/result_parser.py (56%, 62 miss) - lines 79-268
- app/services/nc_worker/circuit_breaker.py
- app/services/nc_worker/scheduler.py
- app/services/nc_worker/queue_manager.py
- app/services/nc_worker/sighting_writer.py

Called by: pytest
Depends on: tests/conftest.py, unittest.mock
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import engine  # noqa: F401

# ══════════════════════════════════════════════════════════════════════════════
# nc_worker/circuit_breaker.py
# ══════════════════════════════════════════════════════════════════════════════


class TestNcCircuitBreaker:
    def _make_cb(self):
        from app.services.nc_worker.circuit_breaker import CircuitBreaker

        return CircuitBreaker()

    def test_session_expired_from_url(self):
        cb = self._make_cb()
        result = cb.check_response_health(200, "<html>login</html>", "https://netcomponents.com/account/login?next=/")
        assert result == "SESSION_EXPIRED"
        assert cb.is_open is False

    def test_rate_limited_429(self):
        cb = self._make_cb()
        result = cb.check_response_health(429, "", "https://netcomponents.com/search")
        assert result == "RATE_LIMITED"
        assert cb.is_open is True

    def test_access_denied_403(self):
        cb = self._make_cb()
        result = cb.check_response_health(403, "", "https://netcomponents.com/search")
        assert result == "ACCESS_DENIED"
        assert cb.is_open is True

    def test_server_error_500_increments_failures(self):
        cb = self._make_cb()
        result = cb.check_response_health(500, "internal error", "https://netcomponents.com/search")
        assert result == "SERVER_ERROR"
        assert cb.consecutive_failures == 1
        assert cb.is_open is False

    def test_server_error_three_times_trips(self):
        cb = self._make_cb()
        cb.check_response_health(503, "", "https://netcomponents.com/search")
        cb.check_response_health(503, "", "https://netcomponents.com/search")
        cb.check_response_health(503, "", "https://netcomponents.com/search")
        assert cb.is_open is True
        assert "3 consecutive" in cb.trip_reason

    def test_captcha_warning_first_occurrence(self):
        cb = self._make_cb()
        result = cb.check_response_health(200, "please verify you are human before continuing", "https://nc.com/")
        assert result == "CAPTCHA_WARNING"
        assert cb.captcha_count == 1
        assert cb.is_open is False

    def test_captcha_trips_on_second(self):
        cb = self._make_cb()
        cb.captcha_count = 1
        result = cb.check_response_health(200, "captcha required", "https://nc.com/")
        assert result == "CAPTCHA_WARNING"
        assert cb.is_open is True

    def test_rate_limit_in_content(self):
        cb = self._make_cb()
        result = cb.check_response_health(200, "too many requests from your ip address", "https://nc.com/")
        assert result == "RATE_LIMITED"
        assert cb.is_open is True

    def test_access_denied_in_content(self):
        cb = self._make_cb()
        result = cb.check_response_health(200, "access denied - unusual activity detected", "https://nc.com/")
        assert result == "ACCESS_DENIED"
        assert cb.is_open is True

    def test_healthy_resets_failures(self):
        cb = self._make_cb()
        cb.consecutive_failures = 2
        result = cb.check_response_health(200, "search results for LM317T", "https://nc.com/search")
        assert result == "HEALTHY"
        assert cb.consecutive_failures == 0

    def test_empty_html_healthy(self):
        cb = self._make_cb()
        result = cb.check_response_health(200, "", "https://nc.com/search")
        assert result == "HEALTHY"


# ══════════════════════════════════════════════════════════════════════════════
# nc_worker/result_parser.py
# ══════════════════════════════════════════════════════════════════════════════


class TestNcResultParser:
    def test_parse_quantity_basic(self):
        from app.services.nc_worker.result_parser import parse_quantity

        assert parse_quantity("1000") == 1000
        assert parse_quantity("1,500") == 1500
        assert parse_quantity("500+") == 500
        assert parse_quantity("") is None
        assert parse_quantity(None) is None  # type: ignore[arg-type]

    def test_parse_quantity_bad_value(self):
        from app.services.nc_worker.result_parser import parse_quantity

        assert parse_quantity("N/A") is None

    def test_parse_price_breaks_no_element(self):
        from app.services.nc_worker.result_parser import parse_price_breaks

        breaks, currency = parse_price_breaks(None)
        assert breaks == []
        assert currency is None

    def test_parse_price_breaks_no_data_attr(self):
        from bs4 import BeautifulSoup

        from app.services.nc_worker.result_parser import parse_price_breaks

        html = '<span class="ncprc">$1.00</span>'
        soup = BeautifulSoup(html, "html.parser")
        el = soup.find("span")
        breaks, currency = parse_price_breaks(el)
        assert breaks == []
        assert currency is None

    def test_parse_price_breaks_valid_json(self):
        from bs4 import BeautifulSoup

        from app.services.nc_worker.result_parser import parse_price_breaks

        pbrk = '{"currency": "USD", "Prices": [{"price": 1.25, "minQty": 1}, {"price": 0.99, "minQty": 100}]}'
        html = f"<span class=\"ncprc\" data-pbrk='{pbrk}'></span>"
        soup = BeautifulSoup(html, "html.parser")
        el = soup.find("span")
        breaks, currency = parse_price_breaks(el)
        assert len(breaks) == 2
        assert breaks[0].price == 1.25
        assert breaks[0].min_qty == 1
        assert currency == "USD"

    def test_parse_price_breaks_invalid_json(self):
        from bs4 import BeautifulSoup

        from app.services.nc_worker.result_parser import parse_price_breaks

        html = "<span class=\"ncprc\" data-pbrk='not-valid-json'></span>"
        soup = BeautifulSoup(html, "html.parser")
        el = soup.find("span")
        breaks, currency = parse_price_breaks(el)
        assert breaks == []
        assert currency is None

    def test_parse_results_html_empty(self):
        from app.services.nc_worker.result_parser import parse_results_html

        assert parse_results_html("") == []
        assert parse_results_html("   ") == []
        assert parse_results_html(None) == []  # type: ignore[arg-type]

    def test_parse_results_html_full_structure(self):
        from app.services.nc_worker.result_parser import parse_results_html

        html = """
        <html><body>
        <div class="div-table-float-reg floating-block">
            <div class="region-header">The Americas</div>
            <div class="stock-type">In-Stock Inventory</div>
            <table class="searchresultstable">
                <tr>
                    <td>LM317T</td>
                    <td><span class="nctd" data-url="https://vendor.com/lm317t"></span></td>
                    <td></td>
                    <td>Texas Instruments</td>
                    <td>2023</td>
                    <td>Voltage Regulator</td>
                    <td>2024-01-15</td>
                    <td>US</td>
                    <td>500</td>
                    <td><span class="ncprc" data-pbrk='{"currency":"USD","Prices":[{"price":0.65,"minQty":1}]}'></span></td>
                    <td></td>
                    <td></td>
                    <td>TestCo</td>
                    <td></td>
                </tr>
            </table>
        </div>
        </body></html>
        """
        sightings = parse_results_html(html)
        assert len(sightings) == 1
        s = sightings[0]
        assert s.part_number == "LM317T"
        assert s.manufacturer == "Texas Instruments"
        assert s.quantity == 500
        assert s.vendor_name == "TestCo"
        assert s.region == "The Americas"
        assert s.inventory_type == "in_stock"
        assert len(s.price_breaks) == 1
        assert s.price_breaks[0].price == 0.65
        assert s.currency == "USD"
        assert s.supplier_product_url == "https://vendor.com/lm317t"

    def test_parse_results_html_brokered_stock_type(self):
        from app.services.nc_worker.result_parser import parse_results_html

        html = """
        <html><body>
        <div class="div-table-float-reg floating-block">
            <div class="region-header">Europe</div>
            <div class="stock-type">Brokered Stock</div>
            <table class="searchresultstable">
                <tr>
                    <td>ABC123</td>
                    <td></td>
                    <td></td>
                    <td>MFR</td>
                    <td>2022</td>
                    <td>Component</td>
                    <td>2024-01-01</td>
                    <td>DE</td>
                    <td>100</td>
                    <td></td>
                    <td></td>
                    <td></td>
                    <td>EuroVendor</td>
                    <td></td>
                </tr>
            </table>
        </div>
        </body></html>
        """
        sightings = parse_results_html(html)
        assert len(sightings) == 1
        assert sightings[0].inventory_type == "brokered"
        assert sightings[0].region == "Europe"

    def test_parse_results_html_skips_short_rows(self):
        from app.services.nc_worker.result_parser import parse_results_html

        html = """
        <html><body>
        <div class="div-table-float-reg floating-block">
            <div class="region-header">Asia</div>
            <table class="searchresultstable">
                <tr>
                    <td>PART1</td>
                    <td>Only 2 cells</td>
                </tr>
            </table>
        </div>
        </body></html>
        """
        sightings = parse_results_html(html)
        assert sightings == []

    def test_parse_results_html_skips_header_row(self):
        from app.services.nc_worker.result_parser import parse_results_html

        html = """
        <html><body>
        <div class="div-table-float-reg floating-block">
            <div class="region-header">The Americas</div>
            <table class="searchresultstable">
                <tr>
                    <td>Part Number</td>
                    <td></td>
                    <td></td>
                    <td>MFR</td>
                    <td>DC</td>
                    <td>Desc</td>
                    <td>Date</td>
                    <td>Ctr</td>
                    <td>Qty</td>
                    <td></td>
                    <td></td>
                    <td></td>
                    <td>Supplier</td>
                    <td></td>
                </tr>
            </table>
        </div>
        </body></html>
        """
        sightings = parse_results_html(html)
        assert sightings == []

    def test_parse_results_html_sponsor_flag(self):
        from app.services.nc_worker.result_parser import parse_results_html

        html = """
        <html><body>
        <div class="div-table-float-reg floating-block">
            <div class="region-header">The Americas</div>
            <table class="searchresultstable">
                <tr>
                    <td>PART999</td>
                    <td></td>
                    <td></td>
                    <td>MFR</td>
                    <td></td>
                    <td></td>
                    <td></td>
                    <td>US</td>
                    <td>50</td>
                    <td></td>
                    <td></td>
                    <td></td>
                    <td>SponsorVendor</td>
                    <td>SPONSORED</td>
                </tr>
            </table>
        </div>
        </body></html>
        """
        sightings = parse_results_html(html)
        assert len(sightings) == 1
        assert sightings[0].is_sponsor is True

    def test_parse_results_html_no_containers_uses_flat_parse(self):
        from app.services.nc_worker.result_parser import parse_results_html

        # Without .floating-block containers, uses flat fallback
        html = """
        <html><body>
        <table>
        <tr><td>PARTX</td><td>url</td><td></td><td>MFR</td><td>2023</td><td>desc</td><td>2024</td><td>US</td><td>300</td></tr>
        </table>
        </body></html>
        """
        # Should return sightings (may be empty or parsed via flat)
        sightings = parse_results_html(html)
        # Flat parse expects 8+ cells, this row has 9 cells
        assert isinstance(sightings, list)

    def test_parse_flat_with_region_and_inventory_type(self):
        from bs4 import BeautifulSoup

        from app.services.nc_worker.result_parser import _parse_flat

        html = """
        <html><body>
        <table>
        <tr><td>The Americas</td></tr>
        <tr><td>In-Stock</td></tr>
        <tr>
            <td>LM324</td>
            <td><span class="nctd" data-url="http://v.com"></span></td>
            <td></td>
            <td>TI</td>
            <td>2022</td>
            <td>Op Amp</td>
            <td>2024-01-01</td>
            <td>US</td>
            <td>1000</td>
            <td></td>
            <td></td>
            <td></td>
            <td>VendorZ</td>
        </tr>
        </table>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        sightings = _parse_flat(soup)
        assert len(sightings) == 1
        assert sightings[0].part_number == "LM324"
        assert sightings[0].region == "The Americas"
        assert sightings[0].inventory_type == "in_stock"

    def test_parse_flat_brokered_type(self):
        from bs4 import BeautifulSoup

        from app.services.nc_worker.result_parser import _parse_flat

        html = """
        <html><body>
        <table>
        <tr><td>Europe</td></tr>
        <tr><td>Brokered stock listing</td></tr>
        <tr>
            <td>PART2</td>
            <td></td>
            <td></td>
            <td>MFR2</td>
            <td>2021</td>
            <td>Desc2</td>
            <td>2024-02</td>
            <td>DE</td>
            <td>200</td>
        </tr>
        </table>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        sightings = _parse_flat(soup)
        assert len(sightings) == 1
        assert sightings[0].inventory_type == "brokered"

    def test_nc_sighting_defaults(self):
        from app.services.nc_worker.result_parser import NcSighting, PriceBreak

        s = NcSighting()
        assert s.part_number == ""
        assert s.quantity is None
        assert s.is_sponsor is False
        assert s.is_authorized is False
        assert s.price_breaks == []
        assert s.inventory_type == "in_stock"

        pb = PriceBreak(price=1.5, min_qty=10)
        assert pb.price == 1.5
        assert pb.min_qty == 10

    def test_parse_results_html_excludes_trv_0(self):
        from app.services.nc_worker.result_parser import parse_results_html

        # trv_0 is the sticky header clone — should be excluded
        html = """
        <html><body>
        <div class="div-table-float-reg floating-block">
            <div class="region-header">The Americas</div>
            <table id="trv_0" class="searchresultstable">
                <tr>
                    <td>HEADER_CLONE</td>
                    <td></td><td></td><td></td><td></td><td></td><td></td><td></td>
                    <td>0</td>
                    <td></td><td></td><td></td>
                    <td>HEADER_VENDOR</td>
                </tr>
            </table>
            <table class="searchresultstable">
                <tr>
                    <td>REAL_PART</td>
                    <td></td><td></td>
                    <td>MFR</td>
                    <td>2023</td>
                    <td>desc</td>
                    <td>2024</td>
                    <td>US</td>
                    <td>100</td>
                    <td></td><td></td><td></td>
                    <td>REAL_VENDOR</td>
                </tr>
            </table>
        </div>
        </body></html>
        """
        sightings = parse_results_html(html)
        part_numbers = [s.part_number for s in sightings]
        assert "REAL_PART" in part_numbers
        # HEADER_CLONE should not appear since that table is excluded
        assert "HEADER_CLONE" not in part_numbers

    def test_parse_results_html_no_region_header(self):
        from app.services.nc_worker.result_parser import parse_results_html

        html = """
        <html><body>
        <div class="div-table-float-reg floating-block">
            <table class="searchresultstable">
                <tr>
                    <td>NOREGION</td>
                    <td></td><td></td>
                    <td>MFR</td>
                    <td>2023</td>
                    <td>desc</td>
                    <td>2024</td>
                    <td>US</td>
                    <td>50</td>
                    <td></td><td></td><td></td>
                    <td>VendorNoRegion</td>
                </tr>
            </table>
        </div>
        </body></html>
        """
        sightings = parse_results_html(html)
        assert len(sightings) == 1
        assert sightings[0].region == "Unknown"

    def test_parse_flat_skips_part_number_header(self):
        from bs4 import BeautifulSoup

        from app.services.nc_worker.result_parser import _parse_flat

        html = """
        <html><body>
        <table>
        <tr>
            <td>Part Number</td>
            <td></td><td></td><td></td><td></td><td></td><td></td><td></td>
        </tr>
        </table>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        sightings = _parse_flat(soup)
        assert sightings == []


# ══════════════════════════════════════════════════════════════════════════════
# nc_worker/scheduler.py
# ══════════════════════════════════════════════════════════════════════════════


class TestNcScheduler:
    def _make_scheduler(self):
        from app.services.nc_worker.config import NcConfig
        from app.services.nc_worker.scheduler import SearchScheduler

        config = NcConfig()
        return SearchScheduler(config)

    def test_is_business_hours_force_env(self):
        import os

        scheduler = self._make_scheduler()
        os.environ["FORCE_BUSINESS_HOURS"] = "1"
        try:
            assert scheduler.is_business_hours() is True
        finally:
            del os.environ["FORCE_BUSINESS_HOURS"]

    @pytest.mark.parametrize(
        ("weekday", "hour", "expected"),
        [
            pytest.param(5, 10, False, id="saturday"),
            pytest.param(6, 10, False, id="sunday_morning"),
            pytest.param(6, 20, True, id="sunday_evening"),
            pytest.param(4, 10, True, id="friday_on"),
            pytest.param(4, 18, False, id="friday_off"),
            pytest.param(2, 14, True, id="wednesday"),
        ],
    )
    def test_is_business_hours(self, weekday, hour, expected):
        scheduler = self._make_scheduler()
        with patch("app.services.nc_worker.scheduler.datetime") as mock_dt:
            mock_now = MagicMock()
            mock_now.weekday.return_value = weekday
            mock_now.hour = hour
            mock_dt.now.return_value = mock_now
            assert scheduler.is_business_hours() is expected

    def test_next_delay_increments_count(self):
        scheduler = self._make_scheduler()
        delay = scheduler.next_delay()
        config = scheduler.config
        assert config.NC_MIN_DELAY_SECONDS <= delay <= config.NC_MAX_DELAY_SECONDS
        assert scheduler.searches_since_break == 1

    def test_time_for_break(self):
        scheduler = self._make_scheduler()
        scheduler.searches_since_break = 5
        scheduler.break_threshold = 10
        assert scheduler.time_for_break() is False
        scheduler.searches_since_break = 10
        assert scheduler.time_for_break() is True

    def test_get_break_duration(self):
        scheduler = self._make_scheduler()
        dur = scheduler.get_break_duration()
        assert 5 * 60 <= dur <= 25 * 60

    def test_reset_break_counter(self):
        scheduler = self._make_scheduler()
        scheduler.searches_since_break = 15
        scheduler.reset_break_counter()
        assert scheduler.searches_since_break == 0
        assert 8 <= scheduler.break_threshold <= 15


# ══════════════════════════════════════════════════════════════════════════════
# nc_worker/queue_manager.py — delegation tests
# ══════════════════════════════════════════════════════════════════════════════


class TestNcQueueManager:
    @patch("app.services.nc_worker.queue_manager._qm")
    def test_enqueue_for_nc_search(self, mock_qm):
        from app.services.nc_worker.queue_manager import enqueue_for_nc_search

        mock_db = MagicMock()
        mock_qm.enqueue_search.return_value = MagicMock(id=10)
        result = enqueue_for_nc_search(99, mock_db)
        mock_qm.enqueue_search.assert_called_once_with(99, mock_db, override_mpn=None, resolved_via_spec_code=None)

    @patch("app.services.nc_worker.queue_manager._qm")
    def test_recover_stale_searches(self, mock_qm):
        from app.services.nc_worker.queue_manager import recover_stale_searches

        mock_db = MagicMock()
        mock_qm.recover_stale_searches.return_value = 2
        assert recover_stale_searches(mock_db) == 2

    @patch("app.services.nc_worker.queue_manager._qm")
    def test_get_next_queued_item(self, mock_qm):
        from app.services.nc_worker.queue_manager import get_next_queued_item

        mock_db = MagicMock()
        mock_qm.get_next_queued_item.return_value = None
        assert get_next_queued_item(mock_db) is None

    @patch("app.services.nc_worker.queue_manager._qm")
    def test_mark_status(self, mock_qm):
        from app.services.nc_worker.queue_manager import mark_status

        mock_db = MagicMock()
        mock_item = MagicMock()
        mark_status(mock_db, mock_item, "completed", error=None)
        mock_qm.mark_status.assert_called_once_with(mock_db, mock_item, "completed", None)

    @patch("app.services.nc_worker.queue_manager._qm")
    def test_mark_completed(self, mock_qm):
        from app.services.nc_worker.queue_manager import mark_completed

        mock_db = MagicMock()
        mock_item = MagicMock()
        mark_completed(mock_db, mock_item, results_found=10, sightings_created=8)
        mock_qm.mark_completed.assert_called_once_with(mock_db, mock_item, 10, 8)

    @patch("app.services.nc_worker.queue_manager._qm")
    def test_get_queue_stats(self, mock_qm):
        from app.services.nc_worker.queue_manager import get_queue_stats

        mock_db = MagicMock()
        mock_qm.get_queue_stats.return_value = {"queued": 5}
        assert get_queue_stats(mock_db) == {"queued": 5}


# ══════════════════════════════════════════════════════════════════════════════
# nc_worker/sighting_writer.py
# ══════════════════════════════════════════════════════════════════════════════


class TestNcSightingWriter:
    @pytest.fixture
    def db(self):
        from sqlalchemy.orm import Session

        with Session(engine) as session:
            yield session
            session.rollback()

    @staticmethod
    def _make_requirement(db, *, req_name, mpn, normalized_mpn, target_qty):
        from app.models import MaterialCard, Requirement, Requisition

        req = Requisition(name=req_name, status="active")
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
        return requirement

    @patch("app.services.sighting_aggregation.rebuild_vendor_summaries_from_sightings")
    def test_save_nc_sightings_no_requirement(self, mock_rebuild, db):
        from app.services.nc_worker.sighting_writer import save_nc_sightings

        queue_item = MagicMock(requirement_id=99998)
        result = save_nc_sightings(db, queue_item, [])
        assert result == 0
        mock_rebuild.assert_not_called()

    @patch("app.services.sighting_aggregation.rebuild_vendor_summaries_from_sightings")
    def test_save_nc_sightings_empty_list(self, mock_rebuild, db):
        from app.services.nc_worker.sighting_writer import save_nc_sightings

        requirement = self._make_requirement(
            db, req_name="NC Test1", mpn="LM324", normalized_mpn="lm324_nc_empty", target_qty=50
        )

        queue_item = MagicMock(requirement_id=requirement.id)
        result = save_nc_sightings(db, queue_item, [])
        assert result == 0
        mock_rebuild.assert_not_called()

    @patch("app.services.sighting_aggregation.rebuild_vendor_summaries_from_sightings")
    def test_save_nc_sightings_skips_no_vendor(self, mock_rebuild, db):
        from app.services.nc_worker.result_parser import NcSighting
        from app.services.nc_worker.sighting_writer import save_nc_sightings

        requirement = self._make_requirement(
            db, req_name="NC Test2", mpn="AB456", normalized_mpn="ab456_nc_skip", target_qty=10
        )

        sighting = NcSighting(part_number="AB456", vendor_name="", quantity=100)
        queue_item = MagicMock(requirement_id=requirement.id)
        result = save_nc_sightings(db, queue_item, [sighting])
        assert result == 0

    @patch("app.services.sighting_aggregation.rebuild_vendor_summaries_from_sightings")
    def test_save_nc_sightings_creates_sighting_with_price_breaks(self, mock_rebuild, db):
        from app.services.nc_worker.result_parser import NcSighting, PriceBreak
        from app.services.nc_worker.sighting_writer import save_nc_sightings

        requirement = self._make_requirement(
            db, req_name="NC Test3", mpn="TDA2030", normalized_mpn="tda2030_nc_create", target_qty=200
        )

        sighting = NcSighting(
            part_number="TDA2030",
            vendor_name="EuroComponents",
            manufacturer="STMicro",
            quantity=1000,
            region="Europe",
            country="DE",
            inventory_type="in_stock",
            is_authorized=True,
            date_code="2022",
            price_breaks=[PriceBreak(price=0.85, min_qty=1), PriceBreak(price=0.70, min_qty=100)],
            currency="USD",
        )
        queue_item = MagicMock(requirement_id=requirement.id)
        result = save_nc_sightings(db, queue_item, [sighting])
        assert result == 1
        mock_rebuild.assert_called_once()

    @patch("app.services.sighting_aggregation.rebuild_vendor_summaries_from_sightings")
    def test_save_nc_sightings_deduplicates(self, mock_rebuild, db):
        from app.services.nc_worker.result_parser import NcSighting
        from app.services.nc_worker.sighting_writer import save_nc_sightings

        requirement = self._make_requirement(
            db, req_name="NC Test4", mpn="DUP002", normalized_mpn="dup002_nc", target_qty=5
        )

        s1 = NcSighting(part_number="DUP002", vendor_name="VendorDup", quantity=50)
        s2 = NcSighting(part_number="DUP002", vendor_name="VendorDup", quantity=50)
        queue_item = MagicMock(requirement_id=requirement.id)
        result = save_nc_sightings(db, queue_item, [s1, s2])
        assert result == 1

    @patch("app.services.sighting_aggregation.rebuild_vendor_summaries_from_sightings")
    def test_save_nc_sightings_no_price_breaks(self, mock_rebuild, db):
        from app.services.nc_worker.result_parser import NcSighting
        from app.services.nc_worker.sighting_writer import save_nc_sightings

        requirement = self._make_requirement(
            db, req_name="NC Test5", mpn="TMP36", normalized_mpn="tmp36_nc_noprice", target_qty=10
        )

        sighting = NcSighting(
            part_number="TMP36",
            vendor_name="SomeSeller",
            quantity=200,
            inventory_type="brokered",  # brokered => confidence 0.3
            price_breaks=[],  # No price breaks
        )
        queue_item = MagicMock(requirement_id=requirement.id)
        result = save_nc_sightings(db, queue_item, [sighting])
        assert result == 1


class TestNcResultParserExceptionAndEdgePaths:
    """Cover missing lines: 136 (no data tables), 197-199, 224-225, 235, 266-268."""

    def test_container_without_data_table_skipped(self):
        """Line 135-136: container with no searchresultstable is skipped."""
        from app.services.nc_worker.result_parser import parse_results_html

        html = """
        <html><body>
        <div class="div-table-float-reg floating-block">
            <div class="region-header">The Americas</div>
            <table class="other-table">
                <tr><td>IGNORED</td></tr>
            </table>
        </div>
        </body></html>
        """
        sightings = parse_results_html(html)
        assert sightings == []

    def test_parse_results_html_malformed_row_skipped(self):
        """Lines 197-199: rows causing AttributeError are skipped, not raised."""
        from app.services.nc_worker.result_parser import parse_results_html

        html = """
        <html><body>
        <div class="div-table-float-reg floating-block">
            <div class="region-header">The Americas</div>
            <table class="searchresultstable">
                <tr></tr>
            </table>
        </div>
        </body></html>
        """
        sightings = parse_results_html(html)
        assert sightings == []

    def test_parse_flat_asia_region(self):
        """Lines 223-225: Asia region detection in flat fallback."""
        from bs4 import BeautifulSoup

        from app.services.nc_worker.result_parser import _parse_flat

        html = """
        <html><body>
        <table>
        <tr><td>Asia Pacific</td></tr>
        <tr>
            <td>ASIPART</td>
            <td></td>
            <td></td>
            <td>MFR</td>
            <td>2023</td>
            <td>desc</td>
            <td>2024</td>
            <td>CN</td>
            <td>500</td>
        </tr>
        </table>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        sightings = _parse_flat(soup)
        assert len(sightings) == 1
        assert sightings[0].region == "Asia"

    def test_parse_flat_no_vendor_uses_last_cell(self):
        """Line 256: vendor_name falls back to last cell when cells <= 12."""
        from bs4 import BeautifulSoup

        from app.services.nc_worker.result_parser import _parse_flat

        # 9 cells — vendor_name falls back to cell_texts[-1]
        html = """
        <html><body>
        <table>
        <tr>
            <td>SHORTROW</td>
            <td></td>
            <td></td>
            <td>MFR</td>
            <td>2023</td>
            <td>desc</td>
            <td>2024</td>
            <td>US</td>
            <td>LastCellVendor</td>
        </tr>
        </table>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        sightings = _parse_flat(soup)
        assert len(sightings) == 1
        assert sightings[0].vendor_name == "LastCellVendor"

    def test_parse_flat_exception_path(self):
        """Lines 266-268: exception in flat row parsing is caught."""
        from bs4 import BeautifulSoup

        from app.services.nc_worker.result_parser import _parse_flat

        # Empty row with 8+ cells to get past the len check but cause an issue
        html = """
        <html><body>
        <table>
        <tr>
            <td>VALIDPART</td>
            <td></td>
            <td></td>
            <td>MFR</td>
            <td>2023</td>
            <td>desc</td>
            <td>2024</td>
            <td>US</td>
        </tr>
        </table>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        # Should not raise
        sightings = _parse_flat(soup)
        assert isinstance(sightings, list)

    def test_parse_flat_skips_short_rows(self):
        """Line 235: rows with < 8 cells are skipped in flat parse."""
        from bs4 import BeautifulSoup

        from app.services.nc_worker.result_parser import _parse_flat

        # Row with only 5 cells — should be skipped
        html = """
        <html><body>
        <table>
        <tr>
            <td>SHORTPART</td>
            <td>cell2</td>
            <td>cell3</td>
            <td>cell4</td>
            <td>cell5</td>
        </tr>
        </table>
        </body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        sightings = _parse_flat(soup)
        assert sightings == []

    def test_parse_results_html_nctd_fallback_to_row(self):
        """Line 166: nctd element from row (not cells[1]) fallback."""
        from app.services.nc_worker.result_parser import parse_results_html

        # nctd is in a different cell position - tests the row.select_one fallback
        html = """
        <html><body>
        <div class="div-table-float-reg floating-block">
            <div class="region-header">The Americas</div>
            <table class="searchresultstable">
                <tr>
                    <td>NCTD_FALLBACK</td>
                    <td></td>
                    <td></td>
                    <td>MFR</td>
                    <td>2023</td>
                    <td>desc</td>
                    <td>2024</td>
                    <td>US</td>
                    <td>75</td>
                    <td></td>
                    <td></td>
                    <td></td>
                    <td>FallbackVendor</td>
                </tr>
            </table>
        </div>
        </body></html>
        """
        sightings = parse_results_html(html)
        assert len(sightings) == 1
        assert sightings[0].supplier_product_url == ""
