"""test_file_utils.py — Tests for app/file_utils.py.

Tests file parsing (CSV, TSV, Excel) and stock row normalization.

Called by: pytest
Depends on: app/file_utils.py
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

os.environ["TESTING"] = "1"

from app.file_utils import (
    ParseError,
    _looks_like_html,
    _parse_html_table,
    extract_mpns,
    extract_mpns_with_rows,
    normalize_stock_row,
    parse_tabular_file,
)

# ═══════════════════════════════════════════════════════════════════════
#  parse_tabular_file
# ═══════════════════════════════════════════════════════════════════════


def _stub_openpyxl_rows(rows):
    """Wire the patched openpyxl stub so load_workbook().active.iter_rows() yields
    rows."""
    mock_openpyxl = sys.modules["openpyxl"]
    mock_wb = MagicMock()
    mock_ws = MagicMock()
    mock_ws.iter_rows.return_value = rows
    mock_wb.active = mock_ws
    mock_openpyxl.load_workbook.return_value = mock_wb


class TestParseTabularFile:
    def test_csv_basic(self):
        content = b"mpn,qty,price\nLM317T,1000,0.50\nNE555P,500,0.25"
        rows = parse_tabular_file(content, "stock.csv")
        assert len(rows) == 2
        assert rows[0]["mpn"] == "LM317T"
        assert rows[0]["qty"] == "1000"

    def test_tsv_basic(self):
        content = b"mpn\tqty\tprice\nLM317T\t1000\t0.50"
        rows = parse_tabular_file(content, "stock.tsv")
        assert len(rows) == 1
        assert rows[0]["mpn"] == "LM317T"

    def test_csv_headers_lowered_and_stripped(self):
        content = b" MPN , Qty , Price \nLM317T,1000,0.50"
        rows = parse_tabular_file(content, "data.csv")
        assert len(rows) == 1
        assert "mpn" in rows[0]
        assert "qty" in rows[0]

    @patch.dict("sys.modules", {"openpyxl": MagicMock()})
    def test_excel_parsed(self):
        _stub_openpyxl_rows(
            [
                ("MPN", "Qty", "Price"),
                ("LM317T", 1000, 0.50),
                ("NE555P", 500, 0.25),
            ]
        )
        rows = parse_tabular_file(b"fake-excel", "stock.xlsx")
        assert len(rows) == 2
        assert rows[0]["mpn"] == "LM317T"
        assert rows[0]["qty"] == "1000"

    @patch.dict("sys.modules", {"openpyxl": MagicMock()})
    def test_excel_xls_extension(self):
        _stub_openpyxl_rows([("MPN",), ("ABC123",)])
        rows = parse_tabular_file(b"fake", "file.xls")
        assert len(rows) == 1

    @patch.dict("sys.modules", {"openpyxl": MagicMock()})
    def test_excel_empty_rows_skipped(self):
        _stub_openpyxl_rows(
            [
                ("MPN", "Qty"),
                (None, None),  # empty row
                ("LM317T", 100),
            ]
        )
        rows = parse_tabular_file(b"fake", "stock.xlsx")
        assert len(rows) == 1

    def test_unknown_extension_treated_as_csv(self):
        content = b"mpn,qty\nLM317T,1000"
        rows = parse_tabular_file(content, "data.txt")
        assert len(rows) == 1

    def test_corrupt_file_returns_empty(self):
        rows = parse_tabular_file(b"\x00\x01\x02", "stock.csv")
        # CSV parser may return empty or partial; should not raise
        assert isinstance(rows, list)

    def test_empty_content_returns_empty(self):
        rows = parse_tabular_file(b"", "empty.csv")
        assert rows == []

    def test_no_filename_defaults_csv(self):
        content = b"mpn,qty\nLM317T,100"
        rows = parse_tabular_file(content, "")
        assert len(rows) == 1

    def test_utf8_bom_handled(self):
        content = b"\xef\xbb\xbfmpn,qty\nLM317T,1000"
        rows = parse_tabular_file(content, "bom.csv")
        assert len(rows) == 1
        assert "mpn" in rows[0]


# ═══════════════════════════════════════════════════════════════════════
#  normalize_stock_row
# ═══════════════════════════════════════════════════════════════════════


class TestNormalizeStockRow:
    def test_standard_row(self):
        row = {"mpn": "LM317T", "qty": "1000", "price": "0.50"}
        result = normalize_stock_row(row)
        assert result is not None
        assert result["mpn"] == "LM317T"
        assert result["qty"] == 1000
        assert result["price"] == 0.50

    @pytest.mark.parametrize(
        ("row", "expected_mpn"),
        [
            ({"pn": "NE555P", "quantity": "500"}, "NE555P"),
            ({"part number": "ABC123XY", "avail": "200"}, "ABC123XY"),
            ({" MPN ": "LM317T", " QTY ": "100"}, "LM317T"),
            ({"sku": "SKU12345", "qty": "50"}, "SKU12345"),
        ],
        ids=["pn", "part_number", "stripped_lowered_keys", "sku"],
    )
    def test_alternate_mpn_headers(self, row, expected_mpn):
        result = normalize_stock_row(row)
        assert result is not None
        assert result["mpn"] == expected_mpn

    @pytest.mark.parametrize(
        ("row", "expected_qty"),
        [
            ({"mpn": "LM317T", "available": "500"}, 500),
            ({"mpn": "LM317T", "stock": "750"}, 750),
        ],
        ids=["available", "stock"],
    )
    def test_alternate_qty_headers(self, row, expected_qty):
        result = normalize_stock_row(row)
        assert result["qty"] == expected_qty

    def test_alternate_price_header(self):
        row = {"mpn": "LM317T", "unit price": "1.25"}
        result = normalize_stock_row(row)
        assert result["price"] == 1.25

    @pytest.mark.parametrize(
        "row",
        [
            {"qty": "1000", "price": "0.50"},  # no mpn
            {"mpn": "AB"},  # < 3 chars
            {"mpn": ""},  # empty mpn
        ],
        ids=["no_mpn", "short_mpn", "empty_mpn"],
    )
    def test_invalid_mpn_returns_none(self, row):
        assert normalize_stock_row(row) is None

    @pytest.mark.parametrize(
        ("row", "field", "expected"),
        [
            ({"mpn": "LM317T", "manufacturer": "Texas Instruments"}, "manufacturer", "Texas Instruments"),
            ({"mpn": "LM317T", "mfr": "TI"}, "manufacturer", "TI"),
            ({"mpn": "LM317T", "condition": "New"}, "condition", "New"),
            ({"mpn": "LM317T", "packaging": "Tape & Reel"}, "packaging", "Tape & Reel"),
            ({"mpn": "LM317T", "date_code": "2024+"}, "date_code", "2024+"),
            ({"mpn": "LM317T", "lead_time": "4-6 weeks"}, "lead_time", "4-6 weeks"),
        ],
        ids=["manufacturer", "mfr_alias", "condition", "packaging", "date_code", "lead_time"],
    )
    def test_optional_fields(self, row, field, expected):
        result = normalize_stock_row(row)
        assert result[field] == expected

    @pytest.mark.parametrize(
        ("row", "expected_currency"),
        [
            ({"mpn": "LM317T", "price": "€1.25"}, "EUR"),
            ({"mpn": "LM317T", "price": "1.25", "currency": "GBP"}, "GBP"),
            ({"mpn": "LM317T", "price": "1.25"}, "USD"),
        ],
        ids=["from_price_symbol", "from_currency_field", "default_usd"],
    )
    def test_currency_detection(self, row, expected_currency):
        result = normalize_stock_row(row)
        assert result["currency"] == expected_currency

    def test_no_qty_or_price_still_returns(self):
        row = {"mpn": "LM317T"}
        result = normalize_stock_row(row)
        assert result is not None
        assert result["qty"] is None
        assert result["price"] is None


# ═══════════════════════════════════════════════════════════════════════
#  _looks_like_html
# ═══════════════════════════════════════════════════════════════════════


class TestLooksLikeHtml:
    def test_html_tag(self):
        assert _looks_like_html(b"<html><body></body></html>") is True

    def test_head_tag(self):
        assert _looks_like_html(b"<head><title>t</title></head>") is True

    def test_table_tag(self):
        assert _looks_like_html(b"<table><tr><td>a</td></tr></table>") is True

    def test_doctype(self):
        assert _looks_like_html(b"<!DOCTYPE html>") is True

    def test_meta_tag(self):
        assert _looks_like_html(b"<meta charset='utf-8'>") is True

    def test_csv_content_is_not_html(self):
        assert _looks_like_html(b"mpn,qty,price\nABC123,10,1.50") is False

    def test_leading_whitespace_before_html_tag(self):
        assert _looks_like_html(b"  \n<html>") is True

    def test_plain_text_is_not_html(self):
        assert _looks_like_html(b"just some plain text") is False


# ═══════════════════════════════════════════════════════════════════════
#  _parse_html_table
# ═══════════════════════════════════════════════════════════════════════


class TestParseHtmlTable:
    _BASIC = b"<table><tr><th>mpn</th><th>qty</th></tr><tr><td>ABC123</td><td>10</td></tr></table>"

    def test_basic_table_returns_rows(self):
        rows = _parse_html_table(self._BASIC)
        assert len(rows) == 1
        assert rows[0]["mpn"] == "ABC123"
        assert rows[0]["qty"] == "10"

    def test_empty_table_returns_empty(self):
        assert _parse_html_table(b"<table></table>") == []

    def test_blank_data_rows_skipped(self):
        html = (
            b"<table>"
            b"<tr><th>mpn</th><th>qty</th></tr>"
            b"<tr><td></td><td></td></tr>"
            b"<tr><td>ABC123</td><td>10</td></tr>"
            b"</table>"
        )
        rows = _parse_html_table(html)
        assert len(rows) == 1
        assert rows[0]["mpn"] == "ABC123"

    def test_multiple_data_rows(self):
        html = (
            b"<table>"
            b"<tr><th>mpn</th><th>qty</th></tr>"
            b"<tr><td>ABC123</td><td>10</td></tr>"
            b"<tr><td>DEF456</td><td>20</td></tr>"
            b"</table>"
        )
        rows = _parse_html_table(html)
        assert len(rows) == 2
        assert rows[1]["mpn"] == "DEF456"

    def test_headers_are_lowercased(self):
        html = b"<table><tr><th>MPN</th><th>QTY</th></tr><tr><td>X1</td><td>5</td></tr></table>"
        rows = _parse_html_table(html)
        assert "mpn" in rows[0]
        assert "qty" in rows[0]

    def test_values_are_stripped(self):
        html = b"<table><tr><th>mpn</th></tr><tr><td>  ABC  </td></tr></table>"
        rows = _parse_html_table(html)
        assert rows[0]["mpn"] == "ABC"

    def test_iso_encoded_bytes(self):
        html = "<table><tr><th>mpn</th></tr><tr><td>TEST</td></tr></table>"
        rows = _parse_html_table(html.encode("iso-8859-1"))
        assert len(rows) == 1
        assert rows[0]["mpn"] == "TEST"

    def test_no_table_returns_empty(self):
        assert _parse_html_table(b"<html><body><p>No table</p></body></html>") == []

    def test_th_cells_used_as_headers(self):
        html = b"<table><tr><th>part number</th></tr><tr><td>R100</td></tr></table>"
        rows = _parse_html_table(html)
        assert rows[0]["part number"] == "R100"


# ═══════════════════════════════════════════════════════════════════════
#  parse_tabular_file — HTML branches and exception handler
# ═══════════════════════════════════════════════════════════════════════


class TestParseTabularFileHtmlBranches:
    _HTML = b"<table><tr><th>mpn</th><th>qty</th></tr><tr><td>LM317T</td><td>100</td></tr></table>"

    def test_xlsx_extension_with_html_content(self):
        # ERP exports: .xlsx filename but the bytes are really HTML — line 79
        rows = parse_tabular_file(self._HTML, "export.xlsx")
        assert len(rows) == 1
        assert rows[0]["mpn"] == "LM317T"

    def test_xls_extension_with_html_content(self):
        rows = parse_tabular_file(self._HTML, "export.xls")
        assert len(rows) == 1

    def test_non_xlsx_html_content(self):
        # Non-xlsx filename + HTML body → line 83
        rows = parse_tabular_file(self._HTML, "report.html")
        assert len(rows) == 1
        assert rows[0]["mpn"] == "LM317T"

    def test_html_content_with_csv_extension(self):
        # CSV extension but HTML content → falls through to HTML branch (line 83)
        rows = parse_tabular_file(self._HTML, "data.csv")
        assert len(rows) == 1

    def test_exception_in_excel_parse_raises_parse_error(self):
        # Invalid xlsx bytes → openpyxl raises → the parser signals a HARD failure with a
        # typed ParseError (distinct from a genuinely-empty file that returns []).
        with pytest.raises(ParseError):
            parse_tabular_file(b"not html and not valid xlsx at all", "data.xlsx")

    def test_exception_with_mock_raises_parse_error(self):
        with (
            patch("app.file_utils._parse_excel", side_effect=RuntimeError("corrupt")),
            pytest.raises(ParseError),
        ):
            parse_tabular_file(b"definitely not html content", "stock.xls")


# ═══════════════════════════════════════════════════════════════════════
#  extract_mpns_with_rows
# ═══════════════════════════════════════════════════════════════════════


class TestExtractMpnsWithRows:
    def test_empty_rows_returns_empty(self):
        assert extract_mpns_with_rows([]) == []

    def test_recognized_mpn_column(self):
        rows = [{"mpn": "ABC123"}, {"mpn": "DEF456"}]
        assert extract_mpns_with_rows(rows) == [(2, "ABC123"), (3, "DEF456")]

    def test_part_number_column(self):
        rows = [{"part number": "XYZ789"}]
        assert extract_mpns_with_rows(rows) == [(2, "XYZ789")]

    def test_pn_column(self):
        assert extract_mpns_with_rows([{"pn": "LM317T"}]) == [(2, "LM317T")]

    def test_part_hash_column(self):
        assert extract_mpns_with_rows([{"part#": "C0402"}]) == [(2, "C0402")]

    def test_single_unknown_column_used_as_fallback(self):
        # One col not in _MPN_COLUMN_NAMES → single-col fallback (line 150-151)
        rows = [{"customcol": "ABC123"}]
        assert extract_mpns_with_rows(rows) == [(2, "ABC123")]

    def test_multiple_unknown_columns_returns_empty(self):
        # Multiple unrecognized cols → col is None (line 152-153)
        rows = [{"foo": "bar", "baz": "qux"}]
        assert extract_mpns_with_rows(rows) == []

    def test_blank_values_skipped_row_numbers_preserved(self):
        rows = [{"mpn": "ABC"}, {"mpn": ""}, {"mpn": "DEF"}]
        # Row 3 (blank) is skipped; row 4 is DEF
        assert extract_mpns_with_rows(rows) == [(2, "ABC"), (4, "DEF")]

    def test_file_row_numbering_starts_at_2(self):
        rows = [{"mpn": "A"}, {"mpn": "B"}, {"mpn": "C"}]
        result = extract_mpns_with_rows(rows)
        assert result[0][0] == 2
        assert result[1][0] == 3
        assert result[2][0] == 4

    def test_all_blank_mpns_returns_empty(self):
        rows = [{"mpn": ""}, {"mpn": "   "}]
        assert extract_mpns_with_rows(rows) == []

    def test_whitespace_only_values_skipped(self):
        rows = [{"mpn": "   "}, {"mpn": "ABC"}]
        assert extract_mpns_with_rows(rows) == [(3, "ABC")]


# ═══════════════════════════════════════════════════════════════════════
#  extract_mpns
# ═══════════════════════════════════════════════════════════════════════


class TestExtractMpns:
    def test_returns_mpn_strings(self):
        rows = [{"mpn": "ABC123"}, {"mpn": "DEF456"}]
        assert extract_mpns(rows) == ["ABC123", "DEF456"]

    def test_empty_rows(self):
        assert extract_mpns([]) == []

    def test_blanks_dropped(self):
        rows = [{"mpn": "ABC"}, {"mpn": ""}, {"mpn": "DEF"}]
        assert extract_mpns(rows) == ["ABC", "DEF"]

    def test_preserves_order(self):
        rows = [{"mpn": "C"}, {"mpn": "A"}, {"mpn": "B"}]
        assert extract_mpns(rows) == ["C", "A", "B"]

    def test_no_recognized_column_returns_empty(self):
        rows = [{"col1": "v1", "col2": "v2"}]
        assert extract_mpns(rows) == []

    def test_strips_row_tuples(self):
        # Verify extract_mpns drops the file_row integer and returns only strings
        rows = [{"part number": "R1"}, {"part number": "C2"}]
        result = extract_mpns(rows)
        assert result == ["R1", "C2"]
        assert all(isinstance(v, str) for v in result)
