"""tests/test_tbf_result_parser.py — Tests for app/services/tbf_worker/result_parser.py."""

import os

os.environ["TESTING"] = "1"

from app.services.tbf_worker.result_parser import (
    TbfSighting,
    _is_phone_like,
    _parse_price,
    parse_quantity,
    parse_results_html,
)


class TestIsPhoneLike:
    def test_phone_number_is_phone_like(self):
        assert _is_phone_like("+30 2492024777")

    def test_company_name_not_phone(self):
        assert not _is_phone_like("Arrow Electronics")

    def test_empty_string_not_phone(self):
        assert not _is_phone_like("")

    def test_none_not_phone(self):
        assert not _is_phone_like(None)

    def test_all_digits_is_phone(self):
        assert _is_phone_like("1234567890")

    def test_mixed_alpha_not_phone(self):
        assert not _is_phone_like("ABC123")


class TestParseQuantity:
    def test_none_returns_none(self):
        assert parse_quantity(None) is None

    def test_empty_returns_none(self):
        assert parse_quantity("") is None

    def test_plain_number(self):
        assert parse_quantity("1000") == 1000

    def test_comma_separated(self):
        assert parse_quantity("10,000") == 10000

    def test_plus_suffix_stripped(self):
        assert parse_quantity("500+") == 500

    def test_text_returns_none(self):
        assert parse_quantity("N/A") is None

    def test_zero(self):
        assert parse_quantity("0") == 0


class TestParsePrice:
    def test_call_returns_empty(self):
        price, currency = _parse_price("CALL")
        assert price == ""
        assert currency == ""

    def test_call_lowercase(self):
        price, currency = _parse_price("call")
        assert price == ""
        assert currency == ""

    def test_euro(self):
        price, currency = _parse_price("€ 114")
        assert price == "114"
        assert currency == "EUR"

    def test_dollar(self):
        price, currency = _parse_price("$ 99")
        assert price == "99"
        assert currency == "USD"

    def test_pound(self):
        price, currency = _parse_price("£ 50")
        assert price == "50"
        assert currency == "GBP"

    def test_unknown_symbol(self):
        price, currency = _parse_price("¥ 500")
        assert price == "¥ 500"
        assert currency == ""

    def test_empty_returns_empty(self):
        price, currency = _parse_price("")
        assert price == ""
        assert currency == ""

    def test_none_returns_empty(self):
        price, currency = _parse_price(None)
        assert price == ""
        assert currency == ""


class TestParseResultsHtml:
    def test_empty_html_returns_empty(self):
        assert parse_results_html("") == []

    def test_none_returns_empty(self):
        assert parse_results_html(None) == []

    def test_no_table_returns_empty(self):
        assert parse_results_html("<div>No table here</div>") == []

    def test_table_without_data_rows_returns_empty(self):
        html = "<table><tr><td>header</td></tr></table>"
        assert parse_results_html(html) == []

    def test_parses_valid_row(self):
        html = """
        <table>
          <tr class="hover-higlight-anchor">
            <td>
              <div>LM317T</div>
              <div title="Voltage Regulator">Voltage Regulator</div>
            </td>
            <td>Texas Instruments</td>
            <td>1,000</td>
            <td>NEW</td>
            <td>$ 0.50</td>
            <td>Gold</td>
            <td title="Arrow Electronics">
              <div>Arrow Electronics</div>
              <div>+1 800 555 0001</div>
            </td>
            <td>US</td>
          </tr>
        </table>
        """
        results = parse_results_html(html)
        assert len(results) == 1
        s = results[0]
        assert isinstance(s, TbfSighting)
        assert s.part_number == "LM317T"
        assert s.manufacturer == "Texas Instruments"
        assert s.quantity == 1000
        assert s.price == "0.50"
        assert s.currency == "USD"
        assert s.country == "US"

    def test_skips_row_with_too_few_cells(self):
        html = """
        <table>
          <tr class="hover-higlight-anchor">
            <td>LM317T</td>
            <td>TI</td>
          </tr>
        </table>
        """
        results = parse_results_html(html)
        assert len(results) == 0

    def test_parses_multiple_rows(self):
        row_html = """
        <tr class="hover-higlight-anchor">
          <td><div>PART{}</div><div>Desc</div></td>
          <td>Mfr</td>
          <td>100</td>
          <td>NEW</td>
          <td>$ 1.00</td>
          <td>Gold</td>
          <td title="Vendor{}"><div>Vendor{}</div><div>+1 555 000 0001</div></td>
          <td>US</td>
        </tr>
        """
        rows = "".join(row_html.format(i, i, i) for i in range(3))
        html = f"<table>{rows}</table>"
        results = parse_results_html(html)
        assert len(results) == 3
