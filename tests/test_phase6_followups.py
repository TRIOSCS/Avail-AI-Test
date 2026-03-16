"""test_phase6_followups.py — Tests for Phase 6: RFQ Follow-ups & Response Review.

Verifies: follow-up queue listing, send follow-up, response review
(mark reviewed/rejected), poll inbox, responses tab with poll button.

Called by: pytest
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Requisition, Requirement, User


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def req_with_contacts(db_session: Session, test_user: User) -> Requisition:
    """A requisition with RFQ contacts (some stale, some recent)."""
    from app.models.offers import Contact as RfqContact

    req = Requisition(
        name="Follow-Up Test Req",
        status="sourcing",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    # Stale contact (sent 5 days ago)
    stale = RfqContact(
        requisition_id=req.id,
        user_id=test_user.id,
        contact_type="email",
        vendor_name="Stale Vendor",
        vendor_name_normalized="stale vendor",
        vendor_contact="stale@vendor.com",
        parts_included="LM317T, NE555P",
        subject="RFQ - Parts",
        status="sent",
        created_at=datetime.now(timezone.utc) - timedelta(days=5),
    )
    db_session.add(stale)

    # Recent contact (sent today)
    recent = RfqContact(
        requisition_id=req.id,
        user_id=test_user.id,
        contact_type="email",
        vendor_name="Recent Vendor",
        vendor_name_normalized="recent vendor",
        vendor_contact="recent@vendor.com",
        parts_included="LM317T",
        subject="RFQ - Parts",
        status="sent",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(recent)

    db_session.commit()
    db_session.refresh(req)
    return req


@pytest.fixture()
def stale_contact(db_session: Session, req_with_contacts: Requisition):
    """The stale contact from req_with_contacts."""
    from app.models.offers import Contact as RfqContact

    return (
        db_session.query(RfqContact)
        .filter(
            RfqContact.requisition_id == req_with_contacts.id,
            RfqContact.vendor_name == "Stale Vendor",
        )
        .first()
    )


@pytest.fixture()
def vendor_response(db_session: Session, req_with_contacts: Requisition):
    """A vendor response for the test requisition."""
    from app.models.offers import VendorResponse

    vr = VendorResponse(
        requisition_id=req_with_contacts.id,
        vendor_name="Test Vendor",
        vendor_email="test@vendor.com",
        subject="Re: RFQ - Parts",
        classification="quote",
        confidence=0.85,
        status="new",
        parsed_data={"parts": ["LM317T"], "qty": 100, "price": 0.45},
        received_at=datetime.now(timezone.utc),
    )
    db_session.add(vr)
    db_session.commit()
    db_session.refresh(vr)
    return vr


# ── Follow-Up Queue ──────────────────────────────────────────────────


class TestFollowUpQueue:
    """Tests for the cross-requisition follow-up queue."""

    def test_follow_ups_list_loads(
        self, client: TestClient, req_with_contacts: Requisition
    ):
        resp = client.get("/v2/partials/follow-ups")
        assert resp.status_code == 200
        assert "Follow-Up Queue" in resp.text

    def test_follow_ups_shows_stale_contacts(
        self, client: TestClient, req_with_contacts: Requisition
    ):
        resp = client.get("/v2/partials/follow-ups")
        assert resp.status_code == 200
        assert "Stale Vendor" in resp.text
        # Recent contact should NOT appear (sent today, not stale)
        assert "Recent Vendor" not in resp.text

    def test_follow_ups_shows_days_waiting(
        self, client: TestClient, req_with_contacts: Requisition
    ):
        resp = client.get("/v2/partials/follow-ups")
        assert resp.status_code == 200
        # Stale contact was sent 5 days ago
        assert "day" in resp.text

    def test_follow_ups_empty_state(
        self, client: TestClient, test_user: User
    ):
        """No stale contacts → empty state message."""
        resp = client.get("/v2/partials/follow-ups")
        assert resp.status_code == 200
        assert "No follow-ups needed" in resp.text


# ── Send Follow-Up ───────────────────────────────────────────────────


class TestSendFollowUp:
    """Tests for sending follow-up emails."""

    def test_send_follow_up(
        self,
        client: TestClient,
        db_session: Session,
        stale_contact,
    ):
        resp = client.post(
            f"/v2/partials/follow-ups/{stale_contact.id}/send",
            data={"body": "Following up on our RFQ..."},
        )
        assert resp.status_code == 200
        assert "Follow-up sent" in resp.text
        assert "Stale Vendor" in resp.text

    def test_send_follow_up_empty_body(
        self,
        client: TestClient,
        stale_contact,
    ):
        """Empty body uses default template — should still succeed."""
        resp = client.post(
            f"/v2/partials/follow-ups/{stale_contact.id}/send",
            data={"body": ""},
        )
        assert resp.status_code == 200
        assert "Follow-up sent" in resp.text

    def test_send_follow_up_404(self, client: TestClient):
        resp = client.post("/v2/partials/follow-ups/99999/send", data={})
        assert resp.status_code == 404


# ── Response Review ──────────────────────────────────────────────────


class TestResponseReview:
    """Tests for reviewing vendor responses."""

    def test_mark_reviewed(
        self,
        client: TestClient,
        db_session: Session,
        req_with_contacts: Requisition,
        vendor_response,
    ):
        resp = client.post(
            f"/v2/partials/requisitions/{req_with_contacts.id}/responses/{vendor_response.id}/review",
            data={"status": "reviewed"},
        )
        assert resp.status_code == 200
        assert "Reviewed" in resp.text

        db_session.refresh(vendor_response)
        assert vendor_response.status == "reviewed"

    def test_mark_rejected(
        self,
        client: TestClient,
        db_session: Session,
        req_with_contacts: Requisition,
        vendor_response,
    ):
        resp = client.post(
            f"/v2/partials/requisitions/{req_with_contacts.id}/responses/{vendor_response.id}/review",
            data={"status": "rejected"},
        )
        assert resp.status_code == 200
        assert "Rejected" in resp.text

        db_session.refresh(vendor_response)
        assert vendor_response.status == "rejected"

    def test_review_invalid_status(
        self,
        client: TestClient,
        req_with_contacts: Requisition,
        vendor_response,
    ):
        resp = client.post(
            f"/v2/partials/requisitions/{req_with_contacts.id}/responses/{vendor_response.id}/review",
            data={"status": "invalid"},
        )
        assert resp.status_code == 400

    def test_review_response_404(
        self, client: TestClient, req_with_contacts: Requisition
    ):
        resp = client.post(
            f"/v2/partials/requisitions/{req_with_contacts.id}/responses/99999/review",
            data={"status": "reviewed"},
        )
        assert resp.status_code == 404


# ── Responses Tab ────────────────────────────────────────────────────


class TestResponsesTab:
    """Tests for the responses tab with poll inbox button."""

    def test_responses_tab_loads(
        self, client: TestClient, req_with_contacts: Requisition, vendor_response
    ):
        resp = client.get(
            f"/v2/partials/requisitions/{req_with_contacts.id}/tab/responses"
        )
        assert resp.status_code == 200
        assert "Test Vendor" in resp.text
        assert "Poll Inbox" in resp.text

    def test_responses_tab_empty(
        self, client: TestClient, req_with_contacts: Requisition
    ):
        resp = client.get(
            f"/v2/partials/requisitions/{req_with_contacts.id}/tab/responses"
        )
        assert resp.status_code == 200
        assert "No vendor responses" in resp.text
        assert "Poll Inbox" in resp.text

    def test_responses_shows_classification_badge(
        self, client: TestClient, req_with_contacts: Requisition, vendor_response
    ):
        resp = client.get(
            f"/v2/partials/requisitions/{req_with_contacts.id}/tab/responses"
        )
        assert resp.status_code == 200
        assert "Quote" in resp.text
        assert "85%" in resp.text

    def test_poll_inbox(
        self, client: TestClient, req_with_contacts: Requisition
    ):
        resp = client.post(
            f"/v2/partials/requisitions/{req_with_contacts.id}/poll-inbox"
        )
        assert resp.status_code == 200

    def test_poll_inbox_404(self, client: TestClient):
        resp = client.post("/v2/partials/requisitions/99999/poll-inbox")
        assert resp.status_code == 404
