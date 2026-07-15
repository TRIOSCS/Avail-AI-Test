"""Characterization pins for the two legacy domain-extractor sites vs
parse_website_domain.

What: the measured, documented consolidation decision for the two legacy extractors
      (PR #714 deferral):

      Site A — app.utils.vendor_helpers.scrape_website_contacts cache key: MIGRATED
      onto app.utils.normalization.parse_website_domain (with a raw-string fallback
      for unparseable input). A frozen replica of the removed inline extractor
      verifies that every common URL class keeps its pre-consolidation cache key
      (no cache-miss wave there) and that each intentionally changed key class is
      exactly the documented set of inline key-quality bugs (uppercase scheme,
      blanket www-replace, userinfo/port/query leakage).

      Site B — app.enrichment_service._clean_domain: deliberately NOT migrated.
      These tests pin its current behavior (including the classes where it diverges
      from parse_website_domain) so an accidental change trips CI, and assert the
      documented divergence is still real — if the two ever converge, the decision
      in _clean_domain's docstring must be revisited.

Called by: pytest
Depends on: app/utils/vendor_helpers.py (scrape_website_contacts),
      app/utils/normalization.py (parse_website_domain),
      app/enrichment_service.py (_clean_domain), app/cache/intel_cache.py (patched)
"""

from unittest.mock import patch

import pytest

from app.enrichment_service import _clean_domain
from app.utils.normalization import parse_website_domain
from app.utils.vendor_helpers import scrape_website_contacts


def _old_inline_scrape_key_domain(raw_url: str) -> str:
    """Frozen replica of scrape_website_contacts' pre-consolidation inline extractor
    (removed in this PR) — kept here to characterize cache-key stability."""
    url = raw_url
    if not url.startswith("http"):
        url = "https://" + url
    url = url.rstrip("/")
    try:
        return url.split("//", 1)[1].split("/")[0].lower().replace("www.", "")
    except IndexError:
        return raw_url.lower()


def _new_scrape_key_domain(raw_url: str) -> str:
    """The consolidated derivation now used by scrape_website_contacts."""
    return parse_website_domain(raw_url) or raw_url.lower()


# ── Site A: scrape_website_contacts cache key ─────────────────────────

# Common URL classes: scheme presence, www, subdomains, paths, path+query,
# uppercase HOST, trailing slashes, bare domains, IPs — plus junk that falls back
# to the raw string. The consolidated key MUST match the old inline key for all
# of these (no cache-miss wave on the dominant real-world inputs).
_UNCHANGED_KEY_INPUTS = [
    "http://example.com",
    "https://example.com",
    "example.com",
    "https://www.example.com",
    "www.example.com",
    "WWW.EXAMPLE.COM",
    "https://shop.example.com",
    "https://wwwx.example.com",
    "https://example.com/contact",
    "example.com/about/team",
    "https://www.example.com/products/",
    "https://example.com/?utm=1",
    "https://example.com/page?q=hello&x=2",
    "Example.Com",
    "https://example.com/",
    "https://example.com///",
    "example.com.",
    "trio-scs.com",
    "digikey.co.uk",
    "http://192.168.1.10",
    # Junk: parse_website_domain rejects -> raw-string fallback == old fallback/passthrough,
    # so distinct junk inputs keep distinct keys.
    "not a url",
    "unknown",
    "httpfoo",
    "just-text",
    "example",
]

# Intentionally changed key classes — each old key was a key-quality bug in the
# inline extractor. (old key, new key) per input; the switch costs at most one
# 7-day cache-miss wave on these rare classes.
_CHANGED_KEY_CASES = [
    # Uppercase scheme: startswith("http") is case-sensitive, so EVERY such URL
    # collapsed onto the single shared key "scrape:https:".
    ("HTTPS://EXAMPLE.COM/PATH", "https:", "example.com"),
    # Blanket .replace("www.", "") mangled hosts containing the substring.
    ("https://sub.www.example.com", "sub.example.com", "sub.www.example.com"),
    ("wwww.example.com", "wexample.com", "wwww.example.com"),
    # Query string leaked into the key when the URL had no path.
    ("https://example.com?utm=1", "example.com?utm=1", "example.com"),
    # Ports leaked into the key.
    ("https://example.com:8080", "example.com:8080", "example.com"),
    ("http://example.com:80/contact", "example.com:80", "example.com"),
    # Userinfo leaked into the key.
    ("https://user@example.com", "user@example.com", "example.com"),
]


class TestScrapeCacheKeyStability:
    @pytest.mark.parametrize("raw_url", _UNCHANGED_KEY_INPUTS)
    def test_common_classes_keep_their_pre_consolidation_key(self, raw_url):
        assert _new_scrape_key_domain(raw_url) == _old_inline_scrape_key_domain(raw_url)

    @pytest.mark.parametrize(("raw_url", "old_key", "new_key"), _CHANGED_KEY_CASES)
    def test_changed_classes_are_exactly_the_documented_inline_bugs(self, raw_url, old_key, new_key):
        assert _old_inline_scrape_key_domain(raw_url) == old_key
        assert _new_scrape_key_domain(raw_url) == new_key

    @pytest.mark.parametrize(
        ("raw_url", "expected_key"),
        [
            ("https://www.acme.com/contact", "scrape:acme.com"),
            ("acme.com", "scrape:acme.com"),
            ("HTTPS://ACME.COM/PATH", "scrape:acme.com"),
            ("not a url", "scrape:not a url"),
        ],
    )
    async def test_scrape_website_contacts_uses_the_consolidated_key(self, raw_url, expected_key):
        """Drive the real code path: a cache hit returns early, capturing the key."""
        captured: list[str] = []
        sentinel = {"emails": ["sales@acme.com"], "phones": []}

        def _capture(key):
            captured.append(key)
            return sentinel

        with patch("app.cache.intel_cache.get_cached", side_effect=_capture):
            result = await scrape_website_contacts(raw_url)

        assert captured == [expected_key]
        assert result == sentinel


# ── Site B: enrichment_service._clean_domain (deliberately NOT migrated) ──


class TestCleanDomainCharacterization:
    """Pin _clean_domain's current behavior — its output feeds provider lookups and
    normalize_company_output's persisted out["domain"], so changes must be
    deliberate."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # Classes where it agrees with parse_website_domain.
            ("https://example.com", "example.com"),
            ("http://www.example.com", "example.com"),
            ("WWW.EXAMPLE.COM", "example.com"),
            ("https://shop.example.com", "shop.example.com"),
            ("example.com/about/team", "example.com"),
            ("https://www.example.com/products/", "example.com"),
            ("HTTPS://EXAMPLE.COM/PATH", "example.com"),
            ("https://example.com///", "example.com"),
            ("digikey.co.uk", "digikey.co.uk"),
            # Divergent classes (documented in _clean_domain's docstring).
            ("example.com.", "example.com"),  # trailing dot stripped (shared keeps it)
            ("https://example.com:8080", "example.com:8080"),  # port kept (shared strips)
            ("user@example.com", "user@example.com"),  # userinfo kept (shared strips)
            ("https://example.com?utm=1", "example.com?utm=1"),  # no-path query kept
            ("unknown", "unknown"),  # junk passes through (shared rejects to "")
            ("N/A", "n"),  # junk passes through, split on "/" (shared rejects to "")
            ("example", "example"),  # no-TLD token accepted (shared rejects to "")
            ("", ""),
        ],
    )
    def test_pins_current_behavior(self, raw, expected):
        assert _clean_domain(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "example.com.",
            "https://example.com:8080",
            "user@example.com",
            "unknown",
            "N/A",
            "example",
        ],
    )
    def test_documented_divergence_from_shared_extractor_is_still_real(self, raw):
        """If these ever converge, the not-migrated decision in _clean_domain's
        docstring must be revisited (and this file updated)."""
        assert _clean_domain(raw) != parse_website_domain(raw)
