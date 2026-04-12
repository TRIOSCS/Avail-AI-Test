"""tests/test_htmx_views_nightly8.py — Coverage improvement for htmx_views.py.

Targets missing lines: requisition bulk-assign, add_offer, edit_offer,
log_activity, rfq_send test-mode, follow_up_send, mark_response_reviewed,
edit_requirement, add_search_result_to_req, buy_plans_list, buy-plan workflow,
sourcing workspace filters, bulk-archive/unarchive, log-phone, rfq-prepare.

Called by: pytest
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

import os
import uuid
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

os.environ["TESTING"] = "1"

from app.constants import (  # noqa: E402
    BuyPlanStatus,
    OfferStatus,
    QuoteStatus,
    RequisitionStatus,
    SourcingStatus,
)
from app.models import (  # noqa: E402
    BuyPlan,
    Offer,
    Quote,
    Requirement,
    Requisition,
    SourcingLead,
    User,
)
from app.models.offers import Contact as RfqContact
from app.models.offers import VendorResponse

# ── Helpers ───────────────────────────────────────────────────────────────


def _req(db: Session, user: User, **kw) -> Requisition:
    defaults = dict(
        name=f"N8-REQ-{uuid.uuid4().hex[:6]}",
        customer_name="Acme",
        status=RequisitionStatus.ACTIVE,
        created_by=user.id,
        claimed_by_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    r = Requisition(**defaults)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _requirement(db: Session, req: Requisition, **kw) -> Requirement:
    defaults = dict(
        requisition_id=req.id,
        primary_mpn=f"LM{uuid.uuid4().hex[:4]}",
        normalized_mpn=f"LM{uuid.uuid4().hex[:4]}",
        target_qty=100,
        sourcing_status=SourcingStatus.OPEN,
    )
    defaults.update(kw)
    r = Requirement(**defaults)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _offer(db: Session, req: Requisition, user: User, **kw) -> Offer:
    defaults = dict(
        requisition_id=req.id,
        vendor_name="Arrow Electronics",
        vendor_name_normalized="arrow electronics",
        mpn="LM317T",
        normalized_mpn="LM317T",
        qty_available=1000,
        unit_price=0.50,
        status=OfferStatus.ACTIVE,
        entered_by_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    o = Offer(**defaults)
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


def _rfq_contact(db: Session, req: Requisition, user: User, **kw) -> RfqContact:
    defaults = dict(
        requisition_id=req.id,
        user_id=user.id,
        contact_type="email",
        vendor_name="Arrow Electronics",
        vendor_name_normalized="arrow electronics",
        vendor_contact="sales@arrow.com",
        parts_included="LM317T x100",
        subject="RFQ",
        status="sent",
    )
    defaults.update(kw)
    c = RfqContact(**defaults)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _vendor_response(db: Session, req: Requisition, **kw) -> VendorResponse:
    defaults = dict(
        requisition_id=req.id,
        vendor_name="Arrow",
        vendor_email="sales@arrow.com",
        subject="Re: RFQ",
        body="We have stock.",
        status="new",
        received_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    vr = VendorResponse(**defaults)
    db.add(vr)
    db.commit()
    db.refresh(vr)
    return vr


def _sourcing_lead(db: Session, req: Requisition, item: Requirement, **kw) -> SourcingLead:
    defaults = dict(
        lead_id=uuid.uuid4().hex,
        requirement_id=item.id,
        requisition_id=req.id,
        part_number_requested=item.primary_mpn,
        part_number_matched=item.primary_mpn,
        vendor_name="Digi-Key",
        vendor_name_normalized="digi-key",
        primary_source_type="api",
        primary_source_name="DigiKey",
        confidence_score=0.85,
        confidence_band="high",
        reason_summary="Good match",
        buyer_status="new",
    )
    defaults.update(kw)
    lead = SourcingLead(**defaults)
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return lead


def _buy_plan(db: Session, req: Requisition, quote: Quote, user: User, **kw) -> BuyPlan:
    defaults = dict(
        requisition_id=req.id,
        quote_id=quote.id,
        submitted_by_id=user.id,
        status=BuyPlanStatus.DRAFT,
    )
    defaults.update(kw)
    bp = BuyPlan(**defaults)
    db.add(bp)
    db.commit()
    db.refresh(bp)
    return bp


def _quote(db: Session, req: Requisition, user: User, **kw) -> Quote:
    defaults = dict(
        requisition_id=req.id,
        quote_number=f"Q-{uuid.uuid4().hex[:6]}",
        status=QuoteStatus.DRAFT,
        created_by_id=user.id,
    )
    defaults.update(kw)
    q = Quote(**defaults)
    db.add(q)
    db.commit()
    db.refresh(q)
    return q


# ── Section 1: requisitions_bulk_action (assign branch) ──────────────


class TestBulkActionAssign:
    """Covers lines 1635-1665 (assign branch of bulk action)."""

    def test_bulk_assign_owner(self, client: TestClient, db_session: Session, test_user: User):
        r1 = _req(db_session, test_user)
        resp = client.post(
            "/v2/partials/requisitions/bulk/assign",
            data={"ids": str(r1.id), "owner_id": str(test_user.id)},
        )
        assert resp.status_code == 200

    def test_bulk_assign_no_owner_id(self, client: TestClient, db_session: Session, test_user: User):
        r1 = _req(db_session, test_user)
        resp = client.post(
            "/v2/partials/requisitions/bulk/assign",
            data={"ids": str(r1.id)},
        )
        # No owner_id is supplied — should succeed (no-op assign)
        assert resp.status_code == 200

    def test_bulk_assign_invalid_owner_id(self, client: TestClient, db_session: Session, test_user: User):
        r1 = _req(db_session, test_user)
        resp = client.post(
            "/v2/partials/requisitions/bulk/assign",
            data={"ids": str(r1.id), "owner_id": "not-a-number"},
        )
        assert resp.status_code == 400

    def test_bulk_too_many_ids(self, client: TestClient, db_session: Session, test_user: User):
        ids = ",".join(str(i) for i in range(1, 202))
        resp = client.post(
            "/v2/partials/requisitions/bulk/archive",
            data={"ids": ids},
        )
        assert resp.status_code == 400

    def test_bulk_invalid_id_format(self, client: TestClient, db_session: Session, test_user: User):
        resp = client.post(
            "/v2/partials/requisitions/bulk/archive",
            data={"ids": "1,abc,3"},
        )
        assert resp.status_code == 400


# ── Section 2: requisition_create with parts_text ────────────────────


class TestRequisitionCreateWithParts:
    """Covers lines 1022, 1052-1072 (create req with parts text)."""

    def test_create_with_parts_text(self, client: TestClient, db_session: Session, test_user: User):
        resp = client.post(
            "/v2/partials/requisitions/create",
            data={
                "name": "N8-CREATE-01",
                "customer_name": "Acme",
                "parts_text": "LM317T, 500\nTL431, 200\n",
            },
        )
        assert resp.status_code == 200

    def test_create_with_invalid_qty_in_parts(self, client: TestClient, db_session: Session, test_user: User):
        resp = client.post(
            "/v2/partials/requisitions/create",
            data={
                "name": "N8-CREATE-02",
                "parts_text": "LM317T, notanumber",
            },
        )
        assert resp.status_code == 200

    def test_create_minimal(self, client: TestClient, db_session: Session, test_user: User):
        resp = client.post(
            "/v2/partials/requisitions/create",
            data={"name": "N8-CREATE-03"},
        )
        assert resp.status_code == 200


# ── Section 3: add_requirement_row ───────────────────────────────────


class TestAddRequirementRow:
    """Covers lines 1158-1176 (add requirement to req)."""

    def test_add_requirement_success(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/requirements",
            data={
                "primary_mpn": "LM317T",
                "manufacturer": "Texas Instruments",
                "target_qty": "500",
            },
        )
        assert resp.status_code == 200

    def test_add_requirement_missing_manufacturer(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/requirements",
            data={
                "primary_mpn": "LM317T",
                "manufacturer": "",
            },
        )
        assert resp.status_code == 422

    def test_add_requirement_req_not_found(self, client: TestClient, db_session: Session, test_user: User):
        resp = client.post(
            "/v2/partials/requisitions/999999/requirements",
            data={"primary_mpn": "LM317T", "manufacturer": "TI"},
        )
        assert resp.status_code == 404


# ── Section 4: requisition_search_all ────────────────────────────────


class TestRequisitionSearchAll:
    """Covers lines 1207-1227 (search all requirements)."""

    def test_search_all_with_requirements(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        _requirement(db_session, req, primary_mpn="LM317T")
        resp = client.post(f"/v2/partials/requisitions/{req.id}/search-all")
        assert resp.status_code == 200

    def test_search_all_no_requirements(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        resp = client.post(f"/v2/partials/requisitions/{req.id}/search-all")
        assert resp.status_code == 200

    def test_search_all_not_found(self, client: TestClient):
        resp = client.post("/v2/partials/requisitions/999999/search-all")
        assert resp.status_code == 404


# ── Section 5: add_offer ────────────────────────────────────────────


class TestAddOffer:
    """Covers lines 2040-2084 (add manual offer)."""

    def test_add_offer_success(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/add-offer",
            data={
                "vendor_name": "Arrow Electronics",
                "mpn": "LM317T",
                "qty_available": "1000",
                "unit_price": "0.50",
                "lead_time": "4 weeks",
                "condition": "New",
            },
        )
        assert resp.status_code == 200

    def test_add_offer_missing_vendor_name(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/add-offer",
            data={"vendor_name": "", "mpn": "LM317T"},
        )
        assert resp.status_code == 400

    def test_add_offer_missing_mpn(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/add-offer",
            data={"vendor_name": "Arrow", "mpn": ""},
        )
        assert resp.status_code == 400

    def test_add_offer_req_not_found(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/999999/add-offer",
            data={"vendor_name": "Arrow", "mpn": "LM317T"},
        )
        assert resp.status_code == 404

    def test_add_offer_with_requirement_id(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/add-offer",
            data={
                "vendor_name": "Arrow Electronics",
                "mpn": item.primary_mpn,
                "qty_available": "500",
                "unit_price": "1.25",
                "requirement_id": str(item.id),
            },
        )
        assert resp.status_code == 200


# ── Section 6: edit_offer_htmx ───────────────────────────────────────


class TestEditOfferHtmx:
    """Covers lines 2152-2215 (edit offer via POST to /edit)."""

    def test_edit_offer_vendor_name(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        o = _offer(db_session, req, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/offers/{o.id}/edit",
            data={
                "vendor_name": "Mouser Electronics",
                "qty_available": "2000",
                "unit_price": "0.75",
                "lead_time": "2 weeks",
            },
        )
        assert resp.status_code == 200

    def test_edit_offer_numeric_fields(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        o = _offer(db_session, req, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/offers/{o.id}/edit",
            data={"moq": "100", "spq": "10"},
        )
        assert resp.status_code == 200

    def test_edit_offer_not_found(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/offers/999999/edit",
            data={"vendor_name": "Mouser"},
        )
        assert resp.status_code == 404

    def test_edit_offer_req_not_found(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/999999/offers/1/edit",
            data={"vendor_name": "Mouser"},
        )
        assert resp.status_code == 404

    def test_edit_offer_with_valid_until(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        o = _offer(db_session, req, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/offers/{o.id}/edit",
            data={"valid_until": "2026-12-31", "notes": "Updated"},
        )
        assert resp.status_code == 200


# ── Section 7: log_activity_htmx ─────────────────────────────────────


class TestLogActivity:
    """Covers lines 2385-2404 (log activity for requisition)."""

    def test_log_note_activity(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/log-activity",
            data={
                "activity_type": "note",
                "notes": "Contacted vendor about availability.",
                "vendor_name": "Arrow Electronics",
            },
        )
        assert resp.status_code == 200

    def test_log_phone_call_activity(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/log-activity",
            data={
                "activity_type": "phone_call",
                "notes": "Called about LM317T",
                "vendor_name": "Mouser",
            },
        )
        assert resp.status_code == 200

    def test_log_email_activity(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/log-activity",
            data={
                "activity_type": "email_sent",
                "vendor_name": "DigiKey",
                "contact_email": "rfq@digikey.com",
            },
        )
        assert resp.status_code == 200

    def test_log_activity_req_not_found(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/999999/log-activity",
            data={"activity_type": "note", "notes": "test"},
        )
        assert resp.status_code == 404


# ── Section 8: send_rfq test-mode ───────────────────────────────────


class TestRfqSendTestMode:
    """Covers lines 2542-2641 (rfq_send in TESTING=1 mode)."""

    def test_rfq_send_creates_contacts_in_test_mode(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/rfq-send",
            data={
                "vendor_names": ["Arrow Electronics", "DigiKey"],
                "vendor_emails": ["sales@arrow.com", "rfq@digikey.com"],
                "subject": "RFQ - LM317T",
                "body": "Please provide pricing.",
                "parts_summary": "LM317T x500",
            },
        )
        assert resp.status_code == 200
        contacts = db_session.query(RfqContact).filter(RfqContact.requisition_id == req.id).all()
        assert len(contacts) == 2

    def test_rfq_send_skips_empty_emails(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/rfq-send",
            data={
                "vendor_names": ["Arrow", "Unknown"],
                "vendor_emails": ["sales@arrow.com", ""],
            },
        )
        assert resp.status_code == 200
        contacts = db_session.query(RfqContact).filter(RfqContact.requisition_id == req.id).all()
        assert len(contacts) == 1

    def test_rfq_send_no_vendors_returns_400(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/rfq-send",
            data={},
        )
        assert resp.status_code == 400

    def test_rfq_send_req_not_found(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/999999/rfq-send",
            data={"vendor_names": ["Arrow"], "vendor_emails": ["sales@arrow.com"]},
        )
        assert resp.status_code == 404


# ── Section 9: send_follow_up_htmx ───────────────────────────────────


class TestSendFollowUpHtmx:
    """Covers lines 2728-2752 (send follow-up email)."""

    def test_send_follow_up_in_test_mode(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        contact = _rfq_contact(db_session, req, test_user)
        resp = client.post(
            f"/v2/partials/follow-ups/{contact.id}/send",
            data={"body": "Just following up on our RFQ."},
        )
        assert resp.status_code == 200
        db_session.refresh(contact)
        assert contact.status == "sent"

    def test_send_follow_up_contact_not_found(self, client: TestClient):
        resp = client.post(
            "/v2/partials/follow-ups/999999/send",
            data={},
        )
        assert resp.status_code == 404

    def test_send_follow_up_no_body(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        contact = _rfq_contact(db_session, req, test_user)
        resp = client.post(
            f"/v2/partials/follow-ups/{contact.id}/send",
            data={},
        )
        assert resp.status_code == 200


# ── Section 10: mark_response_reviewed ───────────────────────────────


class TestMarkResponseReviewed:
    """Covers lines 2803-2815 (mark vendor response as reviewed)."""

    def test_mark_reviewed(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        vr = _vendor_response(db_session, req)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/responses/{vr.id}/review",
            data={"status": "reviewed"},
        )
        assert resp.status_code == 200
        db_session.refresh(vr)
        assert vr.status == "reviewed"

    def test_mark_rejected(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        vr = _vendor_response(db_session, req)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/responses/{vr.id}/review",
            data={"status": "rejected"},
        )
        assert resp.status_code == 200
        db_session.refresh(vr)
        assert vr.status == "rejected"

    def test_mark_invalid_status(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        vr = _vendor_response(db_session, req)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/responses/{vr.id}/review",
            data={"status": "invalid"},
        )
        assert resp.status_code == 400

    def test_mark_response_not_found(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/responses/999999/review",
            data={"status": "reviewed"},
        )
        assert resp.status_code == 404


# ── Section 11: edit_requirement_htmx ────────────────────────────────


class TestEditRequirementHtmx:
    """Covers lines 2935-2953 (edit requirement via PATCH)."""

    def test_edit_requirement_mpn(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        resp = client.put(
            f"/v2/partials/requisitions/{req.id}/requirements/{item.id}",
            data={
                "primary_mpn": "TL431",
                "target_qty": "200",
                "manufacturer": "TI",
                "need_by_date": "",
            },
        )
        assert resp.status_code == 200

    def test_edit_requirement_with_need_by_date(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        resp = client.put(
            f"/v2/partials/requisitions/{req.id}/requirements/{item.id}",
            data={
                "primary_mpn": "LM317T",
                "target_qty": "100",
                "manufacturer": "TI",
                "need_by_date": "2026-06-30",
            },
        )
        assert resp.status_code == 200

    def test_edit_requirement_not_found(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        resp = client.put(
            f"/v2/partials/requisitions/{req.id}/requirements/999999",
            data={"primary_mpn": "LM317T", "manufacturer": "TI"},
        )
        assert resp.status_code == 404

    def test_edit_requirement_invalid_need_by_date(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        resp = client.put(
            f"/v2/partials/requisitions/{req.id}/requirements/{item.id}",
            data={
                "primary_mpn": item.primary_mpn,
                "target_qty": "100",
                "manufacturer": "TI",
                "need_by_date": "not-a-date",
            },
        )
        assert resp.status_code == 200


# ── Section 12: add_search_result_to_requisition ─────────────────────


class TestAddSearchResultToReq:
    """Covers lines 3314-3386 (add search result to existing req)."""

    def test_add_new_requirement_with_sightings(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        resp = client.post(
            "/v2/partials/search/add-to-requisition",
            json={
                "requisition_id": req.id,
                "mpn": "LM317T",
                "items": [
                    {
                        "vendor_name": "Arrow Electronics",
                        "mpn_matched": "LM317T",
                        "qty_available": 5000,
                        "unit_price": 0.45,
                        "source_type": "api",
                        "confidence": 0.9,
                        "score": 85.0,
                    }
                ],
            },
        )
        assert resp.status_code == 200
        assert "LM317T" in resp.text or "Added" in resp.text

    def test_add_to_existing_requirement(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req, primary_mpn="LM317T")
        resp = client.post(
            "/v2/partials/search/add-to-requisition",
            json={
                "requisition_id": req.id,
                "mpn": "LM317T",
                "items": [
                    {
                        "vendor_name": "Mouser",
                        "mpn_matched": "LM317T",
                        "qty_available": 1000,
                        "unit_price": 0.55,
                        "source_type": "api",
                        "confidence": 0.8,
                        "score": 75.0,
                    }
                ],
            },
        )
        assert resp.status_code == 200

    def test_add_to_req_missing_fields(self, client: TestClient):
        resp = client.post(
            "/v2/partials/search/add-to-requisition",
            json={"requisition_id": None, "mpn": "", "items": []},
        )
        assert resp.status_code == 400

    def test_add_to_req_not_found(self, client: TestClient):
        resp = client.post(
            "/v2/partials/search/add-to-requisition",
            json={"requisition_id": 999999, "mpn": "LM317T", "items": [{"vendor_name": "Arrow"}]},
        )
        assert resp.status_code == 404

    def test_add_multiple_sightings(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        resp = client.post(
            "/v2/partials/search/add-to-requisition",
            json={
                "requisition_id": req.id,
                "mpn": "TL431",
                "items": [
                    {"vendor_name": "Arrow", "mpn_matched": "TL431", "confidence": 0.9, "score": 80.0},
                    {"vendor_name": "Mouser", "mpn_matched": "TL431", "confidence": 0.85, "score": 78.0},
                    {"vendor_name": "DigiKey", "mpn_matched": "TL431", "confidence": 0.8, "score": 75.0},
                ],
            },
        )
        assert resp.status_code == 200
        assert "3" in resp.text or "Added" in resp.text


# ── Section 13: create_quote_from_offers ────────────────────────────


class TestCreateQuoteFromOffers:
    """Covers lines 1916-1971, 1987-2010 (create quote from selected offers)."""

    def test_create_quote_from_offers_success(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        o1 = _offer(db_session, req, test_user, mpn="LM317T", qty_available=500, unit_price=0.50)
        o2 = _offer(db_session, req, test_user, mpn="TL431", qty_available=200, unit_price=0.75)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/create-quote",
            data={"offer_ids": [str(o1.id), str(o2.id)]},
        )
        assert resp.status_code == 200

    def test_create_quote_no_offers_selected(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/create-quote",
            data={},
        )
        assert resp.status_code == 400

    def test_create_quote_offers_not_found(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/create-quote",
            data={"offer_ids": ["999999"]},
        )
        assert resp.status_code == 404

    def test_create_quote_req_not_found(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/999999/create-quote",
            data={"offer_ids": ["1"]},
        )
        assert resp.status_code == 404


# ── Section 14: buy_plans_list_partial ───────────────────────────────


class TestBuyPlansListPartial:
    """Covers lines 5891, 5898-5904 (buy plans list)."""

    def test_buy_plans_list_empty(self, client: TestClient, db_session: Session):
        resp = client.get("/v2/partials/buy-plans")
        assert resp.status_code == 200

    def test_buy_plans_list_with_status_filter(self, client: TestClient, db_session: Session, test_user: User):
        resp = client.get("/v2/partials/buy-plans?status=pending")
        assert resp.status_code == 200

    def test_buy_plans_list_mine_filter(self, client: TestClient, db_session: Session, test_user: User):
        resp = client.get("/v2/partials/buy-plans?mine=true")
        assert resp.status_code == 200

    def test_buy_plans_list_with_search(self, client: TestClient, db_session: Session, test_user: User):
        resp = client.get("/v2/partials/buy-plans?q=SO-1234")
        assert resp.status_code == 200


# ── Section 15: sourcing workspace with filter params ────────────────


class TestSourcingWorkspaceFilters:
    """Covers lines 6688-6822 (workspace filter branches)."""

    def test_sourcing_workspace_with_confidence_filter(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        resp = client.get(f"/v2/partials/sourcing/{item.id}/workspace?confidence=high&safety=verified")
        assert resp.status_code == 200

    def test_sourcing_workspace_with_freshness_filter(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        resp = client.get(f"/v2/partials/sourcing/{item.id}/workspace?freshness=7d")
        assert resp.status_code == 200

    def test_sourcing_leads_rows_with_filters(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        _sourcing_lead(db_session, req, item, confidence_band="high", contact_email="vendor@test.com")
        resp = client.get(f"/v2/partials/sourcing/{item.id}/workspace-list?confidence=high&contactability=has_email")
        assert resp.status_code == 200

    def test_sourcing_leads_rows_corroborated_filter(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        resp = client.get(f"/v2/partials/sourcing/{item.id}/workspace-list?corroborated=yes&source=api")
        assert resp.status_code == 200

    def test_sourcing_leads_not_corroborated(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        resp = client.get(f"/v2/partials/sourcing/{item.id}/workspace-list?corroborated=no")
        assert resp.status_code == 200


# ── Section 16: bulk-archive and bulk-unarchive parts ─────────────────


class TestBulkArchiveParts:
    """Covers lines 9867-9929 (bulk archive and unarchive parts)."""

    def test_bulk_archive_requirements(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req, sourcing_status=SourcingStatus.OPEN)
        resp = client.post(
            "/v2/partials/parts/bulk-archive",
            json={"requirement_ids": [item.id], "requisition_ids": []},
        )
        assert resp.status_code == 200
        db_session.refresh(item)
        assert item.sourcing_status == SourcingStatus.ARCHIVED

    def test_bulk_archive_requisitions(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user, status=RequisitionStatus.ACTIVE)
        item = _requirement(db_session, req)
        resp = client.post(
            "/v2/partials/parts/bulk-archive",
            json={"requirement_ids": [], "requisition_ids": [req.id]},
        )
        assert resp.status_code == 200
        db_session.refresh(req)
        assert req.status == RequisitionStatus.ARCHIVED

    def test_bulk_archive_empty_body(self, client: TestClient):
        resp = client.post(
            "/v2/partials/parts/bulk-archive",
            json={"requirement_ids": [], "requisition_ids": []},
        )
        assert resp.status_code == 200

    def test_bulk_unarchive_requirements(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req, sourcing_status=SourcingStatus.ARCHIVED)
        resp = client.post(
            "/v2/partials/parts/bulk-unarchive",
            json={"requirement_ids": [item.id], "requisition_ids": []},
        )
        assert resp.status_code == 200
        db_session.refresh(item)
        assert item.sourcing_status == SourcingStatus.OPEN

    def test_bulk_unarchive_requisitions(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user, status=RequisitionStatus.ARCHIVED)
        item = _requirement(db_session, req, sourcing_status=SourcingStatus.ARCHIVED)
        resp = client.post(
            "/v2/partials/parts/bulk-unarchive",
            json={"requirement_ids": [], "requisition_ids": [req.id]},
        )
        assert resp.status_code == 200
        db_session.refresh(req)
        assert req.status == RequisitionStatus.ACTIVE

    def test_bulk_unarchive_empty(self, client: TestClient):
        resp = client.post(
            "/v2/partials/parts/bulk-unarchive",
            json={"requirement_ids": [], "requisition_ids": []},
        )
        assert resp.status_code == 200


# ── Section 17: log_phone_call ────────────────────────────────────────


class TestLogPhoneCall:
    """Covers lines 5409-5444 (log phone call)."""

    def test_log_phone_call_success(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/log-phone",
            data={
                "vendor_name": "Arrow Electronics",
                "vendor_phone": "+1-555-0100",
                "notes": "Discussed LM317T availability",
            },
        )
        assert resp.status_code == 200

    def test_log_phone_call_missing_vendor(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/log-phone",
            data={"vendor_name": "", "vendor_phone": "555-1234"},
        )
        assert resp.status_code == 400

    def test_log_phone_call_missing_phone(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/log-phone",
            data={"vendor_name": "Arrow", "vendor_phone": ""},
        )
        assert resp.status_code == 400

    def test_log_phone_req_not_found(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/999999/log-phone",
            data={"vendor_name": "Arrow", "vendor_phone": "555-1234"},
        )
        assert resp.status_code == 404


# ── Section 18: rfq_prepare_panel ───────────────────────────────────


class TestRfqPreparePanel:
    """Covers lines 5383 area (rfq-prepare partial)."""

    def test_rfq_prepare_with_requirements(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        _requirement(db_session, req, primary_mpn="LM317T")
        resp = client.get(f"/v2/partials/requisitions/{req.id}/rfq-prepare")
        assert resp.status_code == 200

    def test_rfq_prepare_empty_req(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        resp = client.get(f"/v2/partials/requisitions/{req.id}/rfq-prepare")
        assert resp.status_code == 200


# ── Section 19: inline_save for requisition ──────────────────────────


class TestRequisitionInlineSave:
    """Covers lines 1705, 1731, 1746-1747 (inline save for req fields)."""

    def test_inline_save_urgency(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        resp = client.patch(
            f"/v2/partials/requisitions/{req.id}/inline",
            data={"field": "urgency", "value": "hot", "context": "row"},
        )
        assert resp.status_code == 200

    def test_inline_save_deadline(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        resp = client.patch(
            f"/v2/partials/requisitions/{req.id}/inline",
            data={"field": "deadline", "value": "2026-06-30", "context": "row"},
        )
        assert resp.status_code == 200

    def test_inline_save_owner(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        resp = client.patch(
            f"/v2/partials/requisitions/{req.id}/inline",
            data={"field": "owner", "value": str(test_user.id), "context": "row"},
        )
        assert resp.status_code == 200

    def test_inline_save_name(self, client: TestClient, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        resp = client.patch(
            f"/v2/partials/requisitions/{req.id}/inline",
            data={"field": "name", "value": "Renamed Req", "context": "header"},
        )
        assert resp.status_code == 200

    def test_inline_save_not_found(self, client: TestClient):
        resp = client.patch(
            "/v2/partials/requisitions/999999/inline",
            data={"field": "name", "value": "test"},
        )
        assert resp.status_code == 404
