"""Tests for command center actions endpoint.

Called by: pytest
Depends on: conftest.py fixtures, app.routers.command_center
"""

from datetime import datetime, timedelta, timezone

from app.models.offers import Contact, Offer, VendorResponse
from app.models.quotes import Quote


class TestCommandCenterActions:
    """GET /api/command-center/actions returns aggregated action items."""

    ENDPOINT = "/api/command-center/actions"

    def test_returns_200_with_expected_structure(self, client):
        resp = client.get(self.ENDPOINT)
        assert resp.status_code == 200
        data = resp.json()
        assert "stale_rfqs" in data
        assert "pending_quotes" in data
        assert "pending_reviews" in data
        assert "today_responses" in data
        assert isinstance(data["stale_rfqs"], list)
        assert isinstance(data["pending_quotes"], list)
        assert isinstance(data["pending_reviews"], list)
        assert isinstance(data["today_responses"], list)

    def test_empty_db_returns_empty_arrays(self, client):
        resp = client.get(self.ENDPOINT)
        assert resp.status_code == 200
        data = resp.json()
        assert data["stale_rfqs"] == []
        assert data["pending_quotes"] == []
        assert data["pending_reviews"] == []
        assert data["today_responses"] == []

    def test_stale_rfqs_included(self, client, db_session, test_requisition, test_user):
        """Contacts with status='sent' created >48h ago appear in stale_rfqs."""
        stale_contact = Contact(
            requisition_id=test_requisition.id,
            user_id=test_user.id,
            contact_type="rfq",
            vendor_name="Stale Vendor Inc",
            status="sent",
            created_at=datetime.now(timezone.utc) - timedelta(hours=72),
        )
        db_session.add(stale_contact)
        db_session.commit()

        resp = client.get(self.ENDPOINT)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["stale_rfqs"]) == 1
        assert data["stale_rfqs"][0]["vendor_name"] == "Stale Vendor Inc"
        assert data["stale_rfqs"][0]["requisition_id"] == test_requisition.id

    def test_recent_rfqs_not_stale(self, client, db_session, test_requisition, test_user):
        """Contacts created <48h ago should NOT appear in stale_rfqs."""
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

        resp = client.get(self.ENDPOINT)
        data = resp.json()
        assert len(data["stale_rfqs"]) == 0

    def test_pending_quotes_included(
        self, client, db_session, test_requisition, test_user, test_company, test_customer_site
    ):
        """Quotes with status='sent', sent >5 days ago, no result appear in
        pending_quotes."""
        old_quote = Quote(
            requisition_id=test_requisition.id,
            customer_site_id=test_customer_site.id,
            quote_number="PEND-Q-001",
            status="sent",
            sent_at=datetime.now(timezone.utc) - timedelta(days=7),
            result=None,
            line_items=[],
            subtotal=500.00,
            total_cost=250.00,
            total_margin_pct=50.00,
            created_by_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(old_quote)
        db_session.commit()

        resp = client.get(self.ENDPOINT)
        data = resp.json()
        assert len(data["pending_quotes"]) == 1
        assert data["pending_quotes"][0]["quote_number"] == "PEND-Q-001"
        assert data["pending_quotes"][0]["days_pending"] >= 7

    def test_pending_reviews_included(self, client, db_session, test_requisition, test_user):
        """Offers with status='needs_review' appear in pending_reviews."""
        review_offer = Offer(
            requisition_id=test_requisition.id,
            vendor_name="Review Vendor",
            mpn="TEST-MPN-001",
            qty_available=500,
            unit_price=1.25,
            entered_by_id=test_user.id,
            status="needs_review",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(review_offer)
        db_session.commit()

        resp = client.get(self.ENDPOINT)
        data = resp.json()
        assert len(data["pending_reviews"]) == 1
        assert data["pending_reviews"][0]["vendor_name"] == "Review Vendor"
        assert data["pending_reviews"][0]["mpn"] == "TEST-MPN-001"

    def test_today_responses_included(self, client, db_session, test_requisition):
        """VendorResponses created today appear in today_responses."""
        vr = VendorResponse(
            requisition_id=test_requisition.id,
            vendor_name="Today Vendor",
            subject="Re: RFQ for LM317T",
            confidence=0.85,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.commit()

        resp = client.get(self.ENDPOINT)
        data = resp.json()
        assert len(data["today_responses"]) == 1
        assert data["today_responses"][0]["vendor_name"] == "Today Vendor"
        assert data["today_responses"][0]["confidence"] == 0.85

    def test_unauthenticated_returns_401(self, unauthenticated_client):
        resp = unauthenticated_client.get(self.ENDPOINT)
        assert resp.status_code in (401, 403)
