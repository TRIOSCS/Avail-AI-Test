"""
Tests for enrichment backfill, email propagation, website scraping, and M365 endpoints.

Covers the uncommitted enrichment features:
- POST /api/enrichment/backfill-emails
- GET /api/enrichment/m365-status
- POST /api/enrichment/deep-email-scan/{user_id}
- POST /api/enrichment/scrape-websites
- _propagate_vendor_emails() in search_service
- Stats endpoint vendor_emails count

Called by: pytest tests/test_enrichment_backfill.py -v
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    ActivityLog,
    Requirement,
    Requisition,
    Sighting,
    User,
    VendorCard,
    VendorContact,
)

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def admin_client(db_session: Session, admin_user: User) -> TestClient:
    from app.database import get_db
    from app.dependencies import require_admin, require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_admin():
        return admin_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_admin
    app.dependency_overrides[require_admin] = _override_admin

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


@pytest.fixture()
def vendor_arrow(db_session: Session) -> VendorCard:
    card = VendorCard(
        normalized_name="arrow electronics",
        display_name="Arrow Electronics",
        domain="arrow.com",
        emails=["sales@arrow.com", "info@arrow.com"],
        phones=["+1-555-0100"],
        sighting_count=50,
        website="https://arrow.com",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.commit()
    db_session.refresh(card)
    return card


@pytest.fixture()
def vendor_mouser(db_session: Session) -> VendorCard:
    card = VendorCard(
        normalized_name="mouser electronics",
        display_name="Mouser Electronics",
        domain="mouser.com",
        emails=[],
        sighting_count=30,
        website="https://mouser.com",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.commit()
    db_session.refresh(card)
    return card


# ── Email Backfill Tests ─────────────────────────────────────────────


class TestEmailBackfill:
    """POST /api/enrichment/backfill-emails"""

    def test_backfill_from_activity_log(
        self, admin_client, db_session, vendor_arrow, admin_user
    ):
        """Activity log emails get promoted to VendorContact records."""
        # Create an activity log entry with an email not yet in vendor_contacts
        activity = ActivityLog(
            user_id=admin_user.id,
            activity_type="email_sent",
            channel="email",
            vendor_card_id=vendor_arrow.id,
            contact_email="john@arrow.com",
            contact_name="John Smith",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(activity)
        db_session.commit()

        resp = admin_client.post("/api/enrichment/backfill-emails")
        assert resp.status_code == 200
        data = resp.json()
        assert data["activity_log_created"] >= 1
        assert data["total_created"] >= 1

        # Verify VendorContact was created
        vc = (
            db_session.query(VendorContact)
            .filter_by(vendor_card_id=vendor_arrow.id, email="john@arrow.com")
            .first()
        )
        assert vc is not None
        assert vc.source == "activity_log"
        assert vc.full_name == "John Smith"

    def test_backfill_from_vendor_card_emails(
        self, admin_client, db_session, vendor_arrow
    ):
        """VendorCard.emails list entries get promoted to VendorContact records."""
        resp = admin_client.post("/api/enrichment/backfill-emails")
        assert resp.status_code == 200
        data = resp.json()
        assert data["vendor_card_created"] >= 1

        # Both emails from vendor_arrow should be in vendor_contacts now
        contacts = (
            db_session.query(VendorContact)
            .filter_by(vendor_card_id=vendor_arrow.id, source="vendor_card_import")
            .all()
        )
        emails = {c.email for c in contacts}
        assert "sales@arrow.com" in emails

    def test_backfill_from_brokerbin_sightings(
        self, admin_client, db_session, vendor_arrow
    ):
        """BrokerBin sighting emails get promoted to VendorContact records."""
        # Need a requisition + requirement to create a sighting
        req = Requisition(
            name="REQ-BB-001",
            customer_name="Test",
            status="open",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        item = Requirement(
            requisition_id=req.id,
            primary_mpn="LM317T",
            target_qty=100,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.flush()

        sighting = Sighting(
            requirement_id=item.id,
            vendor_name="Arrow Electronics",
            vendor_email="rfq-team@arrow.com",
            mpn_matched="LM317T",
            source_type="brokerbin",
            qty_available=500,
        )
        db_session.add(sighting)
        db_session.commit()

        resp = admin_client.post("/api/enrichment/backfill-emails")
        assert resp.status_code == 200
        data = resp.json()
        assert data["brokerbin_created"] >= 1

        vc = (
            db_session.query(VendorContact)
            .filter_by(vendor_card_id=vendor_arrow.id, email="rfq-team@arrow.com")
            .first()
        )
        assert vc is not None
        assert vc.source == "brokerbin"

    def test_backfill_no_duplicates(self, admin_client, db_session, vendor_arrow):
        """Running backfill twice doesn't create duplicate contacts."""
        resp1 = admin_client.post("/api/enrichment/backfill-emails")
        _count1 = resp1.json()["total_created"]  # noqa: F841

        resp2 = admin_client.post("/api/enrichment/backfill-emails")
        count2 = resp2.json()["total_created"]
        assert count2 == 0  # No new records on second run

    def test_backfill_skips_invalid_emails(
        self, admin_client, db_session, vendor_arrow, admin_user
    ):
        """Activity log entries with invalid emails are skipped."""
        for bad_email in ["", "not-an-email", None]:
            activity = ActivityLog(
                user_id=admin_user.id,
                activity_type="email_sent",
                channel="email",
                vendor_card_id=vendor_arrow.id,
                contact_email=bad_email,
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(activity)
        db_session.commit()

        resp = admin_client.post("/api/enrichment/backfill-emails")
        assert resp.status_code == 200
        assert resp.json()["activity_log_created"] == 0


# ── M365 Status Tests ────────────────────────────────────────────────


class TestM365Status:
    """GET /api/enrichment/m365-status"""

    def test_returns_user_list(self, admin_client, db_session, admin_user):
        resp = admin_client.get("/api/enrichment/m365-status")
        assert resp.status_code == 200
        data = resp.json()
        assert "users" in data
        users = data["users"]
        assert len(users) >= 1
        user_data = next(u for u in users if u["email"] == admin_user.email)
        assert "m365_connected" in user_data
        assert "last_inbox_scan" in user_data
        assert "last_deep_scan" in user_data

    def test_shows_connected_status(self, admin_client, db_session, admin_user):
        admin_user.m365_connected = True
        db_session.commit()

        resp = admin_client.get("/api/enrichment/m365-status")
        user_data = next(
            u for u in resp.json()["users"] if u["email"] == admin_user.email
        )
        assert user_data["m365_connected"] is True


# ── Deep Email Scan Tests ────────────────────────────────────────────


class TestDeepEmailScan:
    """POST /api/enrichment/deep-email-scan/{user_id}"""

    def test_user_not_found(self, admin_client):
        resp = admin_client.post("/api/enrichment/deep-email-scan/99999")
        assert resp.status_code == 404

    def test_user_not_m365_connected(self, admin_client, db_session, admin_user):
        admin_user.m365_connected = False
        db_session.commit()

        resp = admin_client.post(
            f"/api/enrichment/deep-email-scan/{admin_user.id}"
        )
        assert resp.status_code == 400
        assert "M365" in resp.json()["detail"]


# ── Website Scraping Tests ───────────────────────────────────────────


class TestWebsiteScraping:
    """POST /api/enrichment/scrape-websites"""

    def test_scrape_returns_counts(self, admin_client):
        """Endpoint returns vendor and email counts."""
        with patch(
            "app.services.website_scraper.scrape_vendor_websites",
            new_callable=AsyncMock,
            return_value={"vendors_scraped": 5, "emails_found": 12},
        ):
            resp = admin_client.post(
                "/api/enrichment/scrape-websites",
                json={"max_vendors": 100},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["vendors_scraped"] == 5
            assert data["emails_found"] == 12

    def test_scrape_default_max(self, admin_client):
        """Without body, default max_vendors is 500."""
        with patch(
            "app.services.website_scraper.scrape_vendor_websites",
            new_callable=AsyncMock,
            return_value={"vendors_scraped": 0, "emails_found": 0},
        ):
            resp = admin_client.post("/api/enrichment/scrape-websites")
            assert resp.status_code == 200


# ── Search Propagation Tests ─────────────────────────────────────────


class TestSearchPropagation:
    """_propagate_vendor_emails() in search_service.py"""

    def test_sighting_email_creates_vendor_contact(
        self, db_session, vendor_arrow
    ):
        from app.search_service import _propagate_vendor_emails

        req = Requisition(
            name="REQ-PROP-001",
            customer_name="Test",
            status="open",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        item = Requirement(
            requisition_id=req.id,
            primary_mpn="LM317T",
            target_qty=100,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.flush()

        sighting = Sighting(
            requirement_id=item.id,
            vendor_name="Arrow Electronics",
            vendor_email="new-contact@arrow.com",
            mpn_matched="LM317T",
            source_type="brokerbin",
            qty_available=500,
        )
        db_session.add(sighting)
        db_session.commit()

        _propagate_vendor_emails([sighting], db_session)

        vc = (
            db_session.query(VendorContact)
            .filter_by(vendor_card_id=vendor_arrow.id, email="new-contact@arrow.com")
            .first()
        )
        assert vc is not None
        assert vc.source == "brokerbin"
        assert vc.confidence == 60

    def test_propagation_no_duplicate(self, db_session, vendor_arrow):
        from app.search_service import _propagate_vendor_emails

        # Pre-create a contact
        existing = VendorContact(
            vendor_card_id=vendor_arrow.id,
            email="existing@arrow.com",
            source="manual",
            confidence=80,
        )
        db_session.add(existing)
        db_session.commit()

        req = Requisition(
            name="REQ-DUP-001",
            customer_name="Test",
            status="open",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        item = Requirement(
            requisition_id=req.id,
            primary_mpn="LM317T",
            target_qty=100,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.flush()

        sighting = Sighting(
            requirement_id=item.id,
            vendor_name="Arrow Electronics",
            vendor_email="existing@arrow.com",
            mpn_matched="LM317T",
            source_type="brokerbin",
            qty_available=500,
        )
        db_session.add(sighting)
        db_session.commit()

        _propagate_vendor_emails([sighting], db_session)

        count = (
            db_session.query(VendorContact)
            .filter_by(vendor_card_id=vendor_arrow.id, email="existing@arrow.com")
            .count()
        )
        assert count == 1  # No duplicate

    def test_propagation_skips_no_email(self, db_session, vendor_arrow):
        from app.search_service import _propagate_vendor_emails

        req = Requisition(
            name="REQ-NOEMAIL-001",
            customer_name="Test",
            status="open",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        item = Requirement(
            requisition_id=req.id,
            primary_mpn="LM317T",
            target_qty=100,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.flush()

        sighting = Sighting(
            requirement_id=item.id,
            vendor_name="Arrow Electronics",
            vendor_email=None,
            mpn_matched="LM317T",
            source_type="brokerbin",
            qty_available=500,
        )
        db_session.add(sighting)
        db_session.commit()

        _propagate_vendor_emails([sighting], db_session)

        contacts = (
            db_session.query(VendorContact)
            .filter_by(vendor_card_id=vendor_arrow.id)
            .all()
        )
        assert len(contacts) == 0

    def test_propagation_with_phone(self, db_session, vendor_arrow):
        from app.search_service import _propagate_vendor_emails

        req = Requisition(
            name="REQ-PHONE-001",
            customer_name="Test",
            status="open",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        item = Requirement(
            requisition_id=req.id,
            primary_mpn="LM317T",
            target_qty=100,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.flush()

        sighting = Sighting(
            requirement_id=item.id,
            vendor_name="Arrow Electronics",
            vendor_email="phone-test@arrow.com",
            vendor_phone="+1-555-9999",
            mpn_matched="LM317T",
            source_type="brokerbin",
            qty_available=500,
        )
        db_session.add(sighting)
        db_session.commit()

        _propagate_vendor_emails([sighting], db_session)

        vc = (
            db_session.query(VendorContact)
            .filter_by(vendor_card_id=vendor_arrow.id, email="phone-test@arrow.com")
            .first()
        )
        assert vc is not None


# ── Stats Endpoint Tests ─────────────────────────────────────────────


class TestEnrichmentStatsVendorEmails:
    """Stats endpoint now includes vendor_emails count."""

    def test_stats_includes_vendor_emails(
        self, admin_client, db_session, vendor_arrow
    ):
        # Create a vendor contact with email
        vc = VendorContact(
            vendor_card_id=vendor_arrow.id,
            email="stat-test@arrow.com",
            source="test",
            confidence=80,
        )
        db_session.add(vc)
        db_session.commit()

        resp = admin_client.get("/api/enrichment/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "vendor_emails" in data
        assert data["vendor_emails"] >= 1

    def test_stats_vendor_emails_zero(self, admin_client):
        resp = admin_client.get("/api/enrichment/stats")
        assert resp.status_code == 200
        assert resp.json()["vendor_emails"] == 0


# ── Website Scraper Unit Tests ───────────────────────────────────────


class TestWebsiteScraperUnit:
    """Unit tests for the website_scraper service."""

    def test_classify_email_contact_page(self):
        from app.services.website_scraper import _classify_email

        assert _classify_email("john@example.com", "/contact") == 70

    def test_classify_email_about_page(self):
        from app.services.website_scraper import _classify_email

        assert _classify_email("john@example.com", "/about") == 60

    def test_classify_email_homepage(self):
        from app.services.website_scraper import _classify_email

        assert _classify_email("john@example.com", "") == 55

    def test_classify_email_generic(self):
        from app.services.website_scraper import _classify_email

        assert _classify_email("noreply@example.com", "/contact") == 40
        assert _classify_email("support@example.com", "/about") == 40
        assert _classify_email("info@example.com", "") == 40
