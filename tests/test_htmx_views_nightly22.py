"""tests/test_htmx_views_nightly22.py — Happy-path coverage for uncovered routes.

Targets (by missing line ranges):
  - add_sightings_from_search (3314-3386)  POST JSON
  - lead_status_update (6539-6607)         POST form
  - lead_feedback (6624-6639)              POST form
  - bulk_archive / bulk_unarchive (9867-9929)  POST JSON
  - log_phone_call (5410-5444)             POST form
  - edit_vendor (3854-3876)                POST form
  - add_vendor_review (4004-4020)          POST form
  - log_activity (2385-2404)               POST form
  - review_response happy paths (2803-2815) POST form
  - update_response_status (5538-5547)     PATCH form
  - edit_company (5038-5057)               POST form
  - edit_site (5074-5083)                  POST form
  - create_quote_from_offers happy path (1916-1971) POST form
  - save_parsed_offers with data (1530-1587) POST form
  - add_offer manual entry (2040-2084)     POST form
  - add_offers_to_draft_quote (7683-7724)  POST JSON
  - buy_plan_cancel (6175-6197)            POST form
  - bulk_action activate/assign (1633-1665) POST form
  - requisitions_bulk_action archive/activate/assign

Called by: pytest autodiscovery
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

import os

os.environ["TESTING"] = "1"

import json
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import BuyPlanStatus, OfferStatus, QuoteStatus, SOVerificationStatus
from app.models import Company, CustomerSite, Requisition, User, VendorCard
from app.models.offers import Offer, VendorResponse
from app.models.quotes import Quote, QuoteLine
from app.models.sourcing_lead import SourcingLead


# ── Helpers ───────────────────────────────────────────────────────────────


def _make_req(db: Session, user: User, name: str | None = None) -> Requisition:
    from app.models import Requirement

    req = Requisition(
        name=name or f"REQ-{uuid.uuid4().hex[:6]}",
        customer_name="TestCo",
        status="active",
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()
    item = Requirement(
        requisition_id=req.id,
        primary_mpn="BC547",
        target_qty=100,
        created_at=datetime.now(timezone.utc),
    )
    db.add(item)
    db.commit()
    db.refresh(req)
    return req


def _make_offer(db: Session, req: Requisition, user: User, mpn: str = "BC547") -> Offer:
    o = Offer(
        requisition_id=req.id,
        vendor_name="TestVendor",
        mpn=mpn,
        unit_price=2.50,
        qty_available=500,
        status=OfferStatus.ACTIVE,
        source="manual",
        entered_by_id=user.id,
    )
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


def _make_quote(db: Session, req: Requisition, user: User) -> Quote:
    q = Quote(
        requisition_id=req.id,
        quote_number=f"Q-{uuid.uuid4().hex[:8]}",
        status=QuoteStatus.DRAFT,
        created_by_id=user.id,
    )
    db.add(q)
    db.commit()
    db.refresh(q)
    return q


def _make_lead(db: Session, req: Requisition) -> SourcingLead:
    from app.models import Requirement

    req_item = db.query(Requirement).filter(Requirement.requisition_id == req.id).first()
    lead = SourcingLead(
        lead_id=uuid.uuid4().hex,
        requirement_id=req_item.id,
        requisition_id=req.id,
        part_number_requested="BC547",
        part_number_matched="BC547",
        vendor_name="TestVendor",
        vendor_name_normalized="testvendor",
        primary_source_type="broker",
        primary_source_name="BrokerBin",
        confidence_score=70.0,
        confidence_band="medium",
        reason_summary="Test lead",
        risk_flags=[],
        evidence_count=1,
        corroborated=False,
        vendor_safety_flags=[],
        buyer_status="new",
    )
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return lead


def _make_vendor_response(db: Session, req: Requisition) -> VendorResponse:
    vr = VendorResponse(
        requisition_id=req.id,
        vendor_name="TestVendor",
        vendor_email="test@vendor.com",
        subject="RE: RFQ",
        body="We have 1000 units available",
        status="new",
        received_at=datetime.now(timezone.utc),
        message_id=f"msg-{uuid.uuid4().hex}",
    )
    db.add(vr)
    db.commit()
    db.refresh(vr)
    return vr


def _make_buy_plan(db: Session, req: Requisition) -> object:
    from app.models.buy_plan import BuyPlan

    q = Quote(
        requisition_id=req.id,
        quote_number=f"Q-{uuid.uuid4().hex[:8]}",
        status="draft",
    )
    db.add(q)
    db.flush()
    bp = BuyPlan(
        quote_id=q.id,
        requisition_id=req.id,
        status=BuyPlanStatus.DRAFT,
        so_status=SOVerificationStatus.PENDING,
    )
    db.add(bp)
    db.commit()
    db.refresh(bp)
    return bp


# ── Add Sightings From Search ─────────────────────────────────────────────


class TestAddSightingsFromSearch:
    def test_add_sightings_success(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        """Happy path: JSON with requisition_id, mpn, and items."""
        payload = {
            "requisition_id": test_requisition.id,
            "mpn": "LM741",
            "items": [
                {
                    "vendor_name": "TestBroker",
                    "mpn_matched": "LM741",
                    "manufacturer": "TI",
                    "qty_available": 1000,
                    "unit_price": 0.85,
                    "currency": "USD",
                    "source_type": "broker",
                    "is_authorized": False,
                    "confidence": 0.7,
                    "score": 65,
                    "vendor_url": "https://example.com",
                },
            ],
        }
        resp = client.post(
            "/v2/partials/search/add-to-requisition",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200

    def test_add_sightings_creates_requirement_if_missing(
        self, client: TestClient, db_session: Session, test_requisition: Requisition
    ):
        """MPN not already a requirement — auto-creates Requirement."""
        payload = {
            "requisition_id": test_requisition.id,
            "mpn": "NE555_NEW",
            "items": [
                {
                    "vendor_name": "TestBroker2",
                    "qty_available": 500,
                    "unit_price": 0.30,
                    "score": 50,
                },
            ],
        }
        resp = client.post(
            "/v2/partials/search/add-to-requisition",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200

    def test_add_sightings_missing_fields(self, client: TestClient):
        resp = client.post(
            "/v2/partials/search/add-to-requisition",
            content=json.dumps({"requisition_id": 99999}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_add_sightings_req_not_found(self, client: TestClient):
        payload = {
            "requisition_id": 99999,
            "mpn": "LM741",
            "items": [{"vendor_name": "X"}],
        }
        resp = client.post(
            "/v2/partials/search/add-to-requisition",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 404


# ── Lead Status Update ────────────────────────────────────────────────────


class TestLeadStatusUpdate:
    def test_update_status_has_stock(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_req(db_session, test_user)
        lead = _make_lead(db_session, req)
        resp = client.post(
            f"/v2/partials/sourcing/leads/{lead.id}/status",
            data={"status": "has_stock", "note": "Confirmed 1000 units"},
        )
        assert resp.status_code == 200

    def test_update_status_no_stock(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_req(db_session, test_user)
        lead = _make_lead(db_session, req)
        resp = client.post(
            f"/v2/partials/sourcing/leads/{lead.id}/status",
            data={"status": "no_stock"},
        )
        assert resp.status_code == 200

    def test_update_status_contacted(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_req(db_session, test_user)
        lead = _make_lead(db_session, req)
        resp = client.post(
            f"/v2/partials/sourcing/leads/{lead.id}/status",
            data={"status": "contacted"},
        )
        assert resp.status_code == 200

    def test_update_status_invalid(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_req(db_session, test_user)
        lead = _make_lead(db_session, req)
        resp = client.post(
            f"/v2/partials/sourcing/leads/{lead.id}/status",
            data={"status": "invalid_status"},
        )
        assert resp.status_code == 400

    def test_update_status_lead_not_found(self, client: TestClient):
        resp = client.post(
            "/v2/partials/sourcing/leads/99999/status",
            data={"status": "contacted"},
        )
        assert resp.status_code == 404

    def test_update_status_with_hx_target_row(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """When HX-Target starts with 'lead-row-', returns lead_row template."""
        req = _make_req(db_session, test_user)
        lead = _make_lead(db_session, req)
        resp = client.post(
            f"/v2/partials/sourcing/leads/{lead.id}/status",
            data={"status": "replied"},
            headers={"HX-Target": f"lead-row-{lead.id}"},
        )
        assert resp.status_code == 200


# ── Lead Feedback ─────────────────────────────────────────────────────────


class TestLeadFeedback:
    def test_feedback_success(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_req(db_session, test_user)
        lead = _make_lead(db_session, req)
        resp = client.post(
            f"/v2/partials/sourcing/leads/{lead.id}/feedback",
            data={"note": "Called but no answer", "reason_code": "no_answer"},
        )
        assert resp.status_code == 200

    def test_feedback_lead_not_found(self, client: TestClient):
        resp = client.post(
            "/v2/partials/sourcing/leads/99999/feedback",
            data={"note": "test"},
        )
        assert resp.status_code == 404


# ── Bulk Archive / Unarchive ──────────────────────────────────────────────


class TestBulkArchive:
    def test_bulk_archive_requirement_ids(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        from app.models import Requirement

        req = _make_req(db_session, test_user)
        r = db_session.query(Requirement).filter(Requirement.requisition_id == req.id).first()
        resp = client.post(
            "/v2/partials/parts/bulk-archive",
            content=json.dumps({"requirement_ids": [r.id], "requisition_ids": []}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200

    def test_bulk_archive_requisition_ids(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        req = _make_req(db_session, test_user)
        resp = client.post(
            "/v2/partials/parts/bulk-archive",
            content=json.dumps({"requirement_ids": [], "requisition_ids": [req.id]}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200

    def test_bulk_archive_empty(self, client: TestClient):
        resp = client.post(
            "/v2/partials/parts/bulk-archive",
            content=json.dumps({"requirement_ids": [], "requisition_ids": []}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200

    def test_bulk_unarchive_requirement_ids(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        from app.models import Requirement

        req = _make_req(db_session, test_user)
        r = db_session.query(Requirement).filter(Requirement.requisition_id == req.id).first()
        resp = client.post(
            "/v2/partials/parts/bulk-unarchive",
            content=json.dumps({"requirement_ids": [r.id], "requisition_ids": []}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200

    def test_bulk_unarchive_requisition_cascade(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        req = _make_req(db_session, test_user)
        resp = client.post(
            "/v2/partials/parts/bulk-unarchive",
            content=json.dumps({"requirement_ids": [], "requisition_ids": [req.id]}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200


# ── Log Phone Call ────────────────────────────────────────────────────────


class TestLogPhoneCall:
    def test_log_phone_success(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        req = _make_req(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/log-phone",
            data={"vendor_name": "DigiKey", "vendor_phone": "+1-952-555-0100", "notes": "Spoke with rep"},
        )
        assert resp.status_code == 200

    def test_log_phone_missing_fields(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        req = _make_req(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/log-phone",
            data={"vendor_name": "DigiKey"},
        )
        assert resp.status_code == 400


# ── Edit Vendor ───────────────────────────────────────────────────────────


class TestEditVendor:
    def test_edit_vendor_success(
        self, client: TestClient, db_session: Session, test_vendor_card: VendorCard
    ):
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/edit",
            data={"display_name": "Arrow Electronics Updated", "website": "https://arrow.com"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert test_vendor_card.display_name == "Arrow Electronics Updated"

    def test_edit_vendor_emails_and_phones(
        self, client: TestClient, test_vendor_card: VendorCard
    ):
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/edit",
            data={"emails": "a@arrow.com, b@arrow.com", "phones": "+1-555-0101"},
        )
        assert resp.status_code == 200

    def test_edit_vendor_not_found(self, client: TestClient):
        resp = client.post(
            "/v2/partials/vendors/99999/edit",
            data={"display_name": "X"},
        )
        assert resp.status_code == 404


# ── Add Vendor Review ─────────────────────────────────────────────────────


class TestAddVendorReview:
    def test_add_review_success(
        self, client: TestClient, test_vendor_card: VendorCard
    ):
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/reviews",
            data={"rating": "4", "comment": "Good supplier, fast delivery"},
        )
        assert resp.status_code == 200

    def test_add_review_invalid_rating(
        self, client: TestClient, test_vendor_card: VendorCard
    ):
        """Invalid rating falls back to 3."""
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/reviews",
            data={"rating": "not-a-number"},
        )
        assert resp.status_code == 200

    def test_add_review_vendor_not_found(self, client: TestClient):
        resp = client.post(
            "/v2/partials/vendors/99999/reviews",
            data={"rating": "5"},
        )
        assert resp.status_code == 404


# ── Log Activity ──────────────────────────────────────────────────────────


class TestLogActivity:
    def test_log_activity_note(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        req = _make_req(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/log-activity",
            data={
                "activity_type": "note",
                "vendor_name": "Arrow",
                "notes": "Sent price inquiry",
            },
        )
        assert resp.status_code == 200

    def test_log_activity_phone_call(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        req = _make_req(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/log-activity",
            data={
                "activity_type": "phone_call",
                "vendor_name": "Mouser",
                "contact_phone": "+1-800-555-0199",
            },
        )
        assert resp.status_code == 200


# ── Review Response ───────────────────────────────────────────────────────


class TestReviewResponse:
    def test_review_as_reviewed(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        req = _make_req(db_session, test_user)
        vr = _make_vendor_response(db_session, req)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/responses/{vr.id}/review",
            data={"status": "reviewed"},
        )
        assert resp.status_code == 200
        db_session.refresh(vr)
        assert vr.status == "reviewed"

    def test_review_as_rejected(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        req = _make_req(db_session, test_user)
        vr = _make_vendor_response(db_session, req)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/responses/{vr.id}/review",
            data={"status": "rejected"},
        )
        assert resp.status_code == 200


# ── Update Response Status (PATCH) ───────────────────────────────────────


class TestUpdateResponseStatus:
    def test_patch_status_reviewed(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        req = _make_req(db_session, test_user)
        vr = _make_vendor_response(db_session, req)
        resp = client.patch(
            f"/v2/partials/requisitions/{req.id}/responses/{vr.id}/status",
            data={"status": "reviewed"},
        )
        assert resp.status_code == 200

    def test_patch_status_flagged(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        req = _make_req(db_session, test_user)
        vr = _make_vendor_response(db_session, req)
        resp = client.patch(
            f"/v2/partials/requisitions/{req.id}/responses/{vr.id}/status",
            data={"status": "flagged"},
        )
        assert resp.status_code == 200

    def test_patch_status_invalid(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        req = _make_req(db_session, test_user)
        vr = _make_vendor_response(db_session, req)
        resp = client.patch(
            f"/v2/partials/requisitions/{req.id}/responses/{vr.id}/status",
            data={"status": "bad_status"},
        )
        assert resp.status_code == 400


# ── Edit Company ──────────────────────────────────────────────────────────


class TestEditCompany:
    def test_edit_company_success(
        self, client: TestClient, db_session: Session, test_company: Company
    ):
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/edit",
            data={"name": "Acme Corp Updated", "website": "https://acme-corp.com"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_company)
        assert test_company.name == "Acme Corp Updated"

    def test_edit_company_not_found(self, client: TestClient):
        resp = client.post(
            "/v2/partials/customers/99999/edit",
            data={"name": "X"},
        )
        assert resp.status_code == 404


# ── Edit Site ─────────────────────────────────────────────────────────────


class TestEditSite:
    def test_edit_site_success(
        self, client: TestClient, db_session: Session, test_company: Company
    ):
        site = CustomerSite(
            company_id=test_company.id,
            site_name="Main HQ",
            site_type="headquarters",
            is_active=True,
        )
        db_session.add(site)
        db_session.commit()
        db_session.refresh(site)

        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/{site.id}/edit",
            data={"site_name": "New HQ Name", "city": "Boston", "country": "US"},
        )
        assert resp.status_code == 200

    def test_edit_site_not_found(self, client: TestClient, test_company: Company):
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/99999/edit",
            data={"site_name": "X"},
        )
        assert resp.status_code == 404


# ── Create Quote From Offers (happy path) ────────────────────────────────


class TestCreateQuoteFromOffersHappy:
    def test_create_quote_success(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        req = _make_req(db_session, test_user)
        offer = _make_offer(db_session, req, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/create-quote",
            data={"offer_ids": str(offer.id)},
        )
        assert resp.status_code == 200

    def test_create_quote_multiple_offers(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        req = _make_req(db_session, test_user)
        o1 = _make_offer(db_session, req, test_user, mpn="LM741")
        o2 = _make_offer(db_session, req, test_user, mpn="NE555")
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/create-quote",
            data={"offer_ids": [str(o1.id), str(o2.id)]},
        )
        assert resp.status_code == 200

    def test_create_quote_offers_not_found(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """offer_ids exist but don't belong to this requisition → 404."""
        req = _make_req(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/create-quote",
            data={"offer_ids": "99999"},
        )
        assert resp.status_code == 404


# ── Save Parsed Offers ────────────────────────────────────────────────────


class TestSaveParsedOffersHappy:
    def test_save_offers_with_matching_mpn(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """MPN matches existing requirement → offer saved with requirement_id."""
        req = _make_req(db_session, test_user)
        # _make_req creates a requirement for BC547
        form_data = (
            b"vendor_name=TestVendor"
            b"&offers%5B0%5D.mpn=BC547"
            b"&offers%5B0%5D.qty_available=1000"
            b"&offers%5B0%5D.unit_price=0.50"
            b"&offers%5B0%5D.condition=new"
        )
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/save-parsed-offers",
            content=form_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 200

    def test_save_offers_new_vendor_card(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """Vendor not in VendorCard table — auto-creates VendorCard."""
        req = _make_req(db_session, test_user)
        form_data = (
            b"vendor_name=BrandNewVendorXYZ123"
            b"&offers%5B0%5D.mpn=BC547"
            b"&offers%5B0%5D.qty_available=500"
        )
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/save-parsed-offers",
            content=form_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 200

    def test_save_offers_empty_returns_warning(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """No offers in form → 200 with warning message."""
        req = _make_req(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/save-parsed-offers",
            data={"vendor_name": "TestVendor"},
        )
        assert resp.status_code == 200
        assert b"No offers" in resp.content


# ── Add Offer (manual entry) ──────────────────────────────────────────────


class TestAddOfferManual:
    def test_add_offer_success(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        req = _make_req(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/add-offer",
            data={
                "vendor_name": "Manual Vendor",
                "mpn": "LM317T",
                "qty_available": "200",
                "unit_price": "1.25",
                "condition": "new",
                "lead_time": "2 weeks",
            },
        )
        assert resp.status_code == 200

    def test_add_offer_missing_vendor(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        req = _make_req(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/add-offer",
            data={"mpn": "LM317T"},
        )
        assert resp.status_code == 400

    def test_add_offer_missing_mpn(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        req = _make_req(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/add-offer",
            data={"vendor_name": "ManualVendor"},
        )
        assert resp.status_code == 400


# ── Add Offers to Draft Quote ─────────────────────────────────────────────


class TestAddOffersToDraftQuote:
    def test_add_offers_success(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        req = _make_req(db_session, test_user)
        offer = _make_offer(db_session, req, test_user)
        quote = _make_quote(db_session, req, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/add-offers-to-quote",
            content=json.dumps({"offer_ids": [offer.id], "quote_id": quote.id}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200

    def test_add_offers_duplicate_skipped(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """Adding the same offer twice — second add is skipped."""
        req = _make_req(db_session, test_user)
        offer = _make_offer(db_session, req, test_user)
        quote = _make_quote(db_session, req, test_user)
        # Add once
        existing_line = QuoteLine(
            quote_id=quote.id,
            offer_id=offer.id,
            mpn=offer.mpn,
            manufacturer="",
            qty=1,
            cost_price=2.50,
            sell_price=2.50,
            margin_pct=0.0,
        )
        db_session.add(existing_line)
        db_session.commit()
        # Try to add again
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/add-offers-to-quote",
            content=json.dumps({"offer_ids": [offer.id], "quote_id": quote.id}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200

    def test_add_offers_missing_params(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        req = _make_req(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/add-offers-to-quote",
            content=json.dumps({}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400


# ── Buy Plan Cancel ───────────────────────────────────────────────────────


class TestBuyPlanCancel:
    def test_cancel_draft_plan(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        req = _make_req(db_session, test_user)
        bp = _make_buy_plan(db_session, req)
        resp = client.post(
            f"/v2/partials/buy-plans/{bp.id}/cancel",
            data={"reason": "Customer cancelled order"},
        )
        assert resp.status_code == 200

    def test_cancel_not_found(self, client: TestClient):
        resp = client.post(
            "/v2/partials/buy-plans/99999/cancel",
            data={"reason": "test"},
        )
        assert resp.status_code == 404


# ── Requisitions Bulk Action ──────────────────────────────────────────────


class TestRequisitionsBulkAction:
    def test_bulk_archive(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        req = _make_req(db_session, test_user)
        resp = client.post(
            "/v2/partials/requisitions/bulk/archive",
            data={"ids": str(req.id)},
        )
        assert resp.status_code == 200

    def test_bulk_activate(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        req = _make_req(db_session, test_user)
        resp = client.post(
            "/v2/partials/requisitions/bulk/activate",
            data={"ids": str(req.id)},
        )
        assert resp.status_code == 200

    def test_bulk_action_invalid_ids(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/bulk/archive",
            data={"ids": "not-an-int"},
        )
        assert resp.status_code == 400

    def test_bulk_action_no_ids(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/bulk/archive",
            data={},
        )
        assert resp.status_code == 400
