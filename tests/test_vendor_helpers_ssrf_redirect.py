"""Regression tests for SSRF-via-redirect in scrape_website_contacts.

is_private_url only validates the INITIAL url; the fetch must not follow a 3xx to an
unvalidated host. These tests prove a vendor page that 302s to an internal host / cloud
metadata (169.254.169.254) is NOT fetched, while a redirect to a public host still works.

Tests: app.utils.vendor_helpers._safe_get / scrape_website_contacts
"""

import asyncio
from unittest.mock import patch
from urllib.parse import urlparse

from app.utils.vendor_helpers import scrape_website_contacts

METADATA_URL = "http://169.254.169.254/latest/meta-data/"


class _FakeResp:
    def __init__(self, status_code, *, headers=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text


def _host_is_private(url: str) -> bool:
    """Stand-in for is_private_url: only the cloud-metadata IP counts as private."""
    return (urlparse(url).hostname or "") == "169.254.169.254"


def test_redirect_to_internal_host_not_followed():
    """A 302 to an internal/metadata host is blocked, not followed to the internal
    service."""
    fetched: list[str] = []

    async def fake_get(url, headers=None, timeout=None):
        fetched.append(url)
        # Vendor page issues a 302 pointing at the cloud-metadata endpoint.
        return _FakeResp(302, headers={"location": METADATA_URL})

    class _FakeClient:
        get = staticmethod(fake_get)

    async def _run():
        with (
            patch("app.cache.intel_cache.get_cached", return_value=None),
            patch("app.cache.intel_cache.set_cached"),
            patch("app.utils.vendor_helpers.is_private_url", side_effect=_host_is_private),
            patch("app.utils.vendor_helpers.http", _FakeClient),
        ):
            return await scrape_website_contacts("https://evil-vendor.com")

    result = asyncio.get_event_loop().run_until_complete(_run())

    # Scraper returns empty — it never reached the internal metadata service.
    assert result == {"emails": [], "phones": []}
    # The internal/metadata host must never have been requested.
    assert all(urlparse(u).hostname != "169.254.169.254" for u in fetched), fetched
    # The vendor pages themselves were attempted (so this isn't a no-op pass).
    assert fetched, "expected the public vendor pages to be fetched"


def test_redirect_to_public_host_is_followed():
    """A 302 to another PUBLIC host is followed (no over-blocking) and content
    extracted."""
    final_html = '<a href="mailto:sales@vendor.com">Email</a>'
    fetched: list[str] = []

    async def fake_get(url, headers=None, timeout=None):
        fetched.append(url)
        if "vendor.com" in (urlparse(url).hostname or "") and "/landing" not in url:
            # First hop: redirect to a public landing page on a public host.
            return _FakeResp(302, headers={"location": "https://www.vendor.com/landing"})
        return _FakeResp(200, text=final_html)

    class _FakeClient:
        get = staticmethod(fake_get)

    async def _run():
        with (
            patch("app.cache.intel_cache.get_cached", return_value=None),
            patch("app.cache.intel_cache.set_cached"),
            # No host here is private.
            patch("app.utils.vendor_helpers.is_private_url", return_value=False),
            patch("app.utils.vendor_helpers.http", _FakeClient),
        ):
            return await scrape_website_contacts("https://vendor.com")

    result = asyncio.get_event_loop().run_until_complete(_run())

    assert "sales@vendor.com" in result["emails"]
    # The public landing page was followed.
    assert any("/landing" in u for u in fetched), fetched
