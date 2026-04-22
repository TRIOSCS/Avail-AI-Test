"""tests/test_htmx_views_nightly15.py — Coverage for offer queue, RFQ send, responses.

Targets:
  - offer_review_queue (GET)
  - promote_offer_htmx (POST)
  - reject_offer_htmx (POST)
  - offer_changelog (GET)
  - rfq_compose (GET)
  - rfq_send (POST, test-mode)
  - send_follow_up_htmx (POST, test-mode)
  - review_response_htmx (POST)

Called by: pytest autodiscovery
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import ContactStatus, OfferStatus
from app.models import Offer, Requisition, User
from app.models.offers import Contact as RfqContact
from app.models.offers import VendorResponse

# ── Helpers ───────────────────────────────────────────────────────────────


def _make_offer(db: Session, req: Requisition, user: User, **kw) -> Offer:
    defaults = dict(
        requisition_id=req.id,
        vendor_name="TestVendor",
        mpn="NE555",
        status=OfferStatus.ACTIVE,
        source="manual",
        entered_by_id=user.id,
    )
    defaults.update(kw)
    o = Offer(**defaults)
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


def _make_contact(db: Session, req: Requisition, user: User, **kw) -> RfqContact:
    defaults = dict(
        requisition_id=req.id,
        user_id=user.id,
        contact_type="email",
        vendor_name="AcmeDist",
        vendor_name_normalized="acmedist",
        vendor_contact="acme@example.com",
        status=ContactStatus.SENT,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    c = RfqContact(**defaults)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _make_vendor_response(db: Session, req: Requisition, **kw) -> VendorResponse:
    defaults = dict(
        requisition_id=req.id,
        vendor_name="AcmeDist",
        vendor_email="acme@example.com",
        subject="RE: RFQ",
        body="We have 500 pcs at $0.10 each.",
        status="new",
        received_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    vr = VendorResponse(**defaults)
    db.add(vr)
    db.commit()
    db.refresh(vr)
    return vr


# ── Offer Review Queue ────────────────────────────────────────────────────


class TestOfferReviewQueue:
    def test_empty_queue(self, client: TestClient):
        resp = client.get("/v2/partials/offers/review-queue")
        assert resp.status_code == 200

    def test_queue_with_pending_offer(
        self, client: TestClient, db_session: Session, test_requisition: Requisition, test_user: User
    ):
        _make_offer(db_session, test_requisition, test_user, status=OfferStatus.PENDING_REVIEW)
        resp = client.get("/v2/partials/offers/review-queue")
        assert resp.status_code == 200


# ── Promote Offer ─────────────────────────────────────────────────────────


class TestPromoteOffer:
    def test_promote_success(
        self, client: TestClient, db_session: Session, test_requisition: Requisition, test_user: User
    ):
        offer = _make_offer(db_session, test_requisition, test_user, status=OfferStatus.PENDING_REVIEW)
        resp = client.post(f"/v2/partials/offers/{offer.id}/promote")
        assert resp.status_code == 200
        db_session.refresh(offer)
        assert offer.status == OfferStatus.ACTIVE

    def test_promote_not_found(self, client: TestClient):
        resp = client.post("/v2/partials/offers/99999/promote")
        assert resp.status_code == 404

    def test_promote_wrong_status(
        self, client: TestClient, db_session: Session, test_requisition: Requisition, test_user: User
    ):
        offer = _make_offer(db_session, test_requisition, test_user, status=OfferStatus.ACTIVE)
        resp = client.post(f"/v2/partials/offers/{offer.id}/promote")
        assert resp.status_code == 400


# ── Reject Offer ──────────────────────────────────────────────────────────


class TestRejectOffer:
    def test_reject_success(
        self, client: TestClient, db_session: Session, test_requisition: Requisition, test_user: User
    ):
        offer = _make_offer(db_session, test_requisition, test_user, status=OfferStatus.PENDING_REVIEW)
        resp = client.post(f"/v2/partials/offers/{offer.id}/reject")
        assert resp.status_code == 200
        db_session.refresh(offer)
        assert offer.status == OfferStatus.REJECTED

    def test_reject_not_found(self, client: TestClient):
        resp = client.post("/v2/partials/offers/99999/reject")
        assert resp.status_code == 404

    def test_reject_wrong_status(
        self, client: TestClient, db_session: Session, test_requisition: Requisition, test_user: User
    ):
        offer = _make_offer(db_session, test_requisition, test_user, status=OfferStatus.ACTIVE)
        resp = client.post(f"/v2/partials/offers/{offer.id}/reject")
        assert resp.status_code == 400


# ── Offer Changelog ───────────────────────────────────────────────────────


class TestOfferChangelog:
    def test_get_changelog(
        self, client: TestClient, db_session: Session, test_requisition: Requisition, test_user: User
    ):
        offer = _make_offer(db_session, test_requisition, test_user)
        resp = client.get(f"/v2/partials/offers/{offer.id}/changelog")
        assert resp.status_code == 200

    def test_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/offers/99999/changelog")
        assert resp.status_code == 404


# ── RFQ Compose ───────────────────────────────────────────────────────────


class TestRfqCompose:
    def test_compose_empty(self, client: TestClient, test_requisition: Requisition):
        resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/rfq-compose")
        assert resp.status_code == 200

    def test_req_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/requisitions/99999/rfq-compose")
        assert resp.status_code == 404


# ── RFQ Send (Test Mode) ──────────────────────────────────────────────────


class TestRfqSend:
    def test_send_creates_contacts(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        """In TESTING=1 mode, creates Contact records without sending real email."""
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/rfq-send",
            data={
                "vendor_names": "AcmeDist",
                "vendor_emails": "acme@example.com",
                "subject": "RFQ Test",
                "body": "Please quote NE555",
            },
        )
        assert resp.status_code == 200

    def test_send_multiple_vendors(self, client: TestClient, test_requisition: Requisition):
        form_bytes = (
            b"vendor_names=VendorA&vendor_emails=a%40example.com&vendor_names=VendorB&vendor_emails=b%40example.com"
        )
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/rfq-send",
            content=form_bytes,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 200

    def test_send_no_vendors_selected(self, client: TestClient, test_requisition: Requisition):
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/rfq-send",
            data={"subject": "RFQ"},
        )
        assert resp.status_code == 400

    def test_send_req_not_found(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/99999/rfq-send",
            data={"vendor_names": "V", "vendor_emails": "v@example.com"},
        )
        assert resp.status_code == 404

    def test_send_vendor_no_email(self, client: TestClient, test_requisition: Requisition):
        """Vendor with no email is skipped but request succeeds."""
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/rfq-send",
            data={"vendor_names": "VendorNoEmail", "vendor_emails": ""},
        )
        assert resp.status_code == 200


# ── Send Follow-Up (Test Mode) ────────────────────────────────────────────


class TestSendFollowUp:
    def test_send_follow_up_test_mode(
        self, client: TestClient, db_session: Session, test_requisition: Requisition, test_user: User
    ):
        """In TESTING=1 mode, marks contact as sent without real email."""
        contact = _make_contact(db_session, test_requisition, test_user)
        resp = client.post(
            f"/v2/partials/follow-ups/{contact.id}/send",
            data={"body": "Following up on our request..."},
        )
        assert resp.status_code == 200
        db_session.refresh(contact)
        assert contact.status == ContactStatus.SENT

    def test_send_follow_up_not_found(self, client: TestClient):
        resp = client.post("/v2/partials/follow-ups/99999/send")
        assert resp.status_code == 404


# ── Review Response ───────────────────────────────────────────────────────


class TestReviewResponse:
    def test_mark_reviewed(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        vr = _make_vendor_response(db_session, test_requisition)
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/responses/{vr.id}/review",
            data={"status": "reviewed"},
        )
        assert resp.status_code == 200
        db_session.refresh(vr)
        assert vr.status == "reviewed"

    def test_mark_rejected(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        vr = _make_vendor_response(db_session, test_requisition)
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/responses/{vr.id}/review",
            data={"status": "rejected"},
        )
        assert resp.status_code == 200
        db_session.refresh(vr)
        assert vr.status == "rejected"

    def test_invalid_status(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        vr = _make_vendor_response(db_session, test_requisition)
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/responses/{vr.id}/review",
            data={"status": "approved"},
        )
        assert resp.status_code == 400

    def test_response_not_found(self, client: TestClient, test_requisition: Requisition):
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/responses/99999/review",
            data={"status": "reviewed"},
        )
        assert resp.status_code == 404
