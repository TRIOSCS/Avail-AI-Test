"""test_datasheet_capture_nightly.py — Coverage boost for
app/services/datasheet_capture.py.

Targets missing lines: _is_safe_url (all branches), download_pdf (redirect loop,
404, size cap, non-PDF content), pdf_contains_mpn (short key, parse fail, found),
find_datasheet_url (connector url, TESTING env guard), capture_datasheet (already has
datasheets, card=None and resolve returns None).

Called by: pytest
Depends on: tests/conftest.py (db_session)
"""

from __future__ import annotations

import os
import socket

os.environ["TESTING"] = "1"
os.environ["RATE_LIMIT_ENABLED"] = "false"

from unittest.mock import AsyncMock, MagicMock, patch

from app.services.datasheet_capture import (
    MAX_DATASHEET_BYTES,
    _is_safe_url,
    download_pdf,
    find_datasheet_url,
    pdf_contains_mpn,
)

# ══════════════════════════════════════════════════════════════════════
# _is_safe_url
# ══════════════════════════════════════════════════════════════════════


class TestIsSafeUrl:
    def test_public_ip_returns_true(self, monkeypatch):
        """Hostname resolves to a public IP → True."""
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *a, **kw: [(None, None, None, None, ("8.8.8.8", 443))],
        )
        assert _is_safe_url("https://example.com/file.pdf") is True

    def test_private_ip_returns_false(self, monkeypatch):
        """Hostname resolves to 192.168.x.x (private) → False."""
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *a, **kw: [(None, None, None, None, ("192.168.1.1", 80))],
        )
        assert _is_safe_url("http://internal.local/file.pdf") is False

    def test_loopback_ip_returns_false(self, monkeypatch):
        """Hostname resolves to 127.0.0.1 (loopback) → False."""
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *a, **kw: [(None, None, None, None, ("127.0.0.1", 80))],
        )
        assert _is_safe_url("http://localhost/evil.pdf") is False

    def test_non_http_scheme_returns_false(self, monkeypatch):
        """ftp:// scheme → False (no DNS lookup needed)."""
        # DNS should not be called; we monkeypatch it anyway to be safe
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [])
        assert _is_safe_url("ftp://files.example.com/doc.pdf") is False

    def test_no_hostname_returns_false(self, monkeypatch):
        """Malformed URL with no hostname → False."""
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [])
        assert _is_safe_url("http:///path/only") is False

    def test_dns_failure_returns_false(self, monkeypatch):
        """socket.getaddrinfo raises (DNS failure) → False."""
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("Name not found")),
        )
        assert _is_safe_url("https://nonexistent.example.invalid/file.pdf") is False

    def test_link_local_ip_returns_false(self, monkeypatch):
        """169.254.x.x (link-local) → False."""
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *a, **kw: [(None, None, None, None, ("169.254.1.1", 80))],
        )
        assert _is_safe_url("http://metadata.internal/data") is False


# ══════════════════════════════════════════════════════════════════════
# download_pdf
# ══════════════════════════════════════════════════════════════════════


class TestDownloadPdf:
    async def test_empty_url_returns_none(self):
        """Empty string url → None immediately."""
        result = await download_pdf("")
        assert result is None

    async def test_unsafe_url_returns_none(self):
        """_is_safe_url returns False → download blocked, returns None."""
        with patch("app.services.datasheet_capture._is_safe_url", return_value=False):
            result = await download_pdf("http://192.168.1.1/evil.pdf")
        assert result is None

    async def test_redirect_loop_returns_none(self):
        """Continuous 301 redirects exhaust MAX_REDIRECTS → None."""
        redirect_resp = MagicMock(
            status_code=301,
            headers={"location": "http://redirect.example.com/next.pdf"},
        )
        with (
            patch("app.services.datasheet_capture._is_safe_url", return_value=True),
            patch("app.services.datasheet_capture.http") as mock_http,
        ):
            mock_http.get = AsyncMock(return_value=redirect_resp)
            result = await download_pdf("http://redirect.example.com/start.pdf")
        assert result is None

    async def test_non_200_status_returns_none(self):
        """Status 404 → None."""
        resp = MagicMock(status_code=404, content=b"", headers={})
        with (
            patch("app.services.datasheet_capture._is_safe_url", return_value=True),
            patch("app.services.datasheet_capture.http") as mock_http,
        ):
            mock_http.get = AsyncMock(return_value=resp)
            result = await download_pdf("http://example.com/missing.pdf")
        assert result is None

    async def test_content_too_large_returns_none(self):
        """Content exceeds MAX_DATASHEET_BYTES → None."""
        big_content = b"%PDF-" + b"x" * (MAX_DATASHEET_BYTES + 1)
        resp = MagicMock(
            status_code=200,
            content=big_content,
            headers={"content-type": "application/pdf"},
        )
        with (
            patch("app.services.datasheet_capture._is_safe_url", return_value=True),
            patch("app.services.datasheet_capture.http") as mock_http,
        ):
            mock_http.get = AsyncMock(return_value=resp)
            result = await download_pdf("http://example.com/huge.pdf")
        assert result is None

    async def test_html_content_type_returns_none(self):
        """Response body is HTML (not PDF) → None."""
        html_bytes = b"<html><body>Not a PDF</body></html>"
        resp = MagicMock(
            status_code=200,
            content=html_bytes,
            headers={"content-type": "text/html"},
        )
        with (
            patch("app.services.datasheet_capture._is_safe_url", return_value=True),
            patch("app.services.datasheet_capture.http") as mock_http,
        ):
            mock_http.get = AsyncMock(return_value=resp)
            result = await download_pdf("http://example.com/page.html")
        assert result is None

    async def test_pdf_content_returns_bytes(self):
        """Valid PDF content → bytes returned."""
        pdf_bytes = b"%PDF-1.4 minimal content"
        resp = MagicMock(
            status_code=200,
            content=pdf_bytes,
            headers={"content-type": "application/pdf"},
        )
        with (
            patch("app.services.datasheet_capture._is_safe_url", return_value=True),
            patch("app.services.datasheet_capture.http") as mock_http,
        ):
            mock_http.get = AsyncMock(return_value=resp)
            result = await download_pdf("https://example.com/datasheet.pdf")
        assert result == pdf_bytes

    async def test_redirect_missing_location_returns_none(self):
        """301 with no Location header → None."""
        redirect_resp = MagicMock(
            status_code=301,
            headers={},  # no location
        )
        with (
            patch("app.services.datasheet_capture._is_safe_url", return_value=True),
            patch("app.services.datasheet_capture.http") as mock_http,
        ):
            mock_http.get = AsyncMock(return_value=redirect_resp)
            result = await download_pdf("https://example.com/redirect.pdf")
        assert result is None

    async def test_http_exception_returns_none(self):
        """http.get raises an exception → None, no crash."""
        with (
            patch("app.services.datasheet_capture._is_safe_url", return_value=True),
            patch("app.services.datasheet_capture.http") as mock_http,
        ):
            mock_http.get = AsyncMock(side_effect=ConnectionError("timeout"))
            result = await download_pdf("https://example.com/datasheet.pdf")
        assert result is None


# ══════════════════════════════════════════════════════════════════════
# pdf_contains_mpn
# ══════════════════════════════════════════════════════════════════════


class TestPdfContainsMpn:
    def test_short_key_returns_false(self):
        """MPN normalizes to < 4 chars → False without parsing."""
        # "AB" normalizes to "ab" (2 chars) — below the 4-char minimum
        result = pdf_contains_mpn(b"%PDF-1.4 dummy", "AB")
        assert result is False

    def test_parse_failure_returns_false(self):
        """pypdf raises on bad bytes → False, no crash."""
        result = pdf_contains_mpn(b"not a pdf at all", "LM317T")
        assert result is False

    def test_mpn_found_in_pdf(self):
        """Mock PdfReader finds MPN text → True."""
        mock_page = MagicMock()
        mock_page.extract_text.return_value = "Datasheet for LM317T voltage regulator"
        mock_reader = MagicMock()
        mock_reader.pages = [mock_page]

        # PdfReader is lazily imported inside pdf_contains_mpn via `from pypdf import PdfReader`
        with patch("pypdf.PdfReader", return_value=mock_reader):
            result = pdf_contains_mpn(b"%PDF-1.4 fake", "LM317T")
        assert result is True

    def test_mpn_not_in_pdf(self):
        """MPN text absent from PDF → False."""
        mock_page = MagicMock()
        mock_page.extract_text.return_value = "Some unrelated component datasheet"
        mock_reader = MagicMock()
        mock_reader.pages = [mock_page]

        with patch("pypdf.PdfReader", return_value=mock_reader):
            result = pdf_contains_mpn(b"%PDF-1.4 fake", "NE555")
        assert result is False


# ══════════════════════════════════════════════════════════════════════
# find_datasheet_url
# ══════════════════════════════════════════════════════════════════════


class TestFindDatasheetUrl:
    async def test_connector_url_returned_directly(self):
        """Card with datasheet_url → returns (url, 'connector') without Claude."""
        card = MagicMock()
        card.datasheet_url = "https://ti.com/LM317T.pdf"
        result = await find_datasheet_url(card, "LM317T")
        assert result == ("https://ti.com/LM317T.pdf", "connector")

    async def test_testing_env_returns_none_without_connector_url(self):
        """TESTING=1 and no connector URL → returns None (no Claude call)."""
        # TESTING is already 1 in this test suite
        card = MagicMock()
        card.datasheet_url = None
        result = await find_datasheet_url(card, "LM317T")
        assert result is None

    async def test_none_card_with_testing_env_returns_none(self):
        """card=None and TESTING=1 → returns None."""
        result = await find_datasheet_url(None, "LM317T")
        assert result is None


# ══════════════════════════════════════════════════════════════════════
# capture_datasheet — additional branches
# ══════════════════════════════════════════════════════════════════════


class TestCaptureDatasheetAdditionalBranches:
    async def test_capture_skips_card_with_existing_datasheets(self, db_session):
        """Card already has datasheets → find_datasheet_url never called."""
        from app.services import datasheet_capture as dc

        # Create a mock card that already has datasheets
        mock_card = MagicMock()
        mock_card.datasheets = [MagicMock()]  # non-empty — should short-circuit
        mock_card.datasheet_searched_at = None

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = mock_card
        mock_db.get.return_value = mock_card

        with (
            patch("app.services.datasheet_capture.SessionLocal", return_value=mock_db),
            patch("app.services.datasheet_capture.find_datasheet_url", new_callable=AsyncMock) as mock_find,
        ):
            await dc.capture_datasheet("LM317T", 1)

        mock_find.assert_not_called()

    async def test_capture_no_card_resolve_returns_none(self, db_session):
        """card=None and resolve_material_card also returns None → no datasheet stored."""
        from app.services import datasheet_capture as dc

        # No card in DB — query returns None
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None

        with (
            patch("app.services.datasheet_capture.SessionLocal", return_value=mock_db),
            patch(
                "app.services.datasheet_capture.find_datasheet_url",
                AsyncMock(return_value=("https://example.com/file.pdf", "web")),
            ),
            patch(
                "app.services.datasheet_capture.download_pdf",
                AsyncMock(return_value=b"%PDF-1.4 data"),
            ),
            patch("app.services.datasheet_capture.pdf_contains_mpn", return_value=True),
            # resolve_material_card is imported lazily: from ..search_service import resolve_material_card
            patch("app.search_service.resolve_material_card", return_value=None),
        ):
            await dc.capture_datasheet("UNKNOWNMPN9999", 1)

        # No datasheets stored (mock_db.add never called with a datasheet)
        mock_db.add.assert_not_called()
