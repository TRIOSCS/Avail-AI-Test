"""test_file_utils.py — Tests for app/file_utils.py.

Tests file parsing (CSV, TSV, Excel) and stock row normalization.

Called by: pytest
Depends on: app/file_utils.py
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

from app.file_utils import (
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
