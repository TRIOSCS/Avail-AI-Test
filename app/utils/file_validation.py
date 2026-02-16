"""File validation — type checking and encoding detection.

Hardening: H3 (file type validation), H4 (encoding detection).

Uses `filetype` library for magic-byte validation (don't trust extensions)
and `charset-normalizer` for detecting encoding of CSV/TSV files from
international vendors.
"""

import hashlib
import logging

log = logging.getLogger("avail.file_validation")

# Maximum file size for processing (10 MB)
MAX_FILE_SIZE = 10 * 1024 * 1024

# Allowed MIME types for attachment parsing
ALLOWED_TYPES = {
    # Excel
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel": "xls",
    # CSV/TSV (detected by extension since they're plain text)
    "text/csv": "csv",
    "text/plain": "txt",
    "text/tab-separated-values": "tsv",
}

# File extensions we process
PROCESSABLE_EXTENSIONS = {".xlsx", ".xls", ".csv", ".tsv"}


def validate_file(content: bytes, filename: str) -> tuple[bool, str | None]:
    """Validate a file's actual type and encoding.

    Returns:
        (is_valid, file_type_or_reason) — e.g. (True, "xlsx") or (False, "Empty file")
    """
    # Size check
    if len(content) > MAX_FILE_SIZE:
        return False, f"File too large ({len(content)} bytes, max {MAX_FILE_SIZE})"

    if len(content) == 0:
        return False, "Empty file"

    ext = _get_extension(filename)

    # H3: Magic-byte validation for binary files
    try:
        import filetype as ft

        kind = ft.guess(content)
        if kind:
            mime = kind.mime
            if mime in ALLOWED_TYPES:
                return True, ALLOWED_TYPES[mime]
            # Binary file with wrong type
            if ext in (".xlsx", ".xls"):
                return False, f"File claims to be {ext} but detected as {mime}"
    except ImportError:
        log.warning("filetype library not installed — falling back to extension check")

    # Text files (CSV/TSV) — can't detect by magic bytes, use extension + encoding
    if ext in (".csv", ".tsv", ".txt"):
        encoding = detect_encoding(content)
        if encoding:
            file_type = "csv" if ext in (".csv", ".txt") else "tsv"
            return True, file_type
        return False, "Could not detect text encoding"

    # Fallback: trust extension for xlsx if filetype wasn't available
    if ext in PROCESSABLE_EXTENSIONS:
        return True, ext.lstrip(".")

    return False, f"Unsupported file type: {ext}"


def detect_encoding(content: bytes) -> str | None:
    """H4: Detect text encoding using charset-normalizer.

    Handles vendor files from international sources (Asian, European encodings).
    Falls back to utf-8-sig if detection fails.
    """
    try:
        from charset_normalizer import from_bytes

        results = from_bytes(content)
        best = results.best()
        if best:
            encoding = best.encoding
            log.debug(f"Detected encoding: {encoding}")
            return encoding
    except ImportError:
        log.warning("charset-normalizer not installed — using utf-8-sig fallback")
    except Exception as e:
        log.debug(f"Encoding detection error: {e}")

    # Fallback: try common encodings
    for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252", "gb2312", "shift_jis"):
        try:
            content.decode(enc)
            return enc
        except (UnicodeDecodeError, LookupError):
            continue

    return "utf-8-sig"  # Last resort — will replace bad chars


def decode_text(content: bytes, encoding: str | None = None) -> str:
    """Decode bytes to string using detected or specified encoding."""
    enc = encoding or detect_encoding(content) or "utf-8-sig"
    return content.decode(enc, errors="replace")


def file_fingerprint(content: bytes, rows: int = 10) -> str:
    """Generate a fingerprint from the first N rows of a file.

    Used for column mapping cache lookup — same vendor + same layout = cache hit.
    """
    # Use first 4KB as fingerprint source (covers headers + first rows)
    sample = content[:4096]
    return hashlib.md5(sample, usedforsecurity=False).hexdigest()


def _get_extension(filename: str) -> str:
    """Get lowercase file extension."""
    if not filename:
        return ""
    parts = filename.lower().rsplit(".", 1)
    return f".{parts[-1]}" if len(parts) > 1 else ""


def is_password_protected(content: bytes) -> bool:
    """Check if an Excel file appears to be password-protected."""
    try:
        import openpyxl
        import io

        openpyxl.load_workbook(io.BytesIO(content), read_only=True)
        return False
    except Exception as e:
        err_str = str(e).lower()
        if "password" in err_str or "encrypted" in err_str:
            return True
        return False  # Other errors aren't password-related
