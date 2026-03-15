"""test_file_utils.py — Tests for app/file_utils.py.

Tests file parsing (CSV, TSV, Excel) and stock row normalization.

Called by: pytest
Depends on: app/file_utils.py
"""

import sys
from unittest.mock import MagicMock, patch

from app.file_utils import (
    normalize_stock_row,
    parse_tabular_file,
)

# ═══════════════════════════════════════════════════════════════════════
#  parse_tabular_file
# ═══════════════════════════════════════════════════════════════════════


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
        mock_openpyxl = sys.modules["openpyxl"]
        mock_wb = MagicMock()
        mock_ws = MagicMock()
        mock_ws.iter_rows.return_value = [
            ("MPN", "Qty", "Price"),
            ("LM317T", 1000, 0.50),
            ("NE555P", 500, 0.25),
        ]
        mock_wb.active = mock_ws
        mock_openpyxl.load_workbook.return_value = mock_wb

        rows = parse_tabular_file(b"fake-excel", "stock.xlsx")
        assert len(rows) == 2
        assert rows[0]["mpn"] == "LM317T"
        assert rows[0]["qty"] == "1000"

    @patch.dict("sys.modules", {"openpyxl": MagicMock()})
    def test_excel_xls_extension(self):
        mock_openpyxl = sys.modules["openpyxl"]
        mock_wb = MagicMock()
        mock_ws = MagicMock()
        mock_ws.iter_rows.return_value = [("MPN",), ("ABC123",)]
        mock_wb.active = mock_ws
        mock_openpyxl.load_workbook.return_value = mock_wb

        rows = parse_tabular_file(b"fake", "file.xls")
        assert len(rows) == 1

    @patch.dict("sys.modules", {"openpyxl": MagicMock()})
    def test_excel_empty_rows_skipped(self):
        mock_openpyxl = sys.modules["openpyxl"]
        mock_wb = MagicMock()
        mock_ws = MagicMock()
        mock_ws.iter_rows.return_value = [
            ("MPN", "Qty"),
            (None, None),  # empty row
            ("LM317T", 100),
        ]
        mock_wb.active = mock_ws
        mock_openpyxl.load_workbook.return_value = mock_wb

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

    def test_alternate_mpn_header_pn(self):
        row = {"pn": "NE555P", "quantity": "500"}
        result = normalize_stock_row(row)
        assert result is not None
        assert result["mpn"] == "NE555P"

    def test_alternate_mpn_header_part_number(self):
        row = {"part number": "ABC123XY", "avail": "200"}
        result = normalize_stock_row(row)
        assert result is not None
        assert result["mpn"] == "ABC123XY"

    def test_alternate_qty_header(self):
        row = {"mpn": "LM317T", "available": "500"}
        result = normalize_stock_row(row)
        assert result["qty"] == 500

    def test_alternate_qty_stock(self):
        row = {"mpn": "LM317T", "stock": "750"}
        result = normalize_stock_row(row)
        assert result["qty"] == 750

    def test_alternate_price_header(self):
        row = {"mpn": "LM317T", "unit price": "1.25"}
        result = normalize_stock_row(row)
        assert result["price"] == 1.25

    def test_no_mpn_returns_none(self):
        row = {"qty": "1000", "price": "0.50"}
        result = normalize_stock_row(row)
        assert result is None

    def test_short_mpn_returns_none(self):
        row = {"mpn": "AB"}  # < 3 chars
        result = normalize_stock_row(row)
        assert result is None

    def test_empty_mpn_returns_none(self):
        row = {"mpn": ""}
        result = normalize_stock_row(row)
        assert result is None

    def test_optional_fields_manufacturer(self):
        row = {"mpn": "LM317T", "manufacturer": "Texas Instruments"}
        result = normalize_stock_row(row)
        assert result["manufacturer"] == "Texas Instruments"

    def test_optional_fields_mfr_alias(self):
        row = {"mpn": "LM317T", "mfr": "TI"}
        result = normalize_stock_row(row)
        assert result["manufacturer"] == "TI"

    def test_optional_fields_condition(self):
        row = {"mpn": "LM317T", "condition": "New"}
        result = normalize_stock_row(row)
        assert result["condition"] == "New"

    def test_optional_fields_packaging(self):
        row = {"mpn": "LM317T", "packaging": "Tape & Reel"}
        result = normalize_stock_row(row)
        assert result["packaging"] == "Tape & Reel"

    def test_optional_fields_date_code(self):
        row = {"mpn": "LM317T", "date_code": "2024+"}
        result = normalize_stock_row(row)
        assert result["date_code"] == "2024+"

    def test_optional_fields_lead_time(self):
        row = {"mpn": "LM317T", "lead_time": "4-6 weeks"}
        result = normalize_stock_row(row)
        assert result["lead_time"] == "4-6 weeks"

    def test_currency_detection_from_price(self):
        row = {"mpn": "LM317T", "price": "€1.25"}
        result = normalize_stock_row(row)
        assert result["currency"] == "EUR"

    def test_currency_detection_from_currency_field(self):
        row = {"mpn": "LM317T", "price": "1.25", "currency": "GBP"}
        result = normalize_stock_row(row)
        assert result["currency"] == "GBP"

    def test_default_currency_usd(self):
        row = {"mpn": "LM317T", "price": "1.25"}
        result = normalize_stock_row(row)
        assert result["currency"] == "USD"

    def test_no_qty_or_price_still_returns(self):
        row = {"mpn": "LM317T"}
        result = normalize_stock_row(row)
        assert result is not None
        assert result["qty"] is None
        assert result["price"] is None

    def test_header_keys_stripped_and_lowered(self):
        row = {" MPN ": "LM317T", " QTY ": "100"}
        result = normalize_stock_row(row)
        assert result is not None
        assert result["mpn"] == "LM317T"

    def test_sku_header_as_mpn(self):
        row = {"sku": "SKU12345", "qty": "50"}
        result = normalize_stock_row(row)
        assert result is not None
        assert result["mpn"] == "SKU12345"
