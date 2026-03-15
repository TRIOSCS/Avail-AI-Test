"""test_phase7_email_integration.py — Tests for Phase 7: Email Integration.

Verifies: vendor emails tab showing Contact + VendorResponse history,
email stats, empty state.

Called by: pytest
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User, VendorCard


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def vendor_with_emails(db_session: Session, test_user: User) -> VendorCard:
    """A vendor with RFQ contacts and vendor responses."""
    from app.models import Requisition
    from app.models.offers import Contact as RfqContact, VendorResponse

    vendor = VendorCard(
        display_name="Email Test Vendor",
        normalized_name="email test vendor",
        vendor_score=75,
    )
    db_session.add(vendor)
    db_session.flush()

    req = Requisition(
        name="Email Test Req",
        status="sourcing",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    # RFQ contact
    contact = RfqContact(
        requisition_id=req.id,
        user_id=test_user.id,
        contact_type="email",
        vendor_name="Email Test Vendor",
        vendor_name_normalized="email test vendor",
        vendor_contact="sales@emailtest.com",
        parts_included="LM317T",
        subject="RFQ - LM317T",
        status="sent",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(contact)

    # Vendor response
    vr = VendorResponse(
        requisition_id=req.id,
        vendor_name="Email Test Vendor",
        vendor_email="sales@emailtest.com",
        subject="Re: RFQ - LM317T",
        classification="quote",
        confidence=0.9,
        status="new",
        parsed_data={"parts": ["LM317T"], "price": 0.45},
        received_at=datetime.now(timezone.utc),
    )
    db_session.add(vr)

    db_session.commit()
    db_session.refresh(vendor)
    return vendor


# ── Vendor Emails Tab ────────────────────────────────────────────────


class TestVendorEmailsTab:
    """Tests for the vendor emails tab."""

    def test_emails_tab_loads(
        self, client: TestClient, vendor_with_emails: VendorCard
    ):
        resp = client.get(
            f"/v2/partials/vendors/{vendor_with_emails.id}/tab/emails"
        )
        assert resp.status_code == 200
        assert "Outbound RFQs" in resp.text
        assert "Vendor Responses" in resp.text

    def test_emails_tab_shows_contacts(
        self, client: TestClient, vendor_with_emails: VendorCard
    ):
        resp = client.get(
            f"/v2/partials/vendors/{vendor_with_emails.id}/tab/emails"
        )
        assert resp.status_code == 200
        assert "RFQ - LM317T" in resp.text
        assert "sales@emailtest.com" in resp.text

    def test_emails_tab_shows_responses(
        self, client: TestClient, vendor_with_emails: VendorCard
    ):
        resp = client.get(
            f"/v2/partials/vendors/{vendor_with_emails.id}/tab/emails"
        )
        assert resp.status_code == 200
        assert "Re: RFQ - LM317T" in resp.text
        assert "Quote" in resp.text
        assert "90%" in resp.text

    def test_emails_tab_shows_stats(
        self, client: TestClient, vendor_with_emails: VendorCard
    ):
        resp = client.get(
            f"/v2/partials/vendors/{vendor_with_emails.id}/tab/emails"
        )
        assert resp.status_code == 200
        assert "RFQs Sent" in resp.text
        assert "Response Rate" in resp.text

    def test_emails_tab_empty_state(
        self, client: TestClient, db_session: Session
    ):
        """Vendor with no email history shows empty state."""
        vendor = VendorCard(
            display_name="Empty Vendor",
            normalized_name="empty vendor",
        )
        db_session.add(vendor)
        db_session.commit()
        db_session.refresh(vendor)

        resp = client.get(f"/v2/partials/vendors/{vendor.id}/tab/emails")
        assert resp.status_code == 200
        assert "No email history" in resp.text

    def test_vendor_detail_has_emails_tab(
        self, client: TestClient, vendor_with_emails: VendorCard
    ):
        """The emails tab should appear in the vendor detail tabs."""
        resp = client.get(f"/v2/partials/vendors/{vendor_with_emails.id}")
        assert resp.status_code == 200
        assert "Emails" in resp.text
