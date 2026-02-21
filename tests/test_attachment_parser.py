"""
test_attachment_parser.py -- Tests for vendor attachment parsing service.

Tests deterministic header matching, AI column detection, column mapping
cache, CSV/Excel parsing, row extraction, and the end-to-end pipeline.

Called by: pytest
Depends on: app/services/attachment_parser.py
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.attachment_parser import (
    HEADER_PATTERNS,
    _extract_row,
    _match_headers_deterministic,
    _parse_csv,
    _parse_excel,
    parse_attachment,
)


# ── Deterministic header matching ───────────────────────────────────


class TestMatchHeadersDeterministic:
    """Tests for _match_headers_deterministic -- regex-based column mapping."""

    def test_match_headers_standard(self):
        """Standard headers map to the correct fields."""
        headers = ["Part Number", "Manufacturer", "Qty", "Price"]
        mapping = _match_headers_deterministic(headers)

        assert mapping[0] == "mpn"
        assert mapping[1] == "manufacturer"
        assert mapping[2] == "qty"
        assert mapping[3] == "unit_price"

    def test_match_headers_alternates(self):
        """Alternate header forms still resolve correctly."""
        headers = ["MPN", "Mfg", "Quantity", "Unit Price"]
        mapping = _match_headers_deterministic(headers)

        assert mapping[0] == "mpn"
        assert mapping[1] == "manufacturer"
        assert mapping[2] == "qty"
        assert mapping[3] == "unit_price"

    def test_match_headers_partial(self):
        """Only recognized headers are mapped; unknown ones are skipped."""
        headers = ["Part Number", "Foo Column", "Bar Column"]
        mapping = _match_headers_deterministic(headers)

        assert mapping[0] == "mpn"
        assert 1 not in mapping
        assert 2 not in mapping
        assert len(mapping) == 1

    def test_match_headers_empty(self):
        """Empty header list returns empty mapping."""
        mapping = _match_headers_deterministic([])
        assert mapping == {}

    def test_match_headers_no_duplicates(self):
        """When two columns could match the same field, only the first wins."""
        # Both "Part Number" and "P/N" match the mpn pattern
        headers = ["Part Number", "P/N", "Qty"]
        mapping = _match_headers_deterministic(headers)

        # First column gets mpn
        assert mapping[0] == "mpn"
        # Second column should NOT also get mpn (each field used once)
        assert mapping.get(1) is None
        # Third column maps normally
        assert mapping[2] == "qty"


# ── AI column detection ─────────────────────────────────────────────


class TestAIDetectColumns:
    """Tests for _ai_detect_columns -- Claude-based fallback detection."""

    @pytest.mark.asyncio
    async def test_ai_detect_success(self):
        """Successful AI detection returns mapping for high-confidence fields."""
        mock_result = {
            "mappings": [
                {"column_index": 0, "field_name": "mpn", "confidence": 0.95},
                {"column_index": 1, "field_name": "manufacturer", "confidence": 0.85},
                {"column_index": 2, "field_name": "qty", "confidence": 0.90},
                {"column_index": 3, "field_name": "ignore", "confidence": 0.99},
            ]
        }
        with patch(
            "app.utils.claude_client.claude_structured",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            from app.services.attachment_parser import _ai_detect_columns

            result = await _ai_detect_columns(
                headers=["Col A", "Col B", "Col C", "Col D"],
                sample_rows=[["LM317T", "TI", "1000", "misc"]],
                vendor_domain="vendor.com",
            )

        # "ignore" fields are excluded
        assert result == {0: "mpn", 1: "manufacturer", 2: "qty"}

    @pytest.mark.asyncio
    async def test_ai_detect_failure(self):
        """AI failure (exception) returns empty mapping."""
        with patch(
            "app.utils.claude_client.claude_structured",
            new_callable=AsyncMock,
            side_effect=RuntimeError("API unavailable"),
        ):
            from app.services.attachment_parser import _ai_detect_columns

            result = await _ai_detect_columns(
                headers=["A", "B"],
                sample_rows=[["x", "y"]],
                vendor_domain="vendor.com",
            )

        assert result == {}

    @pytest.mark.asyncio
    async def test_ai_detect_low_confidence_filtered(self):
        """Fields below 0.5 confidence are filtered out."""
        mock_result = {
            "mappings": [
                {"column_index": 0, "field_name": "mpn", "confidence": 0.9},
                {"column_index": 1, "field_name": "manufacturer", "confidence": 0.3},
                {"column_index": 2, "field_name": "qty", "confidence": 0.1},
            ]
        }
        with patch(
            "app.utils.claude_client.claude_structured",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            from app.services.attachment_parser import _ai_detect_columns

            result = await _ai_detect_columns(
                headers=["A", "B", "C"],
                sample_rows=[["LM317T", "maybe", "??"]],
                vendor_domain="vendor.com",
            )

        # Only mpn passes the 0.5 threshold
        assert result == {0: "mpn"}


# ── Get or detect mapping (cache + fallback) ────────────────────────


class TestGetOrDetectMapping:
    """Tests for _get_or_detect_mapping -- cache / deterministic / AI pipeline."""

    @pytest.mark.asyncio
    async def test_get_mapping_cache_hit(self, db_session):
        """Returns cached mapping when fingerprint matches."""
        from app.models import ColumnMappingCache

        # Insert a cache entry
        entry = ColumnMappingCache(
            vendor_domain="cached-vendor.com",
            file_fingerprint="abc123fingerprint",
            mapping={"0": "mpn", "1": "qty"},
            confidence=0.9,
        )
        db_session.add(entry)
        db_session.commit()

        from app.services.attachment_parser import _get_or_detect_mapping

        result = await _get_or_detect_mapping(
            headers=["A", "B"],
            sample_rows=[["x", "y"]],
            vendor_domain="cached-vendor.com",
            file_fingerprint="abc123fingerprint",
            db=db_session,
        )

        # String keys from cache are converted back to int keys
        assert result == {0: "mpn", 1: "qty"}

    @pytest.mark.asyncio
    async def test_get_mapping_deterministic_sufficient(self):
        """When deterministic finds MPN, AI is not called."""
        with patch(
            "app.services.attachment_parser._ai_detect_columns",
            new_callable=AsyncMock,
        ) as mock_ai:
            from app.services.attachment_parser import _get_or_detect_mapping

            result = await _get_or_detect_mapping(
                headers=["Part Number", "Qty", "Price"],
                sample_rows=[["LM317T", "100", "0.50"]],
                vendor_domain="test.com",
                file_fingerprint="fp123",
                db=None,  # No db = no caching
            )

        # Deterministic found mpn, so AI should not be called
        mock_ai.assert_not_called()
        assert result[0] == "mpn"
        assert result[1] == "qty"
        assert result[2] == "unit_price"

    @pytest.mark.asyncio
    async def test_get_mapping_ai_fallback(self):
        """When deterministic cannot find MPN, AI fallback is invoked."""
        ai_mapping = {0: "mpn", 1: "qty"}
        with patch(
            "app.services.attachment_parser._ai_detect_columns",
            new_callable=AsyncMock,
            return_value=ai_mapping,
        ) as mock_ai:
            from app.services.attachment_parser import _get_or_detect_mapping

            result = await _get_or_detect_mapping(
                headers=["Unknown Col A", "Unknown Col B"],
                sample_rows=[["LM317T", "500"]],
                vendor_domain="mystery.com",
                file_fingerprint="fp456",
                db=None,
            )

        mock_ai.assert_called_once()
        assert result.get(0) == "mpn"
        assert result.get(1) == "qty"

    @pytest.mark.asyncio
    async def test_get_mapping_caches_result(self, db_session):
        """After detection, the mapping is upserted into ColumnMappingCache."""
        from app.models import ColumnMappingCache
        from app.services.attachment_parser import _get_or_detect_mapping

        # Use headers that deterministic can resolve (includes mpn)
        result = await _get_or_detect_mapping(
            headers=["Part Number", "Qty"],
            sample_rows=[["LM317T", "100"]],
            vendor_domain="newvendor.com",
            file_fingerprint="fp_new_789",
            db=db_session,
        )

        # Verify the mapping was cached
        cached = (
            db_session.query(ColumnMappingCache)
            .filter_by(
                vendor_domain="newvendor.com",
                file_fingerprint="fp_new_789",
            )
            .first()
        )
        assert cached is not None
        assert cached.mapping is not None
        # The cache stores string keys
        assert cached.mapping["0"] == "mpn"


# ── File parsing ────────────────────────────────────────────────────


class TestParseCSV:
    """Tests for _parse_csv -- CSV/TSV parsing with encoding detection."""

    def test_parse_csv_basic(self):
        """Standard CSV bytes parse into headers + data rows."""
        csv_content = b"Part Number,Qty,Price\nLM317T,1000,0.50\nSN74HC595N,500,0.25\n"

        with patch(
            "app.utils.file_validation.detect_encoding",
            return_value="utf-8",
        ):
            headers, data_rows = _parse_csv(csv_content, "stock.csv")

        assert headers == ["Part Number", "Qty", "Price"]
        assert len(data_rows) == 2
        assert data_rows[0] == ["LM317T", "1000", "0.50"]
        assert data_rows[1] == ["SN74HC595N", "500", "0.25"]

    def test_parse_csv_tsv_detection(self):
        """Tab-separated content is correctly detected and parsed."""
        tsv_content = b"MPN\tQty\tPrice\nLM317T\t1000\t0.50\n"

        with patch(
            "app.utils.file_validation.detect_encoding",
            return_value="utf-8",
        ):
            headers, data_rows = _parse_csv(tsv_content, "stock.tsv")

        assert headers == ["MPN", "Qty", "Price"]
        assert len(data_rows) == 1
        assert data_rows[0] == ["LM317T", "1000", "0.50"]


class TestParseExcel:
    """Tests for _parse_excel -- openpyxl-based Excel parsing."""

    def test_parse_excel_mock(self):
        """Mocked openpyxl returns expected headers and rows."""
        # Build a mock workbook
        mock_ws = MagicMock()
        mock_ws.iter_rows.return_value = [
            ("Part Number", "Qty", "Price"),
            ("LM317T", 1000, 0.50),
            ("SN74HC595N", 500, 0.25),
        ]

        mock_wb = MagicMock()
        mock_wb.active = mock_ws

        with patch(
            "openpyxl.load_workbook",
            return_value=mock_wb,
        ):
            headers, data_rows = _parse_excel(b"fake-excel-bytes")

        assert headers == ["Part Number", "Qty", "Price"]
        assert len(data_rows) == 2
        assert data_rows[0] == ["LM317T", "1000", "0.5"]
        assert data_rows[1] == ["SN74HC595N", "500", "0.25"]
        mock_wb.close.assert_called_once()


# ── Row extraction ──────────────────────────────────────────────────


class TestExtractRow:
    """Tests for _extract_row -- single-row field extraction with normalization."""

    def test_extract_row_full(self):
        """Row with all mapped columns produces a complete normalized dict."""
        mapping = {
            0: "mpn",
            1: "manufacturer",
            2: "qty",
            3: "unit_price",
            4: "condition",
        }
        row = ["LM317T", "Texas Instruments", "1000", "0.50", "New"]

        result = _extract_row(row, mapping)

        assert result is not None
        assert result["mpn"] == "LM317T"
        assert result["manufacturer"] == "Texas Instruments"
        assert result["qty"] == 1000
        assert result["unit_price"] == 0.50
        assert result["condition"] == "new"

    def test_extract_row_no_mpn_returns_none(self):
        """Row with empty/short MPN returns None (MPN is required)."""
        mapping = {0: "mpn", 1: "qty"}

        # Empty MPN
        assert _extract_row(["", "1000"], mapping) is None
        # MPN too short (< 3 chars after strip)
        assert _extract_row(["AB", "1000"], mapping) is None

    def test_extract_row_sparse_mapping(self):
        """Row with only MPN mapped still returns a valid dict."""
        mapping = {0: "mpn"}
        row = ["LM317T", "extra", "data"]

        result = _extract_row(row, mapping)
        assert result is not None
        assert result["mpn"] == "LM317T"
        # Other fields default to empty string or absent
        assert result.get("manufacturer", "") == ""


# ── End-to-end pipeline ─────────────────────────────────────────────


class TestParseAttachmentEndToEnd:
    """Tests for parse_attachment -- the full orchestration pipeline."""

    @pytest.mark.asyncio
    async def test_parse_attachment_end_to_end(self):
        """CSV bytes flow through validation, parsing, mapping, and extraction."""
        csv_bytes = (
            b"Part Number,Manufacturer,Qty,Price\n"
            b"LM317T,Texas Instruments,1000,0.50\n"
            b"SN74HC595N,NXP,500,0.25\n"
        )

        with patch(
            "app.utils.file_validation.validate_file",
            return_value=(True, "csv"),
        ), patch(
            "app.utils.file_validation.file_fingerprint",
            return_value="test_fp_001",
        ), patch(
            "app.utils.file_validation.detect_encoding",
            return_value="utf-8",
        ):
            results = await parse_attachment(
                file_bytes=csv_bytes,
                filename="stock_list.csv",
                vendor_domain="ti.com",
                db=None,
            )

        assert len(results) == 2

        # First row
        assert results[0]["mpn"] == "LM317T"
        assert results[0]["manufacturer"] == "Texas Instruments"
        assert results[0]["qty"] == 1000
        assert results[0]["unit_price"] == 0.50

        # Second row
        assert results[1]["mpn"] == "SN74HC595N"
        assert results[1]["manufacturer"] == "NXP"

    @pytest.mark.asyncio
    async def test_parse_attachment_invalid_file(self):
        """Invalid file returns empty list."""
        with patch(
            "app.utils.file_validation.validate_file",
            return_value=(False, "Unsupported file type"),
        ):
            results = await parse_attachment(
                file_bytes=b"not a real file",
                filename="readme.txt",
            )

        assert results == []

    @pytest.mark.asyncio
    async def test_parse_attachment_no_mpn_column(self):
        """File with no detectable MPN column returns empty list."""
        csv_bytes = b"Color,Size,Weight\nRed,Large,5kg\n"

        with patch(
            "app.utils.file_validation.validate_file",
            return_value=(True, "csv"),
        ), patch(
            "app.utils.file_validation.file_fingerprint",
            return_value="fp_no_mpn",
        ), patch(
            "app.utils.file_validation.detect_encoding",
            return_value="utf-8",
        ), patch(
            "app.services.attachment_parser._get_or_detect_mapping",
            new_callable=AsyncMock,
            return_value={0: "description", 1: "condition"},
        ):
            results = await parse_attachment(
                file_bytes=csv_bytes,
                filename="no_mpn.csv",
                vendor_domain="unknown.com",
            )

        # No mpn in mapping.values() => returns []
        assert results == []
