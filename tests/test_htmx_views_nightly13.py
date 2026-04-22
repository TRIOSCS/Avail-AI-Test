"""tests/test_htmx_views_nightly13.py — Coverage for vendor CRUD, follow-ups, response
review.

Targets: vendor edit form/save, toggle-blacklist, contact-timeline, contact-nudges,
vendor reviews (get/add/delete), prospect save/promote/delete, follow-ups list/send,
response review, poll-inbox, delete-requirement, search add-to-requisition,
companies redirect, AI cleanup email (test mode).

Called by: pytest autodiscovery
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

import os

os.environ["TESTING"] = "1"

import uuid
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    Requirement,
    Requisition,
    User,
    VendorCard,
    VendorContact,
)
from app.models.enrichment import ProspectContact
from app.models.offers import Contact as RfqContact
from app.models.vendors import VendorReview

# ── Helpers ──────────────────────────────────────────────────────────────


def _review(db: Session, vendor: VendorCard, user: User, **kw) -> VendorReview:
    defaults = dict(
        vendor_card_id=vendor.id,
        user_id=user.id,
        rating=4,
        comment="Good vendor",
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    r = VendorReview(**defaults)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _prospect(db: Session, vendor: VendorCard, **kw) -> ProspectContact:
    defaults = dict(
        vendor_card_id=vendor.id,
        full_name="Bob Prospect",
        title="Sales",
        email=f"bob-{uuid.uuid4().hex[:6]}@vendor.com",
        source="web_search",
        confidence="medium",
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    p = ProspectContact(**defaults)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _rfq_contact(db: Session, req: Requisition, user: User | None = None, **kw) -> RfqContact:
    # RfqContact.user_id is NOT NULL in the schema
    # We pass user_id directly; caller must supply user or pass user_id in kw
    defaults: dict = dict(
        requisition_id=req.id,
        vendor_name="Arrow Electronics",
        vendor_contact="sales@arrow.com",
        vendor_name_normalized="arrow electronics",
        contact_type="email",
        status="sent",
        subject="RFQ for LM317T",
        parts_included=["LM317T"],
        created_at=datetime.now(timezone.utc),
    )
    if user is not None:
        defaults["user_id"] = user.id
    defaults.update(kw)
    c = RfqContact(**defaults)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


# ── Vendor edit tests ────────────────────────────────────────────────────


class TestVendorEditForm:
    def test_get_edit_form_returns_200(self, client: TestClient, test_vendor_card: VendorCard) -> None:
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/edit-form")
        assert resp.status_code == 200

    def test_get_edit_form_not_found(self, client: TestClient) -> None:
        resp = client.get("/v2/partials/vendors/99999/edit-form")
        assert resp.status_code == 404


class TestEditVendor:
    def test_edit_vendor_display_name(
        self, client: TestClient, db_session: Session, test_vendor_card: VendorCard
    ) -> None:
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/edit",
            data={"display_name": "Updated Vendor Name"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert test_vendor_card.display_name == "Updated Vendor Name"

    def test_edit_vendor_website(self, client: TestClient, db_session: Session, test_vendor_card: VendorCard) -> None:
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/edit",
            data={"website": "https://updated.com"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert test_vendor_card.website == "https://updated.com"

    def test_edit_vendor_emails(self, client: TestClient, db_session: Session, test_vendor_card: VendorCard) -> None:
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/edit",
            data={"emails": "a@vendor.com, b@vendor.com"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert "a@vendor.com" in test_vendor_card.emails

    def test_edit_vendor_not_found(self, client: TestClient) -> None:
        resp = client.post("/v2/partials/vendors/99999/edit", data={"display_name": "X"})
        assert resp.status_code == 404


class TestToggleBlacklist:
    def test_toggle_blacklist_on(self, client: TestClient, db_session: Session, test_vendor_card: VendorCard) -> None:
        test_vendor_card.is_blacklisted = False
        db_session.commit()
        resp = client.post(f"/v2/partials/vendors/{test_vendor_card.id}/toggle-blacklist")
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert test_vendor_card.is_blacklisted is True

    def test_toggle_blacklist_off(self, client: TestClient, db_session: Session, test_vendor_card: VendorCard) -> None:
        test_vendor_card.is_blacklisted = True
        db_session.commit()
        resp = client.post(f"/v2/partials/vendors/{test_vendor_card.id}/toggle-blacklist")
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert test_vendor_card.is_blacklisted is False

    def test_toggle_blacklist_not_found(self, client: TestClient) -> None:
        resp = client.post("/v2/partials/vendors/99999/toggle-blacklist")
        assert resp.status_code == 404


class TestContactTimeline:
    def test_contact_timeline_returns_200(
        self,
        client: TestClient,
        db_session: Session,
        test_vendor_card: VendorCard,
        test_vendor_contact: VendorContact,
    ) -> None:
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/contacts/{test_vendor_contact.id}/timeline")
        assert resp.status_code == 200

    def test_contact_timeline_not_found(self, client: TestClient, test_vendor_card: VendorCard) -> None:
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/contacts/99999/timeline")
        assert resp.status_code == 404


class TestContactNudges:
    def test_contact_nudges_returns_200(self, client: TestClient, test_vendor_card: VendorCard) -> None:
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/contact-nudges")
        assert resp.status_code == 200

    def test_contact_nudges_not_found(self, client: TestClient) -> None:
        resp = client.get("/v2/partials/vendors/99999/contact-nudges")
        assert resp.status_code == 404


class TestVendorReviews:
    def test_get_reviews_empty(self, client: TestClient, test_vendor_card: VendorCard) -> None:
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/reviews")
        assert resp.status_code == 200

    def test_get_reviews_with_data(
        self, client: TestClient, db_session: Session, test_vendor_card: VendorCard, test_user: User
    ) -> None:
        _review(db_session, test_vendor_card, test_user, rating=5)
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/reviews")
        assert resp.status_code == 200

    def test_add_review_success(self, client: TestClient, db_session: Session, test_vendor_card: VendorCard) -> None:
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/reviews",
            data={"rating": "5", "comment": "Excellent supplier"},
        )
        assert resp.status_code == 200
        reviews = db_session.query(VendorReview).filter_by(vendor_card_id=test_vendor_card.id).all()
        assert len(reviews) == 1
        assert reviews[0].rating == 5

    def test_add_review_invalid_rating(
        self, client: TestClient, db_session: Session, test_vendor_card: VendorCard
    ) -> None:
        # invalid rating falls back to 3
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/reviews",
            data={"rating": "notanumber", "comment": "ok"},
        )
        assert resp.status_code == 200
        reviews = db_session.query(VendorReview).filter_by(vendor_card_id=test_vendor_card.id).all()
        assert reviews[0].rating == 3

    def test_delete_review_own(
        self, client: TestClient, db_session: Session, test_vendor_card: VendorCard, test_user: User
    ) -> None:
        review = _review(db_session, test_vendor_card, test_user)
        resp = client.delete(f"/v2/partials/vendors/{test_vendor_card.id}/reviews/{review.id}")
        assert resp.status_code == 200
        assert db_session.get(VendorReview, review.id) is None

    def test_delete_review_not_found(self, client: TestClient, test_vendor_card: VendorCard) -> None:
        resp = client.delete(f"/v2/partials/vendors/{test_vendor_card.id}/reviews/99999")
        assert resp.status_code == 404

    def test_delete_review_not_own(
        self,
        client: TestClient,
        db_session: Session,
        test_vendor_card: VendorCard,
        test_user: User,
    ) -> None:
        other_user = User(
            email="other@test.com",
            name="Other",
            role="buyer",
            azure_id="other-az-01",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(other_user)
        db_session.commit()
        db_session.refresh(other_user)
        review = _review(db_session, test_vendor_card, other_user)
        resp = client.delete(f"/v2/partials/vendors/{test_vendor_card.id}/reviews/{review.id}")
        assert resp.status_code == 403


# ── Prospect contact CRUD ────────────────────────────────────────────────


class TestProspectSave:
    def test_save_prospect(self, client: TestClient, db_session: Session, test_vendor_card: VendorCard) -> None:
        p = _prospect(db_session, test_vendor_card)
        resp = client.post(f"/v2/partials/vendors/{test_vendor_card.id}/ai/prospect/{p.id}/save")
        assert resp.status_code == 200
        db_session.refresh(p)
        assert p.is_saved is True

    def test_save_prospect_not_found(self, client: TestClient, test_vendor_card: VendorCard) -> None:
        resp = client.post(f"/v2/partials/vendors/{test_vendor_card.id}/ai/prospect/99999/save")
        assert resp.status_code == 404


class TestProspectPromote:
    def test_promote_new_contact(self, client: TestClient, db_session: Session, test_vendor_card: VendorCard) -> None:
        p = _prospect(db_session, test_vendor_card, email="promote@vendor.com")
        resp = client.post(f"/v2/partials/vendors/{test_vendor_card.id}/ai/prospect/{p.id}/promote")
        assert resp.status_code == 200
        # VendorContact should be created
        vc = db_session.query(VendorContact).filter_by(email="promote@vendor.com").first()
        assert vc is not None

    def test_promote_existing_contact_updates(
        self, client: TestClient, db_session: Session, test_vendor_card: VendorCard
    ) -> None:
        # Existing contact with no title
        vc = VendorContact(
            vendor_card_id=test_vendor_card.id,
            email="existing@vendor.com",
            full_name="Existing Contact",
            source="manual",
        )
        db_session.add(vc)
        db_session.commit()
        p = _prospect(
            db_session,
            test_vendor_card,
            email="existing@vendor.com",
            full_name="Updated Name",
        )
        resp = client.post(f"/v2/partials/vendors/{test_vendor_card.id}/ai/prospect/{p.id}/promote")
        assert resp.status_code == 200

    def test_promote_not_found(self, client: TestClient, test_vendor_card: VendorCard) -> None:
        resp = client.post(f"/v2/partials/vendors/{test_vendor_card.id}/ai/prospect/99999/promote")
        assert resp.status_code == 404


class TestProspectDelete:
    def test_delete_prospect(self, client: TestClient, db_session: Session, test_vendor_card: VendorCard) -> None:
        p = _prospect(db_session, test_vendor_card)
        pid = p.id
        resp = client.delete(f"/v2/partials/vendors/{test_vendor_card.id}/ai/prospect/{p.id}")
        assert resp.status_code == 200
        assert resp.text == ""
        assert db_session.get(ProspectContact, pid) is None

    def test_delete_prospect_not_found(self, client: TestClient, test_vendor_card: VendorCard) -> None:
        resp = client.delete(f"/v2/partials/vendors/{test_vendor_card.id}/ai/prospect/99999")
        assert resp.status_code == 404


# ── Follow-ups ────────────────────────────────────────────────────────────


class TestFollowUpsList:
    def test_follow_ups_empty(self, client: TestClient) -> None:
        resp = client.get("/v2/partials/follow-ups")
        assert resp.status_code == 200

    def test_follow_ups_with_stale_contact(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_user: User,
    ) -> None:
        from datetime import timedelta

        old_date = datetime.now(timezone.utc) - timedelta(days=10)
        _rfq_contact(db_session, test_requisition, user=test_user, created_at=old_date)
        resp = client.get("/v2/partials/follow-ups")
        assert resp.status_code == 200


class TestSendFollowUp:
    def test_send_follow_up_test_mode(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_user: User,
    ) -> None:
        contact = _rfq_contact(db_session, test_requisition, user=test_user)
        resp = client.post(
            f"/v2/partials/follow-ups/{contact.id}/send",
            data={"body": "Following up on your quote."},
        )
        assert resp.status_code == 200

    def test_send_follow_up_not_found(self, client: TestClient) -> None:
        resp = client.post("/v2/partials/follow-ups/99999/send", data={"body": "hi"})
        assert resp.status_code == 404


# ── Response review + poll inbox ─────────────────────────────────────────


class TestResponseReview:
    def _make_response(self, db: Session, req: Requisition):
        from app.models.offers import VendorResponse

        vr = VendorResponse(
            requisition_id=req.id,
            vendor_name="Arrow",
            vendor_email="sales@arrow.com",
            subject="Re: RFQ",
            body="We have stock",
            status="new",
            received_at=datetime.now(timezone.utc),
        )
        db.add(vr)
        db.commit()
        db.refresh(vr)
        return vr

    def test_review_response_reviewed(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
    ) -> None:
        vr = self._make_response(db_session, test_requisition)
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/responses/{vr.id}/review",
            data={"status": "reviewed"},
        )
        assert resp.status_code == 200
        db_session.refresh(vr)
        assert vr.status == "reviewed"

    def test_review_response_rejected(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
    ) -> None:
        vr = self._make_response(db_session, test_requisition)
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/responses/{vr.id}/review",
            data={"status": "rejected"},
        )
        assert resp.status_code == 200

    def test_review_response_invalid_status(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
    ) -> None:
        vr = self._make_response(db_session, test_requisition)
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/responses/{vr.id}/review",
            data={"status": "invalid"},
        )
        assert resp.status_code == 400

    def test_review_response_not_found(self, client: TestClient, test_requisition: Requisition) -> None:
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/responses/99999/review",
            data={"status": "reviewed"},
        )
        assert resp.status_code == 404


class TestPollInbox:
    def test_poll_inbox_returns_200(self, client: TestClient, test_requisition: Requisition) -> None:
        resp = client.post(f"/v2/partials/requisitions/{test_requisition.id}/poll-inbox")
        assert resp.status_code == 200

    def test_poll_inbox_not_found(self, client: TestClient) -> None:
        resp = client.post("/v2/partials/requisitions/99999/poll-inbox")
        assert resp.status_code == 404


# ── Delete requirement ────────────────────────────────────────────────────


class TestDeleteRequirement:
    def test_delete_requirement(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
    ) -> None:
        req_item = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        assert req_item is not None
        rid = req_item.id
        resp = client.delete(f"/v2/partials/requisitions/{test_requisition.id}/requirements/{rid}")
        assert resp.status_code == 200
        assert db_session.get(Requirement, rid) is None

    def test_delete_requirement_not_found(self, client: TestClient, test_requisition: Requisition) -> None:
        resp = client.delete(f"/v2/partials/requisitions/{test_requisition.id}/requirements/99999")
        assert resp.status_code == 404

    def test_delete_requirement_wrong_req(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
    ) -> None:
        req1 = Requisition(
            name="REQ-A",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        req2 = Requisition(
            name="REQ-B",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add_all([req1, req2])
        db_session.flush()
        item = Requirement(
            requisition_id=req2.id,
            primary_mpn="BC547",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.commit()
        resp = client.delete(f"/v2/partials/requisitions/{req1.id}/requirements/{item.id}")
        assert resp.status_code == 404


# ── Search add-to-requisition ─────────────────────────────────────────────


class TestAddToRequisition:
    def test_add_items_success(self, client: TestClient, test_requisition: Requisition) -> None:
        payload = {
            "requisition_id": test_requisition.id,
            "mpn": "BC547",
            "items": [{"vendor_name": "Arrow", "qty_available": 1000, "unit_price": 0.10}],
        }
        resp = client.post(
            "/v2/partials/search/add-to-requisition",
            json=payload,
        )
        assert resp.status_code == 200
        assert "Added 1" in resp.text

    def test_add_items_missing_fields(self, client: TestClient) -> None:
        resp = client.post(
            "/v2/partials/search/add-to-requisition",
            json={"requisition_id": 1},
        )
        assert resp.status_code == 400

    def test_add_items_req_not_found(self, client: TestClient) -> None:
        payload = {
            "requisition_id": 99999,
            "mpn": "LM317T",
            "items": [{"vendor_name": "Arrow"}],
        }
        resp = client.post(
            "/v2/partials/search/add-to-requisition",
            json=payload,
        )
        assert resp.status_code == 404

    def test_add_items_creates_requirement_if_missing(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
    ) -> None:
        # Use a new MPN not already in the requisition
        payload = {
            "requisition_id": test_requisition.id,
            "mpn": "NEWMPN999",
            "items": [{"vendor_name": "Mouser", "qty_available": 500}],
        }
        resp = client.post(
            "/v2/partials/search/add-to-requisition",
            json=payload,
        )
        assert resp.status_code == 200
        # New requirement should exist
        new_req = (
            db_session.query(Requirement).filter_by(requisition_id=test_requisition.id, primary_mpn="NEWMPN999").first()
        )
        assert new_req is not None


# ── Companies redirect ────────────────────────────────────────────────────


class TestCompaniesRedirect:
    def test_companies_redirects_to_customers(self, client: TestClient) -> None:
        resp = client.get("/v2/companies", follow_redirects=False)
        assert resp.status_code == 301
        assert "/v2/customers" in resp.headers["location"]

    def test_companies_path_redirects(self, client: TestClient) -> None:
        resp = client.get("/v2/companies/123", follow_redirects=False)
        assert resp.status_code == 301
        assert "/v2/customers/123" in resp.headers["location"]

    def test_partials_companies_redirects(self, client: TestClient) -> None:
        resp = client.get("/v2/partials/companies", follow_redirects=False)
        assert resp.status_code == 301
        assert "/v2/partials/customers" in resp.headers["location"]


# ── AI cleanup email ──────────────────────────────────────────────────────


class TestAiCleanupEmail:
    def test_cleanup_empty_body(self, client: TestClient, test_requisition: Requisition) -> None:
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/ai-cleanup-email",
            data={"body": ""},
        )
        assert resp.status_code == 200
        assert "Write your email first" in resp.text

    def test_cleanup_with_body_mocked(self, client: TestClient, test_requisition: Requisition) -> None:
        with patch("app.utils.claude_client.claude_text") as mock_ct:
            mock_ct.return_value = "Dear Vendor, please provide a quote."
            resp = client.post(
                f"/v2/partials/requisitions/{test_requisition.id}/ai-cleanup-email",
                data={"body": "hey vendor pls quote"},
            )
        assert resp.status_code == 200

    def test_cleanup_req_not_found(self, client: TestClient) -> None:
        resp = client.post(
            "/v2/partials/requisitions/99999/ai-cleanup-email",
            data={"body": "some email text"},
        )
        assert resp.status_code == 404


# ── Log activity ──────────────────────────────────────────────────────────


class TestLogActivity:
    def test_log_activity_note(self, client: TestClient, test_requisition: Requisition) -> None:
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/log-activity",
            data={"activity_type": "note", "notes": "Called vendor today"},
        )
        assert resp.status_code == 200

    def test_log_activity_phone_call(self, client: TestClient, test_requisition: Requisition) -> None:
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/log-activity",
            data={"activity_type": "phone_call", "vendor_name": "Arrow"},
        )
        assert resp.status_code == 200

    def test_log_activity_req_not_found(self, client: TestClient) -> None:
        resp = client.post(
            "/v2/partials/requisitions/99999/log-activity",
            data={"activity_type": "note", "notes": "test"},
        )
        assert resp.status_code == 404
