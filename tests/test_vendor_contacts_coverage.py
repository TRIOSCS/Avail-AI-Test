"""tests/test_vendor_contacts_coverage.py — Coverage tests for app/routers/vendor_contacts.py.

Endpoints covered:
  POST /api/vendor-contact                                (3-tier lookup)
  GET  /api/vendor-contacts/bulk
  GET  /api/vendors/{card_id}/contacts
  GET  /api/vendors/{card_id}/contacts/{contact_id}/timeline
  GET  /api/vendors/{card_id}/contact-nudges
  GET  /api/vendors/{card_id}/contacts/{contact_id}/summary
  POST /api/vendors/{card_id}/contacts/{contact_id}/log-call
  POST /api/vendors/{card_id}/contacts                    (add contact)
  PUT  /api/vendors/{card_id}/contacts/{contact_id}
  DELETE /api/vendors/{card_id}/contacts/{contact_id}
  GET  /api/vendors/{card_id}/email-metrics
  POST /api/vendor-card/add-email

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_vendor_card, test_user)
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy.orm import Session

from app.models import ActivityLog, VendorCard, VendorContact

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_card(db: Session, name: str, **kwargs) -> VendorCard:
    card = VendorCard(
        normalized_name=name.lower(),
        display_name=name,
        **kwargs,
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


def _make_contact(db: Session, card: VendorCard, email: str, **kwargs) -> VendorContact:
    vc = VendorContact(
        vendor_card_id=card.id,
        email=email,
        source="manual",
        **kwargs,
    )
    db.add(vc)
    db.commit()
    db.refresh(vc)
    return vc


# ---------------------------------------------------------------------------
# POST /api/vendor-contact  (3-tier lookup)
# ---------------------------------------------------------------------------


class TestLookupVendorContact:
    def test_tier1_cache_hit_returns_cached(self, client, db_session):
        """Vendor already has emails → instant cache return (tier 1)."""
        _make_card(db_session, "Cache Vendor", emails=["cache@vendor.com"])
        resp = client.post("/api/vendor-contact", json={"vendor_name": "Cache Vendor"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["tier"] == 1
        assert data["source"] == "cached"
        assert "cache@vendor.com" in data["emails"]

    def test_creates_new_card_if_not_found(self, client, db_session):
        """Vendor not in DB → card auto-created, tier returned."""
        with patch("app.routers.vendor_contacts.get_credential_cached", return_value=None):
            resp = client.post("/api/vendor-contact", json={"vendor_name": "Brand New Vendor ABC"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["vendor_name"] == "Brand New Vendor ABC"
        assert data["card_id"] is not None

    def test_tier2_website_scrape(self, client, db_session):
        """Card has website but no emails → scrape path."""
        _make_card(db_session, "Scrape Vendor", emails=[], website="https://scrapevendor.com")
        scraped = {"emails": ["info@scrapevendor.com"], "phones": []}
        with patch("app.routers.vendor_contacts.scrape_website_contacts", new_callable=AsyncMock, return_value=scraped):
            resp = client.post("/api/vendor-contact", json={"vendor_name": "Scrape Vendor"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["tier"] == 2
        assert data["source"] == "website_scrape"

    def test_tier2_scrape_failure_falls_through(self, client, db_session):
        """Scrape raises exception → falls through to tier 3 / no-key path."""
        _make_card(db_session, "Broken Scrape Vendor", emails=[], website="https://broken.com")
        with patch(
            "app.routers.vendor_contacts.scrape_website_contacts",
            new_callable=AsyncMock,
            side_effect=Exception("scrape error"),
        ):
            with patch("app.routers.vendor_contacts.get_credential_cached", return_value=None):
                resp = client.post("/api/vendor-contact", json={"vendor_name": "Broken Scrape Vendor"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["tier"] == 0

    def test_tier3_no_api_key_returns_tier0(self, client, db_session):
        """No API key configured → tier 0 with error."""
        _make_card(db_session, "No Key Vendor", emails=[])
        with patch("app.routers.vendor_contacts.get_credential_cached", return_value=None):
            resp = client.post("/api/vendor-contact", json={"vendor_name": "No Key Vendor"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["tier"] == 0
        assert "error" in data

    def test_tier3_ai_lookup_success(self, client, db_session):
        """AI key present → claude_json called, tier 3 returned."""
        _make_card(db_session, "AI Lookup Vendor", emails=[])
        ai_result = {"emails": ["sales@ailookup.com"], "phones": ["+1-555-1234"], "website": "https://ailookup.com"}
        with patch("app.routers.vendor_contacts.get_credential_cached", return_value="sk-fake-key"):
            with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=ai_result):
                resp = client.post("/api/vendor-contact", json={"vendor_name": "AI Lookup Vendor"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["tier"] == 3
        assert data["source"] == "ai_lookup"

    def test_tier3_ai_lookup_exception_returns_tier0(self, client, db_session):
        """AI lookup raises → tier 0 with error string."""
        _make_card(db_session, "AI Error Vendor", emails=[])
        with patch("app.routers.vendor_contacts.get_credential_cached", return_value="sk-fake-key"):
            with patch(
                "app.utils.claude_client.claude_json",
                new_callable=AsyncMock,
                side_effect=Exception("AI error"),
            ):
                resp = client.post("/api/vendor-contact", json={"vendor_name": "AI Error Vendor"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["tier"] == 0

    def test_empty_vendor_name_returns_422(self, client):
        resp = client.post("/api/vendor-contact", json={"vendor_name": "  "})
        assert resp.status_code == 422

    def test_missing_vendor_name_returns_422(self, client):
        resp = client.post("/api/vendor-contact", json={})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/vendor-contacts/bulk
# ---------------------------------------------------------------------------


class TestBulkVendorContacts:
    def test_empty_returns_zero(self, client, db_session):
        resp = client.get("/api/vendor-contacts/bulk")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_returns_contacts_with_vendor_name(self, client, db_session, test_vendor_card):
        _make_contact(db_session, test_vendor_card, "bulk@arrow.com", full_name="Bulk Contact")
        resp = client.get("/api/vendor-contacts/bulk")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        item = data["items"][0]
        assert item["vendor_name"] == "Arrow Electronics"
        assert item["email"] == "bulk@arrow.com"

    def test_excludes_blacklisted_vendor_contacts(self, client, db_session):
        card = _make_card(db_session, "Blacklisted Vendor", is_blacklisted=True)
        _make_contact(db_session, card, "bl@blacklisted.com")
        resp = client.get("/api/vendor-contacts/bulk")
        assert resp.status_code == 200
        data = resp.json()
        assert not any(item["email"] == "bl@blacklisted.com" for item in data["items"])

    def test_pagination_limit_offset(self, client, db_session, test_vendor_card):
        for i in range(5):
            _make_contact(db_session, test_vendor_card, f"p{i}@arrow.com")
        resp = client.get("/api/vendor-contacts/bulk?limit=2&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) <= 2

    def test_limit_validation(self, client):
        resp = client.get("/api/vendor-contacts/bulk?limit=0")
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/vendors/{card_id}/contacts
# ---------------------------------------------------------------------------


class TestListVendorContacts:
    def test_empty_returns_list(self, client, db_session, test_vendor_card):
        resp = client.get(f"/api/vendors/{test_vendor_card.id}/contacts")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_contacts(self, client, db_session, test_vendor_card):
        _make_contact(db_session, test_vendor_card, "c1@arrow.com", full_name="John Smith")
        resp = client.get(f"/api/vendors/{test_vendor_card.id}/contacts")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["email"] == "c1@arrow.com"
        assert data[0]["full_name"] == "John Smith"

    def test_contact_fields_present(self, client, db_session, test_vendor_card):
        _make_contact(
            db_session,
            test_vendor_card,
            "fields@arrow.com",
            full_name="Field Test",
            title="Sales Manager",
            confidence=90,
        )
        resp = client.get(f"/api/vendors/{test_vendor_card.id}/contacts")
        assert resp.status_code == 200
        item = resp.json()[0]
        expected_keys = {
            "id",
            "contact_type",
            "full_name",
            "email",
            "phone",
            "source",
            "is_verified",
            "confidence",
            "interaction_count",
        }
        for key in expected_keys:
            assert key in item


# ---------------------------------------------------------------------------
# GET /api/vendors/{card_id}/contacts/{contact_id}/timeline
# ---------------------------------------------------------------------------


class TestGetContactTimeline:
    def test_returns_empty_timeline(self, client, db_session, test_vendor_card):
        vc = _make_contact(db_session, test_vendor_card, "tl@arrow.com")
        resp = client.get(f"/api/vendors/{test_vendor_card.id}/contacts/{vc.id}/timeline")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_activity_events(self, client, db_session, test_vendor_card, test_user):
        vc = _make_contact(db_session, test_vendor_card, "ev@arrow.com")
        activity = ActivityLog(
            user_id=test_user.id,
            activity_type="call_initiated",
            channel="phone",
            vendor_card_id=test_vendor_card.id,
            vendor_contact_id=vc.id,
            auto_logged=True,
            occurred_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(activity)
        db_session.commit()
        resp = client.get(f"/api/vendors/{test_vendor_card.id}/contacts/{vc.id}/timeline")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["activity_type"] == "call_initiated"

    def test_nonexistent_contact_returns_404(self, client, db_session, test_vendor_card):
        resp = client.get(f"/api/vendors/{test_vendor_card.id}/contacts/999999/timeline")
        assert resp.status_code == 404
        assert "error" in resp.json()

    def test_wrong_vendor_card_returns_404(self, client, db_session, test_vendor_card):
        vc = _make_contact(db_session, test_vendor_card, "wv@arrow.com")
        resp = client.get(f"/api/vendors/999999/contacts/{vc.id}/timeline")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/vendors/{card_id}/contact-nudges
# ---------------------------------------------------------------------------


class TestGetContactNudges:
    def test_returns_nudges(self, client, db_session, test_vendor_card):
        nudges = [{"contact_id": 1, "nudge": "follow up"}]
        with patch("app.services.contact_intelligence.generate_contact_nudges", return_value=nudges):
            resp = client.get(f"/api/vendors/{test_vendor_card.id}/contact-nudges")
        assert resp.status_code == 200

    def test_nonexistent_vendor_returns_404(self, client):
        resp = client.get("/api/vendors/999999/contact-nudges")
        assert resp.status_code == 404
        assert "error" in resp.json()


# ---------------------------------------------------------------------------
# GET /api/vendors/{card_id}/contacts/{contact_id}/summary
# ---------------------------------------------------------------------------


class TestGetContactSummary:
    def test_returns_summary(self, client, db_session, test_vendor_card):
        vc = _make_contact(db_session, test_vendor_card, "summ@arrow.com")
        with patch("app.services.contact_intelligence.generate_contact_summary", return_value="Great relationship."):
            resp = client.get(f"/api/vendors/{test_vendor_card.id}/contacts/{vc.id}/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["summary"] == "Great relationship."


# ---------------------------------------------------------------------------
# POST /api/vendors/{card_id}/contacts/{contact_id}/log-call
# ---------------------------------------------------------------------------


class TestLogContactCall:
    def test_log_call_success(self, client, db_session, test_vendor_card):
        vc = _make_contact(db_session, test_vendor_card, "call@arrow.com", full_name="Call Me")
        resp = client.post(f"/api/vendors/{test_vendor_card.id}/contacts/{vc.id}/log-call")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["activity_id"] is not None

    def test_log_call_increments_interaction_count(self, client, db_session, test_vendor_card):
        vc = _make_contact(db_session, test_vendor_card, "inc@arrow.com")
        client.post(f"/api/vendors/{test_vendor_card.id}/contacts/{vc.id}/log-call")
        db_session.refresh(vc)
        assert vc.interaction_count == 1

    def test_log_call_nonexistent_contact_returns_404(self, client, db_session, test_vendor_card):
        resp = client.post(f"/api/vendors/{test_vendor_card.id}/contacts/999999/log-call")
        assert resp.status_code == 404
        assert "error" in resp.json()

    def test_log_call_wrong_vendor_returns_404(self, client, db_session, test_vendor_card):
        vc = _make_contact(db_session, test_vendor_card, "wv2@arrow.com")
        resp = client.post(f"/api/vendors/999999/contacts/{vc.id}/log-call")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/vendors/{card_id}/contacts  (add contact)
# ---------------------------------------------------------------------------


class TestAddVendorContact:
    def test_add_contact_happy_path(self, client, db_session, test_vendor_card):
        resp = client.post(
            f"/api/vendors/{test_vendor_card.id}/contacts",
            json={"email": "new@arrow.com", "full_name": "New Contact"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["duplicate"] is False
        assert data["id"] is not None

    def test_add_contact_also_updates_card_emails(self, client, db_session, test_vendor_card):
        client.post(
            f"/api/vendors/{test_vendor_card.id}/contacts",
            json={"email": "added@arrow.com"},
        )
        db_session.refresh(test_vendor_card)
        assert "added@arrow.com" in test_vendor_card.emails

    def test_add_duplicate_contact_returns_flag(self, client, db_session, test_vendor_card):
        client.post(
            f"/api/vendors/{test_vendor_card.id}/contacts",
            json={"email": "dup@arrow.com"},
        )
        resp = client.post(
            f"/api/vendors/{test_vendor_card.id}/contacts",
            json={"email": "dup@arrow.com"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["duplicate"] is True

    def test_add_contact_nonexistent_vendor_returns_404(self, client):
        resp = client.post(
            "/api/vendors/999999/contacts",
            json={"email": "ghost@example.com"},
        )
        assert resp.status_code == 404
        assert "error" in resp.json()

    def test_add_contact_invalid_email_returns_422(self, client, db_session, test_vendor_card):
        resp = client.post(
            f"/api/vendors/{test_vendor_card.id}/contacts",
            json={"email": "not-an-email"},
        )
        assert resp.status_code == 422

    def test_add_contact_missing_email_returns_422(self, client, db_session, test_vendor_card):
        resp = client.post(
            f"/api/vendors/{test_vendor_card.id}/contacts",
            json={"full_name": "No Email"},
        )
        assert resp.status_code == 422

    def test_add_contact_with_phone(self, client, db_session, test_vendor_card):
        resp = client.post(
            f"/api/vendors/{test_vendor_card.id}/contacts",
            json={"email": "phone@arrow.com", "phone": "+1-555-9876"},
        )
        assert resp.status_code == 200
        vc = db_session.query(VendorContact).filter_by(email="phone@arrow.com").first()
        assert vc is not None


# ---------------------------------------------------------------------------
# PUT /api/vendors/{card_id}/contacts/{contact_id}
# ---------------------------------------------------------------------------


class TestUpdateVendorContact:
    def test_update_full_name(self, client, db_session, test_vendor_card):
        vc = _make_contact(db_session, test_vendor_card, "upd@arrow.com", full_name="Old Name")
        resp = client.put(
            f"/api/vendors/{test_vendor_card.id}/contacts/{vc.id}",
            json={"full_name": "New Name"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        db_session.refresh(vc)
        assert vc.full_name == "New Name"

    def test_update_title(self, client, db_session, test_vendor_card):
        vc = _make_contact(db_session, test_vendor_card, "title@arrow.com")
        resp = client.put(
            f"/api/vendors/{test_vendor_card.id}/contacts/{vc.id}",
            json={"title": "VP Sales"},
        )
        assert resp.status_code == 200
        db_session.refresh(vc)
        assert vc.title == "VP Sales"

    def test_update_email_updates_card_emails_array(self, client, db_session, test_vendor_card):
        vc = _make_contact(db_session, test_vendor_card, "old@arrow.com")
        test_vendor_card.emails = ["old@arrow.com"]
        db_session.commit()
        resp = client.put(
            f"/api/vendors/{test_vendor_card.id}/contacts/{vc.id}",
            json={"email": "new@arrow.com"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert "new@arrow.com" in test_vendor_card.emails
        assert "old@arrow.com" not in test_vendor_card.emails

    def test_update_email_conflict_returns_409(self, client, db_session, test_vendor_card):
        _make_contact(db_session, test_vendor_card, "existing@arrow.com")
        vc2 = _make_contact(db_session, test_vendor_card, "other@arrow.com")
        resp = client.put(
            f"/api/vendors/{test_vendor_card.id}/contacts/{vc2.id}",
            json={"email": "existing@arrow.com"},
        )
        assert resp.status_code == 409
        assert "error" in resp.json()

    def test_update_nonexistent_contact_returns_404(self, client, db_session, test_vendor_card):
        resp = client.put(
            f"/api/vendors/{test_vendor_card.id}/contacts/999999",
            json={"full_name": "Ghost"},
        )
        assert resp.status_code == 404
        assert "error" in resp.json()

    def test_update_sets_last_seen_at(self, client, db_session, test_vendor_card):
        vc = _make_contact(db_session, test_vendor_card, "seen@arrow.com")
        resp = client.put(
            f"/api/vendors/{test_vendor_card.id}/contacts/{vc.id}",
            json={"label": "Purchasing"},
        )
        assert resp.status_code == 200
        db_session.refresh(vc)
        assert vc.last_seen_at is not None


# ---------------------------------------------------------------------------
# DELETE /api/vendors/{card_id}/contacts/{contact_id}
# ---------------------------------------------------------------------------


class TestDeleteVendorContact:
    def test_delete_contact_success(self, client, db_session, test_vendor_card):
        vc = _make_contact(db_session, test_vendor_card, "del@arrow.com")
        cid = vc.id
        resp = client.delete(f"/api/vendors/{test_vendor_card.id}/contacts/{cid}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert db_session.get(VendorContact, cid) is None

    def test_delete_removes_from_card_emails_array(self, client, db_session, test_vendor_card):
        vc = _make_contact(db_session, test_vendor_card, "rm@arrow.com")
        test_vendor_card.emails = ["rm@arrow.com", "keep@arrow.com"]
        db_session.commit()
        client.delete(f"/api/vendors/{test_vendor_card.id}/contacts/{vc.id}")
        db_session.refresh(test_vendor_card)
        assert "rm@arrow.com" not in (test_vendor_card.emails or [])
        assert "keep@arrow.com" in (test_vendor_card.emails or [])

    def test_delete_nonexistent_contact_returns_404(self, client, db_session, test_vendor_card):
        resp = client.delete(f"/api/vendors/{test_vendor_card.id}/contacts/999999")
        assert resp.status_code == 404
        assert "error" in resp.json()

    def test_delete_wrong_vendor_returns_404(self, client, db_session, test_vendor_card):
        vc = _make_contact(db_session, test_vendor_card, "wdel@arrow.com")
        resp = client.delete(f"/api/vendors/999999/contacts/{vc.id}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/vendors/{card_id}/email-metrics
# ---------------------------------------------------------------------------


class TestVendorEmailMetrics:
    def test_returns_metrics_empty(self, client, db_session, test_vendor_card):
        resp = client.get(f"/api/vendors/{test_vendor_card.id}/email-metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert data["vendor_name"] == "Arrow Electronics"
        assert data["total_rfqs_sent"] == 0

    def test_nonexistent_vendor_returns_404(self, client):
        resp = client.get("/api/vendors/999999/email-metrics")
        assert resp.status_code == 404
        assert "error" in resp.json()


# ---------------------------------------------------------------------------
# POST /api/vendor-card/add-email
# ---------------------------------------------------------------------------


class TestAddEmailToCard:
    def test_add_email_creates_card_if_missing(self, client, db_session):
        with patch("app.routers.vendor_contacts.get_credential_cached", return_value=None):
            resp = client.post(
                "/api/vendor-card/add-email",
                json={"vendor_name": "Totally New Vendor", "email": "info@totallynew.com"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "info@totallynew.com" in data["emails"]
        assert data["contact_created"] is True

    def test_add_email_existing_card(self, client, db_session, test_vendor_card):
        with patch("app.routers.vendor_contacts.get_credential_cached", return_value=None):
            resp = client.post(
                "/api/vendor-card/add-email",
                json={"vendor_name": "Arrow Electronics", "email": "newemail@arrow.com"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "newemail@arrow.com" in data["emails"]

    def test_add_duplicate_email_does_not_create_contact(self, client, db_session, test_vendor_card):
        """Adding the same email twice should not create a second VendorContact."""
        with patch("app.routers.vendor_contacts.get_credential_cached", return_value=None):
            client.post(
                "/api/vendor-card/add-email",
                json={"vendor_name": "Arrow Electronics", "email": "once@arrow.com"},
            )
        with patch("app.routers.vendor_contacts.get_credential_cached", return_value=None):
            resp = client.post(
                "/api/vendor-card/add-email",
                json={"vendor_name": "Arrow Electronics", "email": "once@arrow.com"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["contact_created"] is False

    def test_add_email_sets_domain_on_card(self, client, db_session, test_vendor_card):
        with patch("app.routers.vendor_contacts.get_credential_cached", return_value=None):
            resp = client.post(
                "/api/vendor-card/add-email",
                json={"vendor_name": "Arrow Electronics", "email": "domain@uniquevendor.biz"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["domain"] is not None

    def test_add_email_generic_domain_skipped(self, client, db_session):
        """Emails from gmail/yahoo should not set domain on the card."""
        with patch("app.routers.vendor_contacts.get_credential_cached", return_value=None):
            resp = client.post(
                "/api/vendor-card/add-email",
                json={"vendor_name": "Generic Email Vendor", "email": "vendor@gmail.com"},
            )
        assert resp.status_code == 200
        data = resp.json()
        # domain should not be set (or be None) for generic email providers
        assert data.get("domain") is None

    def test_add_email_triggers_enrichment(self, client, db_session):
        """When a business domain is found and API key exists, enrichment is triggered."""
        with patch("app.routers.vendor_contacts.get_credential_cached", return_value="fake-key"):
            with patch("app.routers.vendor_contacts.safe_background_task", new_callable=AsyncMock) as mock_bg:
                with patch("app.routers.vendor_contacts._background_enrich_vendor", return_value=None):
                    resp = client.post(
                        "/api/vendor-card/add-email",
                        json={"vendor_name": "Enrich Trigger Vendor", "email": "enrich@triggervendor.io"},
                    )
        assert resp.status_code == 200
        data = resp.json()
        assert data["enrich_triggered"] is True

    def test_add_email_invalid_email_returns_422(self, client):
        resp = client.post(
            "/api/vendor-card/add-email",
            json={"vendor_name": "Arrow Electronics", "email": "not-an-email"},
        )
        assert resp.status_code == 422

    def test_add_email_missing_vendor_name_returns_422(self, client):
        resp = client.post(
            "/api/vendor-card/add-email",
            json={"email": "info@example.com"},
        )
        assert resp.status_code == 422
