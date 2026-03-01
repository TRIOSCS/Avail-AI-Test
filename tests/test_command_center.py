"""
test_command_center.py — Tests for the Command Center API endpoint.

Covers:
- GET /api/command-center/actions returns correct structure
- Stale RFQs (vendor contacts sent >48h ago) are returned
- Pending quotes (sent >5 days ago) are returned
- Today's vendor responses are returned
- Empty state returns empty arrays
"""

from datetime import datetime, timedelta, timezone

from app.models.offers import Contact, Offer, VendorResponse
from app.models.quotes import Quote


class TestCommandCenterActions:
    def test_empty_state(self, client):
        """No data returns empty arrays for all action types."""
        resp = client.get("/api/command-center/actions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["stale_rfqs"] == []
        assert data["pending_quotes"] == []
        assert data["pending_reviews"] == []
        assert data["today_responses"] == []

    def test_stale_rfqs_returned(self, client, db_session, test_user, test_requisition):
        """Contacts with status='sent' created >48h ago appear as stale RFQs."""
        old_contact = Contact(
            requisition_id=test_requisition.id,
            user_id=test_user.id,
            contact_type="rfq",
            vendor_name="Stale Vendor",
            status="sent",
            created_at=datetime.now(timezone.utc) - timedelta(hours=72),
        )
        db_session.add(old_contact)
        db_session.commit()

        resp = client.get("/api/command-center/actions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["stale_rfqs"]) == 1
        assert data["stale_rfqs"][0]["vendor_name"] == "Stale Vendor"

    def test_recent_rfqs_not_stale(self, client, db_session, test_user, test_requisition):
        """Contacts sent <48h ago should NOT appear as stale."""
        recent_contact = Contact(
            requisition_id=test_requisition.id,
            user_id=test_user.id,
            contact_type="rfq",
            vendor_name="Fresh Vendor",
            status="sent",
            created_at=datetime.now(timezone.utc) - timedelta(hours=12),
        )
        db_session.add(recent_contact)
        db_session.commit()

        resp = client.get("/api/command-center/actions")
        data = resp.json()
        assert len(data["stale_rfqs"]) == 0

    def test_pending_quotes_returned(self, client, db_session, test_user, test_requisition):
        """Quotes with status='sent' and sent >5 days ago appear as pending."""
        from app.models.crm import Company, CustomerSite

        co = Company(name="Test Co", is_active=True, created_at=datetime.now(timezone.utc))
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(
            company_id=co.id,
            site_name="HQ",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(site)
        db_session.flush()

        old_quote = Quote(
            requisition_id=test_requisition.id,
            customer_site_id=site.id,
            quote_number="Q-TEST-001",
            status="sent",
            sent_at=datetime.now(timezone.utc) - timedelta(days=7),
            created_at=datetime.now(timezone.utc) - timedelta(days=7),
        )
        db_session.add(old_quote)
        db_session.commit()

        resp = client.get("/api/command-center/actions")
        data = resp.json()
        assert len(data["pending_quotes"]) == 1
        assert data["pending_quotes"][0]["quote_number"] == "Q-TEST-001"
        assert data["pending_quotes"][0]["days_pending"] >= 7

    def test_today_responses_returned(self, client, db_session):
        """VendorResponses created today appear in today_responses."""
        vr = VendorResponse(
            vendor_name="Active Vendor",
            vendor_email="active@vendor.com",
            subject="Re: RFQ for LM317T",
            confidence=0.85,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.commit()

        resp = client.get("/api/command-center/actions")
        data = resp.json()
        assert len(data["today_responses"]) == 1
        assert data["today_responses"][0]["vendor_name"] == "Active Vendor"
        assert data["today_responses"][0]["confidence"] == 0.85

    def test_yesterday_responses_excluded(self, client, db_session):
        """VendorResponses from yesterday should NOT appear in today's inbox."""
        vr = VendorResponse(
            vendor_name="Yesterday Vendor",
            vendor_email="old@vendor.com",
            subject="Re: Old RFQ",
            created_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        db_session.add(vr)
        db_session.commit()

        resp = client.get("/api/command-center/actions")
        data = resp.json()
        assert len(data["today_responses"]) == 0

    def test_needs_review_offers(self, client, db_session, test_requisition):
        """Offers with status='needs_review' appear in pending_reviews."""
        offer = Offer(
            requisition_id=test_requisition.id,
            vendor_name="Review Vendor",
            mpn="LM317T",
            status="needs_review",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.commit()

        resp = client.get("/api/command-center/actions")
        data = resp.json()
        assert len(data["pending_reviews"]) == 1
        assert data["pending_reviews"][0]["vendor_name"] == "Review Vendor"
