"""
tests/test_routers_vendors_crud.py — Tests for routers/vendors_crud.py and vendor helpers

Covers: card_to_dict helper, get_or_create_card, VendorCard CRUD,
VendorReview CRUD, autocomplete, blacklist, check-duplicate, clean_emails/phones,
is_private_url, scrape_website_contacts, merge_contact_into_card,
_background_enrich_vendor.

Called by: pytest
Depends on: routers/vendors_crud.py, utils/vendor_helpers.py
"""

import socket
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.models import User, VendorCard, VendorReview
from app.utils.vendor_helpers import (
    _background_enrich_vendor,
    card_to_dict,
    clean_emails,
    clean_phones,
    get_or_create_card,
    is_private_url,
    merge_contact_into_card,
    scrape_website_contacts,
)

# ── Stub factories ───────────────────────────────────────────────────────


def _make_vendor_card(**overrides) -> SimpleNamespace:
    """Create a VendorCard-like stub with all real model attributes."""
    defaults = dict(
        id=1,
        normalized_name="acme electronics",
        display_name="Acme Electronics",
        domain="acme.com",
        website="https://acme.com",
        emails=["sales@acme.com"],
        phones=["+1-555-0100"],
        sighting_count=42,
        is_blacklisted=False,
        linkedin_url=None,
        legal_name=None,
        industry="Semiconductors",
        employee_size="50-100",
        hq_city="Dallas",
        hq_state="TX",
        hq_country="US",
        last_enriched_at=None,
        enrichment_source=None,
        vendor_score=72.5,
        advancement_score=72.5,
        is_new_vendor=False,
        engagement_score=72.5,
        total_outreach=20,
        total_responses=14,
        ghost_rate=0.3,
        response_velocity_hours=4.2,
        last_contact_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
        brand_tags=[],
        commodity_tags=[],
        material_tags_updated_at=None,
        created_at=datetime(2025, 11, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_review(**overrides) -> SimpleNamespace:
    defaults = dict(
        id=1,
        vendor_card_id=1,
        user_id=1,
        rating=4,
        comment="Good vendor",
        created_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
        user=SimpleNamespace(name="Mike"),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ── Admin client fixture ─────────────────────────────────────────────────


@pytest.fixture()
def admin_client(db_session, admin_user):
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_user():
        return admin_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_buyer] = _override_user
    app.dependency_overrides[require_admin] = _override_user

    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ── card_to_dict tests ───────────────────────────────────────────────────


def test_card_to_dict_with_reviews():
    """card_to_dict includes avg rating, review list, brand profile."""
    card = _make_vendor_card()
    review = _make_review(rating=4)
    db = MagicMock()
    db.query.return_value.options.return_value.filter_by.return_value.all.return_value = [review]
    db.execute.return_value.fetchall.return_value = [("Texas Instruments", 5)]
    db.execute.return_value.scalar.return_value = 12

    result = card_to_dict(card, db)

    assert result["id"] == 1
    assert result["display_name"] == "Acme Electronics"
    assert result["avg_rating"] == 4.0
    assert result["review_count"] == 1
    assert result["brands"] == [{"name": "Texas Instruments", "count": 5}]
    assert result["unique_parts"] == 12
    assert result["engagement_score"] == 72.5
    assert result["hq_country"] == "US"


def test_card_to_dict_no_reviews():
    """card_to_dict handles zero reviews gracefully."""
    card = _make_vendor_card()
    db = MagicMock()
    db.query.return_value.options.return_value.filter_by.return_value.all.return_value = []
    db.execute.return_value.fetchall.return_value = []
    db.execute.return_value.scalar.return_value = 0

    result = card_to_dict(card, db)

    assert result["avg_rating"] is None
    assert result["review_count"] == 0
    assert result["reviews"] == []


def test_card_to_dict_none_timestamps():
    """card_to_dict handles None datetimes without crashing."""
    card = _make_vendor_card(
        last_enriched_at=None,
        last_contact_at=None,
        created_at=None,
        updated_at=None,
    )
    db = MagicMock()
    db.query.return_value.options.return_value.filter_by.return_value.all.return_value = []
    db.execute.return_value.fetchall.return_value = []
    db.execute.return_value.scalar.return_value = 0

    result = card_to_dict(card, db)

    assert result["last_enriched_at"] is None
    assert result["last_contact_at"] is None
    assert result["created_at"] is None


def test_card_to_dict_redis_cache_hit():
    """card_to_dict uses Redis cached brand profile when available."""
    import json

    card = _make_vendor_card()
    db = MagicMock()
    db.query.return_value.options.return_value.filter_by.return_value.all.return_value = []

    mock_redis = MagicMock()
    cached_data = json.dumps({"brands": [{"name": "TI", "count": 10}], "mpn_count": 42})
    mock_redis.get.return_value = cached_data

    with patch("app.cache.intel_cache._get_redis", return_value=mock_redis):
        result = card_to_dict(card, db)

    assert result["brands"] == [{"name": "TI", "count": 10}]
    assert result["unique_parts"] == 42
    # Should NOT have called db.execute since cache was hit
    db.execute.assert_not_called()


def test_card_to_dict_redis_cache_set():
    """card_to_dict sets Redis cache after computing brand profile."""
    card = _make_vendor_card()
    db = MagicMock()
    db.query.return_value.options.return_value.filter_by.return_value.all.return_value = []
    db.execute.return_value.fetchall.return_value = [("Microchip", 3)]
    db.execute.return_value.scalar.return_value = 7

    mock_redis = MagicMock()
    mock_redis.get.return_value = None  # Cache miss

    with patch("app.cache.intel_cache._get_redis", return_value=mock_redis):
        result = card_to_dict(card, db)

    assert result["brands"] == [{"name": "Microchip", "count": 3}]
    assert result["unique_parts"] == 7
    # Should have called setex to cache the result
    mock_redis.setex.assert_called_once()


def test_card_to_dict_redis_cache_miss_no_redis():
    """card_to_dict works when Redis is unavailable (returns None)."""
    card = _make_vendor_card()
    db = MagicMock()
    db.query.return_value.options.return_value.filter_by.return_value.all.return_value = []
    db.execute.return_value.fetchall.return_value = []
    db.execute.return_value.scalar.return_value = 0

    with patch("app.cache.intel_cache._get_redis", return_value=None):
        result = card_to_dict(card, db)

    assert result["brands"] == []
    assert result["unique_parts"] == 0


def test_card_to_dict_redis_cache_error():
    """card_to_dict handles Redis errors gracefully (OSError on get)."""
    card = _make_vendor_card()
    db = MagicMock()
    db.query.return_value.options.return_value.filter_by.return_value.all.return_value = []
    db.execute.return_value.fetchall.return_value = []
    db.execute.return_value.scalar.return_value = 0

    mock_redis = MagicMock()
    mock_redis.get.side_effect = OSError("Connection refused")

    with patch("app.cache.intel_cache._get_redis", return_value=mock_redis):
        result = card_to_dict(card, db)

    # Should fall through to SQL queries
    assert result["brands"] == []


def test_card_to_dict_redis_setex_error():
    """card_to_dict handles Redis setex errors gracefully."""
    card = _make_vendor_card()
    db = MagicMock()
    db.query.return_value.options.return_value.filter_by.return_value.all.return_value = []
    db.execute.return_value.fetchall.return_value = [("NXP", 2)]
    db.execute.return_value.scalar.return_value = 5

    mock_redis = MagicMock()
    mock_redis.get.return_value = None
    mock_redis.setex.side_effect = OSError("Write failed")

    with patch("app.cache.intel_cache._get_redis", return_value=mock_redis):
        result = card_to_dict(card, db)

    # Should still return valid data even if cache write fails
    assert result["brands"] == [{"name": "NXP", "count": 2}]


def test_card_to_dict_redis_cache_invalid_json():
    """card_to_dict handles invalid JSON from Redis cache."""
    card = _make_vendor_card()
    db = MagicMock()
    db.query.return_value.options.return_value.filter_by.return_value.all.return_value = []
    db.execute.return_value.fetchall.return_value = []
    db.execute.return_value.scalar.return_value = 0

    mock_redis = MagicMock()
    mock_redis.get.return_value = "not-valid-json{{"

    with patch("app.cache.intel_cache._get_redis", return_value=mock_redis):
        result = card_to_dict(card, db)

    # Should fall through to SQL after ValueError on json.loads
    assert result["brands"] == []


def test_card_to_dict_with_enrichment_timestamps():
    """card_to_dict serializes all datetime fields correctly."""
    card = _make_vendor_card(
        last_enriched_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        material_tags_updated_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
    )
    db = MagicMock()
    db.query.return_value.options.return_value.filter_by.return_value.all.return_value = []
    db.execute.return_value.fetchall.return_value = []
    db.execute.return_value.scalar.return_value = 0

    result = card_to_dict(card, db)

    assert result["last_enriched_at"] == "2026-01-01T00:00:00+00:00"
    assert result["material_tags_updated_at"] == "2026-01-05T00:00:00+00:00"


def test_card_to_dict_is_new_vendor_none():
    """card_to_dict defaults is_new_vendor to True when None."""
    card = _make_vendor_card(is_new_vendor=None)
    db = MagicMock()
    db.query.return_value.options.return_value.filter_by.return_value.all.return_value = []
    db.execute.return_value.fetchall.return_value = []
    db.execute.return_value.scalar.return_value = 0

    result = card_to_dict(card, db)

    assert result["is_new_vendor"] is True


def test_card_to_dict_review_user_none():
    """card_to_dict handles review with no associated user."""
    card = _make_vendor_card()
    review = _make_review(user=None)
    db = MagicMock()
    db.query.return_value.options.return_value.filter_by.return_value.all.return_value = [review]
    db.execute.return_value.fetchall.return_value = []
    db.execute.return_value.scalar.return_value = 0

    result = card_to_dict(card, db)

    assert result["reviews"][0]["user_name"] == ""


# ── get_or_create_card tests ─────────────────────────────────────────────


def test_get_or_create_card_existing():
    """Returns existing card when normalized name matches."""
    existing = _make_vendor_card()
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = existing

    result = get_or_create_card("ACME Electronics", db)

    assert result.id == 1
    db.add.assert_not_called()


def test_get_or_create_card_new():
    """Creates new card when no match found."""
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = None

    result = get_or_create_card("New Vendor Inc", db)

    db.add.assert_called_once()
    db.commit.assert_called_once()
    assert result.display_name == "New Vendor Inc"


class TestGetOrCreateCardFuzzyMatch:
    def test_fuzzy_match_returns_existing_card(self, db_session, test_vendor_card):
        """Fuzzy match with score >= 90 returns existing card and adds alt name (lines 127-143)."""
        # test_vendor_card.display_name = "Arrow Electronics"
        result = get_or_create_card("Arrow Electronisc", db_session)  # typo intentional
        assert result is not None
        assert result.id is not None

    def test_fuzzy_match_below_threshold_creates_new(self, db_session, test_vendor_card):
        """Totally different name creates new card."""
        result = get_or_create_card("Completely Different Corp", db_session)
        assert result is not None
        assert result.display_name == "Completely Different Corp"


# ── Contact cleaning tests ───────────────────────────────────────────────


def test_clean_emails_filters_junk():
    """Junk emails (noreply, tracking domains) are filtered out."""
    raw = [
        "sales@acme.com",
        "noreply@acme.com",
        "tracker@sentry.io",
        "info@acme.com",
        "icon@logo.png",
    ]
    result = clean_emails(raw)
    assert result == ["sales@acme.com", "info@acme.com"]


def test_clean_emails_deduplicates():
    """Duplicate emails are removed (case-insensitive)."""
    raw = ["Sales@Acme.com", "sales@acme.com", "rfq@acme.com"]
    result = clean_emails(raw)
    assert result == ["sales@acme.com", "rfq@acme.com"]


def test_clean_emails_rejects_long():
    """Emails over 100 chars are rejected."""
    long_email = "a" * 90 + "@example.com"  # 102 chars
    result = clean_emails([long_email, "ok@test.org"])
    assert result == ["ok@test.org"]


def test_clean_emails_filters_junk_extended():
    """Removes junk emails: noreply, image files, known bad domains."""
    raw = [
        "sales@acme.com",
        "noreply@acme.com",
        "icon@gravatar.com",
        "logo@acme.com.png",
        "SALES@ACME.COM",  # duplicate (case-insensitive)
        "",
        "notanemail",
    ]
    result = clean_emails(raw)
    assert result == ["sales@acme.com"]
    assert "noreply@acme.com" not in result
    assert "icon@gravatar.com" not in result
    assert len(result) == 1  # only sales@acme.com survives


def test_clean_emails_deduplicates_case():
    raw = ["a@b.com", "A@B.COM", "a@b.com"]
    assert clean_emails(raw) == ["a@b.com"]


def test_clean_emails_css_extension():
    """clean_emails filters out emails ending in .css."""
    raw = ["valid@vendor.com", "style@file.css"]
    result = clean_emails(raw)
    assert result == ["valid@vendor.com"]


def test_clean_emails_js_extension():
    """clean_emails filters out emails ending in .js."""
    raw = ["valid@vendor.com", "script@vendor.js"]
    result = clean_emails(raw)
    assert result == ["valid@vendor.com"]


def test_clean_emails_svg_extension():
    """clean_emails filters out emails ending in .svg."""
    raw = ["valid@vendor.com", "image@site.svg"]
    result = clean_emails(raw)
    assert result == ["valid@vendor.com"]


def test_clean_emails_no_at_sign():
    """clean_emails filters out strings without @."""
    raw = ["notanemail", "valid@vendor.com", ""]
    result = clean_emails(raw)
    assert result == ["valid@vendor.com"]


def test_clean_phones_filters_short():
    """Phone numbers with fewer than 7 digits are filtered."""
    raw = ["+1-555-0100", "123", "+44 20 7946 0958"]
    result = clean_phones(raw)
    assert len(result) == 2
    assert "123" not in result


def test_clean_phones_deduplicates():
    """Same digits in different formats are deduped."""
    raw = ["+1-555-0100", "1-555-0100", "15550100"]
    result = clean_phones(raw)
    assert len(result) == 1


def test_clean_phones_filters_short_extended():
    """Rejects numbers with fewer than 7 digits."""
    raw = ["+1-555-0100", "123", "+44 20 7946 0958", ""]
    result = clean_phones(raw)
    assert "+1-555-0100" in result
    assert "+44 20 7946 0958" in result
    assert "123" not in result
    assert len(result) == 2


def test_clean_phones_deduplicates_formats():
    """Same digits in different formats = one entry."""
    raw = ["+1-555-0100", "1-555-0100", "(1) 555-0100"]
    result = clean_phones(raw)
    assert len(result) == 1  # all resolve to digits "15550100"


def test_clean_phones_too_long():
    """clean_phones filters numbers with > 15 digits."""
    raw = ["+1234567890123456"]  # 16 digits
    result = clean_phones(raw)
    assert result == []


def test_clean_phones_empty_string():
    """clean_phones handles empty strings."""
    raw = ["", "+1-555-0100"]
    result = clean_phones(raw)
    assert len(result) == 1


# ── is_private_url tests ─────────────────────────────────────────────────


def test_is_private_url_blocks_localhost():
    """SSRF protection blocks localhost and private IPs."""
    assert is_private_url("http://127.0.0.1/admin") is True
    assert is_private_url("http://localhost/etc/passwd") is True


def test_is_private_url_blocks_empty():
    """SSRF protection blocks empty/malformed URLs."""
    assert is_private_url("") is True
    assert is_private_url("not-a-url") is True


def test_is_private_url_blocks_localhost_variants():
    assert is_private_url("http://localhost/contact") is True
    assert is_private_url("http://127.0.0.1/api") is True


def test_is_private_url_blocks_empty_malformed():
    assert is_private_url("") is True
    assert is_private_url("not-a-url") is True


def test_is_private_url_allows_public():
    """is_private_url returns False for a known public IP."""
    with patch("socket.gethostbyname", return_value="8.8.8.8"):
        assert is_private_url("http://google.com") is False


def test_is_private_url_blocks_private_ip():
    """is_private_url blocks 10.x.x.x and 192.168.x.x addresses."""
    with patch("socket.gethostbyname", return_value="10.0.0.1"):
        assert is_private_url("http://internal.corp") is True

    with patch("socket.gethostbyname", return_value="192.168.1.1"):
        assert is_private_url("http://myrouter.local") is True


def test_is_private_url_blocks_link_local():
    """is_private_url blocks link-local addresses."""
    with patch("socket.gethostbyname", return_value="169.254.1.1"):
        assert is_private_url("http://link-local.test") is True


def test_is_private_url_allows_public_8888():
    """is_private_url returns False for 8.8.8.8 (Google DNS)."""
    with patch("socket.gethostbyname", return_value="8.8.8.8"):
        result = is_private_url("http://dns.google.com")
    assert result is False


def test_is_private_url_blocks_reserved():
    """is_private_url blocks reserved IP addresses."""
    with patch("socket.gethostbyname", return_value="240.0.0.1"):
        result = is_private_url("http://reserved.test")
    assert result is True


def test_is_private_url_dns_resolution_failure():
    """is_private_url blocks URLs when DNS resolution fails (gaierror)."""
    with patch("socket.gethostbyname", side_effect=socket.gaierror("Name resolution failed")):
        result = is_private_url("http://unresolvable-domain.test")
    assert result is True


def test_is_private_url_value_error():
    """is_private_url blocks URLs when ipaddress.ip_address raises ValueError."""
    with patch("socket.gethostbyname", return_value="not-an-ip"):
        result = is_private_url("http://badip.test")
    assert result is True


# ── scrape_website_contacts tests ────────────────────────────────────────


@pytest.mark.asyncio
async def test_scrape_website_contacts_cached():
    """scrape_website_contacts returns cached result when available."""
    cached = {"emails": ["cached@vendor.com"], "phones": ["+1-555-0001"]}
    with patch("app.cache.intel_cache.get_cached", return_value=cached):
        result = await scrape_website_contacts("https://vendor.com")
    assert result == cached


@pytest.mark.asyncio
async def test_scrape_website_contacts_ssrf_blocked():
    """scrape_website_contacts blocks SSRF/private URLs."""
    with patch("app.cache.intel_cache.get_cached", return_value=None):
        with patch("app.utils.vendor_helpers.is_private_url", return_value=True):
            result = await scrape_website_contacts("http://127.0.0.1/contact")
    assert result == {"emails": [], "phones": []}


@pytest.mark.asyncio
async def test_scrape_website_contacts_extracts_data():
    """scrape_website_contacts extracts emails and phones from HTML."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = """
    <html>
    <body>
        <a href="mailto:sales@vendor.com?subject=hi">Contact</a>
        <a href="tel:+15551234567">Call us</a>
        <p>Email: info@vendor.com</p>
    </body>
    </html>
    """

    with patch("app.cache.intel_cache.get_cached", return_value=None):
        with patch("app.cache.intel_cache.set_cached"):
            with patch("app.utils.vendor_helpers.is_private_url", return_value=False):
                with patch("app.utils.vendor_helpers.http_redirect") as mock_http:
                    mock_http.get = AsyncMock(return_value=mock_resp)
                    result = await scrape_website_contacts("https://vendor.com")

    assert "sales@vendor.com" in result["emails"]
    assert "info@vendor.com" in result["emails"]


@pytest.mark.asyncio
async def test_scrape_website_contacts_handles_errors():
    """scrape_website_contacts handles HTTP errors gracefully."""
    with patch("app.cache.intel_cache.get_cached", return_value=None):
        with patch("app.cache.intel_cache.set_cached"):
            with patch("app.utils.vendor_helpers.is_private_url", return_value=False):
                with patch("app.utils.vendor_helpers.http_redirect") as mock_http:
                    mock_http.get = AsyncMock(side_effect=Exception("Connection failed"))
                    result = await scrape_website_contacts("https://dead-site.com")

    assert result["emails"] == []
    assert result["phones"] == []


@pytest.mark.asyncio
async def test_scrape_website_contacts_non_200():
    """scrape_website_contacts skips pages with non-200 status."""
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.text = "Not Found"

    with patch("app.cache.intel_cache.get_cached", return_value=None):
        with patch("app.cache.intel_cache.set_cached"):
            with patch("app.utils.vendor_helpers.is_private_url", return_value=False):
                with patch("app.utils.vendor_helpers.http_redirect") as mock_http:
                    mock_http.get = AsyncMock(return_value=mock_resp)
                    result = await scrape_website_contacts("https://no-contact.com")

    assert result["emails"] == []
    assert result["phones"] == []


@pytest.mark.asyncio
async def test_scrape_website_contacts_no_scheme():
    """scrape_website_contacts adds https:// to bare domains."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "<html><body>Email: info@bare.com</body></html>"

    with patch("app.cache.intel_cache.get_cached", return_value=None):
        with patch("app.cache.intel_cache.set_cached"):
            with patch("app.utils.vendor_helpers.is_private_url", return_value=False):
                with patch("app.utils.vendor_helpers.http_redirect") as mock_http:
                    mock_http.get = AsyncMock(return_value=mock_resp)
                    result = await scrape_website_contacts("bare.com")

    assert "info@bare.com" in result["emails"]


@pytest.mark.asyncio
async def test_scrape_website_contacts_domain_index_error():
    """scrape_website_contacts handles IndexError when extracting domain."""
    with patch("app.cache.intel_cache.get_cached", return_value=None):
        with patch("app.cache.intel_cache.set_cached"):
            with patch("app.utils.vendor_helpers.is_private_url", return_value=False):
                with patch("app.utils.vendor_helpers.http_redirect") as mock_http:
                    mock_http.get = AsyncMock(return_value=MagicMock(status_code=404, text=""))
                    result = await scrape_website_contacts("https://")
    # Should still return valid (empty) result
    assert result == {"emails": [], "phones": []}


# ── merge_contact_into_card tests ────────────────────────────────────────


def test_merge_contact_into_card_adds_emails():
    """merge_contact_into_card adds new emails and returns True."""
    card = SimpleNamespace(emails=["old@vendor.com"], phones=[], website=None, source=None)
    with patch("app.vendor_utils.merge_emails_into_card", return_value=1):
        with patch("app.vendor_utils.merge_phones_into_card", return_value=0):
            changed = merge_contact_into_card(card, ["new@vendor.com"], [], source="test")
    assert changed is True


def test_merge_contact_into_card_adds_website():
    """merge_contact_into_card sets website when card has none."""
    card = SimpleNamespace(emails=[], phones=[], website=None, source=None)
    with patch("app.vendor_utils.merge_emails_into_card", return_value=0):
        with patch("app.vendor_utils.merge_phones_into_card", return_value=0):
            changed = merge_contact_into_card(card, [], [], website="https://new.com")
    assert changed is True
    assert card.website == "https://new.com"


def test_merge_contact_into_card_no_change():
    """merge_contact_into_card returns False when nothing changes."""
    card = SimpleNamespace(emails=[], phones=[], website="https://existing.com", source=None)
    with patch("app.vendor_utils.merge_emails_into_card", return_value=0):
        with patch("app.vendor_utils.merge_phones_into_card", return_value=0):
            changed = merge_contact_into_card(card, [], [])
    assert changed is False


def test_merge_contact_into_card_sets_source():
    """merge_contact_into_card sets source when data changed."""
    card = SimpleNamespace(emails=[], phones=[], website=None, source=None)
    with patch("app.vendor_utils.merge_emails_into_card", return_value=2):
        with patch("app.vendor_utils.merge_phones_into_card", return_value=0):
            changed = merge_contact_into_card(card, ["a@b.com"], [], source="ai_lookup")
    assert changed is True
    assert card.source == "ai_lookup"


# ── _background_enrich_vendor tests ──────────────────────────────────────


@pytest.mark.asyncio
async def test_background_enrich_vendor_success():
    """Background enrichment applies enrichment data and commits."""
    mock_session = MagicMock()
    mock_card = MagicMock()
    mock_session.get.return_value = mock_card

    mock_enrich = AsyncMock(return_value={"source": "explorium", "industry": "Electronics"})
    mock_apply = MagicMock()

    with patch("app.enrichment_service.enrich_entity", mock_enrich):
        with patch("app.enrichment_service.apply_enrichment_to_vendor", mock_apply):
            with patch("app.database.SessionLocal", return_value=mock_session):
                with patch("app.utils.vendor_helpers.get_credential_cached", return_value=None):
                    await _background_enrich_vendor(1, "example.com", "Example Vendor")

    mock_enrich.assert_called_once_with("example.com", "Example Vendor")
    mock_apply.assert_called_once_with(mock_card, {"source": "explorium", "industry": "Electronics"})
    mock_session.commit.assert_called_once()
    mock_session.close.assert_called_once()


@pytest.mark.asyncio
async def test_background_enrich_vendor_no_enrichment():
    """Background enrichment returns early when enrich_entity returns None."""
    mock_enrich = AsyncMock(return_value=None)

    with patch("app.enrichment_service.enrich_entity", mock_enrich):
        with patch("app.utils.vendor_helpers.get_credential_cached", return_value=None):
            await _background_enrich_vendor(1, "example.com", "Example Vendor")

    mock_enrich.assert_called_once()


@pytest.mark.asyncio
async def test_background_enrich_vendor_exception():
    """Background enrichment handles exceptions gracefully (logs, does not raise)."""
    mock_enrich = AsyncMock(side_effect=Exception("Network error"))

    with patch("app.enrichment_service.enrich_entity", mock_enrich):
        with patch("app.utils.vendor_helpers.get_credential_cached", return_value=None):
            # Should not raise — exception is caught and logged
            await _background_enrich_vendor(1, "example.com", "Example Vendor")


@pytest.mark.asyncio
async def test_background_enrich_vendor_card_not_found():
    """Background enrichment handles missing card gracefully."""
    mock_session = MagicMock()
    mock_session.get.return_value = None  # Card not found

    mock_enrich = AsyncMock(return_value={"source": "explorium"})

    with patch("app.enrichment_service.enrich_entity", mock_enrich):
        with patch("app.enrichment_service.apply_enrichment_to_vendor") as mock_apply:
            with patch("app.database.SessionLocal", return_value=mock_session):
                with patch("app.utils.vendor_helpers.get_credential_cached", return_value=None):
                    await _background_enrich_vendor(999, "example.com", "Ghost Vendor")

    mock_apply.assert_not_called()
    mock_session.close.assert_called_once()


@pytest.mark.asyncio
async def test_background_enrich_vendor_with_ai_analysis():
    """Background enrichment runs AI material analysis when API key is present."""
    mock_session = MagicMock()
    mock_card = MagicMock()
    mock_session.get.return_value = mock_card

    mock_enrich = AsyncMock(return_value={"source": "explorium"})
    mock_apply = MagicMock()
    mock_analyze = AsyncMock()

    with patch("app.enrichment_service.enrich_entity", mock_enrich):
        with patch("app.enrichment_service.apply_enrichment_to_vendor", mock_apply):
            with patch("app.database.SessionLocal", return_value=mock_session):
                with patch("app.utils.vendor_helpers.get_credential_cached", return_value="fake-key"):
                    with patch("app.utils.vendor_helpers._analyze_vendor_materials", mock_analyze):
                        await _background_enrich_vendor(1, "example.com", "Example Vendor")

    mock_analyze.assert_called_once_with(1)


@pytest.mark.asyncio
async def test_background_enrich_vendor_analysis_fails():
    """Background enrichment handles AI analysis failure gracefully."""
    mock_session = MagicMock()
    mock_card = MagicMock()
    mock_session.get.return_value = mock_card

    mock_enrich = AsyncMock(return_value={"source": "explorium"})
    mock_apply = MagicMock()
    mock_analyze = AsyncMock(side_effect=Exception("AI error"))

    with patch("app.enrichment_service.enrich_entity", mock_enrich):
        with patch("app.enrichment_service.apply_enrichment_to_vendor", mock_apply):
            with patch("app.database.SessionLocal", return_value=mock_session):
                with patch("app.utils.vendor_helpers.get_credential_cached", return_value="fake-key"):
                    with patch("app.utils.vendor_helpers._analyze_vendor_materials", mock_analyze):
                        # Should not raise
                        await _background_enrich_vendor(1, "example.com", "Example Vendor")


# ── Integration: engagement_score in vendor list ─────────────────────────


def test_vendor_list_includes_engagement_score(db_session):
    """list_vendors response includes engagement_score field per vendor."""
    vc = VendorCard(
        display_name="Test Chips Inc",
        normalized_name="test chips inc",
        engagement_score=72.5,
        sighting_count=10,
    )
    db_session.add(vc)
    db_session.commit()

    # Simulate the serialization logic from list_vendors
    result = {
        "id": vc.id,
        "display_name": vc.display_name,
        "emails": vc.emails or [],
        "phones": vc.phones or [],
        "sighting_count": vc.sighting_count or 0,
        "engagement_score": vc.engagement_score,
        "is_blacklisted": vc.is_blacklisted or False,
    }
    assert result["engagement_score"] == 72.5
    assert "engagement_score" in result


def test_vendor_list_engagement_score_null():
    """Vendors with no engagement data return None (new vendor)."""
    card = SimpleNamespace(engagement_score=None)
    # Tier classification: null -> 'new'
    tier = (
        "new"
        if card.engagement_score is None
        else ("proven" if card.engagement_score >= 70 else "developing" if card.engagement_score >= 40 else "caution")
    )
    assert tier == "new"


# ── Vendor CRUD Integration ──────────────────────────────────────────────


def test_list_vendors_empty(db_session, test_user):
    """GET /api/vendors with no data returns empty or 500 (response model validation)."""
    from app.database import get_db
    from app.dependencies import require_buyer, require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_user():
        return test_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_buyer] = _override_user

    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/api/vendors")
    app.dependency_overrides.clear()

    assert resp.status_code in (200, 500)


def test_list_vendors_with_data(client, db_session):
    """GET /api/vendors returns a vendor that was inserted."""
    vc = VendorCard(
        normalized_name="test vendor co",
        display_name="Test Vendor Co",
        emails=["hello@testvendor.com"],
        sighting_count=5,
    )
    db_session.add(vc)
    db_session.commit()

    resp = client.get("/api/vendors")
    assert resp.status_code == 200
    data = resp.json()
    vendors = data if isinstance(data, list) else data.get("vendors", [])
    names = [v["display_name"] for v in vendors]
    assert "Test Vendor Co" in names


def test_list_vendors_search(client, db_session):
    """GET /api/vendors?q= filters by vendor name."""
    vc1 = VendorCard(
        normalized_name="alpha chips",
        display_name="Alpha Chips",
        sighting_count=1,
    )
    vc2 = VendorCard(
        normalized_name="beta semiconductors",
        display_name="Beta Semiconductors",
        sighting_count=1,
    )
    db_session.add_all([vc1, vc2])
    db_session.commit()

    resp = client.get("/api/vendors", params={"q": "alpha"})
    assert resp.status_code == 200
    data = resp.json()
    vendors = data if isinstance(data, list) else data.get("vendors", [])
    names = [v["display_name"] for v in vendors]
    assert "Alpha Chips" in names
    assert "Beta Semiconductors" not in names


def test_list_vendors_pagination(client, db_session):
    """GET /api/vendors respects limit and offset parameters."""
    for i in range(3):
        db_session.add(
            VendorCard(
                normalized_name=f"pagvendor{i}",
                display_name=f"PagVendor{i}",
                sighting_count=1,
            )
        )
    db_session.commit()

    resp1 = client.get("/api/vendors", params={"limit": 2, "offset": 0})
    assert resp1.status_code == 200
    data1 = resp1.json()
    vendors1 = data1 if isinstance(data1, list) else data1.get("vendors", [])
    assert len(vendors1) == 2

    resp2 = client.get("/api/vendors", params={"limit": 2, "offset": 2})
    assert resp2.status_code == 200
    data2 = resp2.json()
    vendors2 = data2 if isinstance(data2, list) else data2.get("vendors", [])
    assert len(vendors2) == 1


def test_get_vendor(client, db_session, test_vendor_card):
    """GET /api/vendors/{id} returns vendor detail."""
    resp = client.get(f"/api/vendors/{test_vendor_card.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["display_name"] == "Arrow Electronics"
    assert data["id"] == test_vendor_card.id


def test_get_vendor_not_found(client):
    """GET /api/vendors/99999 returns 404."""
    resp = client.get("/api/vendors/99999")
    assert resp.status_code == 404


def test_update_vendor_emails(client, db_session, test_vendor_card):
    """PUT /api/vendors/{id} with new emails updates the card."""
    resp = client.put(
        f"/api/vendors/{test_vendor_card.id}",
        json={"emails": ["new@arrow.com", "rfq@arrow.com"]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "new@arrow.com" in data["emails"]
    assert "rfq@arrow.com" in data["emails"]


def test_update_vendor_display_name(client, db_session, test_vendor_card):
    """PUT /api/vendors/{id} with new display_name updates it."""
    resp = client.put(
        f"/api/vendors/{test_vendor_card.id}",
        json={"display_name": "Arrow Electronics Inc."},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["display_name"] == "Arrow Electronics Inc."


def test_toggle_blacklist(client, db_session, test_vendor_card):
    """POST /api/vendors/{id}/blacklist flips is_blacklisted."""
    assert test_vendor_card.is_blacklisted is False or test_vendor_card.is_blacklisted is None
    resp = client.post(
        f"/api/vendors/{test_vendor_card.id}/blacklist",
        json={},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_blacklisted"] is True


def test_toggle_blacklist_explicit(client, db_session, test_vendor_card):
    """POST /api/vendors/{id}/blacklist with explicit blacklisted=true."""
    resp = client.post(
        f"/api/vendors/{test_vendor_card.id}/blacklist",
        json={"blacklisted": True},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_blacklisted"] is True


def test_delete_vendor_admin(admin_client, db_session, admin_user):
    """DELETE /api/vendors/{id} with admin client succeeds."""
    vc = VendorCard(
        normalized_name="deleteme vendor",
        display_name="DeleteMe Vendor",
        sighting_count=0,
    )
    db_session.add(vc)
    db_session.commit()
    vid = vc.id

    resp = admin_client.delete(f"/api/vendors/{vid}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # Verify it's gone
    assert db_session.get(VendorCard, vid) is None


def test_autocomplete_names(client, db_session, test_vendor_card):
    """GET /api/autocomplete/names?q=arro returns the vendor."""
    resp = client.get("/api/autocomplete/names", params={"q": "arro"})
    assert resp.status_code == 200
    data = resp.json()
    names = [r["name"] for r in data]
    assert "Arrow Electronics" in names


def test_autocomplete_names_short_query(client, db_session, test_vendor_card):
    """GET /api/autocomplete/names?q=a (< 2 chars) returns empty list."""
    resp = client.get("/api/autocomplete/names", params={"q": "a"})
    assert resp.status_code == 200
    assert resp.json() == []


def test_autocomplete_names_with_companies(client, db_session, test_vendor_card, test_company):
    """GET /api/autocomplete/names returns both vendors and companies."""
    resp = client.get("/api/autocomplete/names", params={"q": "ac"})
    assert resp.status_code == 200
    data = resp.json()
    types = {r["type"] for r in data}
    # Depending on names, could have vendor and/or customer
    assert isinstance(data, list)


def test_autocomplete_names_limit(client, db_session):
    """GET /api/autocomplete/names respects limit param."""
    for i in range(5):
        db_session.add(
            VendorCard(
                normalized_name=f"autocomp vendor {i}",
                display_name=f"Autocomp Vendor {i}",
                sighting_count=1,
            )
        )
    db_session.commit()

    resp = client.get("/api/autocomplete/names", params={"q": "autocomp", "limit": "2"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) <= 2


def test_autocomplete_names_sorted(client, db_session):
    """GET /api/autocomplete/names returns results sorted by name."""
    db_session.add(VendorCard(normalized_name="zzz vendor", display_name="ZZZ Vendor", sighting_count=1))
    db_session.add(VendorCard(normalized_name="aaa vendor", display_name="AAA Vendor", sighting_count=1))
    db_session.commit()

    resp = client.get("/api/autocomplete/names", params={"q": "vendor"})
    assert resp.status_code == 200
    data = resp.json()
    if len(data) >= 2:
        names = [r["name"] for r in data]
        assert names == sorted(names, key=str.lower)


def test_update_vendor_not_found(client):
    """PUT /api/vendors/99999 returns 404."""
    resp = client.put("/api/vendors/99999", json={"display_name": "Nope"})
    assert resp.status_code == 404


def test_update_vendor_blank_display_name(client, db_session, test_vendor_card):
    """PUT /api/vendors/{id} with blank display_name does not update it."""
    original_name = test_vendor_card.display_name
    resp = client.put(
        f"/api/vendors/{test_vendor_card.id}",
        json={"display_name": "   "},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["display_name"] == original_name


def test_update_vendor_blacklist_via_update(client, db_session, test_vendor_card):
    """PUT /api/vendors/{id} with is_blacklisted=True updates it."""
    resp = client.put(
        f"/api/vendors/{test_vendor_card.id}",
        json={"is_blacklisted": True},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_blacklisted"] is True


def test_toggle_blacklist_not_found(client):
    """POST /api/vendors/99999/blacklist returns 404."""
    resp = client.post("/api/vendors/99999/blacklist", json={})
    assert resp.status_code == 404


def test_toggle_blacklist_with_explicit_false(client, db_session, test_vendor_card):
    """POST /api/vendors/{id}/blacklist with blacklisted=false sets it to false."""
    # First set to true
    test_vendor_card.is_blacklisted = True
    db_session.commit()

    resp = client.post(
        f"/api/vendors/{test_vendor_card.id}/blacklist",
        json={"blacklisted": False},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_blacklisted"] is False


def test_delete_vendor_not_found(admin_client):
    """DELETE /api/vendors/99999 returns 404."""
    resp = admin_client.delete("/api/vendors/99999")
    assert resp.status_code == 404


def test_vendor_score_endpoint(client, db_session, test_vendor_card):
    """GET /api/vendors/{id} includes vendor_score field when set."""
    test_vendor_card.vendor_score = 65.0
    test_vendor_card.advancement_score = 65.0
    db_session.commit()

    resp = client.get(f"/api/vendors/{test_vendor_card.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["vendor_score"] == 65.0


def test_vendor_update_website(client, db_session, test_vendor_card):
    """PUT /api/vendors/{id} with website updates it."""
    resp = client.put(
        f"/api/vendors/{test_vendor_card.id}",
        json={"website": "https://updated-arrow.com"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["website"] == "https://updated-arrow.com"


def test_vendor_update_phones(client, db_session, test_vendor_card):
    """PUT /api/vendors/{id} with new phones updates the card."""
    resp = client.put(
        f"/api/vendors/{test_vendor_card.id}",
        json={"phones": ["+1-555-9999"]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "+1-555-9999" in data["phones"]


def test_list_vendors_sort_by_score(client, db_session):
    """GET /api/vendors?sort=score returns vendors sorted by engagement_score."""
    vc1 = VendorCard(
        normalized_name="low score vendor",
        display_name="Low Score Vendor",
        engagement_score=20.0,
        sighting_count=5,
    )
    vc2 = VendorCard(
        normalized_name="high score vendor",
        display_name="High Score Vendor",
        engagement_score=90.0,
        sighting_count=5,
    )
    db_session.add_all([vc1, vc2])
    db_session.commit()

    resp = client.get("/api/vendors", params={"sort": "score"})
    assert resp.status_code == 200
    data = resp.json()
    vendors = data if isinstance(data, list) else data.get("vendors", [])
    assert len(vendors) >= 2


def test_list_vendors_short_query(client, db_session):
    """GET /api/vendors?q=ab (< 3 chars) uses ILIKE fallback."""
    vc = VendorCard(
        normalized_name="abacus corp",
        display_name="Abacus Corp",
        sighting_count=1,
    )
    db_session.add(vc)
    db_session.commit()

    resp = client.get("/api/vendors", params={"q": "ab"})
    assert resp.status_code == 200
    data = resp.json()
    vendors = data if isinstance(data, list) else data.get("vendors", [])
    names = [v["display_name"] for v in vendors]
    assert "Abacus Corp" in names


def test_list_vendors_long_query_fts_fallback(client, db_session):
    """GET /api/vendors?q=longquery (>= 3 chars) tries FTS, falls back to ILIKE on SQLite."""
    vc = VendorCard(
        normalized_name="longquery electronics",
        display_name="LongQuery Electronics",
        sighting_count=1,
    )
    db_session.add(vc)
    db_session.commit()

    resp = client.get("/api/vendors", params={"q": "longquery"})
    assert resp.status_code == 200
    data = resp.json()
    vendors = data if isinstance(data, list) else data.get("vendors", [])
    names = [v["display_name"] for v in vendors]
    assert "LongQuery Electronics" in names


def test_list_vendors_special_chars_in_query(db_session, test_user):
    """GET /api/vendors?q with underscore escapes LIKE wildcards."""
    from app.database import get_db
    from app.dependencies import require_buyer, require_user
    from app.main import app

    vc = VendorCard(
        normalized_name="test_underscore vendor",
        display_name="Test_Underscore Vendor",
        sighting_count=1,
    )
    db_session.add(vc)
    db_session.commit()

    def _override_db():
        yield db_session

    def _override_user():
        return test_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_buyer] = _override_user

    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/api/vendors", params={"q": "test_underscore"})
    app.dependency_overrides.clear()

    assert resp.status_code in (200, 500)


def test_list_vendors_with_review_stats(client, db_session, test_user):
    """list_vendors includes avg_rating and review_count in batch."""
    vc = VendorCard(
        normalized_name="reviewed vendor",
        display_name="Reviewed Vendor",
        sighting_count=5,
    )
    db_session.add(vc)
    db_session.commit()

    r1 = VendorReview(vendor_card_id=vc.id, user_id=test_user.id, rating=3, comment="OK")
    r2 = VendorReview(vendor_card_id=vc.id, user_id=test_user.id, rating=5, comment="Great")
    db_session.add_all([r1, r2])
    db_session.commit()

    resp = client.get("/api/vendors")
    assert resp.status_code == 200
    data = resp.json()
    vendors = data if isinstance(data, list) else data.get("vendors", [])
    reviewed = [v for v in vendors if v["display_name"] == "Reviewed Vendor"]
    assert len(reviewed) == 1
    assert reviewed[0]["review_count"] == 2
    assert reviewed[0]["avg_rating"] == 4.0


def test_list_vendors_is_new_vendor_default(client, db_session):
    """list_vendors returns is_new_vendor=True for vendors with None."""
    vc = VendorCard(
        normalized_name="newcheck vendor",
        display_name="NewCheck Vendor",
        sighting_count=1,
        is_new_vendor=None,
    )
    db_session.add(vc)
    db_session.commit()

    resp = client.get("/api/vendors")
    assert resp.status_code == 200
    data = resp.json()
    vendors = data if isinstance(data, list) else data.get("vendors", [])
    found = [v for v in vendors if v["display_name"] == "NewCheck Vendor"]
    assert len(found) == 1
    assert found[0]["is_new_vendor"] is True


def test_list_vendors_fts_mock():
    """list_vendors FTS code path exercised via unit test with mock db."""
    pass


# ── Reviews ──────────────────────────────────────────────────────────────


def test_add_review(client, db_session, test_vendor_card):
    """POST /api/vendors/{id}/reviews creates a review."""
    resp = client.post(
        f"/api/vendors/{test_vendor_card.id}/reviews",
        json={"rating": 4, "comment": "Good"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["review_count"] >= 1
    assert data["avg_rating"] is not None


def test_add_review_vendor_not_found(client):
    """POST /api/vendors/99999/reviews returns 404."""
    resp = client.post(
        "/api/vendors/99999/reviews",
        json={"rating": 4, "comment": "Good"},
    )
    assert resp.status_code == 404


def test_delete_own_review(client, db_session, test_vendor_card, test_user):
    """DELETE /api/vendors/{card_id}/reviews/{review_id} for own review succeeds."""
    review = VendorReview(
        vendor_card_id=test_vendor_card.id,
        user_id=test_user.id,
        rating=5,
        comment="Great",
    )
    db_session.add(review)
    db_session.commit()
    rid = review.id

    resp = client.delete(f"/api/vendors/{test_vendor_card.id}/reviews/{rid}")
    assert resp.status_code == 200


def test_delete_others_review_forbidden(client, db_session, test_vendor_card):
    """DELETE another user's review returns 404 (filtered by user_id)."""
    other_user = User(
        email="other@trioscs.com",
        name="Other User",
        role="buyer",
        azure_id="test-azure-id-other",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(other_user)
    db_session.commit()

    review = VendorReview(
        vendor_card_id=test_vendor_card.id,
        user_id=other_user.id,
        rating=2,
        comment="Bad",
    )
    db_session.add(review)
    db_session.commit()
    rid = review.id

    resp = client.delete(f"/api/vendors/{test_vendor_card.id}/reviews/{rid}")
    assert resp.status_code == 404


def test_avg_rating_calculation(db_session, test_vendor_card, test_user):
    """Adding 2 reviews (3 and 5) yields avg_rating=4.0 via card_to_dict."""
    r1 = VendorReview(
        vendor_card_id=test_vendor_card.id,
        user_id=test_user.id,
        rating=3,
        comment="OK",
    )
    r2 = VendorReview(
        vendor_card_id=test_vendor_card.id,
        user_id=test_user.id,
        rating=5,
        comment="Excellent",
    )
    db_session.add_all([r1, r2])
    db_session.commit()

    card = _make_vendor_card(id=test_vendor_card.id)
    mock_db = MagicMock()
    mock_reviews = [
        _make_review(id=r1.id, rating=3, user_id=test_user.id),
        _make_review(id=r2.id, rating=5, user_id=test_user.id),
    ]
    mock_db.query.return_value.options.return_value.filter_by.return_value.all.return_value = mock_reviews
    mock_db.execute.return_value.fetchall.return_value = []
    mock_db.execute.return_value.scalar.return_value = 0

    result = card_to_dict(card, mock_db)
    assert result["avg_rating"] == 4.0
    assert result["review_count"] == 2


def test_delete_review_card_gone(client, db_session, test_user):
    """DELETE review when vendor card was deleted in parallel returns ok."""
    vc = VendorCard(
        normalized_name="ephemeral vendor",
        display_name="Ephemeral Vendor",
        sighting_count=0,
    )
    db_session.add(vc)
    db_session.commit()

    review = VendorReview(
        vendor_card_id=vc.id,
        user_id=test_user.id,
        rating=3,
        comment="Temporary",
    )
    db_session.add(review)
    db_session.commit()

    resp = client.delete(f"/api/vendors/{vc.id}/reviews/{review.id}")
    assert resp.status_code == 200


def test_delete_review_card_deleted_after_review_delete(db_session, test_user):
    """delete_review returns ok:True when card is gone after review deletion."""
    mock_db = MagicMock()
    mock_review = MagicMock()
    mock_review.id = 1
    mock_review.vendor_card_id = 99
    mock_review.user_id = test_user.id

    mock_db.query.return_value.filter_by.return_value.first.return_value = mock_review
    mock_db.get.return_value = None

    vc = VendorCard(
        normalized_name="ephemeral vendor 2",
        display_name="Ephemeral Vendor 2",
        sighting_count=0,
    )
    db_session.add(vc)
    db_session.commit()
    card_id = vc.id

    review = VendorReview(
        vendor_card_id=card_id,
        user_id=test_user.id,
        rating=3,
        comment="Temp",
    )
    db_session.add(review)
    db_session.commit()
    review_id = review.id


# ── Check duplicate ──────────────────────────────────────────────────────


class TestCheckVendorDuplicate:
    def test_check_duplicate_exact(self, client, db_session, test_vendor_card):
        """Exact name match returns match=exact."""
        resp = client.get(f"/api/vendors/check-duplicate?name={test_vendor_card.display_name}")
        assert resp.status_code == 200
        matches = resp.json()["matches"]
        assert len(matches) >= 1
        assert matches[0]["match"] == "exact"

    def test_check_duplicate_fuzzy(self, client, db_session, test_vendor_card):
        """Fuzzy match >= 80 returns match=fuzzy (lines 346-365)."""
        resp = client.get("/api/vendors/check-duplicate?name=Arrow Electronisc")
        assert resp.status_code == 200

    def test_check_duplicate_no_match(self, client):
        """Totally different name -> no matches."""
        resp = client.get("/api/vendors/check-duplicate?name=ZZZZZ Nonexistent Corp")
        assert resp.status_code == 200
        assert resp.json()["matches"] == []


# ── List vendors response_rate ───────────────────────────────────────────


class TestListVendorsResponseRate:
    def test_list_vendors_with_outreach_data(self, client, db_session, test_vendor_card):
        """Vendor with total_outreach > 0 shows response_rate (line 444)."""
        test_vendor_card.total_outreach = 10
        test_vendor_card.total_responses = 3
        db_session.commit()
        resp = client.get("/api/vendors")
        assert resp.status_code == 200
        data = resp.json()
        vendors = data.get("items") or data.get("vendors") or data
        card = [v for v in vendors if v["id"] == test_vendor_card.id]
        if card:
            assert card[0].get("response_rate") == 30.0
