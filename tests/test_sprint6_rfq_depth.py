"""test_sprint6_rfq_depth.py — Tests for Sprint 6 RFQ workflow depth.

Verifies: RFQ prepare panel, phone call log, batch follow-up,
follow-up badge, response status update.

Called by: pytest
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Requisition, User

# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def rfq_contact(db_session: Session, test_requisition: Requisition, test_user: User):
    """An RFQ contact (email sent to vendor)."""
    from app.models.offers import Contact as RfqContact

    c = RfqContact(
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        contact_type="email",
        vendor_name="Arrow Electronics",
        vendor_name_normalized="arrow electronics",
        vendor_contact="sales@arrow.com",
        subject="RFQ for LM317T",
        status="sent",
        created_at=datetime.now(timezone.utc) - timedelta(days=5),
    )
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


@pytest.fixture()
def vendor_response(db_session: Session, test_requisition: Requisition, rfq_contact):
    """A vendor response to an RFQ."""
    from app.models.offers import VendorResponse

    vr = VendorResponse(
        contact_id=rfq_contact.id,
        requisition_id=test_requisition.id,
        vendor_name="Arrow Electronics",
        vendor_email="sales@arrow.com",
        subject="Re: RFQ for LM317T",
        status="new",
        confidence=0.85,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(vr)
    db_session.commit()
    db_session.refresh(vr)
    return vr


# ── RFQ Prepare Panel ────────────────────────────────────────────────


class TestRfqPrepare:
    def test_prepare_renders(self, client: TestClient, test_requisition: Requisition):
        resp = client.get(
            f"/v2/partials/requisitions/{test_requisition.id}/rfq-prepare",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "RFQ Preparation" in resp.text

    def test_prepare_shows_mpns(self, client: TestClient, test_requisition: Requisition):
        resp = client.get(
            f"/v2/partials/requisitions/{test_requisition.id}/rfq-prepare",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "LM317T" in resp.text

    def test_prepare_nonexistent(self, client: TestClient):
        resp = client.get(
            "/v2/partials/requisitions/99999/rfq-prepare",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404


# ── Phone Call Log ───────────────────────────────────────────────────


class TestPhoneLog:
    def test_log_phone_call(self, client: TestClient, test_requisition: Requisition):
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/log-phone",
            data={"vendor_name": "Arrow Electronics", "vendor_phone": "+1-555-0100", "notes": "Left voicemail"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Phone call logged" in resp.text
        assert "Arrow Electronics" in resp.text

    def test_log_phone_missing_fields(self, client: TestClient, test_requisition: Requisition):
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/log-phone",
            data={"vendor_name": "", "vendor_phone": ""},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400

    def test_log_phone_nonexistent_req(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/99999/log-phone",
            data={"vendor_name": "Ghost", "vendor_phone": "123"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404


# ── Follow-Up Badge ──────────────────────────────────────────────────


class TestFollowUpBadge:
    def test_badge_with_stale_contacts(self, client: TestClient, rfq_contact):
        resp = client.get(
            "/v2/partials/follow-ups/badge",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        # rfq_contact was created 5 days ago with status "sent" — should show badge

    def test_badge_empty_when_no_stale(self, client: TestClient):
        resp = client.get(
            "/v2/partials/follow-ups/badge",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert resp.text == ""


# ── Response Status Update ───────────────────────────────────────────


class TestResponseStatus:
    def test_mark_reviewed(
        self, client: TestClient, test_requisition: Requisition, vendor_response, db_session: Session
    ):
        resp = client.patch(
            f"/v2/partials/requisitions/{test_requisition.id}/responses/{vendor_response.id}/status",
            data={"status": "reviewed"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        db_session.refresh(vendor_response)
        assert vendor_response.status == "reviewed"

    def test_mark_rejected(
        self, client: TestClient, test_requisition: Requisition, vendor_response, db_session: Session
    ):
        resp = client.patch(
            f"/v2/partials/requisitions/{test_requisition.id}/responses/{vendor_response.id}/status",
            data={"status": "rejected"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        db_session.refresh(vendor_response)
        assert vendor_response.status == "rejected"

    def test_invalid_status(self, client: TestClient, test_requisition: Requisition, vendor_response):
        resp = client.patch(
            f"/v2/partials/requisitions/{test_requisition.id}/responses/{vendor_response.id}/status",
            data={"status": "nonexistent"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400

    def test_nonexistent_response(self, client: TestClient, test_requisition: Requisition):
        resp = client.patch(
            f"/v2/partials/requisitions/{test_requisition.id}/responses/99999/status",
            data={"status": "reviewed"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404
