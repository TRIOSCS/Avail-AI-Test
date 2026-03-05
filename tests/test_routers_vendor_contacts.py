"""
tests/test_routers_vendor_contacts.py — Tests for routers/vendor_contacts.py

Covers: contact lookup waterfall (tier 1-3), vendor contacts CRUD,
bulk contacts, email metrics, add-email-to-card.

Called by: pytest
Depends on: routers/vendor_contacts.py, utils/vendor_helpers.py
"""

from datetime import datetime, timezone

from app.models import Contact, Requisition, VendorCard, VendorContact, VendorResponse

# ── Contacts CRUD ────────────────────────────────────────────────────────


def test_list_vendor_contacts(client, db_session, test_vendor_card, test_vendor_contact):
    """GET /api/vendors/{id}/contacts returns the contacts list."""
    resp = client.get(f"/api/vendors/{test_vendor_card.id}/contacts")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    emails = [c["email"] for c in data]
    assert "john@arrow.com" in emails


def test_add_vendor_contact(client, db_session, test_vendor_card):
    """POST /api/vendors/{id}/contacts with email+name succeeds."""
    resp = client.post(
        f"/api/vendors/{test_vendor_card.id}/contacts",
        json={"email": "jane@arrow.com", "full_name": "Jane Buyer"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["duplicate"] is False
    assert "id" in data


def test_add_vendor_contact_duplicate(client, db_session, test_vendor_card, test_vendor_contact):
    """POST same email twice returns duplicate=True."""
    resp = client.post(
        f"/api/vendors/{test_vendor_card.id}/contacts",
        json={"email": "john@arrow.com", "full_name": "John Sales"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["duplicate"] is True


def test_add_vendor_contact_not_found(client):
    """POST /api/vendors/99999/contacts returns 404."""
    resp = client.post(
        "/api/vendors/99999/contacts",
        json={"email": "x@y.com", "full_name": "Nobody"},
    )
    assert resp.status_code == 404


def test_update_vendor_contact(client, db_session, test_vendor_card, test_vendor_contact):
    """PUT /api/vendors/{card_id}/contacts/{contact_id} updates the contact."""
    resp = client.put(
        f"/api/vendors/{test_vendor_card.id}/contacts/{test_vendor_contact.id}",
        json={"full_name": "John Updated", "title": "VP Sales"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True


def test_update_vendor_contact_email_conflict(client, db_session, test_vendor_card, test_vendor_contact):
    """PUT with email that conflicts with another contact returns 409."""
    vc2 = VendorContact(
        vendor_card_id=test_vendor_card.id,
        full_name="Other Person",
        email="other@arrow.com",
        source="manual",
        is_verified=True,
        confidence=80,
    )
    db_session.add(vc2)
    db_session.commit()

    resp = client.put(
        f"/api/vendors/{test_vendor_card.id}/contacts/{vc2.id}",
        json={"email": "john@arrow.com"},
    )
    assert resp.status_code == 409


def test_delete_vendor_contact(client, db_session, test_vendor_card, test_vendor_contact):
    """DELETE /api/vendors/{card_id}/contacts/{contact_id} removes the contact."""
    resp = client.delete(f"/api/vendors/{test_vendor_card.id}/contacts/{test_vendor_contact.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True


def test_delete_vendor_contact_not_found(client, db_session, test_vendor_card):
    """DELETE nonexistent contact returns 404."""
    resp = client.delete(f"/api/vendors/{test_vendor_card.id}/contacts/99999")
    assert resp.status_code == 404


def test_update_vendor_contact_change_email(client, db_session, test_vendor_card, test_vendor_contact):
    """PUT contact with new email updates email and syncs legacy emails[]."""
    old_email = test_vendor_contact.email
    test_vendor_card.emails = [old_email, "other@arrow.com"]
    db_session.commit()

    resp = client.put(
        f"/api/vendors/{test_vendor_card.id}/contacts/{test_vendor_contact.id}",
        json={"email": "newemail@arrow.com"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True

    db_session.refresh(test_vendor_card)
    assert "newemail@arrow.com" in test_vendor_card.emails
    assert old_email not in test_vendor_card.emails


def test_update_vendor_contact_label_and_phone(client, db_session, test_vendor_card, test_vendor_contact):
    """PUT contact with label and phone updates both fields."""
    resp = client.put(
        f"/api/vendors/{test_vendor_card.id}/contacts/{test_vendor_contact.id}",
        json={"label": "Purchasing", "phone": "+1-555-9999"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True


def test_update_vendor_contact_not_found(client, db_session, test_vendor_card):
    """PUT nonexistent contact returns 404."""
    resp = client.put(
        f"/api/vendors/{test_vendor_card.id}/contacts/99999",
        json={"full_name": "Ghost"},
    )
    assert resp.status_code == 404


def test_update_vendor_contact_set_company_type(client, db_session, test_vendor_card, test_vendor_contact):
    """PUT contact with empty full_name sets contact_type to company."""
    resp = client.put(
        f"/api/vendors/{test_vendor_card.id}/contacts/{test_vendor_contact.id}",
        json={"full_name": ""},
    )
    assert resp.status_code == 200


def test_delete_vendor_contact_cleans_legacy_emails(client, db_session, test_vendor_card, test_vendor_contact):
    """DELETE contact removes email from card's legacy emails[] array."""
    test_vendor_card.emails = ["john@arrow.com", "other@arrow.com"]
    db_session.commit()

    resp = client.delete(f"/api/vendors/{test_vendor_card.id}/contacts/{test_vendor_contact.id}")
    assert resp.status_code == 200

    db_session.refresh(test_vendor_card)
    assert "john@arrow.com" not in test_vendor_card.emails
    assert "other@arrow.com" in test_vendor_card.emails


def test_add_vendor_contact_adds_to_legacy_emails(client, db_session, test_vendor_card):
    """POST /api/vendors/{id}/contacts adds email to card's legacy emails[]."""
    original_emails = test_vendor_card.emails or []
    resp = client.post(
        f"/api/vendors/{test_vendor_card.id}/contacts",
        json={"email": "legacy@arrow.com"},
    )
    assert resp.status_code == 200

    db_session.refresh(test_vendor_card)
    assert "legacy@arrow.com" in test_vendor_card.emails


def test_add_vendor_contact_company_type(client, db_session, test_vendor_card):
    """POST /api/vendors/{id}/contacts without full_name sets type to company."""
    resp = client.post(
        f"/api/vendors/{test_vendor_card.id}/contacts",
        json={"email": "company@arrow.com"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["duplicate"] is False


# ── Bulk vendor contacts ─────────────────────────────────────────────────


def test_vendor_contacts_bulk_empty(client, db_session):
    """GET /api/vendor-contacts/bulk with no data returns empty items."""
    resp = client.get("/api/vendor-contacts/bulk")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0
    assert "limit" in data
    assert "offset" in data


def test_vendor_contacts_bulk_with_data(client, db_session, test_vendor_card, test_vendor_contact):
    """GET /api/vendor-contacts/bulk returns contacts with vendor_name."""
    resp = client.get("/api/vendor-contacts/bulk")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert len(data["items"]) >= 1
    item = data["items"][0]
    assert "vendor_name" in item
    assert item["vendor_name"] == "Arrow Electronics"
    assert "email" in item
    assert item["email"] == "john@arrow.com"


def test_vendor_contacts_bulk_pagination(client, db_session, test_vendor_card, test_vendor_contact):
    """Bulk endpoint respects limit and offset."""
    resp = client.get("/api/vendor-contacts/bulk", params={"limit": 1, "offset": 0})
    assert resp.status_code == 200
    data = resp.json()
    assert data["limit"] == 1
    assert data["offset"] == 0
    assert len(data["items"]) <= 1


def test_vendor_contacts_bulk_excludes_blacklisted(client, db_session, test_vendor_card, test_vendor_contact):
    """Blacklisted vendor contacts are excluded from bulk response."""
    test_vendor_card.is_blacklisted = True
    db_session.commit()
    resp = client.get("/api/vendor-contacts/bulk")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0


# ── Email Metrics ────────────────────────────────────────────────────────


def test_email_metrics(client, db_session, test_vendor_card):
    """GET /api/vendors/{id}/email-metrics returns 200 with metric fields."""
    resp = client.get(f"/api/vendors/{test_vendor_card.id}/email-metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert "vendor_name" in data or "total_rfqs_sent" in data


def test_email_metrics_not_found(client):
    """GET /api/vendors/99999/email-metrics returns 404."""
    resp = client.get("/api/vendors/99999/email-metrics")
    assert resp.status_code == 404


def test_email_metrics_with_contacts_and_responses(client, db_session, test_vendor_card, test_user):
    """GET /api/vendors/{id}/email-metrics with contact/response data."""
    req = Requisition(
        name="REQ-METRIC-001",
        customer_name="Test Co",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    c1 = Contact(
        requisition_id=req.id,
        user_id=test_user.id,
        contact_type="email",
        vendor_name="Arrow Electronics",
        status="responded",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    c2 = Contact(
        requisition_id=req.id,
        user_id=test_user.id,
        contact_type="email",
        vendor_name="Arrow Electronics",
        status="quoted",
        created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    c3 = Contact(
        requisition_id=req.id,
        user_id=test_user.id,
        contact_type="email",
        vendor_name="Arrow Electronics",
        status="sent",
        created_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
    )
    db_session.add_all([c1, c2, c3])
    db_session.flush()

    vr = VendorResponse(
        contact_id=c1.id,
        vendor_name="Arrow Electronics",
        received_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
    )
    db_session.add(vr)
    db_session.commit()

    resp = client.get(f"/api/vendors/{test_vendor_card.id}/email-metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_rfqs_sent"] == 3
    assert data["total_replies"] == 2  # responded + quoted
    assert data["total_quotes"] == 1
    assert data["active_rfqs"] == 1  # sent


# ── Contact Lookup Waterfall ─────────────────────────────────────────────


def test_lookup_tier1_cached(client, db_session, test_vendor_card):
    """Vendor with existing emails returns tier=1, source=cached."""
    resp = client.post(
        "/api/vendor-contact",
        json={"vendor_name": "Arrow Electronics"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["tier"] == 1
    assert data["source"] == "cached"
    assert "sales@arrow.com" in data["emails"]


def test_lookup_tier2_scrape(client, db_session, monkeypatch):
    """Vendor with website but no emails triggers scrape (tier=2)."""
    vc = VendorCard(
        normalized_name="scrapetest co",
        display_name="ScrapeTest Co",
        website="https://scrapetest.example.com",
        emails=[],
        phones=[],
        sighting_count=1,
    )
    db_session.add(vc)
    db_session.commit()

    async def mock_scrape(url):
        return {"emails": ["found@scrapetest.com"], "phones": ["+1-555-9999"]}

    monkeypatch.setattr("app.routers.vendor_contacts.scrape_website_contacts", mock_scrape)

    resp = client.post(
        "/api/vendor-contact",
        json={"vendor_name": "ScrapeTest Co"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["tier"] == 2
    assert data["source"] == "website_scrape"


def test_lookup_tier3_ai(client, db_session, monkeypatch):
    """Vendor with no website/emails triggers AI lookup (tier=3)."""
    vc = VendorCard(
        normalized_name="aitest vendor",
        display_name="AITest Vendor",
        emails=[],
        phones=[],
        sighting_count=1,
    )
    db_session.add(vc)
    db_session.commit()

    monkeypatch.setattr(
        "app.routers.vendor_contacts.get_credential_cached",
        lambda *args, **kwargs: "fake-api-key",
    )

    async def mock_claude_json(**kwargs):
        return {
            "emails": ["ai@aitest.com"],
            "phones": ["+1-555-8888"],
            "website": "https://aitest.example.com",
        }

    monkeypatch.setattr("app.routers.vendor_contacts.claude_json", mock_claude_json, raising=False)
    monkeypatch.setattr("app.utils.claude_client.claude_json", mock_claude_json, raising=False)

    resp = client.post(
        "/api/vendor-contact",
        json={"vendor_name": "AITest Vendor"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["tier"] == 3
    assert data["source"] == "ai_lookup"


def test_lookup_no_api_key(client, db_session, monkeypatch):
    """Vendor with no emails/website and no API key returns tier=0."""
    vc = VendorCard(
        normalized_name="nokey vendor",
        display_name="NoKey Vendor",
        emails=[],
        phones=[],
        sighting_count=1,
    )
    db_session.add(vc)
    db_session.commit()

    monkeypatch.setattr(
        "app.routers.vendor_contacts.get_credential_cached",
        lambda *args, **kwargs: None,
    )

    resp = client.post(
        "/api/vendor-contact",
        json={"vendor_name": "NoKey Vendor"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["tier"] == 0
    assert "error" in data


def test_lookup_creates_card(client, db_session, monkeypatch):
    """Lookup for nonexistent vendor creates a new VendorCard."""
    monkeypatch.setattr(
        "app.routers.vendor_contacts.get_credential_cached",
        lambda *args, **kwargs: None,
    )

    resp = client.post(
        "/api/vendor-contact",
        json={"vendor_name": "Brand New Vendor XYZ"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["card_id"] is not None

    from app.vendor_utils import normalize_vendor_name

    norm = normalize_vendor_name("Brand New Vendor XYZ")
    card = db_session.query(VendorCard).filter_by(normalized_name=norm).first()
    assert card is not None


def test_lookup_ssrf_blocked(client, db_session, monkeypatch):
    """Vendor with private URL returns empty contacts from scrape."""
    vc = VendorCard(
        normalized_name="ssrf test vendor",
        display_name="SSRF Test Vendor",
        website="http://127.0.0.1/evil",
        emails=[],
        phones=[],
        sighting_count=1,
    )
    db_session.add(vc)
    db_session.commit()

    async def mock_scrape(url):
        return {"emails": [], "phones": []}

    monkeypatch.setattr("app.routers.vendor_contacts.scrape_website_contacts", mock_scrape)
    monkeypatch.setattr(
        "app.routers.vendor_contacts.get_credential_cached",
        lambda *args, **kwargs: None,
    )

    resp = client.post(
        "/api/vendor-contact",
        json={"vendor_name": "SSRF Test Vendor"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["emails"] == []
    assert data["tier"] == 0


def test_lookup_creates_card_integrity_error(client, db_session, monkeypatch):
    """lookup_vendor_contact handles IntegrityError on card creation (race condition)."""
    vc = VendorCard(
        normalized_name="race vendor",
        display_name="Race Vendor",
        emails=["already@race.com"],
        sighting_count=1,
    )
    db_session.add(vc)
    db_session.commit()

    resp = client.post("/api/vendor-contact", json={"vendor_name": "Race Vendor"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["tier"] == 1  # Found cached emails


def test_lookup_tier2_scrape_no_emails_after_merge(client, db_session, monkeypatch):
    """Tier 2: scrape returns data but merge doesn't produce card.emails."""
    vc = VendorCard(
        normalized_name="scrape empty vendor",
        display_name="Scrape Empty Vendor",
        website="https://emptyresult.com",
        emails=[],
        phones=[],
        sighting_count=1,
    )
    db_session.add(vc)
    db_session.commit()

    async def mock_scrape(url):
        return {"emails": ["found@scrape.com"], "phones": []}

    monkeypatch.setattr("app.routers.vendor_contacts.scrape_website_contacts", mock_scrape)
    monkeypatch.setattr("app.routers.vendor_contacts.merge_contact_into_card", lambda *a, **kw: False)
    monkeypatch.setattr("app.routers.vendor_contacts.get_credential_cached", lambda *a, **kw: None)

    resp = client.post("/api/vendor-contact", json={"vendor_name": "Scrape Empty Vendor"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["tier"] == 0


def test_lookup_tier2_scrape_exception(client, db_session, monkeypatch):
    """Tier 2: scrape throws exception, falls through to tier 3."""
    vc = VendorCard(
        normalized_name="scrape fail vendor",
        display_name="Scrape Fail Vendor",
        website="https://fails.com",
        emails=[],
        phones=[],
        sighting_count=1,
    )
    db_session.add(vc)
    db_session.commit()

    async def mock_scrape(url):
        raise ConnectionError("Timeout")

    monkeypatch.setattr("app.routers.vendor_contacts.scrape_website_contacts", mock_scrape)
    monkeypatch.setattr("app.routers.vendor_contacts.get_credential_cached", lambda *a, **kw: None)

    resp = client.post("/api/vendor-contact", json={"vendor_name": "Scrape Fail Vendor"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["tier"] == 0


def test_lookup_tier3_ai_string_emails(client, db_session, monkeypatch):
    """Tier 3: AI returns emails as a string instead of list."""
    vc = VendorCard(
        normalized_name="stringemail vendor",
        display_name="StringEmail Vendor",
        emails=[],
        phones=[],
        sighting_count=1,
    )
    db_session.add(vc)
    db_session.commit()

    monkeypatch.setattr("app.routers.vendor_contacts.get_credential_cached", lambda *a, **kw: "fake-key")

    async def mock_claude_json(**kwargs):
        return {
            "emails": "single@vendor.com",  # string, not list
            "phones": "+1-555-0001",  # string, not list
            "email": "extra@vendor.com",  # singular key
            "phone": "+1-555-0002",  # singular key
            "website": "https://stringemail.com",
        }

    monkeypatch.setattr("app.utils.claude_client.claude_json", mock_claude_json)

    resp = client.post("/api/vendor-contact", json={"vendor_name": "StringEmail Vendor"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["tier"] == 3
    assert data["source"] == "ai_lookup"


def test_lookup_tier3_ai_returns_none(client, db_session, monkeypatch):
    """Tier 3: AI returns None/non-dict."""
    vc = VendorCard(
        normalized_name="nullai vendor",
        display_name="NullAI Vendor",
        emails=[],
        phones=[],
        sighting_count=1,
    )
    db_session.add(vc)
    db_session.commit()

    monkeypatch.setattr("app.routers.vendor_contacts.get_credential_cached", lambda *a, **kw: "fake-key")

    async def mock_claude_json(**kwargs):
        return None

    monkeypatch.setattr("app.utils.claude_client.claude_json", mock_claude_json)

    resp = client.post("/api/vendor-contact", json={"vendor_name": "NullAI Vendor"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["tier"] == 3


def test_lookup_tier3_ai_exception(client, db_session, monkeypatch):
    """Tier 3: AI lookup throws exception returns tier=0 with error."""
    vc = VendorCard(
        normalized_name="ai error vendor",
        display_name="AI Error Vendor",
        emails=[],
        phones=[],
        sighting_count=1,
    )
    db_session.add(vc)
    db_session.commit()

    monkeypatch.setattr("app.routers.vendor_contacts.get_credential_cached", lambda *a, **kw: "fake-key")

    async def mock_claude_json(**kwargs):
        raise RuntimeError("API quota exceeded")

    monkeypatch.setattr("app.utils.claude_client.claude_json", mock_claude_json)

    resp = client.post("/api/vendor-contact", json={"vendor_name": "AI Error Vendor"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["tier"] == 0
    assert "error" in data
    assert "API quota exceeded" in data["error"]


def test_lookup_tier3_ai_with_website_hint(client, db_session, monkeypatch):
    """Tier 3: AI lookup includes website hint when card has a website."""
    vc = VendorCard(
        normalized_name="hinted vendor",
        display_name="Hinted Vendor",
        website="https://hinted.com",
        emails=[],
        phones=[],
        sighting_count=1,
    )
    db_session.add(vc)
    db_session.commit()

    async def mock_scrape(url):
        return {"emails": [], "phones": []}

    monkeypatch.setattr("app.routers.vendor_contacts.scrape_website_contacts", mock_scrape)
    monkeypatch.setattr("app.routers.vendor_contacts.get_credential_cached", lambda *a, **kw: "fake-key")

    async def mock_claude_json(**kwargs):
        prompt = kwargs.get("prompt", "")
        assert "hinted.com" in prompt  # website hint should be in prompt
        return {"emails": ["found@hinted.com"], "phones": [], "website": "https://hinted.com"}

    monkeypatch.setattr("app.utils.claude_client.claude_json", mock_claude_json)

    resp = client.post("/api/vendor-contact", json={"vendor_name": "Hinted Vendor"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["tier"] == 3


def test_lookup_vendor_contact_integrity_error_race(client, db_session, monkeypatch):
    """lookup_vendor_contact handles IntegrityError on card flush (race condition)."""
    from app.vendor_utils import normalize_vendor_name

    norm = normalize_vendor_name("Integrity Race Vendor")
    existing_vc = VendorCard(
        normalized_name=norm,
        display_name="Integrity Race Vendor",
        emails=["exists@race.com"],
        sighting_count=1,
    )
    db_session.add(existing_vc)
    db_session.commit()

    monkeypatch.setattr("app.routers.vendor_contacts.get_credential_cached", lambda *a, **kw: None)

    resp = client.post(
        "/api/vendor-contact",
        json={"vendor_name": "Integrity Race Vendor"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["tier"] == 1  # Found cached emails


# ── Add email to card ────────────────────────────────────────────────────


def test_add_email_to_card(client, db_session, monkeypatch):
    """POST /api/vendor-card/add-email creates/updates vendor card with email."""
    monkeypatch.setattr("asyncio.create_task", lambda coro: coro.close())
    monkeypatch.setattr(
        "app.routers.vendor_contacts.get_credential_cached",
        lambda *args, **kwargs: None,
    )

    resp = client.post(
        "/api/vendor-card/add-email",
        json={"vendor_name": "New Email Vendor", "email": "rfq@newemailvendor.com"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["card_id"] is not None
    assert "rfq@newemailvendor.com" in data["emails"]


def test_add_email_generic_domain(client, db_session, monkeypatch):
    """add_email_to_card with generic domain (gmail) does not set card.domain."""
    monkeypatch.setattr("asyncio.create_task", lambda coro: coro.close())
    monkeypatch.setattr("app.routers.vendor_contacts.get_credential_cached", lambda *a, **kw: None)

    resp = client.post(
        "/api/vendor-card/add-email",
        json={"vendor_name": "Gmail Vendor", "email": "vendor@gmail.com"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["domain"] is None


def test_add_email_business_domain(client, db_session, monkeypatch):
    """add_email_to_card with business domain sets card.domain."""
    monkeypatch.setattr("asyncio.create_task", lambda coro: coro.close())
    monkeypatch.setattr("app.routers.vendor_contacts.get_credential_cached", lambda *a, **kw: None)

    resp = client.post(
        "/api/vendor-card/add-email",
        json={"vendor_name": "Business Vendor", "email": "sales@businessvendor.com"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["domain"] == "businessvendor.com"


def test_add_email_existing_contact(client, db_session, monkeypatch):
    """add_email_to_card with existing contact does not create duplicate VendorContact."""
    monkeypatch.setattr("asyncio.create_task", lambda coro: coro.close())
    monkeypatch.setattr("app.routers.vendor_contacts.get_credential_cached", lambda *a, **kw: None)

    resp1 = client.post(
        "/api/vendor-card/add-email",
        json={"vendor_name": "DupeEmail Vendor", "email": "dupe@dupevendor.com"},
    )
    assert resp1.status_code == 200
    assert resp1.json()["contact_created"] is True

    resp2 = client.post(
        "/api/vendor-card/add-email",
        json={"vendor_name": "DupeEmail Vendor", "email": "dupe@dupevendor.com"},
    )
    assert resp2.status_code == 200
    assert resp2.json()["contact_created"] is False


def test_add_email_triggers_enrichment(client, db_session, monkeypatch):
    """add_email_to_card triggers background enrichment when credentials exist."""
    task_created = []
    monkeypatch.setattr("asyncio.create_task", lambda coro: (task_created.append(True), coro.close()))
    monkeypatch.setattr("app.routers.vendor_contacts.get_credential_cached", lambda *a, **kw: "fake-key")

    resp = client.post(
        "/api/vendor-card/add-email",
        json={"vendor_name": "Enrich Trigger Vendor", "email": "trigger@enrichvendor.com"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["enrich_triggered"] is True


def test_add_email_replaces_existing_email(client, db_session, monkeypatch):
    """add_email_to_card replaces existing case-insensitive duplicate in emails[]."""
    monkeypatch.setattr("asyncio.create_task", lambda coro: coro.close())
    monkeypatch.setattr("app.routers.vendor_contacts.get_credential_cached", lambda *a, **kw: None)

    resp1 = client.post(
        "/api/vendor-card/add-email",
        json={"vendor_name": "Case Replace Vendor", "email": "sales@casevendor.com"},
    )
    assert resp1.status_code == 200

    resp2 = client.post(
        "/api/vendor-card/add-email",
        json={"vendor_name": "Case Replace Vendor", "email": "sales@casevendor.com"},
    )
    assert resp2.status_code == 200
    data = resp2.json()
    assert data["emails"].count("sales@casevendor.com") == 1
