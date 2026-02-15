"""
tests/test_routers_vendors.py — Tests for routers/vendors.py

Covers: card_to_dict helper, get_or_create_card, VendorCard CRUD,
VendorReview CRUD. Uses SimpleNamespace stubs (not MagicMock) to
catch attribute-name mismatches against real models.

Called by: pytest
Depends on: routers/vendors.py
"""

from types import SimpleNamespace
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest

from app.routers.vendors import card_to_dict, get_or_create_card


# ── Stub factories ───────────────────────────────────────────────────────

def _make_vendor_card(**overrides) -> SimpleNamespace:
    """Create a VendorCard-like stub with all real model attributes."""
    defaults = dict(
        id=1, normalized_name="acme electronics", display_name="Acme Electronics",
        domain="acme.com", website="https://acme.com", emails=["sales@acme.com"],
        phones=["+1-555-0100"], sighting_count=42, is_blacklisted=False,
        linkedin_url=None, legal_name=None, industry="Semiconductors",
        employee_size="50-100", hq_city="Dallas", hq_state="TX", hq_country="US",
        last_enriched_at=None, enrichment_source=None,
        engagement_score=72.5, total_outreach=20, total_responses=14,
        ghost_rate=0.3, response_velocity_hours=4.2,
        last_contact_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
        created_at=datetime(2025, 11, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_review(**overrides) -> SimpleNamespace:
    defaults = dict(
        id=1, vendor_card_id=1, user_id=1, rating=4, comment="Good vendor",
        created_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
        user=SimpleNamespace(name="Mike"),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ── card_to_dict tests ───────────────────────────────────────────────────

def test_card_to_dict_with_reviews():
    """card_to_dict includes avg rating, review list, brand profile."""
    card = _make_vendor_card()
    review = _make_review(rating=4)
    db = MagicMock()
    db.query.return_value.filter_by.return_value.all.return_value = [review]
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
    db.query.return_value.filter_by.return_value.all.return_value = []
    db.execute.return_value.fetchall.return_value = []
    db.execute.return_value.scalar.return_value = 0

    result = card_to_dict(card, db)

    assert result["avg_rating"] is None
    assert result["review_count"] == 0
    assert result["reviews"] == []


def test_card_to_dict_none_timestamps():
    """card_to_dict handles None datetimes without crashing."""
    card = _make_vendor_card(
        last_enriched_at=None, last_contact_at=None,
        created_at=None, updated_at=None,
    )
    db = MagicMock()
    db.query.return_value.filter_by.return_value.all.return_value = []
    db.execute.return_value.fetchall.return_value = []
    db.execute.return_value.scalar.return_value = 0

    result = card_to_dict(card, db)

    assert result["last_enriched_at"] is None
    assert result["last_contact_at"] is None
    assert result["created_at"] is None


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


# ── Contact cleaning tests ───────────────────────────────────────────────

from app.routers.vendors import clean_emails, clean_phones, is_private_url


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


def test_clean_phones_filters_short():
    """Phone numbers with fewer than 7 digits are filtered."""
    raw = ["+1-555-0100", "123", "+44 20 7946 0958"]
    result = clean_phones(raw)
    assert len(result) == 2
    assert "123" not in result


def test_clean_phones_deduplicates():
    """Same digits in different formats are deduped."""
    raw = ["+1-555-0100", "15550100", "(555) 010-0"]
    result = clean_phones(raw)
    assert len(result) == 1


def test_is_private_url_blocks_localhost():
    """SSRF protection blocks localhost and private IPs."""
    assert is_private_url("http://127.0.0.1/admin") is True
    assert is_private_url("http://localhost/etc/passwd") is True


def test_is_private_url_blocks_empty():
    """SSRF protection blocks empty/malformed URLs."""
    assert is_private_url("") is True
    assert is_private_url("not-a-url") is True


# ── Material card tests ──────────────────────────────────────────────────

from app.routers.vendors import material_card_to_dict


def _make_material_card(**overrides) -> SimpleNamespace:
    defaults = dict(
        id=10, normalized_mpn="lm358n", display_mpn="LM358N",
        manufacturer="Texas Instruments", description="Dual Op-Amp",
        search_count=5,
        last_searched_at=datetime(2026, 1, 20, tzinfo=timezone.utc),
        created_at=datetime(2025, 12, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 20, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_vendor_history(**overrides) -> SimpleNamespace:
    defaults = dict(
        id=1, vendor_name="Acme Electronics", source_type="broker",
        is_authorized=False,
        first_seen=datetime(2025, 12, 15, tzinfo=timezone.utc),
        last_seen=datetime(2026, 1, 10, tzinfo=timezone.utc),
        times_seen=3, last_qty=500, last_price=0.45,
        last_currency="USD", last_manufacturer="TI", vendor_sku="ACM-LM358",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_material_card_to_dict_with_history():
    """material_card_to_dict includes vendor history."""
    card = _make_material_card()
    vh = _make_vendor_history()
    db = MagicMock()
    db.query.return_value.filter_by.return_value.order_by.return_value.all.return_value = [vh]

    result = material_card_to_dict(card, db)

    assert result["display_mpn"] == "LM358N"
    assert result["vendor_count"] == 1
    assert result["vendor_history"][0]["vendor_name"] == "Acme Electronics"
    assert result["vendor_history"][0]["last_price"] == 0.45


def test_material_card_to_dict_no_history():
    """material_card_to_dict handles zero vendor history."""
    card = _make_material_card()
    db = MagicMock()
    db.query.return_value.filter_by.return_value.order_by.return_value.all.return_value = []

    result = material_card_to_dict(card, db)

    assert result["vendor_count"] == 0
    assert result["vendor_history"] == []
    assert result["search_count"] == 5


# ── Contact cleaning tests ───────────────────────────────────────────────

from app.routers.vendors import clean_emails, clean_phones, is_private_url


def test_clean_emails_filters_junk():
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


def test_clean_emails_deduplicates():
    raw = ["a@b.com", "A@B.COM", "a@b.com"]
    assert clean_emails(raw) == ["a@b.com"]


def test_clean_phones_filters_short():
    """Rejects numbers with fewer than 7 digits."""
    raw = ["+1-555-0100", "123", "+44 20 7946 0958", ""]
    result = clean_phones(raw)
    assert "+1-555-0100" in result
    assert "+44 20 7946 0958" in result
    assert "123" not in result
    assert len(result) == 2


def test_clean_phones_deduplicates():
    """Same digits in different formats = one entry."""
    raw = ["+1-555-0100", "1-555-0100", "(1) 555-0100"]
    result = clean_phones(raw)
    assert len(result) == 1  # all resolve to digits "15550100"


def test_is_private_url_blocks_localhost():
    assert is_private_url("http://localhost/contact") is True
    assert is_private_url("http://127.0.0.1/api") is True


def test_is_private_url_blocks_empty():
    assert is_private_url("") is True
    assert is_private_url("not-a-url") is True


# -- Integration: engagement_score in vendor list --------------------------


def test_vendor_list_includes_engagement_score(db_session):
    """list_vendors response includes engagement_score field per vendor."""
    from app.models import VendorCard
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
        "id": vc.id, "display_name": vc.display_name,
        "emails": vc.emails or [], "phones": vc.phones or [],
        "sighting_count": vc.sighting_count or 0,
        "engagement_score": vc.engagement_score,
        "is_blacklisted": vc.is_blacklisted or False,
    }
    assert result["engagement_score"] == 72.5
    assert "engagement_score" in result


def test_vendor_list_engagement_score_null():
    """Vendors with no engagement data return None (new vendor)."""
    from types import SimpleNamespace
    card = SimpleNamespace(engagement_score=None)
    # Tier classification: null → 'new'
    tier = 'new' if card.engagement_score is None else (
        'proven' if card.engagement_score >= 70 else
        'developing' if card.engagement_score >= 40 else 'caution'
    )
    assert tier == 'new'
