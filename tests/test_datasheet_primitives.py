import os

os.environ["TESTING"] = "1"
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.datasheet_capture import _is_safe_url, download_pdf, pdf_contains_mpn


def _pdf_with_text(text: str) -> bytes:
    """Build a minimal but structurally valid PDF that pypdf 6.x can parse.

    The brief's hand-crafted PDF lacked a startxref/xref table so pypdf
    6.13.3 raised PdfStreamError before extracting any text.  This generator
    produces the same logical content (a single page with a Type1 text
    operator) via a proper xref-table structure, which pypdf reliably reads.
    """
    text_b = text.encode()
    stream_data = b"BT /F1 12 Tf 10 100 Td (" + text_b + b") Tj ET"
    pdf: bytearray = bytearray()
    offsets = [0] * 6

    def w(data: bytes) -> None:
        pdf.extend(data)

    w(b"%PDF-1.4\n")

    offsets[1] = len(pdf)
    w(b"1 0 obj\n<</Type /Catalog /Pages 2 0 R>>\nendobj\n")

    offsets[2] = len(pdf)
    w(b"2 0 obj\n<</Type /Pages /Kids [3 0 R] /Count 1>>\nendobj\n")

    offsets[3] = len(pdf)
    w(
        b"3 0 obj\n<</Type /Page /Parent 2 0 R /MediaBox [0 0 200 200]"
        b" /Contents 4 0 R /Resources <</Font <</F1 5 0 R>>>>>>\nendobj\n"
    )

    offsets[4] = len(pdf)
    w(b"4 0 obj\n<</Length " + str(len(stream_data)).encode() + b">>\nstream\n")
    w(stream_data)
    w(b"\nendstream\nendobj\n")

    offsets[5] = len(pdf)
    w(b"5 0 obj\n<</Type /Font /Subtype /Type1 /BaseFont /Helvetica>>\nendobj\n")

    xref_start = len(pdf)
    w(b"xref\n0 6\n")
    w(b"0000000000 65535 f \n")
    for i in range(1, 6):
        w(f"{offsets[i]:010d} 00000 n \n".encode())
    w(b"trailer\n<</Size 6 /Root 1 0 R>>\nstartxref\n")
    w(str(xref_start).encode())
    w(b"\n%%EOF\n")

    return bytes(pdf)


def test_pdf_contains_mpn_true():
    assert pdf_contains_mpn(_pdf_with_text("Part 17P9905 Hard Drive"), "17P9905") is True


def test_pdf_contains_mpn_false_for_wrong_part():
    assert pdf_contains_mpn(_pdf_with_text("Part 1300940294 component"), "17P9905") is False


def test_pdf_contains_mpn_handles_unparseable_bytes():
    assert pdf_contains_mpn(b"not a pdf", "17P9905") is False


# ── _is_safe_url tests ───────────────────────────────────────────────────────


def _mock_getaddrinfo(ip: str):
    """Return a getaddrinfo stub that resolves any hostname to the given IP."""
    return MagicMock(return_value=[(None, None, None, None, (ip, 80))])


def test_is_safe_url_rejects_loopback():
    with patch("app.services.datasheet_capture.socket.getaddrinfo", _mock_getaddrinfo("127.0.0.1")):
        assert _is_safe_url("http://127.0.0.1/x") is False


def test_is_safe_url_rejects_link_local_metadata():
    with patch("app.services.datasheet_capture.socket.getaddrinfo", _mock_getaddrinfo("169.254.169.254")):
        assert _is_safe_url("http://169.254.169.254/latest/meta-data/") is False


def test_is_safe_url_rejects_private_rfc1918():
    with patch("app.services.datasheet_capture.socket.getaddrinfo", _mock_getaddrinfo("10.0.0.5")):
        assert _is_safe_url("http://10.0.0.5/x") is False


def test_is_safe_url_rejects_non_http_scheme():
    # No getaddrinfo patch needed — scheme check runs first
    assert _is_safe_url("ftp://example.com/x") is False


def test_is_safe_url_rejects_no_host():
    assert _is_safe_url("http:///path") is False


def test_is_safe_url_accepts_public_host():
    with patch("app.services.datasheet_capture.socket.getaddrinfo", _mock_getaddrinfo("93.184.216.34")):
        assert _is_safe_url("https://example.com/datasheet.pdf") is True


# ── download_pdf SSRF tests ──────────────────────────────────────────────────


@pytest.mark.anyio
async def test_download_pdf_blocks_unsafe_url():
    """download_pdf must return None without calling http.get for an unsafe URL."""
    with (
        patch("app.services.datasheet_capture._is_safe_url", return_value=False),
        patch("app.services.datasheet_capture.http") as mock_http,
    ):
        result = await download_pdf("http://169.254.169.254/latest/meta-data/")
    assert result is None
    mock_http.get.assert_not_called()


@pytest.mark.anyio
async def test_download_pdf_follows_safe_redirect():
    """download_pdf follows a 3xx to a safe host and returns PDF bytes."""
    redirect_resp = MagicMock()
    redirect_resp.status_code = 302
    redirect_resp.headers = {"location": "https://cdn.example.com/sheet.pdf"}

    pdf_bytes = b"%PDF-1.4 fake content"
    final_resp = MagicMock()
    final_resp.status_code = 200
    final_resp.content = pdf_bytes
    final_resp.headers = {"content-type": "application/pdf"}

    mock_get = AsyncMock(side_effect=[redirect_resp, final_resp])

    with (
        patch("app.services.datasheet_capture._is_safe_url", return_value=True),
        patch("app.services.datasheet_capture.http") as mock_http,
    ):
        mock_http.get = mock_get
        result = await download_pdf("https://example.com/redirect")

    assert result == pdf_bytes
    assert mock_get.call_count == 2


@pytest.mark.anyio
async def test_download_pdf_blocks_redirect_to_unsafe_host():
    """download_pdf must return None when a redirect target is an unsafe host."""
    redirect_resp = MagicMock()
    redirect_resp.status_code = 302
    redirect_resp.headers = {"location": "http://10.0.0.1/internal"}

    # First call (_is_safe_url for the original URL) → True
    # Second call (_is_safe_url for the redirect target) → False
    safe_side_effects = [True, False]

    mock_get = AsyncMock(return_value=redirect_resp)

    with (
        patch("app.services.datasheet_capture._is_safe_url", side_effect=safe_side_effects),
        patch("app.services.datasheet_capture.http") as mock_http,
    ):
        mock_http.get = mock_get
        result = await download_pdf("https://example.com/redirect")

    assert result is None
    # Only one fetch (the original); the redirect target must never be fetched
    assert mock_get.call_count == 1
