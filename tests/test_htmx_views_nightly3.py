"""test_htmx_views_nightly3.py — Third nightly coverage boost for htmx_views.py.

Targets: offer CRUD, log_activity, rfq_compose, search lead detail,
         add_to_requisition, customer sites, buy plan workflow,
         lead detail/status, material card update, quote line CRUD,
         add_offers_to_quote, build_buy_plan, proactive batch_dismiss.

Called by: pytest
Depends on: conftest.py (client, db_session, test_user, admin_user)
"""

import os

os.environ["TESTING"] = "1"

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import BuyPlanStatus, OfferStatus, RequisitionStatus, SourcingStatus
from app.models import Company, Offer, Requirement, Requisition, User
from app.models.buy_plan import BuyPlan
from app.models.crm import CustomerSite, SiteContact
from app.models.intelligence import MaterialCard
from app.models.quotes import Quote, QuoteLine
from app.models.sourcing_lead import SourcingLead

# ── Admin client fixture ─────────────────────────────────────────────────


@pytest.fixture()
def admin_client(db_session: Session, admin_user: User) -> TestClient:
    """TestClient authenticated as an admin user."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    def _db():
        yield db_session

    def _user():
        return admin_user

    async def _token():
        return "mock-token"

    overridden = [get_db, require_user, require_admin, require_buyer, require_fresh_token]
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = _user
    app.dependency_overrides[require_admin] = _user
    app.dependency_overrides[require_buyer] = _user
    app.dependency_overrides[require_fresh_token] = _token

    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in overridden:
            app.dependency_overrides.pop(dep, None)


# ── Helpers ─────────────────────────────────────────────────────────────


def _req(db: Session, user: User, **kw) -> Requisition:
    defaults = dict(
        name="N3-REQ",
        customer_name="N3 Corp",
        status=RequisitionStatus.ACTIVE,
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    obj = Requisition(**defaults)
    db.add(obj)
    db.flush()
    return obj


def _requirement(db: Session, req: Requisition, mpn: str = "LM317T", **kw) -> Requirement:
    defaults = dict(
        requisition_id=req.id,
        primary_mpn=mpn,
        target_qty=100,
        sourcing_status=SourcingStatus.OPEN,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    obj = Requirement(**defaults)
    db.add(obj)
    db.flush()
    return obj


def _offer(
    db: Session,
    req: Requisition,
    vendor_name: str = "TestVendor",
    mpn: str = "LM317T",
    status: str = OfferStatus.ACTIVE,
    **kw,
) -> Offer:
    defaults = dict(
        requisition_id=req.id,
        vendor_name=vendor_name,
        vendor_name_normalized=vendor_name.lower().replace(" ", "-"),
        mpn=mpn,
        normalized_mpn=mpn.upper(),
        source="manual",
        status=status,
        unit_price=1.50,
        qty_available=500,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    obj = Offer(**defaults)
    db.add(obj)
    db.flush()
    return obj


def _quote(db: Session, req: Requisition, user: User, status: str = "draft", **kw) -> Quote:
    qnum = f"Q-N3-{uuid.uuid4().hex[:6]}"
    defaults = dict(
        requisition_id=req.id,
        quote_number=qnum,
        status=status,
        created_by_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    obj = Quote(**defaults)
    db.add(obj)
    db.flush()
    return obj


def _quote_line(db: Session, quote: Quote, mpn: str = "LM317T", **kw) -> QuoteLine:
    defaults = dict(
        quote_id=quote.id,
        mpn=mpn,
        qty=10,
        cost_price=1.0,
        sell_price=1.5,
        margin_pct=33.3,
    )
    defaults.update(kw)
    obj = QuoteLine(**defaults)
    db.add(obj)
    db.flush()
    return obj


def _buy_plan(db: Session, quote: Quote, req: Requisition, **kw) -> BuyPlan:
    defaults = dict(
        quote_id=quote.id,
        requisition_id=req.id,
        status=BuyPlanStatus.DRAFT.value,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    obj = BuyPlan(**defaults)
    db.add(obj)
    db.flush()
    return obj


def _company_site(db: Session, company: Company, site_name: str = "HQ", **kw) -> CustomerSite:
    defaults = dict(
        company_id=company.id,
        site_name=site_name,
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    obj = CustomerSite(**defaults)
    db.add(obj)
    db.flush()
    return obj


def _sourcing_lead(db: Session, req: Requisition, requirement: Requirement, **kw) -> SourcingLead:
    lid = f"LEAD-{uuid.uuid4().hex[:8]}"
    defaults = dict(
        lead_id=lid,
        requirement_id=requirement.id,
        requisition_id=req.id,
        part_number_requested="LM317T",
        part_number_matched="LM317T",
        vendor_name="TestVendor",
        vendor_name_normalized="testvendor",
        primary_source_type="api",
        primary_source_name="test-source",
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    obj = SourcingLead(**defaults)
    db.add(obj)
    db.flush()
    return obj


def _material_card(db: Session, mpn: str = "N3-MPN", **kw) -> MaterialCard:
    defaults = dict(
        normalized_mpn=mpn.lower(),
        display_mpn=mpn,
        manufacturer="TestCo",
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    obj = MaterialCard(**defaults)
    db.add(obj)
    db.flush()
    return obj


# ── Offer Routes ─────────────────────────────────────────────────────────


class TestOfferRoutes:
    def test_add_offer_form(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        _requirement(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/partials/requisitions/{req.id}/add-offer-form")
        assert resp.status_code == 200

    def test_add_offer_success(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        _requirement(db_session, req)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/add-offer",
            data={"vendor_name": "Arrow Electronics", "mpn": "LM317T", "qty_available": "100"},
        )
        assert resp.status_code == 200

    def test_add_offer_missing_vendor_name(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/add-offer",
            data={"mpn": "LM317T"},
        )
        assert resp.status_code == 400

    def test_add_offer_missing_mpn(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/add-offer",
            data={"vendor_name": "Arrow"},
        )
        assert resp.status_code == 400

    def test_review_offer_approve(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        offer = _offer(db_session, req, status=OfferStatus.PENDING_REVIEW)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/offers/{offer.id}/review",
            data={"action": "approve"},
        )
        assert resp.status_code == 200

    def test_review_offer_reject(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        offer = _offer(db_session, req, status=OfferStatus.PENDING_REVIEW)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/offers/{offer.id}/review",
            data={"action": "reject"},
        )
        assert resp.status_code == 200

    def test_review_offer_invalid_action(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        offer = _offer(db_session, req, status=OfferStatus.PENDING_REVIEW)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/offers/{offer.id}/review",
            data={"action": "invalid"},
        )
        assert resp.status_code == 400

    def test_edit_offer_form(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        offer = _offer(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/partials/requisitions/{req.id}/offers/{offer.id}/edit-form")
        assert resp.status_code == 200

    def test_edit_offer_post(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        offer = _offer(db_session, req)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/offers/{offer.id}/edit",
            data={"vendor_name": "Updated Vendor", "qty_available": "200"},
        )
        assert resp.status_code == 200

    def test_delete_offer(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        offer = _offer(db_session, req)
        db_session.commit()

        resp = client.delete(f"/v2/partials/requisitions/{req.id}/offers/{offer.id}")
        assert resp.status_code == 200

    def test_offer_review_queue(self, client, db_session: Session, test_user: User):
        resp = client.get("/v2/partials/offers/review-queue")
        assert resp.status_code == 200

    def test_promote_offer(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        offer = _offer(db_session, req, status=OfferStatus.PENDING_REVIEW)
        db_session.commit()

        resp = client.post(f"/v2/partials/offers/{offer.id}/promote")
        assert resp.status_code == 200

    def test_reject_offer(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        offer = _offer(db_session, req, status=OfferStatus.PENDING_REVIEW)
        db_session.commit()

        resp = client.post(f"/v2/partials/offers/{offer.id}/reject")
        assert resp.status_code == 200

    def test_offer_changelog(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        offer = _offer(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/partials/offers/{offer.id}/changelog")
        assert resp.status_code == 200

    def test_mark_offer_sold(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        offer = _offer(db_session, req, status=OfferStatus.APPROVED)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/offers/{offer.id}/mark-sold",
            data={"sold_qty": "50"},
        )
        assert resp.status_code == 200


# ── Log Activity + RFQ Compose ───────────────────────────────────────────


class TestLogActivityAndRFQCompose:
    def test_log_activity_note(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/log-activity",
            data={"activity_type": "note", "notes": "Followed up with vendor"},
        )
        assert resp.status_code == 200

    def test_log_activity_phone_call(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/log-activity",
            data={"activity_type": "phone_call", "contact_phone": "555-1234"},
        )
        assert resp.status_code == 200

    def test_rfq_compose(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        _requirement(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/partials/requisitions/{req.id}/rfq-compose")
        assert resp.status_code == 200


# ── Search Lead Detail + Add to Requisition ──────────────────────────────


class TestSearchLeadDetailAndAddToReq:
    def test_search_lead_detail_with_results(self, client, db_session: Session):
        mock_results = [
            {
                "vendor_name": "Arrow Electronics",
                "mpn_matched": "LM317T",
                "unit_price": 1.50,
                "qty_available": 500,
                "source_type": "api",
                "vendor_score": 85,
                "is_authorized": True,
                "age_hours": 24,
                "lead_time": "1-2 weeks",
                "condition": "new",
                "evidence_tier": "tier1",
            }
        ]
        with patch("app.search_service.quick_search_mpn", AsyncMock(return_value=mock_results)):
            resp = client.get("/v2/partials/search/lead-detail?mpn=LM317T&idx=0")
        assert resp.status_code == 200

    def test_search_lead_detail_out_of_range(self, client, db_session: Session):
        mock_results = [{"vendor_name": "Arrow", "mpn_matched": "LM317T"}]
        with patch("app.search_service.quick_search_mpn", AsyncMock(return_value=mock_results)):
            resp = client.get("/v2/partials/search/lead-detail?mpn=LM317T&idx=99")
        assert resp.status_code == 200  # returns "Lead not found" HTML

    def test_add_to_requisition_success(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        db_session.commit()

        resp = client.post(
            "/v2/partials/search/add-to-requisition",
            content=json.dumps(
                {
                    "requisition_id": req.id,
                    "mpn": "LM317T",
                    "items": [
                        {
                            "vendor_name": "Arrow",
                            "mpn_matched": "LM317T",
                            "unit_price": 1.50,
                            "qty_available": 100,
                        }
                    ],
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200

    def test_add_to_requisition_missing_fields(self, client, db_session: Session):
        resp = client.post(
            "/v2/partials/search/add-to-requisition",
            content=json.dumps({"requisition_id": 99999}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400


# ── Customer Sites ────────────────────────────────────────────────────────


class TestCustomerSites:
    def test_create_site(self, client, db_session: Session, test_user: User, test_company):
        db_session.commit()

        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites",
            data={"site_name": "Branch Office", "site_type": "Branch", "city": "Boston"},
        )
        assert resp.status_code == 200

    def test_create_site_missing_name(self, client, db_session: Session, test_company):
        db_session.commit()

        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites",
            data={"site_name": ""},
        )
        assert resp.status_code == 200  # returns error HTML inline, not HTTP error

    def test_delete_site(self, client, db_session: Session, test_user: User, test_company):
        site = _company_site(db_session, test_company)
        db_session.commit()

        resp = client.delete(f"/v2/partials/customers/{test_company.id}/sites/{site.id}")
        assert resp.status_code == 200

    def test_site_contacts_list(self, client, db_session: Session, test_user: User, test_company):
        site = _company_site(db_session, test_company)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{test_company.id}/sites/{site.id}/contacts")
        assert resp.status_code == 200

    def test_create_site_contact(self, client, db_session: Session, test_user: User, test_company):
        site = _company_site(db_session, test_company)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/{site.id}/contacts",
            data={"full_name": "Jane Smith", "email": "jane@example.com", "title": "Buyer"},
        )
        assert resp.status_code == 200

    def test_delete_site_contact(self, client, db_session: Session, test_user: User, test_company):
        site = _company_site(db_session, test_company)
        contact = SiteContact(
            customer_site_id=site.id,
            full_name="John Doe",
            email="john@example.com",
            is_primary=False,
        )
        db_session.add(contact)
        db_session.commit()

        resp = client.delete(f"/v2/partials/customers/{test_company.id}/sites/{site.id}/contacts/{contact.id}")
        assert resp.status_code == 200


# ── Buy Plan Workflow ─────────────────────────────────────────────────────


class TestBuyPlanWorkflow:
    def test_buy_plan_cancel(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        quote = _quote(db_session, req, test_user)
        plan = _buy_plan(db_session, quote, req)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/buy-plans/{plan.id}/cancel",
            data={"reason": "Customer withdrew"},
        )
        assert resp.status_code == 200

    def test_buy_plan_cancel_already_cancelled(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        quote = _quote(db_session, req, test_user)
        plan = _buy_plan(db_session, quote, req, status=BuyPlanStatus.CANCELLED.value)
        db_session.commit()

        resp = client.post(f"/v2/partials/buy-plans/{plan.id}/cancel")
        assert resp.status_code == 400

    def test_buy_plan_approve_as_admin(self, admin_client, db_session: Session, admin_user: User):
        req = _req(db_session, admin_user)
        quote = _quote(db_session, req, admin_user)
        plan = _buy_plan(db_session, quote, req, status=BuyPlanStatus.PENDING.value)
        db_session.commit()

        with patch("app.services.buyplan_workflow.approve_buy_plan", return_value=plan):
            with patch("app.services.buyplan_notifications.run_notify_bg", AsyncMock(return_value=None)):
                resp = admin_client.post(
                    f"/v2/partials/buy-plans/{plan.id}/approve",
                    data={"action": "approve"},
                )
        assert resp.status_code == 200

    def test_buy_plan_submit(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        quote = _quote(db_session, req, test_user)
        plan = _buy_plan(db_session, quote, req)
        db_session.commit()

        mock_plan = MagicMock()
        mock_plan.auto_approved = False
        mock_plan.id = plan.id

        with patch("app.services.buyplan_workflow.submit_buy_plan", return_value=mock_plan):
            with patch("app.services.buyplan_notifications.run_notify_bg", AsyncMock(return_value=None)):
                resp = client.post(
                    f"/v2/partials/buy-plans/{plan.id}/submit",
                    data={"sales_order_number": "SO-12345"},
                )
        assert resp.status_code == 200

    def test_buy_plan_submit_missing_so(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        quote = _quote(db_session, req, test_user)
        plan = _buy_plan(db_session, quote, req)
        db_session.commit()

        resp = client.post(f"/v2/partials/buy-plans/{plan.id}/submit", data={})
        assert resp.status_code == 400


# ── Sourcing Lead Detail and Status ──────────────────────────────────────


class TestLeadDetailAndStatus:
    def test_lead_detail_not_found(self, client, db_session: Session):
        resp = client.get("/v2/partials/sourcing/leads/99999")
        assert resp.status_code == 404

    def test_lead_detail_found(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        requirement = _requirement(db_session, req)
        lead = _sourcing_lead(db_session, req, requirement)
        db_session.commit()

        resp = client.get(f"/v2/partials/sourcing/leads/{lead.id}")
        assert resp.status_code == 200

    def test_lead_status_update(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        requirement = _requirement(db_session, req)
        lead = _sourcing_lead(db_session, req, requirement)
        db_session.commit()

        # Return real lead object so DB queries on it succeed
        with patch("app.services.sourcing_leads.update_lead_status", return_value=lead):
            resp = client.post(
                f"/v2/partials/sourcing/leads/{lead.id}/status",
                data={"status": "contacted", "note": "Called vendor"},
            )
        assert resp.status_code in (200, 404)

    def test_lead_feedback(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        requirement = _requirement(db_session, req)
        lead = _sourcing_lead(db_session, req, requirement)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/sourcing/leads/{lead.id}/feedback",
            data={"feedback": "good", "note": "Great price"},
        )
        assert resp.status_code in (200, 404)


# ── Material Card Update ──────────────────────────────────────────────────


class TestMaterialCardUpdate:
    def test_update_material_card(self, client, db_session: Session, test_user: User):
        card = _material_card(db_session)
        db_session.commit()

        resp = client.put(
            f"/v2/partials/materials/{card.id}",
            data={"manufacturer": "Updated Mfr", "description": "New desc"},
        )
        assert resp.status_code == 200

    def test_update_material_card_not_found(self, client, db_session: Session):
        resp = client.put("/v2/partials/materials/99999", data={"manufacturer": "X"})
        assert resp.status_code == 404


# ── Quote Line CRUD ───────────────────────────────────────────────────────


class TestQuoteLineCRUD:
    def test_update_quote_line(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        quote = _quote(db_session, req, test_user)
        line = _quote_line(db_session, quote)
        db_session.commit()

        resp = client.put(
            f"/v2/partials/quotes/{quote.id}/lines/{line.id}",
            data={"qty": "20", "sell_price": "2.50", "cost_price": "1.80"},
        )
        assert resp.status_code == 200

    def test_update_quote_line_not_found(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        quote = _quote(db_session, req, test_user)
        db_session.commit()

        resp = client.put(f"/v2/partials/quotes/{quote.id}/lines/99999", data={"qty": "5"})
        assert resp.status_code == 404

    def test_delete_quote_line(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        quote = _quote(db_session, req, test_user)
        line = _quote_line(db_session, quote)
        db_session.commit()

        resp = client.delete(f"/v2/partials/quotes/{quote.id}/lines/{line.id}")
        assert resp.status_code == 200

    def test_add_quote_line(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        quote = _quote(db_session, req, test_user)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/quotes/{quote.id}/lines",
            data={"mpn": "LM317T", "qty": "10", "cost_price": "1.0", "sell_price": "1.5"},
        )
        assert resp.status_code == 200


# ── Add Offers to Quote + Build Buy Plan ─────────────────────────────────


class TestAddOffersAndBuildBuyPlan:
    def test_add_offers_to_draft_quote(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        quote = _quote(db_session, req, test_user, status="draft")
        offer = _offer(db_session, req)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/add-offers-to-quote",
            content=json.dumps({"quote_id": quote.id, "offer_ids": [offer.id]}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200

    def test_add_offers_to_non_draft_quote(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        quote = _quote(db_session, req, test_user, status="sent")
        offer = _offer(db_session, req)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/add-offers-to-quote",
            content=json.dumps({"quote_id": quote.id, "offer_ids": [offer.id]}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_build_buy_plan_from_won_quote(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        quote = _quote(db_session, req, test_user, status="won")
        # Pre-create plan so the service mock returns a real ORM object
        plan = _buy_plan(db_session, quote, req)
        db_session.commit()

        with patch("app.services.buyplan_builder.build_buy_plan", return_value=plan):
            resp = client.post(f"/v2/partials/quotes/{quote.id}/build-buy-plan")
        assert resp.status_code == 200

    def test_build_buy_plan_non_won_quote(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        quote = _quote(db_session, req, test_user, status="draft")
        db_session.commit()

        resp = client.post(f"/v2/partials/quotes/{quote.id}/build-buy-plan")
        assert resp.status_code == 400


# ── Proactive Batch Dismiss + Prepare ────────────────────────────────────


class TestProactiveBatchDismiss:
    def test_batch_dismiss_no_matches(self, client, db_session: Session, test_user: User):
        with patch(
            "app.services.proactive_service.get_matches_for_user",
            return_value={"groups": [], "stats": {"total": 0}},
        ):
            resp = client.post(
                "/v2/partials/proactive/batch-dismiss",
                data={},
            )
        assert resp.status_code == 200

    def test_proactive_prepare_no_matches(self, client, db_session: Session, test_user: User):
        # No match_ids → redirect to /v2/proactive
        resp = client.post("/v2/proactive/prepare/1", data={}, follow_redirects=False)
        assert resp.status_code == 303

    def test_proactive_draft_no_match_ids(self, client, db_session: Session, test_user: User):
        resp = client.post("/v2/partials/proactive/draft", data={})
        assert resp.status_code == 200  # returns error HTML

    def test_proactive_send_no_matches(self, client, db_session: Session, test_user: User):
        resp = client.post("/v2/proactive/send", data={})
        assert resp.status_code == 400

    def test_proactive_send_no_contacts(self, client, db_session: Session, test_user: User):
        resp = client.post(
            "/v2/proactive/send",
            data={"match_ids": ["1"], "contact_ids": []},
        )
        assert resp.status_code == 400


# ── Requisition Picker + Search Filter ───────────────────────────────────


class TestSearchHelpers:
    def test_requisition_picker(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        db_session.commit()

        resp = client.get("/v2/partials/search/requisition-picker?mpn=LM317T")
        assert resp.status_code == 200

    def test_search_filter_expired(self, client, db_session: Session):
        # Expired/missing search_id returns inline HTML, not HTTP error
        resp = client.get("/v2/partials/search/filter?search_id=nonexistent-id-123")
        assert resp.status_code == 200
