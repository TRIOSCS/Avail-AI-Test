"""test_htmx_views_coverage.py — Additional coverage tests for app/routers/htmx_views.py.

Targets previously uncovered branches including: unauthenticated page load,
customer tabs, company CRUD, vendor find-by-part, search endpoints, requisition
search, rfq-send test mode, follow-up send, offer promote/reject, add-to-requisition,
requisition picker, buy-plan detail, update-requirement HTMX form, and more.

Called by: pytest
Depends on: conftest.py (client, db_session, test_user, test_requisition)
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import BuyPlanStatus, OfferStatus, QuoteStatus, RequisitionStatus, SourcingStatus
from app.models import (
    BuyPlan,
    Company,
    CustomerSite,
    Offer,
    Quote,
    Requirement,
    Requisition,
    Sighting,
    User,
    VendorCard,
)


# ── Helpers ───────────────────────────────────────────────────────────────


def _make_requisition(db: Session, user: User, **kw) -> Requisition:
    defaults = dict(
        name="REQ-COV",
        customer_name="Cov Corp",
        status=RequisitionStatus.ACTIVE,
        created_by=user.id,
        claimed_by_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    req = Requisition(**defaults)
    db.add(req)
    db.flush()
    return req


def _make_requirement(db: Session, req: Requisition, **kw) -> Requirement:
    defaults = dict(
        requisition_id=req.id,
        primary_mpn="LM317T",
        target_qty=100,
        sourcing_status=SourcingStatus.OPEN,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    r = Requirement(**defaults)
    db.add(r)
    db.flush()
    return r


def _make_vendor_card(db: Session, **kw) -> VendorCard:
    defaults = dict(
        normalized_name="test vendor",
        display_name="Test Vendor",
        emails=[],
        phones=[],
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    vc = VendorCard(**defaults)
    db.add(vc)
    db.flush()
    return vc


def _make_company(db: Session, **kw) -> Company:
    defaults = dict(
        name="TestCo Coverage",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    co = Company(**defaults)
    db.add(co)
    db.flush()
    return co


def _make_offer(db: Session, req: Requisition, user: User, **kw) -> Offer:
    defaults = dict(
        requisition_id=req.id,
        vendor_name="Arrow",
        mpn="LM317T",
        qty_available=100,
        unit_price=0.50,
        entered_by_id=user.id,
        status=OfferStatus.ACTIVE,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    o = Offer(**defaults)
    db.add(o)
    db.flush()
    return o


def _make_quote(db: Session, req: Requisition, user: User, **kw) -> Quote:
    defaults = dict(
        requisition_id=req.id,
        quote_number=f"Q-{req.id}-cov",
        status=QuoteStatus.DRAFT,
        created_by_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    q = Quote(**defaults)
    db.add(q)
    db.flush()
    return q


def _make_buy_plan(db: Session, quote: Quote, user: User, **kw) -> BuyPlan:
    defaults = dict(
        quote_id=quote.id,
        requisition_id=quote.requisition_id,
        status=BuyPlanStatus.PENDING,
        submitted_by_id=user.id,
        total_cost=200.0,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    bp = BuyPlan(**defaults)
    db.add(bp)
    db.flush()
    return bp


# ══════════════════════════════════════════════════════════════════════════
# Unauthenticated / login page
# ══════════════════════════════════════════════════════════════════════════


class TestUnauthenticatedPageLoad:
    """When no user is in session, v2_page should return login.html."""

    def test_v2_no_user_returns_login(self, unauthenticated_client: TestClient):
        resp = unauthenticated_client.get("/v2")
        assert resp.status_code == 200
        # The login page should be returned (no redirect)
        assert resp.text  # just confirm a response is returned


# ══════════════════════════════════════════════════════════════════════════
# Full-page view URL routing — partial_url derivation
# ══════════════════════════════════════════════════════════════════════════


class TestV2PageRouting:
    """Test that various URL patterns derive the correct partial_url."""

    def test_v2_crm(self, client: TestClient):
        resp = client.get("/v2/crm")
        assert resp.status_code == 200

    def test_v2_sightings_page(self, client: TestClient):
        resp = client.get("/v2/sightings")
        assert resp.status_code == 200

    def test_v2_buy_plans_detail(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        quote = _make_quote(db_session, req, test_user)
        db_session.commit()
        bp = _make_buy_plan(db_session, quote, test_user)
        db_session.commit()
        resp = client.get(f"/v2/buy-plans/{bp.id}")
        assert resp.status_code == 200

    def test_v2_excess_detail(self, client: TestClient):
        # non-existent ID just checks partial_url derivation, not 404
        resp = client.get("/v2/excess/1")
        assert resp.status_code == 200

    def test_v2_quotes_detail(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        quote = _make_quote(db_session, req, test_user)
        db_session.commit()
        resp = client.get(f"/v2/quotes/{quote.id}")
        assert resp.status_code == 200

    def test_v2_prospecting_detail(self, client: TestClient):
        resp = client.get("/v2/prospecting/1")
        assert resp.status_code == 200

    def test_v2_trouble_tickets_detail(self, client: TestClient):
        resp = client.get("/v2/trouble-tickets/1")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Vendor find-by-part
# ══════════════════════════════════════════════════════════════════════════


class TestVendorFindByPart:
    def test_find_by_part_no_mpn(self, client: TestClient):
        resp = client.get("/v2/partials/vendors/find-by-part")
        assert resp.status_code == 200

    def test_find_by_part_with_mpn(self, client: TestClient):
        resp = client.get("/v2/partials/vendors/find-by-part?mpn=LM317T")
        assert resp.status_code == 200

    def test_find_by_part_short_mpn(self, client: TestClient):
        # Very short MPN that normalize_mpn might return None for
        resp = client.get("/v2/partials/vendors/find-by-part?mpn=AB")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Vendor detail with mpn filter
# ══════════════════════════════════════════════════════════════════════════


class TestVendorDetailMpnFilter:
    def test_vendor_detail_with_mpn(self, client: TestClient, db_session: Session):
        vc = _make_vendor_card(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/vendors/{vc.id}?mpn=LM317T")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Customer tabs
# ══════════════════════════════════════════════════════════════════════════


class TestCustomerTabs:
    def test_tab_sites(self, client: TestClient, db_session: Session):
        co = _make_company(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{co.id}/tab/sites")
        assert resp.status_code == 200

    def test_tab_contacts_empty(self, client: TestClient, db_session: Session):
        co = _make_company(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{co.id}/tab/contacts")
        assert resp.status_code == 200

    def test_tab_requisitions_empty(self, client: TestClient, db_session: Session):
        co = _make_company(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{co.id}/tab/requisitions")
        assert resp.status_code == 200

    def test_tab_activity(self, client: TestClient, db_session: Session):
        co = _make_company(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{co.id}/tab/activity")
        assert resp.status_code == 200

    def test_tab_invalid(self, client: TestClient, db_session: Session):
        co = _make_company(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{co.id}/tab/bogus")
        assert resp.status_code == 404

    def test_tab_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/customers/999999/tab/sites")
        assert resp.status_code == 404

    def test_tab_requisitions_with_data(self, client: TestClient, db_session: Session, test_user: User):
        co = _make_company(db_session, name="Cov Co Req")
        db_session.commit()
        req = _make_requisition(db_session, test_user, customer_name="Cov Co Req")
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{co.id}/tab/requisitions")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Customer detail
# ══════════════════════════════════════════════════════════════════════════


class TestCustomerDetail:
    def test_customer_detail(self, client: TestClient, db_session: Session):
        co = _make_company(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{co.id}")
        assert resp.status_code == 200

    def test_customer_detail_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/customers/999999")
        assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# Company name-check
# ══════════════════════════════════════════════════════════════════════════


class TestCompanyNameCheck:
    def test_company_detail_with_open_req(self, client: TestClient, db_session: Session, test_user: User):
        co = _make_company(db_session, name="OpenReqCo")
        db_session.commit()
        req = _make_requisition(db_session, test_user, customer_name="OpenReqCo")
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{co.id}")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Buy-plan detail
# ══════════════════════════════════════════════════════════════════════════


class TestBuyPlanDetail:
    def test_buy_plan_detail(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        quote = _make_quote(db_session, req, test_user)
        db_session.commit()
        bp = _make_buy_plan(db_session, quote, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/buy-plans/{bp.id}")
        assert resp.status_code == 200

    def test_buy_plan_detail_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/buy-plans/999999")
        assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# Quotes list
# ══════════════════════════════════════════════════════════════════════════


class TestQuotesList:
    def test_quotes_list(self, client: TestClient):
        resp = client.get("/v2/partials/quotes")
        assert resp.status_code == 200

    def test_quotes_list_with_status(self, client: TestClient):
        resp = client.get("/v2/partials/quotes?status=draft")
        assert resp.status_code == 200

    def test_quotes_list_with_search(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        quote = _make_quote(db_session, req, test_user, quote_number="Q-SEARCH-001")
        db_session.commit()
        resp = client.get("/v2/partials/quotes?q=SEARCH")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Quote detail
# ══════════════════════════════════════════════════════════════════════════


class TestQuoteDetail:
    def test_quote_detail(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        quote = _make_quote(db_session, req, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/quotes/{quote.id}")
        assert resp.status_code == 200

    def test_quote_detail_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/quotes/999999")
        assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# RFQ send (test mode — no Graph API)
# ══════════════════════════════════════════════════════════════════════════


class TestRfqSendTestMode:
    def test_rfq_compose(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/rfq-compose")
        assert resp.status_code == 200

    def test_rfq_send_no_vendors(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/rfq-send",
            data={"subject": "Test RFQ", "body": "Please quote"},
        )
        assert resp.status_code == 400

    def test_rfq_send_test_mode(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/rfq-send",
            data={
                "vendor_names": "Arrow Electronics",
                "vendor_emails": "sales@arrow.com",
                "subject": "Test RFQ",
                "body": "Please quote LM317T",
            },
        )
        assert resp.status_code == 200

    def test_ai_cleanup_email_empty(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/ai-cleanup-email",
            data={"body": ""},
        )
        assert resp.status_code == 200
        assert "Write your email" in resp.text

    def test_ai_cleanup_email_with_text(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        with patch("app.utils.claude_client.claude_text", new_callable=AsyncMock, return_value="Cleaned email text."):
            resp = client.post(
                f"/v2/partials/requisitions/{req.id}/ai-cleanup-email",
                data={"body": "pls quote lm317t asap thx"},
            )
            assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Follow-up send
# ══════════════════════════════════════════════════════════════════════════


class TestFollowUpSend:
    def test_send_follow_up_not_found(self, client: TestClient):
        resp = client.post("/v2/partials/follow-ups/999999/send", data={"body": "Follow up"})
        assert resp.status_code == 404

    def test_send_follow_up_test_mode(self, client: TestClient, db_session: Session, test_user: User):
        from app.models.offers import Contact as RfqContact

        req = _make_requisition(db_session, test_user)
        db_session.commit()
        ct = RfqContact(
            requisition_id=req.id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="Arrow",
            vendor_contact="sales@arrow.com",
            status="sent",
            parts_included=["LM317T"],
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(ct)
        db_session.commit()
        resp = client.post(f"/v2/partials/follow-ups/{ct.id}/send", data={"body": "Follow up text"})
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Offer promote / reject
# ══════════════════════════════════════════════════════════════════════════


class TestOfferPromoteReject:
    def test_promote_offer(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        offer = _make_offer(db_session, req, test_user, status=OfferStatus.PENDING_REVIEW)
        db_session.commit()
        resp = client.post(f"/v2/partials/offers/{offer.id}/promote")
        assert resp.status_code == 200

    def test_promote_offer_not_found(self, client: TestClient):
        resp = client.post("/v2/partials/offers/999999/promote")
        assert resp.status_code == 404

    def test_promote_wrong_status(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        offer = _make_offer(db_session, req, test_user, status=OfferStatus.ACTIVE)
        db_session.commit()
        resp = client.post(f"/v2/partials/offers/{offer.id}/promote")
        assert resp.status_code == 400

    def test_reject_offer(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        offer = _make_offer(db_session, req, test_user, status=OfferStatus.PENDING_REVIEW)
        db_session.commit()
        resp = client.post(f"/v2/partials/offers/{offer.id}/reject")
        assert resp.status_code == 200

    def test_reject_offer_not_found(self, client: TestClient):
        resp = client.post("/v2/partials/offers/999999/reject")
        assert resp.status_code == 404

    def test_reject_wrong_status(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        offer = _make_offer(db_session, req, test_user, status=OfferStatus.ACTIVE)
        db_session.commit()
        resp = client.post(f"/v2/partials/offers/{offer.id}/reject")
        assert resp.status_code == 400

    def test_offer_changelog(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        offer = _make_offer(db_session, req, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/offers/{offer.id}/changelog")
        assert resp.status_code == 200

    def test_offer_changelog_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/offers/999999/changelog")
        assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# Poll inbox
# ══════════════════════════════════════════════════════════════════════════


class TestPollInbox:
    def test_poll_inbox(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.post(f"/v2/partials/requisitions/{req.id}/poll-inbox")
        assert resp.status_code == 200

    def test_poll_inbox_not_found(self, client: TestClient):
        resp = client.post("/v2/partials/requisitions/999999/poll-inbox")
        assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# Log activity
# ══════════════════════════════════════════════════════════════════════════


class TestLogActivity:
    def test_log_activity(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/log-activity",
            data={
                "activity_type": "note",
                "vendor_name": "Arrow",
                "notes": "Called vendor",
            },
        )
        assert resp.status_code == 200

    def test_log_activity_phone_call(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/log-activity",
            data={"activity_type": "phone_call", "vendor_name": "Arrow"},
        )
        assert resp.status_code == 200

    def test_log_activity_not_found(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/999999/log-activity",
            data={"activity_type": "note"},
        )
        assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# Add-to-requisition endpoint
# ══════════════════════════════════════════════════════════════════════════


class TestAddToRequisition:
    def test_add_to_requisition_missing_fields(self, client: TestClient):
        resp = client.post(
            "/v2/partials/search/add-to-requisition",
            json={},
        )
        assert resp.status_code == 400

    def test_add_to_requisition_not_found(self, client: TestClient):
        resp = client.post(
            "/v2/partials/search/add-to-requisition",
            json={"requisition_id": 999999, "mpn": "LM317T", "items": [{"vendor_name": "Arrow"}]},
        )
        assert resp.status_code == 404

    def test_add_to_requisition_success(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.post(
            "/v2/partials/search/add-to-requisition",
            json={
                "requisition_id": req.id,
                "mpn": "LM317T",
                "items": [
                    {
                        "vendor_name": "Arrow",
                        "mpn_matched": "LM317T",
                        "qty_available": 500,
                        "unit_price": 0.50,
                        "source_type": "brokerbin",
                        "confidence": 80,
                        "score": 60,
                    }
                ],
            },
        )
        assert resp.status_code == 200
        assert "Added" in resp.text

    def test_add_to_requisition_existing_requirement(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        _make_requirement(db_session, req, primary_mpn="NE555P")
        db_session.commit()
        resp = client.post(
            "/v2/partials/search/add-to-requisition",
            json={
                "requisition_id": req.id,
                "mpn": "NE555P",
                "items": [{"vendor_name": "Mouser", "qty_available": 100, "confidence": 70, "score": 50}],
            },
        )
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Requisition picker
# ══════════════════════════════════════════════════════════════════════════


class TestRequisitionPicker:
    def test_requisition_picker(self, client: TestClient):
        resp = client.get("/v2/partials/search/requisition-picker?mpn=LM317T&items=[]&action=add")
        assert resp.status_code == 200

    def test_requisition_picker_default(self, client: TestClient):
        resp = client.get("/v2/partials/search/requisition-picker")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Update requirement HTMX form
# ══════════════════════════════════════════════════════════════════════════


class TestUpdateRequirementHtmx:
    def test_update_requirement_success(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.put(
            f"/v2/partials/requisitions/{req.id}/requirements/{item.id}",
            data={
                "primary_mpn": "NE555P",
                "manufacturer": "Texas Instruments",
                "target_qty": "200",
            },
        )
        assert resp.status_code == 200

    def test_update_requirement_missing_manufacturer(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.put(
            f"/v2/partials/requisitions/{req.id}/requirements/{item.id}",
            data={"primary_mpn": "NE555P", "manufacturer": ""},
        )
        assert resp.status_code == 422

    def test_update_requirement_not_found(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.put(
            f"/v2/partials/requisitions/{req.id}/requirements/999999",
            data={"primary_mpn": "NE555P", "manufacturer": "TI"},
        )
        assert resp.status_code == 404

    def test_update_requirement_with_date(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.put(
            f"/v2/partials/requisitions/{req.id}/requirements/{item.id}",
            data={
                "primary_mpn": "LM317T",
                "manufacturer": "TI",
                "target_qty": "100",
                "need_by_date": "2026-06-01",
            },
        )
        assert resp.status_code == 200

    def test_update_requirement_bad_date(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.put(
            f"/v2/partials/requisitions/{req.id}/requirements/{item.id}",
            data={
                "primary_mpn": "LM317T",
                "manufacturer": "TI",
                "need_by_date": "bad-date",
            },
        )
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Delete requirement via HTMX route
# ══════════════════════════════════════════════════════════════════════════


class TestDeleteRequirementHtmx:
    def test_delete_requirement_not_found(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.delete(f"/v2/partials/requisitions/{req.id}/requirements/999999")
        assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# Customer quick-create
# ══════════════════════════════════════════════════════════════════════════


class TestCustomerQuickCreate:
    def test_quick_create_new(self, client: TestClient):
        with patch("app.cache.decorators.invalidate_prefix"):
            resp = client.post(
                "/v2/partials/customers/quick-create",
                data={
                    "company_name": "Quick Create Corp",
                    "website": "https://quickcreate.com",
                    "phone": "555-1234",
                    "city": "Austin",
                    "state": "TX",
                    "country": "US",
                },
            )
        assert resp.status_code == 200
        assert "Created" in resp.text

    def test_quick_create_duplicate(self, client: TestClient, db_session: Session):
        co = _make_company(db_session, name="Already Exists Corp")
        site = CustomerSite(company_id=co.id, site_name="HQ")
        db_session.add(site)
        db_session.commit()
        resp = client.post(
            "/v2/partials/customers/quick-create",
            data={"company_name": "Already Exists Corp"},
        )
        assert resp.status_code == 200
        assert "already exists" in resp.text


# ══════════════════════════════════════════════════════════════════════════
# Search partial — search results page with query + review response
# ══════════════════════════════════════════════════════════════════════════


class TestSearchPartials:
    def test_search_results_with_query(self, client: TestClient):
        resp = client.get("/v2/partials/search/results?q=LM317T")
        assert resp.status_code == 200

    def test_search_results_empty(self, client: TestClient):
        resp = client.get("/v2/partials/search/results")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Review vendor response
# ══════════════════════════════════════════════════════════════════════════


class TestReviewResponse:
    def test_review_response_not_found(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/responses/999999/review",
            data={"status": "reviewed"},
        )
        assert resp.status_code == 404

    def test_review_response_invalid_status(self, client: TestClient, db_session: Session, test_user: User):
        from app.models.offers import VendorResponse

        req = _make_requisition(db_session, test_user)
        db_session.commit()
        vr = VendorResponse(
            requisition_id=req.id,
            vendor_name="Arrow",
            body="Quote: LM317T $0.50",
            status="unread",
            received_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/responses/{vr.id}/review",
            data={"status": "invalid_status"},
        )
        assert resp.status_code == 400

    def test_review_response_success(self, client: TestClient, db_session: Session, test_user: User):
        from app.models.offers import VendorResponse

        req = _make_requisition(db_session, test_user)
        db_session.commit()
        vr = VendorResponse(
            requisition_id=req.id,
            vendor_name="Arrow",
            body="Quote: LM317T $0.50",
            status="unread",
            received_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/responses/{vr.id}/review",
            data={"status": "reviewed"},
        )
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Vendor tab — analytics, emails, reviews, find_contacts
# ══════════════════════════════════════════════════════════════════════════


class TestVendorTabsExtra:
    def test_tab_analytics(self, client: TestClient, db_session: Session):
        vc = _make_vendor_card(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/vendors/{vc.id}/tab/analytics")
        assert resp.status_code == 200

    def test_tab_emails(self, client: TestClient, db_session: Session):
        vc = _make_vendor_card(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/vendors/{vc.id}/tab/emails")
        assert resp.status_code == 200

    def test_tab_reviews(self, client: TestClient, db_session: Session):
        vc = _make_vendor_card(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/vendors/{vc.id}/tab/reviews")
        assert resp.status_code == 200

    def test_tab_find_contacts(self, client: TestClient, db_session: Session):
        vc = _make_vendor_card(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/vendors/{vc.id}/tab/find_contacts")
        assert resp.status_code == 200

    def test_vendor_list_with_my_only(self, client: TestClient, db_session: Session):
        resp = client.get("/v2/partials/vendors?my_only=true")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Mark offer sold (already sold branch)
# ══════════════════════════════════════════════════════════════════════════


class TestMarkOfferSoldBranches:
    def test_mark_sold_already_sold(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        offer = _make_offer(db_session, req, test_user, status=OfferStatus.SOLD)
        db_session.commit()
        resp = client.post(f"/v2/partials/requisitions/{req.id}/offers/{offer.id}/mark-sold")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Requisitions list - mpn customer_pn search
# ══════════════════════════════════════════════════════════════════════════


class TestRequisitionListEdgeCases:
    def test_list_search_customer_pn(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user, name="REQ-CPN")
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="LM317T",
            customer_pn="CPN-001",
            target_qty=100,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(r)
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?q=CPN-001")
        assert resp.status_code == 200

    def test_list_sort_status(self, client: TestClient, db_session: Session, test_user: User):
        _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?sort=status&dir=asc")
        assert resp.status_code == 200

    def test_list_sort_customer_name(self, client: TestClient, db_session: Session, test_user: User):
        _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?sort=customer_name&dir=asc")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Parts workspace tab extras
# ══════════════════════════════════════════════════════════════════════════


class TestPartsWorkspaceTabExtras:
    def test_parts_list_endpoint(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        _make_requirement(db_session, req)
        db_session.commit()
        resp = client.get("/v2/partials/parts")
        assert resp.status_code == 200

    def test_parts_list_with_req_filter(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        _make_requirement(db_session, req)
        db_session.commit()
        resp = client.get(f"/v2/partials/parts?req_id={req.id}")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Sightings workspace
# ══════════════════════════════════════════════════════════════════════════


class TestSightingsWorkspaceExtra:
    def test_sightings_workspace(self, client: TestClient):
        resp = client.get("/v2/partials/sightings/workspace")
        assert resp.status_code == 200

    def test_sightings_workspace_with_filters(self, client: TestClient):
        resp = client.get("/v2/partials/sightings/workspace?q=LM317T")
        assert resp.status_code == 200
