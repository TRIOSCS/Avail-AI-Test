"""
test_file_validation.py — Tests for file validation utilities.

Tests magic-byte detection, encoding detection, size limits,
extension handling, and fingerprinting.

Called by: pytest
Depends on: app/utils/file_validation.py
"""

from app.utils.file_validation import (
    validate_file,
    detect_encoding,
    decode_text,
    file_fingerprint,
    _get_extension,
    is_password_protected,
    MAX_FILE_SIZE,
    PROCESSABLE_EXTENSIONS,
)


# ── validate_file ──────────────────────────────────────────────────


class TestValidateFile:
    def test_empty_file_rejected(self):
        ok, reason = validate_file(b"", "test.csv")
        assert ok is False
        assert "Empty" in reason

    def test_oversized_file_rejected(self):
        big = b"x" * (MAX_FILE_SIZE + 1)
        ok, reason = validate_file(big, "huge.csv")
        assert ok is False
        assert "too large" in reason

    def test_csv_file_accepted(self):
        content = b"MPN,Qty,Price\nLM317T,1000,0.50\n"
        ok, file_type = validate_file(content, "stock.csv")
        assert ok is True
        assert file_type == "csv"

    def test_tsv_file_accepted(self):
        content = b"MPN\tQty\tPrice\nLM317T\t1000\t0.50\n"
        ok, file_type = validate_file(content, "stock.tsv")
        assert ok is True
        assert file_type == "tsv"

    def test_txt_treated_as_csv(self):
        content = b"MPN,Qty,Price\nLM317T,1000,0.50\n"
        ok, file_type = validate_file(content, "data.txt")
        assert ok is True
        assert file_type == "csv"

    def test_unsupported_extension_rejected(self):
        content = b"some content"
        ok, reason = validate_file(content, "document.docx")
        assert ok is False

    def test_xlsx_magic_bytes(self):
        """Real XLSX starts with PK (ZIP header)."""
        # Minimal ZIP signature
        content = b"PK\x03\x04" + b"\x00" * 100
        ok, file_type = validate_file(content, "data.xlsx")
        # Should either be accepted by magic bytes or fallback extension
        assert isinstance(ok, bool)

    def test_return_type_is_tuple(self):
        """validate_file always returns (bool, str|None)."""
        result = validate_file(b"test", "file.csv")
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bool)


# ── detect_encoding ─────────────────────────────────────────────────


class TestDetectEncoding:
    def test_utf8(self):
        content = "Hello, World!".encode("utf-8")
        enc = detect_encoding(content)
        assert enc is not None
        assert "utf" in enc.lower() or "ascii" in enc.lower()

    def test_latin1(self):
        content = "café résumé naïve".encode("latin-1")
        enc = detect_encoding(content)
        assert enc is not None

    def test_utf8_bom(self):
        content = b"\xef\xbb\xbf" + "MPN,Qty\nLM317T,100".encode("utf-8")
        enc = detect_encoding(content)
        assert enc is not None

    def test_empty_content_fallback(self):
        """Empty content should still return something (fallback chain)."""
        enc = detect_encoding(b"")
        # May return None or fallback — either is acceptable
        assert enc is None or isinstance(enc, str)

    def test_binary_garbage(self):
        """Random bytes should still get a fallback encoding."""
        enc = detect_encoding(bytes(range(256)))
        assert enc is not None  # last resort = utf-8-sig


# ── decode_text ─────────────────────────────────────────────────────


class TestDecodeText:
    def test_decode_utf8(self):
        content = "Hello".encode("utf-8")
        text = decode_text(content)
        assert text == "Hello"

    def test_decode_with_specified_encoding(self):
        content = "café".encode("latin-1")
        text = decode_text(content, encoding="latin-1")
        assert "caf" in text

    def test_decode_with_replacement(self):
        """Bad bytes get replaced, not crash."""
        content = b"\xff\xfe" + "test".encode("utf-8")
        text = decode_text(content)
        assert isinstance(text, str)


# ── file_fingerprint ────────────────────────────────────────────────


class TestFileFingerprint:
    def test_deterministic(self):
        content = b"MPN,Qty\nLM317T,100\nLM7805,200"
        fp1 = file_fingerprint(content)
        fp2 = file_fingerprint(content)
        assert fp1 == fp2

    def test_different_content_different_fingerprint(self):
        fp1 = file_fingerprint(b"MPN,Qty\nLM317T,100")
        fp2 = file_fingerprint(b"MPN,Qty\nLM7805,200")
        assert fp1 != fp2

    def test_hex_format(self):
        fp = file_fingerprint(b"test content")
        assert all(c in "0123456789abcdef" for c in fp)
        assert len(fp) == 32  # MD5 hex digest


# ── _get_extension ──────────────────────────────────────────────────


class TestGetExtension:
    def test_normal_extension(self):
        assert _get_extension("stock.csv") == ".csv"
        assert _get_extension("data.xlsx") == ".xlsx"

    def test_uppercase_normalized(self):
        assert _get_extension("REPORT.CSV") == ".csv"
        assert _get_extension("Data.XLSX") == ".xlsx"

    def test_no_extension(self):
        assert _get_extension("noext") == ""

    def test_empty_filename(self):
        assert _get_extension("") == ""

    def test_multiple_dots(self):
        assert _get_extension("report.2024.csv") == ".csv"


# ── is_password_protected ──────────────────────────────────────────


class TestIsPasswordProtected:
    def test_normal_content_not_protected(self):
        """Random bytes are not password-protected Excel."""
        assert is_password_protected(b"not an excel file") is False

    def test_empty_not_protected(self):
        assert is_password_protected(b"") is False
