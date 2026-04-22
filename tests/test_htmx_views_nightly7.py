"""test_htmx_views_nightly7.py — Coverage boost for htmx_views.py (nightly batch 7).

Targets uncovered sections:
- rfq_send (POST /v2/partials/requisitions/{req_id}/rfq-send)
- edit_offer (POST /v2/partials/requisitions/{req_id}/offers/{offer_id}/edit)
- send_follow_up_htmx (POST /v2/partials/follow-ups/{contact_id}/send)
- save_parsed_offers (POST /v2/partials/requisitions/{req_id}/save-parsed-offers)
- create_company (POST /v2/partials/customers/create)
- lead_status_update (POST /v2/partials/sourcing/leads/{lead_id}/status)
- buy_plan_submit_partial (POST /v2/partials/buy-plans/{plan_id}/submit)
- buy_plan_verify_so_partial (POST /v2/partials/buy-plans/{plan_id}/verify-so)
- buy_plan_verify_po_partial (POST /v2/partials/buy-plans/{plan_id}/lines/{line_id}/verify-po)
- buy_plan_flag_issue_partial (POST /v2/partials/buy-plans/{plan_id}/lines/{line_id}/issue)
- proactive_draft_for_prepare (POST /v2/partials/proactive/draft)
- proactive_send_offer (POST /v2/proactive/send)
- bulk_archive (POST /v2/partials/parts/bulk-archive)

Called by: pytest
Depends on: conftest.py (client, db_session, test_user, test_requisition, test_vendor_card)
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.constants import BuyPlanStatus, ContactStatus, OfferStatus, RequisitionStatus, SourcingStatus
from app.models import Company, CustomerSite, Offer, Requirement, Requisition, User
from app.models.buy_plan import BuyPlan, BuyPlanLine
from app.models.offers import Contact as RfqContact
from app.models.quotes import Quote
from app.models.sourcing_lead import SourcingLead

# ── Helper factories ──────────────────────────────────────────────────


def _make_requisition(db: Session, user: User, name: str = "REQ-N7-001") -> Requisition:
    req = Requisition(
        name=name,
        customer_name="Test Customer",
        status=RequisitionStatus.ACTIVE,
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()
    item = Requirement(
        requisition_id=req.id,
        primary_mpn="LM317T",
        target_qty=100,
        created_at=datetime.now(timezone.utc),
    )
    db.add(item)
    db.commit()
    db.refresh(req)
    return req


def _make_offer(
    db: Session,
    req: Requisition,
    user: User,
    mpn: str = "LM317T",
    vendor_name: str = "Arrow Electronics",
) -> Offer:
    offer = Offer(
        requisition_id=req.id,
        vendor_name=vendor_name,
        vendor_name_normalized=vendor_name.lower(),
        mpn=mpn,
        qty_available=500,
        unit_price=0.75,
        entered_by_id=user.id,
        status=OfferStatus.ACTIVE,
        created_at=datetime.now(timezone.utc),
    )
    db.add(offer)
    db.commit()
    db.refresh(offer)
    return offer


def _make_rfq_contact(db: Session, req: Requisition, user: User) -> RfqContact:
    contact = RfqContact(
        requisition_id=req.id,
        user_id=user.id,
        contact_type="email",
        vendor_name="Arrow Electronics",
        vendor_name_normalized="arrow electronics",
        vendor_contact="sales@arrow.com",
        subject="RFQ for LM317T",
        status=ContactStatus.SENT,
        created_at=datetime.now(timezone.utc),
    )
    db.add(contact)
    db.commit()
    db.refresh(contact)
    return contact


def _make_sourcing_lead(db: Session, req: Requisition, user: User) -> SourcingLead:
    lead = SourcingLead(
        lead_id=f"lead-{req.id}-arrow-lm317t",
        requirement_id=req.requirements[0].id,
        requisition_id=req.id,
        part_number_requested="LM317T",
        part_number_matched="LM317T",
        match_type="exact",
        vendor_name="Arrow Electronics",
        vendor_name_normalized="arrow electronics",
        primary_source_type="broker",
        primary_source_name="BrokerBin",
        confidence_score=75.0,
        confidence_band="high",
        buyer_status="new",
        reason_summary="Confident match",
        created_at=datetime.now(timezone.utc),
    )
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return lead


def _make_quote(db: Session, req: Requisition, user: User) -> Quote:
    quote = Quote(
        requisition_id=req.id,
        quote_number=f"Q-N7-{req.id}",
        status="draft",
        line_items=[],
        created_by_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(quote)
    db.commit()
    db.refresh(quote)
    return quote


def _make_buy_plan(db: Session, req: Requisition, quote: Quote) -> BuyPlan:
    plan = BuyPlan(
        quote_id=quote.id,
        requisition_id=req.id,
        status=BuyPlanStatus.DRAFT,
        ai_flags=[],
        created_at=datetime.now(timezone.utc),
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def _make_buy_plan_line(db: Session, plan: BuyPlan, req: Requisition) -> BuyPlanLine:
    line = BuyPlanLine(
        buy_plan_id=plan.id,
        requirement_id=req.requirements[0].id if req.requirements else None,
        quantity=100,
        unit_cost=0.50,
        unit_sell=0.75,
        status="awaiting_po",
        created_at=datetime.now(timezone.utc),
    )
    db.add(line)
    db.commit()
    db.refresh(line)
    return line


# ── Section 1: rfq_send ────────────────────────────────────────────────


class TestRfqSend:
    """Tests for POST /v2/partials/requisitions/{req_id}/rfq-send."""

    def test_no_vendors_returns_400(self, client, db_session, test_user):
        req = _make_requisition(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/rfq-send",
            data={},
        )
        assert resp.status_code == 400

    def test_invalid_req_returns_404(self, client, db_session):
        resp = client.post(
            "/v2/partials/requisitions/999999/rfq-send",
            data={"vendor_names": ["Arrow"], "vendor_emails": ["sales@arrow.com"]},
        )
        assert resp.status_code == 404

    def test_test_mode_creates_contacts_and_returns_200(self, client, db_session, test_user):
        req = _make_requisition(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/rfq-send",
            data={
                "vendor_names": ["Arrow Electronics", "DigiKey"],
                "vendor_emails": ["sales@arrow.com", "rfq@digikey.com"],
                "subject": "RFQ for LM317T",
                "body": "Please provide pricing.",
                "parts_summary": "LM317T x100",
            },
        )
        assert resp.status_code == 200
        # In TESTING=1 mode, contacts should be created in DB
        from app.models.offers import Contact as C

        contacts = db_session.query(C).filter(C.requisition_id == req.id).all()
        assert len(contacts) == 2

    def test_vendor_with_no_email_is_skipped(self, client, db_session, test_user):
        req = _make_requisition(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/rfq-send",
            data={
                "vendor_names": ["Arrow Electronics", "Unknown Vendor"],
                "vendor_emails": ["sales@arrow.com", ""],
                "subject": "Test RFQ",
            },
        )
        assert resp.status_code == 200
        from app.models.offers import Contact as C

        contacts = db_session.query(C).filter(C.requisition_id == req.id).all()
        # Only Arrow (with email) should be created
        assert len(contacts) == 1


# ── Section 2: edit_offer ─────────────────────────────────────────────


class TestEditOffer:
    """Tests for POST /v2/partials/requisitions/{req_id}/offers/{offer_id}/edit."""

    def test_offer_not_found_returns_404(self, client, db_session, test_user):
        req = _make_requisition(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/offers/999999/edit",
            data={"vendor_name": "NewVendor"},
        )
        assert resp.status_code == 404

    def test_edit_vendor_name_updates_and_returns_200(self, client, db_session, test_user):
        req = _make_requisition(db_session, test_user)
        offer = _make_offer(db_session, req, test_user)

        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/offers/{offer.id}/edit",
            data={"vendor_name": "Updated Vendor", "qty_available": "250", "unit_price": "1.50"},
        )
        assert resp.status_code == 200
        db_session.refresh(offer)
        assert offer.vendor_name == "Updated Vendor"
        assert offer.qty_available == 250

    def test_edit_invalid_qty_is_skipped(self, client, db_session, test_user):
        req = _make_requisition(db_session, test_user)
        offer = _make_offer(db_session, req, test_user, mpn="LM317T")
        original_qty = offer.qty_available

        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/offers/{offer.id}/edit",
            data={"qty_available": "not-a-number"},
        )
        assert resp.status_code == 200
        db_session.refresh(offer)
        # Invalid qty should be skipped
        assert offer.qty_available == original_qty

    def test_edit_unit_price_as_float(self, client, db_session, test_user):
        req = _make_requisition(db_session, test_user)
        offer = _make_offer(db_session, req, test_user)

        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/offers/{offer.id}/edit",
            data={"unit_price": "2.99"},
        )
        assert resp.status_code == 200
        db_session.refresh(offer)
        assert float(offer.unit_price) == pytest.approx(2.99, abs=0.01)

    def test_edit_with_requirement_id(self, client, db_session, test_user):
        req = _make_requisition(db_session, test_user)
        offer = _make_offer(db_session, req, test_user)
        req_item = req.requirements[0]

        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/offers/{offer.id}/edit",
            data={"requirement_id": str(req_item.id)},
        )
        assert resp.status_code == 200
        db_session.refresh(offer)
        assert offer.requirement_id == req_item.id


# ── Section 3: send_follow_up_htmx ───────────────────────────────────


class TestSendFollowUp:
    """Tests for POST /v2/partials/follow-ups/{contact_id}/send."""

    def test_contact_not_found_returns_404(self, client, db_session):
        resp = client.post("/v2/partials/follow-ups/999999/send", data={})
        assert resp.status_code == 404

    def test_test_mode_updates_status_and_returns_200(self, client, db_session, test_user):
        req = _make_requisition(db_session, test_user)
        contact = _make_rfq_contact(db_session, req, test_user)
        # Set initial status to pending
        contact.status = ContactStatus.PENDING
        db_session.commit()

        resp = client.post(
            f"/v2/partials/follow-ups/{contact.id}/send",
            data={"body": "Following up on our inquiry."},
        )
        assert resp.status_code == 200
        db_session.refresh(contact)
        assert contact.status == ContactStatus.SENT

    def test_test_mode_with_empty_body(self, client, db_session, test_user):
        req = _make_requisition(db_session, test_user)
        contact = _make_rfq_contact(db_session, req, test_user)

        resp = client.post(f"/v2/partials/follow-ups/{contact.id}/send", data={})
        assert resp.status_code == 200


# ── Section 4: save_parsed_offers ────────────────────────────────────


class TestSaveParsedOffers:
    """Tests for POST /v2/partials/requisitions/{req_id}/save-parsed-offers."""

    def test_req_not_found_returns_404(self, client, db_session):
        resp = client.post(
            "/v2/partials/requisitions/999999/save-parsed-offers",
            data={"vendor_name": "TestVendor"},
        )
        assert resp.status_code == 404

    def test_no_offers_data_returns_empty_message(self, client, db_session, test_user):
        req = _make_requisition(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/save-parsed-offers",
            data={"vendor_name": "TestVendor"},
        )
        assert resp.status_code == 200
        assert "No offers to save" in resp.text

    def test_valid_offers_saved_to_db(self, client, db_session, test_user):
        req = _make_requisition(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/save-parsed-offers",
            data={
                "vendor_name": "Arrow Electronics",
                "offers[0].mpn": "LM317T",
                "offers[0].qty_available": "500",
                "offers[0].unit_price": "0.85",
                "offers[0].condition": "new",
                "offers[0].lead_time": "2 weeks",
            },
        )
        assert resp.status_code == 200
        # Verify offer was created
        offers = db_session.query(Offer).filter(Offer.requisition_id == req.id).all()
        assert len(offers) >= 1
        assert any(o.mpn == "LM317T" for o in offers)

    def test_offer_with_missing_mpn_is_skipped(self, client, db_session, test_user):
        req = _make_requisition(db_session, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/save-parsed-offers",
            data={
                "vendor_name": "Arrow Electronics",
                "offers[0].mpn": "",
                "offers[0].qty_available": "100",
            },
        )
        assert resp.status_code == 200
        offers = db_session.query(Offer).filter(Offer.requisition_id == req.id).all()
        assert len(offers) == 0


# ── Section 5: create_company ─────────────────────────────────────────


class TestCreateCompany:
    """Tests for POST /v2/partials/customers/create."""

    def test_missing_name_returns_400(self, client, db_session):
        resp = client.post("/v2/partials/customers/create", data={"name": ""})
        assert resp.status_code == 400

    def test_duplicate_name_returns_409(self, client, db_session, test_user):
        # Create existing company
        co = Company(name="Existing Corp", is_active=True, created_at=datetime.now(timezone.utc))
        db_session.add(co)
        db_session.commit()

        resp = client.post("/v2/partials/customers/create", data={"name": "Existing Corp"})
        assert resp.status_code == 409

    def test_success_creates_company_and_site(self, client, db_session):
        resp = client.post(
            "/v2/partials/customers/create",
            data={"name": "New Test Company", "website": "https://newco.com", "industry": "Electronics"},
        )
        assert resp.status_code == 200
        co = db_session.query(Company).filter(Company.name == "New Test Company").first()
        assert co is not None
        sites = db_session.query(CustomerSite).filter(CustomerSite.company_id == co.id).all()
        assert len(sites) == 1
        assert sites[0].site_name == "HQ"

    def test_success_with_owner_id(self, client, db_session, test_user):
        resp = client.post(
            "/v2/partials/customers/create",
            data={"name": "Owned Company", "owner_id": str(test_user.id)},
        )
        assert resp.status_code == 200
        co = db_session.query(Company).filter(Company.name == "Owned Company").first()
        assert co is not None
        assert co.account_owner_id == test_user.id


# ── Section 6: lead_status_update ────────────────────────────────────


class TestLeadStatusUpdate:
    """Tests for POST /v2/partials/sourcing/leads/{lead_id}/status."""

    def test_lead_not_found_returns_404(self, client, db_session):
        resp = client.post(
            "/v2/partials/sourcing/leads/999999/status",
            data={"status": "contacted"},
        )
        assert resp.status_code == 404

    def test_invalid_status_returns_400(self, client, db_session, test_user):
        req = _make_requisition(db_session, test_user)
        lead = _make_sourcing_lead(db_session, req, test_user)
        resp = client.post(
            f"/v2/partials/sourcing/leads/{lead.id}/status",
            data={"status": "invalid_status_xyz"},
        )
        assert resp.status_code == 400

    def test_valid_status_update_returns_200(self, client, db_session, test_user):
        req = _make_requisition(db_session, test_user)
        lead = _make_sourcing_lead(db_session, req, test_user)
        resp = client.post(
            f"/v2/partials/sourcing/leads/{lead.id}/status",
            data={"status": "contacted", "note": "Called vendor, awaiting reply."},
        )
        assert resp.status_code == 200
        db_session.refresh(lead)
        assert lead.buyer_status == "contacted"

    def test_has_stock_status_boosts_confidence(self, client, db_session, test_user):
        req = _make_requisition(db_session, test_user)
        lead = _make_sourcing_lead(db_session, req, test_user)
        original_score = lead.confidence_score

        resp = client.post(
            f"/v2/partials/sourcing/leads/{lead.id}/status",
            data={"status": "has_stock"},
        )
        assert resp.status_code == 200
        db_session.refresh(lead)
        assert lead.confidence_score >= original_score

    def test_status_update_with_hx_target_lead_row(self, client, db_session, test_user):
        req = _make_requisition(db_session, test_user)
        lead = _make_sourcing_lead(db_session, req, test_user)
        resp = client.post(
            f"/v2/partials/sourcing/leads/{lead.id}/status",
            data={"status": "replied"},
            headers={"HX-Target": f"lead-row-{lead.id}"},
        )
        assert resp.status_code == 200


# ── Section 7: buy_plan_submit_partial ───────────────────────────────


class TestBuyPlanSubmit:
    """Tests for POST /v2/partials/buy-plans/{plan_id}/submit."""

    def test_missing_so_returns_400(self, client, db_session, test_user):
        req = _make_requisition(db_session, test_user)
        quote = _make_quote(db_session, req, test_user)
        plan = _make_buy_plan(db_session, req, quote)

        resp = client.post(
            f"/v2/partials/buy-plans/{plan.id}/submit",
            data={"sales_order_number": ""},
        )
        assert resp.status_code == 400

    def test_submit_success_calls_workflow(self, client, db_session, test_user):
        req = _make_requisition(db_session, test_user)
        quote = _make_quote(db_session, req, test_user)
        plan = _make_buy_plan(db_session, req, quote)

        mock_plan = MagicMock()
        mock_plan.id = plan.id
        mock_plan.auto_approved = False

        with (
            patch("app.routers.htmx_views.buy_plan_detail_partial", new_callable=AsyncMock) as mock_detail,
            patch("app.services.buyplan_workflow.submit_buy_plan", return_value=mock_plan),
            patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock),
        ):
            from fastapi.responses import HTMLResponse

            mock_detail.return_value = HTMLResponse("<div>Plan submitted</div>")
            resp = client.post(
                f"/v2/partials/buy-plans/{plan.id}/submit",
                data={"sales_order_number": "SO-12345"},
            )
        assert resp.status_code == 200

    def test_submit_value_error_returns_400(self, client, db_session, test_user):
        req = _make_requisition(db_session, test_user)
        quote = _make_quote(db_session, req, test_user)
        plan = _make_buy_plan(db_session, req, quote)

        with patch("app.services.buyplan_workflow.submit_buy_plan", side_effect=ValueError("Plan not in draft")):
            resp = client.post(
                f"/v2/partials/buy-plans/{plan.id}/submit",
                data={"sales_order_number": "SO-12345"},
            )
        assert resp.status_code == 400


# ── Section 8: buy_plan_verify_so_partial ────────────────────────────


class TestBuyPlanVerifySo:
    """Tests for POST /v2/partials/buy-plans/{plan_id}/verify-so."""

    def test_verify_so_success(self, client, db_session, test_user):
        req = _make_requisition(db_session, test_user)
        quote = _make_quote(db_session, req, test_user)
        plan = _make_buy_plan(db_session, req, quote)

        mock_plan = MagicMock()
        mock_plan.id = plan.id

        with (
            patch("app.routers.htmx_views.buy_plan_detail_partial", new_callable=AsyncMock) as mock_detail,
            patch("app.services.buyplan_workflow.verify_so", return_value=mock_plan),
            patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock),
        ):
            from fastapi.responses import HTMLResponse

            mock_detail.return_value = HTMLResponse("<div>SO verified</div>")
            resp = client.post(
                f"/v2/partials/buy-plans/{plan.id}/verify-so",
                data={"action": "approve"},
            )
        assert resp.status_code == 200

    def test_verify_so_value_error_returns_400(self, client, db_session, test_user):
        req = _make_requisition(db_session, test_user)
        quote = _make_quote(db_session, req, test_user)
        plan = _make_buy_plan(db_session, req, quote)

        with patch("app.services.buyplan_workflow.verify_so", side_effect=ValueError("Invalid state")):
            resp = client.post(
                f"/v2/partials/buy-plans/{plan.id}/verify-so",
                data={"action": "approve"},
            )
        assert resp.status_code == 400

    def test_verify_so_reject_action(self, client, db_session, test_user):
        req = _make_requisition(db_session, test_user)
        quote = _make_quote(db_session, req, test_user)
        plan = _make_buy_plan(db_session, req, quote)

        mock_plan = MagicMock()
        mock_plan.id = plan.id

        with (
            patch("app.routers.htmx_views.buy_plan_detail_partial", new_callable=AsyncMock) as mock_detail,
            patch("app.services.buyplan_workflow.verify_so", return_value=mock_plan),
            patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock),
        ):
            from fastapi.responses import HTMLResponse

            mock_detail.return_value = HTMLResponse("<div>SO rejected</div>")
            resp = client.post(
                f"/v2/partials/buy-plans/{plan.id}/verify-so",
                data={"action": "reject", "rejection_note": "SO number invalid"},
            )
        assert resp.status_code == 200


# ── Section 9: buy_plan_verify_po_partial ────────────────────────────


class TestBuyPlanVerifyPo:
    """Tests for POST /v2/partials/buy-plans/{plan_id}/lines/{line_id}/verify-po."""

    def test_verify_po_success(self, client, db_session, test_user):
        req = _make_requisition(db_session, test_user)
        quote = _make_quote(db_session, req, test_user)
        plan = _make_buy_plan(db_session, req, quote)
        line = _make_buy_plan_line(db_session, plan, req)

        mock_completed_plan = MagicMock()
        mock_completed_plan.status = BuyPlanStatus.DRAFT  # Not completed

        with (
            patch("app.routers.htmx_views.buy_plan_detail_partial", new_callable=AsyncMock) as mock_detail,
            patch("app.services.buyplan_workflow.verify_po"),
            patch("app.services.buyplan_workflow.check_completion", return_value=mock_completed_plan),
            patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock),
        ):
            from fastapi.responses import HTMLResponse

            mock_detail.return_value = HTMLResponse("<div>PO verified</div>")
            resp = client.post(
                f"/v2/partials/buy-plans/{plan.id}/lines/{line.id}/verify-po",
                data={"action": "approve"},
            )
        assert resp.status_code == 200

    def test_verify_po_value_error_returns_400(self, client, db_session, test_user):
        req = _make_requisition(db_session, test_user)
        quote = _make_quote(db_session, req, test_user)
        plan = _make_buy_plan(db_session, req, quote)
        line = _make_buy_plan_line(db_session, plan, req)

        with patch("app.services.buyplan_workflow.verify_po", side_effect=ValueError("Line not found")):
            resp = client.post(
                f"/v2/partials/buy-plans/{plan.id}/lines/{line.id}/verify-po",
                data={"action": "approve"},
            )
        assert resp.status_code == 400


# ── Section 10: buy_plan_flag_issue_partial ───────────────────────────


class TestBuyPlanFlagIssue:
    """Tests for POST /v2/partials/buy-plans/{plan_id}/lines/{line_id}/issue."""

    def test_flag_issue_success(self, client, db_session, test_user):
        req = _make_requisition(db_session, test_user)
        quote = _make_quote(db_session, req, test_user)
        plan = _make_buy_plan(db_session, req, quote)
        line = _make_buy_plan_line(db_session, plan, req)

        with (
            patch("app.routers.htmx_views.buy_plan_detail_partial", new_callable=AsyncMock) as mock_detail,
            patch("app.services.buyplan_workflow.flag_line_issue"),
        ):
            from fastapi.responses import HTMLResponse

            mock_detail.return_value = HTMLResponse("<div>Issue flagged</div>")
            resp = client.post(
                f"/v2/partials/buy-plans/{plan.id}/lines/{line.id}/issue",
                data={"issue_type": "pricing", "note": "Price too high"},
            )
        assert resp.status_code == 200

    def test_flag_issue_value_error_returns_400(self, client, db_session, test_user):
        req = _make_requisition(db_session, test_user)
        quote = _make_quote(db_session, req, test_user)
        plan = _make_buy_plan(db_session, req, quote)
        line = _make_buy_plan_line(db_session, plan, req)

        with patch("app.services.buyplan_workflow.flag_line_issue", side_effect=ValueError("Line not found")):
            resp = client.post(
                f"/v2/partials/buy-plans/{plan.id}/lines/{line.id}/issue",
                data={"issue_type": "other"},
            )
        assert resp.status_code == 400


# ── Section 11: proactive_draft_for_prepare ───────────────────────────


class TestProactiveDraftForPrepare:
    """Tests for POST /v2/partials/proactive/draft."""

    def test_no_match_ids_returns_error_html(self, client, db_session):
        resp = client.post("/v2/partials/proactive/draft", data={})
        assert resp.status_code == 200
        assert "No matches selected" in resp.text

    def test_no_valid_matches_returns_error_html(self, client, db_session):
        resp = client.post(
            "/v2/partials/proactive/draft",
            data={"match_ids": ["999999"]},
        )
        assert resp.status_code == 200
        assert "No valid matches found" in resp.text

    def test_ai_draft_success_returns_script_html(self, client, db_session, test_user):
        from app.models import ProactiveMatch

        req = _make_requisition(db_session, test_user)
        offer = _make_offer(db_session, req, test_user)
        co = Company(name="Proactive Corp", is_active=True, created_at=datetime.now(timezone.utc))
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="HQ")
        db_session.add(site)
        db_session.flush()

        pm = ProactiveMatch(
            offer_id=offer.id,
            salesperson_id=test_user.id,
            customer_site_id=site.id,
            mpn="LM317T",
            match_score=80,
            status="new",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(pm)
        db_session.commit()
        db_session.refresh(pm)

        with patch("app.services.proactive_email.draft_proactive_email", new_callable=AsyncMock) as mock_draft:
            mock_draft.return_value = {
                "subject": "Parts Available — Proactive Corp",
                "body": "Dear Customer,\n\nWe have LM317T in stock.",
            }
            resp = client.post(
                "/v2/partials/proactive/draft",
                data={"match_ids": [str(pm.id)]},
            )
        assert resp.status_code == 200
        assert "Draft generated" in resp.text or "subject" in resp.text.lower() or "script" in resp.text.lower()

    def test_ai_draft_failure_returns_retry_html(self, client, db_session, test_user):
        from app.models import ProactiveMatch

        req = _make_requisition(db_session, test_user)
        offer = _make_offer(db_session, req, test_user)
        co = Company(name="Fallback Corp", is_active=True, created_at=datetime.now(timezone.utc))
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="HQ")
        db_session.add(site)
        db_session.flush()

        pm = ProactiveMatch(
            offer_id=offer.id,
            salesperson_id=test_user.id,
            customer_site_id=site.id,
            mpn="LM317T",
            match_score=80,
            status="new",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(pm)
        db_session.commit()

        with patch("app.services.proactive_email.draft_proactive_email", new_callable=AsyncMock) as mock_draft:
            mock_draft.side_effect = Exception("AI unavailable")
            resp = client.post(
                "/v2/partials/proactive/draft",
                data={"match_ids": [str(pm.id)]},
            )
        assert resp.status_code == 200
        assert "Auto-draft unavailable" in resp.text or "Retry" in resp.text


# ── Section 12: proactive_send_offer ─────────────────────────────────


class TestProactiveSendOffer:
    """Tests for POST /v2/proactive/send."""

    def test_no_match_ids_returns_400(self, client, db_session):
        resp = client.post("/v2/proactive/send", data={"contact_ids": ["1"]})
        assert resp.status_code == 400

    def test_no_contact_ids_returns_400(self, client, db_session):
        resp = client.post("/v2/proactive/send", data={"match_ids": ["1"]})
        assert resp.status_code == 400

    def test_send_success_returns_200(self, client, db_session, test_user):
        from app.models import ProactiveMatch

        req = _make_requisition(db_session, test_user)
        offer = _make_offer(db_session, req, test_user)
        co = Company(name="SendOffer Corp", is_active=True, created_at=datetime.now(timezone.utc))
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="HQ")
        db_session.add(site)
        db_session.flush()

        pm = ProactiveMatch(
            offer_id=offer.id,
            salesperson_id=test_user.id,
            customer_site_id=site.id,
            mpn="LM317T",
            match_score=80,
            status="new",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(pm)
        db_session.commit()

        mock_result = {"line_items": [{"mpn": "LM317T"}], "recipient_emails": ["buyer@corp.com"]}

        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="mock-token"),
            patch("app.services.proactive_service.send_proactive_offer", new_callable=AsyncMock) as mock_send,
            patch(
                "app.services.proactive_service.get_matches_for_user",
                return_value={"groups": [], "stats": {"total": 0}},
            ),
        ):
            mock_send.return_value = mock_result
            resp = client.post(
                "/v2/proactive/send",
                data={
                    "match_ids": [str(pm.id)],
                    "contact_ids": ["1"],
                    "subject": "Parts Available",
                    "body": "We have what you need.",
                },
            )
        assert resp.status_code == 200

    def test_send_value_error_returns_400(self, client, db_session, test_user):
        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="mock-token"),
            patch("app.services.proactive_service.send_proactive_offer", new_callable=AsyncMock) as mock_send,
        ):
            mock_send.side_effect = ValueError("No valid contacts")
            resp = client.post(
                "/v2/proactive/send",
                data={"match_ids": ["1"], "contact_ids": ["1"]},
            )
        assert resp.status_code == 400

    def test_send_exception_returns_500(self, client, db_session, test_user):
        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="mock-token"),
            patch("app.services.proactive_service.send_proactive_offer", new_callable=AsyncMock) as mock_send,
        ):
            mock_send.side_effect = Exception("Network error")
            resp = client.post(
                "/v2/proactive/send",
                data={"match_ids": ["1"], "contact_ids": ["1"]},
            )
        assert resp.status_code == 500


# ── Section 13: bulk_archive ─────────────────────────────────────────


class TestBulkArchive:
    """Tests for POST /v2/partials/parts/bulk-archive."""

    def test_archive_requirements_returns_200(self, client, db_session, test_user):
        req = _make_requisition(db_session, test_user)
        req_item = req.requirements[0]

        resp = client.post(
            "/v2/partials/parts/bulk-archive",
            json={"requirement_ids": [req_item.id], "requisition_ids": []},
        )
        assert resp.status_code == 200
        db_session.refresh(req_item)
        assert req_item.sourcing_status == SourcingStatus.ARCHIVED

    def test_archive_requisitions_returns_200(self, client, db_session, test_user):
        req = _make_requisition(db_session, test_user)

        resp = client.post(
            "/v2/partials/parts/bulk-archive",
            json={"requirement_ids": [], "requisition_ids": [req.id]},
        )
        assert resp.status_code == 200
        db_session.refresh(req)
        assert req.status == RequisitionStatus.ARCHIVED

    def test_archive_empty_body_returns_200(self, client, db_session):
        resp = client.post(
            "/v2/partials/parts/bulk-archive",
            json={"requirement_ids": [], "requisition_ids": []},
        )
        assert resp.status_code == 200

    def test_archive_cascades_to_requirements(self, client, db_session, test_user):
        req = _make_requisition(db_session, test_user)
        req_item = req.requirements[0]

        resp = client.post(
            "/v2/partials/parts/bulk-archive",
            json={"requirement_ids": [], "requisition_ids": [req.id]},
        )
        assert resp.status_code == 200
        db_session.refresh(req_item)
        assert req_item.sourcing_status == SourcingStatus.ARCHIVED
