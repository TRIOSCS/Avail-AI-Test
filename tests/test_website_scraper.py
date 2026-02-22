"""
test_website_scraper.py — Tests for website_scraper.py

Pure functions tested directly; scraping functions mock httpx.

Called by: pytest
Depends on: app/services/website_scraper.py, conftest.py
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.website_scraper import (
    EMAIL_RE,
    _classify_email,
    _scrape_vendor,
)


# ═══════════════════════════════════════════════════════════════════════
#  _classify_email — pure, no mock
# ═══════════════════════════════════════════════════════════════════════


class TestClassifyEmail:
    def test_generic_noreply(self):
        assert _classify_email("noreply@example.com", "/") == 40

    def test_generic_support(self):
        assert _classify_email("support@example.com", "/") == 40

    def test_generic_info(self):
        assert _classify_email("info@example.com", "/") == 40

    def test_generic_marketing(self):
        assert _classify_email("marketing@example.com", "/") == 40

    def test_contact_page(self):
        assert _classify_email("john@example.com", "/contact-us") == 70

    def test_about_page(self):
        assert _classify_email("john@example.com", "/about") == 60

    def test_homepage(self):
        assert _classify_email("john@example.com", "/") == 55

    def test_person_email_on_contact(self):
        assert _classify_email("jane.doe@example.com", "/contact") == 70


# ═══════════════════════════════════════════════════════════════════════
#  EMAIL_RE — pure regex tests
# ═══════════════════════════════════════════════════════════════════════


class TestEmailRegex:
    def test_standard_email(self):
        matches = EMAIL_RE.findall("Contact us at sales@example.com")
        assert "sales@example.com" in matches

    def test_multiple_emails(self):
        text = "Email sales@ex.com or info@ex.com"
        matches = EMAIL_RE.findall(text)
        assert len(matches) == 2

    def test_no_email(self):
        matches = EMAIL_RE.findall("No email here")
        assert matches == []

    def test_filters_short_tld(self):
        """TLDs must be at least 2 chars."""
        matches = EMAIL_RE.findall("bad@example.x")
        assert matches == []


# ═══════════════════════════════════════════════════════════════════════
#  _scrape_vendor — mock httpx
# ═══════════════════════════════════════════════════════════════════════


def _make_response(text, status_code=200, content_type="text/html"):
    """Create a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = text
    resp.headers = {"content-type": content_type}
    return resp


class TestScrapeVendor:
    @pytest.mark.asyncio
    async def test_extracts_unique_emails(self):
        html = "<html>Email us: alice@vendor.com or bob@vendor.com</html>"
        client = AsyncMock()
        client.get = AsyncMock(return_value=_make_response(html))

        results = await _scrape_vendor(client, "https://vendor.com")
        emails = {r["email"] for r in results}
        assert "alice@vendor.com" in emails
        assert "bob@vendor.com" in emails

    @pytest.mark.asyncio
    async def test_dedup_across_pages(self):
        """Same email on multiple pages → only one result."""
        html = "<html>Contact: sales@vendor.com</html>"
        client = AsyncMock()
        client.get = AsyncMock(return_value=_make_response(html))

        results = await _scrape_vendor(client, "https://vendor.com")
        emails = [r["email"] for r in results]
        assert emails.count("sales@vendor.com") == 1

    @pytest.mark.asyncio
    async def test_filters_image_extensions(self):
        html = "<html>logo@vendor.png user@vendor.jpg real@vendor.com</html>"
        client = AsyncMock()
        client.get = AsyncMock(return_value=_make_response(html))

        results = await _scrape_vendor(client, "https://vendor.com")
        emails = {r["email"] for r in results}
        assert "real@vendor.com" in emails
        assert "logo@vendor.png" not in emails
        assert "user@vendor.jpg" not in emails

    @pytest.mark.asyncio
    async def test_http_errors_skipped(self):
        client = AsyncMock()
        client.get = AsyncMock(return_value=_make_response("", status_code=500))

        results = await _scrape_vendor(client, "https://vendor.com")
        assert results == []

    @pytest.mark.asyncio
    async def test_url_normalization(self):
        """URLs without scheme get https:// prepended."""
        html = "<html>sales@vendor.com</html>"
        client = AsyncMock()
        client.get = AsyncMock(return_value=_make_response(html))

        results = await _scrape_vendor(client, "vendor.com")
        assert len(results) >= 1
        # Verify the client was called with https://
        first_url = client.get.call_args_list[0][0][0]
        assert first_url.startswith("https://")

    @pytest.mark.asyncio
    async def test_connection_error_returns_empty(self):
        client = AsyncMock()
        client.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))

        results = await _scrape_vendor(client, "https://vendor.com")
        assert results == []
