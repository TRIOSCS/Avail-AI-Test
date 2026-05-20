"""test_attachment_parser_coverage.py — Gap tests for attachment_parser.py.

Targets missing lines:
- Line 45: empty header string skipped in _match_headers_deterministic
- Line 132: claude_structured returns result without 'mappings' key
- Lines 223-224: _get_or_detect_mapping cache write exception
- Line 266: delimiter auto-detect (tab > comma) in _parse_csv
- Lines 273, 276: row cap and empty-rows path in _parse_csv
- Line 369: .xlsx extension branch in parse_attachment
- Lines 373-374: unsupported extension returns []
- Line 377: no headers or no data_rows returns []

Called by: pytest
Depends on: app/services/attachment_parser.py, tests/conftest.py
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.services.attachment_parser import (
    _match_headers_deterministic,
    _parse_csv,
    parse_attachment,
)
from tests.conftest import engine  # noqa: F401


class TestMatchHeadersDeterministicEmptyHeader:
    def test_empty_string_header_is_skipped(self):
        """Line 45: empty header string (after strip) is skipped, not matched."""
        # "" after strip → continue without adding to mapping
        headers = ["", "Part Number", "Qty"]
        mapping = _match_headers_deterministic(headers)
        # Index 0 (empty string) should not be in mapping
        assert 0 not in mapping
        # Indices 1 and 2 should still map
        assert mapping[1] == "mpn"
        assert mapping[2] == "qty"

    def test_whitespace_only_header_is_skipped(self):
        """Whitespace-only headers strip to empty and are skipped."""
        headers = ["   ", "MPN"]
        mapping = _match_headers_deterministic(headers)
        assert 0 not in mapping
        assert mapping[1] == "mpn"


class TestAIDetectColumnsNoMappingsKey:
    @pytest.mark.asyncio
    async def test_result_without_mappings_key_returns_empty(self):
        """Line 132: claude_structured returns result that has no 'mappings' key."""
        from app.services.attachment_parser import _ai_detect_columns

        with patch(
            "app.utils.claude_client.claude_structured",
            new_callable=AsyncMock,
            return_value={"some_other_key": "value"},  # No 'mappings'
        ):
            result = await _ai_detect_columns(
                headers=["Col A", "Col B"],
                sample_rows=[["LM317T", "100"]],
                vendor_domain="test.com",
            )
        assert result == {}

    @pytest.mark.asyncio
    async def test_none_result_returns_empty(self):
        """Line 132: claude_structured returns None."""
        from app.services.attachment_parser import _ai_detect_columns

        with patch(
            "app.utils.claude_client.claude_structured",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await _ai_detect_columns(
                headers=["Col A"],
                sample_rows=[["LM317T"]],
                vendor_domain="test.com",
            )
        assert result == {}


class TestGetOrDetectMappingCacheWriteException:
    @pytest.mark.asyncio
    async def test_cache_write_exception_is_swallowed(self, db_session):
        """Lines 223-224: cache write failure is caught and logged, mapping still returned."""
        from app.services.attachment_parser import _get_or_detect_mapping

        # Patch the PostgreSQL insert import inside _get_or_detect_mapping to raise
        with patch(
            "sqlalchemy.dialects.postgresql.insert",
            side_effect=RuntimeError("Cache insert failed"),
        ):
            # Should not raise; mapping should still return (deterministic found mpn)
            result = await _get_or_detect_mapping(
                headers=["Part Number", "Qty"],
                sample_rows=[["LM317T", "100"]],
                vendor_domain="failcache.com",
                file_fingerprint="fp_fail_001",
                db=db_session,
            )
        # Mapping should still be returned despite cache failure
        assert result.get(0) == "mpn"


class TestParseCSVDelimiterAndCap:
    def test_tab_count_exceeds_comma_detects_tab_delimiter(self):
        """Line 266: when tabs > commas in content (non-.tsv file), use tab."""
        # A CSV file (not .tsv) but with more tabs than commas
        tsv_like = b"MPN\tQty\tPrice\nLM317T\t1000\t0.50\n"

        with patch("app.utils.file_validation.detect_encoding", return_value="utf-8"):
            headers, data_rows = _parse_csv(tsv_like, "stock.csv")

        assert headers == ["MPN", "Qty", "Price"]
        assert len(data_rows) == 1

    def test_empty_bytes_returns_empty(self):
        """Line 276: no rows after parse → returns ([], [])."""
        with patch("app.utils.file_validation.detect_encoding", return_value="utf-8"):
            headers, data_rows = _parse_csv(b"", "empty.csv")

        assert headers == []
        assert data_rows == []

    def test_row_cap_at_10000(self):
        """Lines 273: rows beyond 10000 are stopped."""
        # Build a CSV with exactly 10002 rows (header + 10001 data)
        lines = ["MPN,Qty"]
        for i in range(10002):
            lines.append(f"PART{i},100")
        content = "\n".join(lines).encode("utf-8")

        with patch("app.utils.file_validation.detect_encoding", return_value="utf-8"):
            headers, data_rows = _parse_csv(content, "big.csv")

        # Should be capped at 10000 (the break fires when len(rows) > 10000)
        assert len(data_rows) <= 10001  # capped at 10000 data rows


class TestParseAttachmentBranches:
    @pytest.mark.asyncio
    async def test_xlsx_extension_invokes_parse_excel(self):
        """Line 369: .xlsx extension calls _parse_excel."""
        with (
            patch("app.utils.file_validation.validate_file", return_value=(True, "xlsx")),
            patch("app.utils.file_validation.file_fingerprint", return_value="fp_xlsx"),
            patch("app.services.attachment_parser._parse_excel", return_value=([], [])) as mock_excel,
        ):
            result = await parse_attachment(
                file_bytes=b"fake-excel",
                filename="stock.xlsx",
                vendor_domain="vendor.com",
            )

        mock_excel.assert_called_once()
        assert result == []

    @pytest.mark.asyncio
    async def test_unsupported_extension_returns_empty(self):
        """Lines 373-374: unsupported extension (e.g. .pdf) returns []."""
        with patch("app.utils.file_validation.validate_file", return_value=(True, "pdf")):
            result = await parse_attachment(
                file_bytes=b"fake-pdf",
                filename="stock.pdf",
                vendor_domain="vendor.com",
            )
        assert result == []

    @pytest.mark.asyncio
    async def test_empty_headers_returns_empty(self):
        """Line 377: no headers → returns []."""
        with (
            patch("app.utils.file_validation.validate_file", return_value=(True, "csv")),
            patch("app.utils.file_validation.file_fingerprint", return_value="fp_empty"),
            patch("app.utils.file_validation.detect_encoding", return_value="utf-8"),
        ):
            # Empty CSV has no headers
            result = await parse_attachment(
                file_bytes=b"",
                filename="empty.csv",
                vendor_domain="vendor.com",
            )
        assert result == []

    @pytest.mark.asyncio
    async def test_headers_only_no_data_rows_returns_empty(self):
        """Line 377: headers exist but no data rows → returns []."""
        with (
            patch("app.utils.file_validation.validate_file", return_value=(True, "csv")),
            patch("app.utils.file_validation.file_fingerprint", return_value="fp_hdr_only"),
            patch("app.utils.file_validation.detect_encoding", return_value="utf-8"),
        ):
            result = await parse_attachment(
                file_bytes=b"Part Number,Qty\n",
                filename="headers_only.csv",
                vendor_domain="vendor.com",
            )
        assert result == []
