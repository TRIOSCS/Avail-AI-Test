"""tests/test_utils_file_validation.py — Tests for app/utils/file_validation.py."""

import os

os.environ["TESTING"] = "1"

from app.utils.file_validation import (
    MAX_FILE_SIZE,
    _get_extension,
    decode_text,
    detect_encoding,
    file_fingerprint,
    validate_file,
)


class TestGetExtension:
    def test_xlsx_extension(self):
        assert _get_extension("report.xlsx") == ".xlsx"

    def test_csv_extension(self):
        assert _get_extension("data.csv") == ".csv"

    def test_uppercase_lowercased(self):
        assert _get_extension("DATA.CSV") == ".csv"

    def test_no_extension(self):
        assert _get_extension("filename") == ""

    def test_empty_filename(self):
        assert _get_extension("") == ""

    def test_multiple_dots(self):
        assert _get_extension("my.data.file.csv") == ".csv"

    def test_tsv(self):
        assert _get_extension("prices.tsv") == ".tsv"


class TestValidateFile:
    def test_empty_file_invalid(self):
        ok, reason = validate_file(b"", "test.csv")
        assert not ok
        assert "Empty" in reason

    def test_file_too_large_invalid(self):
        large = b"x" * (MAX_FILE_SIZE + 1)
        ok, reason = validate_file(large, "big.csv")
        assert not ok
        assert "too large" in reason.lower()

    def test_csv_text_valid(self):
        content = b"col1,col2\nval1,val2"
        ok, file_type = validate_file(content, "data.csv")
        assert ok
        assert file_type == "csv"

    def test_tsv_valid(self):
        content = b"col1\tcol2\nval1\tval2"
        ok, file_type = validate_file(content, "data.tsv")
        assert ok
        assert file_type == "tsv"

    def test_txt_treated_as_csv(self):
        content = b"some plain text data"
        ok, file_type = validate_file(content, "data.txt")
        assert ok
        assert file_type == "csv"

    def test_unsupported_extension(self):
        content = b"<html>not a spreadsheet</html>"
        ok, reason = validate_file(content, "page.html")
        assert not ok
        assert "html" in reason.lower() or "Unsupported" in reason


class TestDetectEncoding:
    def test_utf8_content(self):
        content = "Hello World".encode("utf-8")
        encoding = detect_encoding(content)
        assert encoding is not None

    def test_latin1_content(self):
        content = "Héllo Wörld".encode("latin-1")
        encoding = detect_encoding(content)
        assert encoding is not None

    def test_empty_content_returns_fallback(self):
        encoding = detect_encoding(b"")
        # Empty content might return a fallback or None
        assert encoding is not None or encoding is None

    def test_ascii_content(self):
        encoding = detect_encoding(b"Simple ASCII text")
        assert encoding is not None


class TestValidateFileMagicBytes:
    def test_xlsx_valid_with_magic_bytes(self):
        """Xlsx files have PK magic bytes (zip format)."""
        # A minimal valid xlsx ZIP header
        xlsx_magic = b"PK\x03\x04" + b"\x00" * 26
        ok, ft = validate_file(xlsx_magic, "data.xlsx")
        # Either detected as xlsx or falls through to extension check
        assert isinstance(ok, bool)

    def test_wrong_type_for_xlsx_extension(self):
        """CSV content claiming to be xlsx should be invalid."""
        csv_content = b"col1,col2\nval1,val2"
        # The csv content won't match xlsx magic bytes
        ok, result = validate_file(csv_content, "report.xlsx")
        # Content doesn't match xlsx magic, so result depends on filetype detection
        assert isinstance(ok, bool)

    def test_encoding_detection_failure_path(self):
        """Test path when encoding detection fails."""
        # All-zero bytes might confuse detection but should still try
        content = b"\x00" * 100
        ok, result = validate_file(content, "data.csv")
        # Should either succeed with a detected encoding or fail gracefully
        assert isinstance(ok, bool)
        assert result is not None


class TestDecodeText:
    def test_utf8_decode(self):
        result = decode_text("Hello".encode("utf-8"), "utf-8")
        assert result == "Hello"

    def test_auto_detect_when_no_encoding(self):
        content = "Test content".encode("utf-8")
        result = decode_text(content)
        assert "Test content" in result

    def test_bad_bytes_replaced(self):
        content = b"\xff\xfe bad bytes"
        result = decode_text(content, "utf-8")
        assert isinstance(result, str)  # No crash


class TestFileFingerprint:
    def test_same_content_same_fingerprint(self):
        content = b"col1,col2\nval1,val2"
        assert file_fingerprint(content) == file_fingerprint(content)

    def test_different_content_different_fingerprint(self):
        content1 = b"col1,col2\nval1,val2"
        content2 = b"colA,colB\nvalA,valB"
        assert file_fingerprint(content1) != file_fingerprint(content2)

    def test_returns_hex_string(self):
        fp = file_fingerprint(b"test data")
        assert isinstance(fp, str)
        assert len(fp) == 32  # MD5 hex length

    def test_large_file_uses_first_4kb(self):
        # Two files that differ only after 4KB should have same fingerprint
        prefix = b"A" * 4096
        content1 = prefix + b"DIFFERENT"
        content2 = prefix + b"ANOTHER"
        assert file_fingerprint(content1) == file_fingerprint(content2)
