"""Tests for ICS and NC worker result parsers and circuit breakers.

Covers:
- app/services/ics_worker/result_parser.py
- app/services/nc_worker/result_parser.py
- app/services/ics_worker/circuit_breaker.py
- app/services/search_worker_base/circuit_breaker.py
- app/services/ics_worker/human_behavior.py
"""

import os

os.environ["TESTING"] = "1"

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── ICS result_parser ──────────────────────────────────────────────────


class TestIcsParseQuantity:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("", None),
            (None, None),
            ("100", 100),
            ("1,000", 1000),
            ("10,000,000", 10000000),
            ("500+", 500),
            ("N/A", None),
            ("POA", None),
        ],
    )
    def test_parse_quantity(self, raw, expected):
        from app.services.ics_worker.result_parser import parse_quantity

        assert parse_quantity(raw) == expected


class TestIcsExtractCompanyInfo:
    def test_extracts_name_email_phone(self):
        from bs4 import BeautifulSoup

        from app.services.ics_worker.result_parser import _extract_company_info

        html = """
        <div class="flex">
            <a href="javascript:OpenProfile(123)">Acme Components</a>
            <a href="mailto:sales@acme.com?subject=test">sales@acme.com</a>
            <span class="clicktocall">+1-555-1234</span>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        block = soup.find("div")
        result = _extract_company_info(block)
        assert result["name"] == "Acme Components"
        assert result["email"] == "sales@acme.com"
        assert result["phone"] == "+1-555-1234"
        assert result["company_id"] == "123"

    def test_empty_block_returns_empty_dict(self):
        from bs4 import BeautifulSoup

        from app.services.ics_worker.result_parser import _extract_company_info

        html = "<div></div>"
        soup = BeautifulSoup(html, "html.parser")
        result = _extract_company_info(soup.find("div"))
        assert result == {"name": "", "email": "", "phone": "", "company_id": ""}

    def test_email_strips_subject_params(self):
        from bs4 import BeautifulSoup

        from app.services.ics_worker.result_parser import _extract_company_info

        html = """<div><a href="mailto:test@example.com?subject=RFQ">email</a></div>"""
        soup = BeautifulSoup(html, "html.parser")
        result = _extract_company_info(soup.find("div"))
        assert result["email"] == "test@example.com"


class TestIcsParseResultsHtml:
    def test_empty_html_returns_empty(self):
        from app.services.ics_worker.result_parser import parse_results_html

        assert parse_results_html("") == []
        assert parse_results_html("   ") == []
        assert parse_results_html(None) == []

    def test_parses_basic_result_row(self):
        from app.services.ics_worker.result_parser import parse_results_html

        html = """
        <html><body>
        <div class="divDateGroup">2026-01-15</div>
        <div class="flex">
            <a href="javascript:OpenProfile(42)">TechSupply Inc</a>
            <a href="mailto:rfq@techsupply.com">rfq@techsupply.com</a>
        </div>
        <tr class="browseMatchItem">
            <td>LM317T</td>
            <td>Voltage Regulator</td>
            <td>500</td>
            <td>$0.45</td>
            <td>Texas Instruments</td>
            <td>2022</td>
            <td><img src="check.png"/></td>
        </tr>
        </body></html>
        """
        results = parse_results_html(html)
        assert len(results) == 1
        r = results[0]
        assert r.part_number == "LM317T"
        assert r.manufacturer == "Texas Instruments"
        assert r.quantity == 500
        assert r.price == "$0.45"
        assert r.in_stock is True
        assert r.uploaded_date == "2026-01-15"
        assert r.vendor_name == "TechSupply Inc"
        assert r.vendor_email == "rfq@techsupply.com"
        assert r.vendor_company_id == "42"

    def test_parses_multiple_rows(self):
        from app.services.ics_worker.result_parser import parse_results_html

        html = """
        <html><body>
        <tr class="browseMatchItem">
            <td>ABC123</td><td>Desc</td><td>100</td><td>$1.00</td>
            <td>Mfr1</td><td>2021</td>
        </tr>
        <tr class="browseMatchItem">
            <td>XYZ789</td><td>Desc</td><td>200</td><td>$2.00</td>
            <td>Mfr2</td><td>2022</td>
        </tr>
        </body></html>
        """
        results = parse_results_html(html)
        assert len(results) == 2
        assert results[0].part_number == "ABC123"
        assert results[1].part_number == "XYZ789"

    def test_skips_rows_with_too_few_cells(self):
        from app.services.ics_worker.result_parser import parse_results_html

        html = """
        <html><body>
        <tr class="browseMatchItem">
            <td>PART1</td><td>Desc</td>
        </tr>
        </body></html>
        """
        results = parse_results_html(html)
        assert len(results) == 0

    def test_stock_checkmark_text(self):
        from app.services.ics_worker.result_parser import parse_results_html

        html = """
        <html><body>
        <tr class="browseMatchItem">
            <td>LM317T</td><td>Desc</td><td>100</td><td>$0.50</td>
            <td>TI</td><td>2022</td><td>✓</td>
        </tr>
        </body></html>
        """
        results = parse_results_html(html)
        assert len(results) == 1
        assert results[0].in_stock is True

    def test_no_stock_cell(self):
        from app.services.ics_worker.result_parser import parse_results_html

        html = """
        <html><body>
        <tr class="browseMatchItem">
            <td>LM317T</td><td>Desc</td><td>100</td><td>$0.50</td>
            <td>TI</td>
        </tr>
        </body></html>
        """
        results = parse_results_html(html)
        assert len(results) == 1
        assert results[0].in_stock is False

    def test_quantity_with_commas_and_plus(self):
        from app.services.ics_worker.result_parser import parse_results_html

        html = """
        <html><body>
        <tr class="browseMatchItem">
            <td>PART1</td><td>Desc</td><td>1,500+</td><td>$0.10</td>
            <td>Mfr</td><td>2023</td>
        </tr>
        </body></html>
        """
        results = parse_results_html(html)
        assert results[0].quantity == 1500


# ── NC result_parser ───────────────────────────────────────────────────


class TestNcParseQuantity:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("", None),
            (None, None),
            ("250", 250),
            ("5,000", 5000),
            ("1000+", 1000),
            ("POA", None),
        ],
    )
    def test_parse_quantity(self, raw, expected):
        from app.services.nc_worker.result_parser import parse_quantity

        assert parse_quantity(raw) == expected


class TestNcParsePriceBreaks:
    def test_none_element_returns_empty(self):
        from app.services.nc_worker.result_parser import parse_price_breaks

        breaks, currency = parse_price_breaks(None)
        assert breaks == []
        assert currency is None

    def test_no_data_pbrk_returns_empty(self):
        from bs4 import BeautifulSoup

        from app.services.nc_worker.result_parser import parse_price_breaks

        html = '<span class="ncprc"></span>'
        soup = BeautifulSoup(html, "html.parser")
        el = soup.find("span")
        breaks, currency = parse_price_breaks(el)
        assert breaks == []
        assert currency is None

    def test_valid_price_breaks(self):
        from bs4 import BeautifulSoup

        from app.services.nc_worker.result_parser import parse_price_breaks

        data = json.dumps(
            {
                "currency": "USD",
                "Prices": [
                    {"price": 1.50, "minQty": 1},
                    {"price": 1.25, "minQty": 100},
                ],
            }
        )
        html = f"<span class=\"ncprc\" data-pbrk='{data}'></span>"
        soup = BeautifulSoup(html, "html.parser")
        el = soup.find("span")
        breaks, currency = parse_price_breaks(el)
        assert currency == "USD"
        assert len(breaks) == 2
        assert breaks[0].price == 1.50
        assert breaks[0].min_qty == 1
        assert breaks[1].price == 1.25
        assert breaks[1].min_qty == 100

    def test_invalid_json_returns_empty(self):
        from bs4 import BeautifulSoup

        from app.services.nc_worker.result_parser import parse_price_breaks

        html = '<span class="ncprc" data-pbrk="not-valid-json"></span>'
        soup = BeautifulSoup(html, "html.parser")
        el = soup.find("span")
        breaks, currency = parse_price_breaks(el)
        assert breaks == []
        assert currency is None


class TestNcParseResultsHtml:
    def test_empty_html_returns_empty(self):
        from app.services.nc_worker.result_parser import parse_results_html

        assert parse_results_html("") == []
        assert parse_results_html("   ") == []
        assert parse_results_html(None) == []

    def test_parses_floating_block_structure(self):
        from app.services.nc_worker.result_parser import parse_results_html

        html = """
        <html><body>
        <div class="div-table-float-reg floating-block">
            <div class="region-header">The Americas</div>
            <div class="stock-type">In-Stock Inventory</div>
            <table class="searchresultstable">
                <tr>
                    <td>LM317T</td>
                    <td><a class="nctd" data-url="/product/123">link</a></td>
                    <td class="ncdsl"></td>
                    <td>Texas Instruments</td>
                    <td>2022</td>
                    <td>Voltage Regulator</td>
                    <td>2026-01</td>
                    <td>US</td>
                    <td>1,000</td>
                    <td><span class="ncprc"></span></td>
                    <td></td>
                    <td></td>
                    <td>Arrow Electronics</td>
                    <td></td>
                </tr>
            </table>
        </div>
        </body></html>
        """
        results = parse_results_html(html)
        assert len(results) == 1
        r = results[0]
        assert r.part_number == "LM317T"
        assert r.manufacturer == "Texas Instruments"
        assert r.region == "The Americas"
        assert r.inventory_type == "in_stock"
        assert r.quantity == 1000
        assert r.vendor_name == "Arrow Electronics"
        assert r.supplier_product_url == "/product/123"

    def test_brokered_inventory_type(self):
        from app.services.nc_worker.result_parser import parse_results_html

        html = """
        <html><body>
        <div class="div-table-float-reg floating-block">
            <div class="region-header">Europe</div>
            <div class="stock-type">Brokered Inventory</div>
            <table class="searchresultstable">
                <tr>
                    <td>PART1</td><td></td><td></td><td>Mfr</td><td></td>
                    <td>Desc</td><td>2026-01</td><td>DE</td><td>100</td>
                    <td></td><td></td><td></td><td>VendorX</td><td></td>
                </tr>
            </table>
        </div>
        </body></html>
        """
        results = parse_results_html(html)
        assert len(results) == 1
        assert results[0].inventory_type == "brokered"
        assert results[0].region == "Europe"

    def test_skips_header_rows(self):
        from app.services.nc_worker.result_parser import parse_results_html

        html = """
        <html><body>
        <div class="div-table-float-reg floating-block">
            <div class="region-header">Asia</div>
            <table class="searchresultstable">
                <tr>
                    <td>Part Number</td><td></td><td></td><td></td><td></td>
                    <td></td><td></td><td></td><td></td><td></td><td></td>
                    <td></td><td></td><td></td>
                </tr>
                <tr>
                    <td>STM32F4</td><td></td><td></td><td>ST</td><td></td>
                    <td></td><td></td><td></td><td>500</td><td></td><td></td>
                    <td></td><td>Mouser</td><td></td>
                </tr>
            </table>
        </div>
        </body></html>
        """
        results = parse_results_html(html)
        assert len(results) == 1
        assert results[0].part_number == "STM32F4"

    def test_fallback_flat_parse_when_no_containers(self):
        from app.services.nc_worker.result_parser import parse_results_html

        html = """
        <html><body>
        <table>
            <tr><td>The Americas</td></tr>
            <tr>
                <td>ESP32</td><td></td><td></td><td>Espressif</td><td>2024</td>
                <td>WiFi SoC</td><td>2026-01</td><td>CN</td><td>200</td>
                <td></td><td></td><td></td><td>DigiKey</td>
            </tr>
        </table>
        </body></html>
        """
        results = parse_results_html(html)
        # Flat parse should find at least the data row
        assert any(r.part_number == "ESP32" for r in results)

    def test_sponsor_column(self):
        from app.services.nc_worker.result_parser import parse_results_html

        html = """
        <html><body>
        <div class="div-table-float-reg floating-block">
            <div class="region-header">Americas</div>
            <table class="searchresultstable">
                <tr>
                    <td>NRF52840</td><td></td><td></td><td>Nordic</td><td></td>
                    <td></td><td></td><td></td><td>50</td><td></td><td></td>
                    <td></td><td>SponsVendor</td><td>S</td>
                </tr>
            </table>
        </div>
        </body></html>
        """
        results = parse_results_html(html)
        assert len(results) == 1
        assert results[0].is_sponsor is True

    def test_price_breaks_parsed(self):
        from app.services.nc_worker.result_parser import parse_results_html

        price_data = json.dumps({"currency": "USD", "Prices": [{"price": 2.50, "minQty": 1}]})
        html = f"""
        <html><body>
        <div class="div-table-float-reg floating-block">
            <div class="region-header">Americas</div>
            <table class="searchresultstable">
                <tr>
                    <td>MCU1</td><td></td><td></td><td>Mfr</td><td></td>
                    <td></td><td></td><td></td><td>100</td>
                    <td><span class="ncprc" data-pbrk='{price_data}'></span></td>
                    <td></td><td></td><td>AuthVendor</td><td></td>
                </tr>
            </table>
        </div>
        </body></html>
        """
        results = parse_results_html(html)
        assert len(results) == 1
        assert results[0].is_authorized is True
        assert len(results[0].price_breaks) == 1
        assert results[0].currency == "USD"


# ── ICS CircuitBreaker ─────────────────────────────────────────────────


def _make_page(url: str, content: str) -> MagicMock:
    page = MagicMock()
    page.url = url
    page.evaluate = AsyncMock(return_value=content)
    return page


class TestIcsCircuitBreaker:
    def test_initial_state(self):
        from app.services.ics_worker.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker()
        assert cb.is_open is False
        assert cb.consecutive_failures == 0
        assert cb.captcha_count == 0

    @pytest.mark.parametrize(
        "url, content, expected_status, expected_open",
        [
            (
                "https://www.icsource.com/search",
                "search results listing 100 parts",
                "HEALTHY",
                False,
            ),
            (
                "https://www.icsource.com/login.aspx",
                "please login",
                "SESSION_EXPIRED",
                False,
            ),
            (
                "https://www.google.com/search",
                "some content",
                "UNEXPECTED_REDIRECT",
                True,
            ),
            (
                "https://www.icsource.com/search",
                "too many requests please wait",
                "RATE_LIMITED",
                True,
            ),
            (
                "https://www.icsource.com/search",
                "access denied by firewall",
                "ACCESS_DENIED",
                True,
            ),
        ],
        ids=[
            "healthy",
            "session_expired",
            "unexpected_redirect",
            "rate_limited",
            "access_denied",
        ],
    )
    async def test_check_page_health_status(self, url, content, expected_status, expected_open):
        from app.services.ics_worker.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker()
        status = await cb.check_page_health(_make_page(url, content))
        assert status == expected_status
        assert cb.is_open is expected_open

    async def test_healthy_page_resets_failures(self):
        from app.services.ics_worker.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker()
        page = _make_page("https://www.icsource.com/search", "search results listing 100 parts")

        status = await cb.check_page_health(page)
        assert status == "HEALTHY"
        assert cb.consecutive_failures == 0

    async def test_captcha_warning_first_time(self):
        from app.services.ics_worker.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker()
        page = _make_page("https://www.icsource.com/search", "please verify you are human captcha")

        status = await cb.check_page_health(page)
        assert status == "CAPTCHA_WARNING"
        assert cb.captcha_count == 1
        assert cb.is_open is False

    async def test_captcha_trips_on_second_detection(self):
        from app.services.ics_worker.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker()
        page = _make_page("https://www.icsource.com/search", "captcha verify")

        await cb.check_page_health(page)
        status = await cb.check_page_health(page)
        assert status == "CAPTCHA_WARNING"
        assert cb.is_open is True

    async def test_page_evaluate_exception_accumulates_failures(self):
        from app.services.ics_worker.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker()
        page = MagicMock()
        page.url = "https://www.icsource.com/search"
        page.evaluate = AsyncMock(side_effect=Exception("Playwright error"))

        # First failure
        status = await cb.check_page_health(page)
        assert status == "CHECK_FAILED"
        assert cb.consecutive_failures == 1
        assert cb.is_open is False

        # Second failure
        await cb.check_page_health(page)
        assert cb.consecutive_failures == 2

        # Third failure trips the breaker
        await cb.check_page_health(page)
        assert cb.consecutive_failures == 3
        assert cb.is_open is True


# ── CircuitBreakerBase ────────────────────────────────────────────────


class TestCircuitBreakerBase:
    def test_empty_results_streak(self):
        from app.services.search_worker_base.circuit_breaker import CircuitBreakerBase

        cb = CircuitBreakerBase()
        for _ in range(9):
            cb.record_empty_results()
        assert cb.is_open is False
        assert cb.empty_results_streak == 9

    def test_ten_empty_results_trips_breaker(self):
        from app.services.search_worker_base.circuit_breaker import CircuitBreakerBase

        cb = CircuitBreakerBase()
        for _ in range(10):
            cb.record_empty_results()
        assert cb.is_open is True
        assert "shadow-block" in cb.trip_reason

    def test_record_results_resets_streak(self):
        from app.services.search_worker_base.circuit_breaker import CircuitBreakerBase

        cb = CircuitBreakerBase()
        for _ in range(5):
            cb.record_empty_results()
        cb.record_results()
        assert cb.empty_results_streak == 0

    def test_should_stop_false_initially(self):
        from app.services.search_worker_base.circuit_breaker import CircuitBreakerBase

        cb = CircuitBreakerBase()
        assert cb.should_stop() is False

    def test_should_stop_true_after_trip(self):
        from app.services.search_worker_base.circuit_breaker import CircuitBreakerBase

        cb = CircuitBreakerBase()
        cb._trip("test reason")
        assert cb.should_stop() is True

    def test_get_trip_info(self):
        from app.services.search_worker_base.circuit_breaker import CircuitBreakerBase

        cb = CircuitBreakerBase()
        cb._trip("test")
        info = cb.get_trip_info()
        assert info["is_open"] is True
        assert info["trip_reason"] == "test"
        assert "captcha_count" in info
        assert "consecutive_failures" in info
        assert "empty_results_streak" in info


# ── HumanBehavior ─────────────────────────────────────────────────────


class TestHumanBehavior:
    async def test_random_delay_completes(self):
        from app.services.ics_worker.human_behavior import HumanBehavior

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await HumanBehavior.random_delay(0.1, 0.2)
            mock_sleep.assert_called_once()
            delay = mock_sleep.call_args[0][0]
            assert 0.1 <= delay <= 0.2

    async def test_human_type(self):
        from app.services.ics_worker.human_behavior import HumanBehavior

        page = MagicMock()
        page.keyboard = MagicMock()
        page.keyboard.type = AsyncMock()
        locator = MagicMock()
        locator.click = AsyncMock()

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await HumanBehavior.human_type(page, locator, "hello")

        locator.click.assert_called_once()
        assert page.keyboard.type.call_count == 5  # one per char in "hello"

    async def test_human_click_with_bounding_box(self):
        from app.services.ics_worker.human_behavior import HumanBehavior

        page = MagicMock()
        page.mouse = MagicMock()
        page.mouse.click = AsyncMock()
        locator = MagicMock()
        locator.bounding_box = AsyncMock(return_value={"x": 100, "y": 200, "width": 80, "height": 30})
        locator.click = AsyncMock()

        await HumanBehavior.human_click(page, locator)
        page.mouse.click.assert_called_once()
        # x should be within bounding box
        x, y = page.mouse.click.call_args[0]
        assert 100 <= x <= 180
        assert 200 <= y <= 230

    async def test_human_click_without_bounding_box(self):
        from app.services.ics_worker.human_behavior import HumanBehavior

        page = MagicMock()
        page.mouse = MagicMock()
        locator = MagicMock()
        locator.bounding_box = AsyncMock(return_value=None)
        locator.click = AsyncMock()

        await HumanBehavior.human_click(page, locator)
        locator.click.assert_called_once()
