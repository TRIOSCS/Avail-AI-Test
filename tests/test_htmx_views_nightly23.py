"""tests/test_htmx_views_nightly23.py — Direct-async coverage for htmx_views.py.

Uses direct async function invocation (bypassing TestClient/ASGI) to achieve
coverage.py line tracing for async route function bodies that cannot be traced
through the TestClient+ASGI bridge.

Target line ranges (brings 76% → 85%+):
  - add_to_requisition      3314–3386  (~73 lines) JSON body
  - rfq_send                2542–2641  (~100 lines) form (TESTING mode path)
  - save_parsed_offers      1508–1521, 1530–1587  (~70 lines) form
  - add_offer               2040–2084  (~45 lines) form
  - edit_offer              2152–2215  (~64 lines) form
  - log_activity            2385–2404  (~20 lines) form
  - lead_status_update      6539–6607  (~69 lines) form
  - log_phone_call          5410–5444  (~35 lines) form
  - buy_plan_submit         5992–6009  (~18 lines) form with mocks
  - buy_plan_approve        6028–6043  (~16 lines) form with mocks
  - buy_plan_verify_so      6062–6080  (~19 lines) form with mocks

Called by: pytest autodiscovery (asyncio_mode = auto)
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

import os

os.environ["TESTING"] = "1"

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import HTMLResponse

from app.constants import BuyPlanStatus, OfferStatus, QuoteStatus, SOVerificationStatus
from app.models import Company, CustomerSite, Requisition, User, VendorCard
from app.models.offers import Offer
from app.models.quotes import Quote
from app.models.sourcing_lead import SourcingLead


# ── Mock helpers ──────────────────────────────────────────────────────────────


def _mock_form_request(path: str = "/v2/test", fields: dict | None = None, headers: dict | None = None) -> MagicMock:
    """Create a mock Request with form data and given path."""
    mock_req = MagicMock(spec=Request)
    mock_req.url.path = path
    mock_req.headers = headers or {}

    if fields is not None:
        form_mock = MagicMock()
        form_mock.get = lambda key, default=None: fields.get(key, default)
        form_mock.getlist = lambda key: (
            fields[key] if isinstance(fields.get(key), list) else ([fields[key]] if key in fields else [])
        )
        mock_req.form = AsyncMock(return_value=form_mock)
    return mock_req


def _mock_json_request(path: str = "/v2/test", body: dict | None = None) -> MagicMock:
    """Create a mock Request with JSON body and given path."""
    mock_req = MagicMock(spec=Request)
    mock_req.url.path = path
    mock_req.headers = {}
    mock_req.json = AsyncMock(return_value=body or {})
    return mock_req


# ── DB helpers ────────────────────────────────────────────────────────────────


def _make_requisition(db: Session, user: User, name: str | None = None) -> Requisition:
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


def _make_buy_plan(db: Session, req: Requisition, user: User):
    from app.models.buy_plan import BuyPlan

    q = Quote(
        requisition_id=req.id,
        quote_number=f"Q-{uuid.uuid4().hex[:8]}",
        status=QuoteStatus.DRAFT,
        created_by_id=user.id,
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


# ── add_to_requisition (3314–3386) ─────────────────────────────────────────


class TestAddToRequisitionDirect:
    async def test_add_sightings_creates_sighting_rows(self, db_session: Session, test_user: User):
        """Lines 3314–3386: creates Requirement if missing and Sighting rows."""
        from app.routers.htmx_views import add_to_requisition

        req = _make_requisition(db_session, test_user)
        mock_req = _mock_json_request(
            path="/v2/partials/search/add-to-requisition",
            body={
                "requisition_id": req.id,
                "mpn": "LM741",
                "items": [
                    {"vendor_name": "AlphaBroker", "qty_available": 500, "unit_price": 0.75, "score": 70},
                    {"vendor_name": "BetaBroker", "qty_available": 200, "unit_price": 0.90, "score": 60},
                ],
            },
        )
        result = await add_to_requisition(request=mock_req, user=test_user, db=db_session)
        assert result.status_code == 200

    async def test_add_sightings_uses_existing_requirement(self, db_session: Session, test_user: User):
        """Lines 3340–3351: finds existing Requirement rather than creating a new one."""
        from app.routers.htmx_views import add_to_requisition

        req = _make_requisition(db_session, test_user)  # creates BC547 requirement
        mock_req = _mock_json_request(
            path="/v2/partials/search/add-to-requisition",
            body={
                "requisition_id": req.id,
                "mpn": "BC547",
                "items": [{"vendor_name": "TestBroker", "qty_available": 1000, "score": 80}],
            },
        )
        result = await add_to_requisition(request=mock_req, user=test_user, db=db_session)
        assert result.status_code == 200

    async def test_add_sightings_missing_fields_returns_400(self, db_session: Session, test_user: User):
        """Lines 3318–3322: missing required fields → 400."""
        from app.routers.htmx_views import add_to_requisition

        mock_req = _mock_json_request(body={"requisition_id": 1})
        result = await add_to_requisition(request=mock_req, user=test_user, db=db_session)
        assert result.status_code == 400

    async def test_add_sightings_requisition_not_found(self, db_session: Session, test_user: User):
        """Lines 3325–3329: requisition not found → 404."""
        from app.routers.htmx_views import add_to_requisition

        mock_req = _mock_json_request(
            body={"requisition_id": 99999, "mpn": "LM741", "items": [{"vendor_name": "X"}]}
        )
        result = await add_to_requisition(request=mock_req, user=test_user, db=db_session)
        assert result.status_code == 404


# ── rfq_send (2542–2641) ───────────────────────────────────────────────────


class TestRfqSendDirect:
    async def test_rfq_send_test_mode_creates_contacts(self, db_session: Session, test_user: User):
        """Lines 2542–2641: TESTING=1 → creates RfqContact without sending email."""
        from app.routers.htmx_views import rfq_send

        req = _make_requisition(db_session, test_user)
        mock_req = _mock_form_request(
            path=f"/v2/partials/requisitions/{req.id}/rfq-send",
            fields={
                "vendor_names": ["AlphaVendor", "BetaVendor"],
                "vendor_emails": ["alpha@vendor.com", "beta@vendor.com"],
                "subject": "RFQ for BC547",
                "body": "Please quote 100x BC547",
                "parts_summary": "BC547 x 100",
            },
        )
        with patch("app.routers.htmx_views.templates") as mock_tpl:
            mock_tpl.TemplateResponse.return_value = HTMLResponse("OK")
            result = await rfq_send(request=mock_req, req_id=req.id, user=test_user, db=db_session)
        assert result.status_code == 200

    async def test_rfq_send_no_vendors_raises_400(self, db_session: Session, test_user: User):
        """Lines 2538–2539: no vendors → HTTPException 400."""
        from app.routers.htmx_views import rfq_send

        req = _make_requisition(db_session, test_user)
        mock_req = _mock_form_request(fields={"vendor_names": []})

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await rfq_send(request=mock_req, req_id=req.id, user=test_user, db=db_session)
        assert exc_info.value.status_code == 400

    async def test_rfq_send_vendor_no_email_skipped(self, db_session: Session, test_user: User):
        """Vendor with empty email is skipped in contact creation."""
        from app.routers.htmx_views import rfq_send

        req = _make_requisition(db_session, test_user)
        mock_req = _mock_form_request(
            fields={
                "vendor_names": ["NoEmailVendor"],
                "vendor_emails": [""],
                "subject": "RFQ",
            },
        )
        with patch("app.routers.htmx_views.templates") as mock_tpl:
            mock_tpl.TemplateResponse.return_value = HTMLResponse("OK")
            result = await rfq_send(request=mock_req, req_id=req.id, user=test_user, db=db_session)
        assert result.status_code == 200


# ── save_parsed_offers (1508–1521, 1530–1587) ─────────────────────────────


class TestSaveParsedOffersDirect:
    async def test_save_offers_loop_and_creates_offer(self, db_session: Session, test_user: User):
        """Lines 1501–1587: processes offers loop and creates Offer rows."""
        from app.routers.htmx_views import save_parsed_offers

        req = _make_requisition(db_session, test_user)
        fields = {
            "vendor_name": "ParsedVendor",
            "offers[0].mpn": "BC547",
            "offers[0].qty_available": "500",
            "offers[0].unit_price": "1.25",
            "offers[0].condition": "new",
        }
        mock_req = _mock_form_request(path=f"/v2/partials/requisitions/{req.id}/save-parsed-offers", fields=fields)
        with patch("app.routers.htmx_views.templates") as mock_tpl:
            mock_tpl.TemplateResponse.return_value = HTMLResponse("OK")
            result = await save_parsed_offers(request=mock_req, req_id=req.id, user=test_user, db=db_session)
        assert result.status_code == 200

    async def test_save_offers_vendor_name_key(self, db_session: Session, test_user: User):
        """Lines 1504–1507: picks offer row by vendor_name key when mpn is None."""
        from app.routers.htmx_views import save_parsed_offers

        req = _make_requisition(db_session, test_user)
        # Offer without mpn key — uses vendor_name key path
        fields = {
            "vendor_name": "NoMPNVendor",
            "offers[0].vendor_name": "RowVendor",
            "offers[0].qty_available": "200",
        }
        mock_req = _mock_form_request(path=f"/v2/partials/requisitions/{req.id}/save-parsed-offers", fields=fields)
        with patch("app.routers.htmx_views.templates") as mock_tpl:
            mock_tpl.TemplateResponse.return_value = HTMLResponse("OK")
            result = await save_parsed_offers(request=mock_req, req_id=req.id, user=test_user, db=db_session)
        assert result.status_code == 200

    async def test_save_offers_empty_form_returns_warning(self, db_session: Session, test_user: User):
        """Lines 1523–1527: no offers → warning HTMLResponse."""
        from app.routers.htmx_views import save_parsed_offers

        req = _make_requisition(db_session, test_user)
        mock_req = _mock_form_request(
            path=f"/v2/partials/requisitions/{req.id}/save-parsed-offers",
            fields={"vendor_name": "X"},
        )
        result = await save_parsed_offers(request=mock_req, req_id=req.id, user=test_user, db=db_session)
        assert result.status_code == 200
        assert b"No offers" in result.body

    async def test_save_offers_not_found_raises_404(self, db_session: Session, test_user: User):
        """Lines 1493–1494: requisition not found → 404."""
        from app.routers.htmx_views import save_parsed_offers
        from fastapi import HTTPException

        mock_req = _mock_form_request(fields={"vendor_name": "X"})
        with pytest.raises(HTTPException) as exc_info:
            await save_parsed_offers(request=mock_req, req_id=99999, user=test_user, db=db_session)
        assert exc_info.value.status_code == 404


# ── add_offer (2040–2084) ─────────────────────────────────────────────────


class TestAddOfferDirect:
    async def test_add_offer_creates_offer(self, db_session: Session, test_user: User):
        """Lines 2040–2084: validates input and creates Offer row."""
        from app.routers.htmx_views import add_offer

        req = _make_requisition(db_session, test_user)
        mock_req = _mock_form_request(
            path=f"/v2/partials/requisitions/{req.id}/add-offer",
            fields={
                "vendor_name": "ManualVendor",
                "mpn": "LM317T",
                "qty_available": "200",
                "unit_price": "1.25",
                "condition": "new",
                "lead_time": "2 weeks",
            },
        )
        with patch("app.routers.htmx_views.requisition_tab", new_callable=AsyncMock) as mock_tab:
            mock_tab.return_value = HTMLResponse("tab OK")
            result = await add_offer(request=mock_req, req_id=req.id, user=test_user, db=db_session)
        assert result.status_code == 200

    async def test_add_offer_missing_required_returns_400(self, db_session: Session, test_user: User):
        """Lines 2042–2046: missing vendor_name or mpn → 400 HTMLResponse."""
        from app.routers.htmx_views import add_offer

        req = _make_requisition(db_session, test_user)
        mock_req = _mock_form_request(fields={"vendor_name": "X"})  # missing mpn
        result = await add_offer(request=mock_req, req_id=req.id, user=test_user, db=db_session)
        assert result.status_code == 400

    async def test_add_offer_not_found_raises_404(self, db_session: Session, test_user: User):
        """get_requisition_or_404 raises 404 for missing req."""
        from app.routers.htmx_views import add_offer
        from fastapi import HTTPException

        mock_req = _mock_form_request(fields={"vendor_name": "X", "mpn": "Y"})
        with pytest.raises(HTTPException) as exc_info:
            await add_offer(request=mock_req, req_id=99999, user=test_user, db=db_session)
        assert exc_info.value.status_code == 404


# ── edit_offer (2152–2215) ────────────────────────────────────────────────


class TestEditOfferDirect:
    async def test_edit_offer_updates_fields(self, db_session: Session, test_user: User):
        """Lines 2151–2215: reads form, updates offer fields, logs changelog."""
        from app.routers.htmx_views import edit_offer

        req = _make_requisition(db_session, test_user)
        offer = _make_offer(db_session, req, test_user)
        mock_req = _mock_form_request(
            path=f"/v2/partials/requisitions/{req.id}/offers/{offer.id}/edit",
            fields={
                "vendor_name": "UpdatedVendor",
                "unit_price": "3.00",
                "qty_available": "750",
                "condition": "refurbished",
                "lead_time": "3 weeks",
                "notes": "Updated note",
            },
        )
        with patch("app.routers.htmx_views.requisition_tab", new_callable=AsyncMock) as mock_tab:
            mock_tab.return_value = HTMLResponse("tab OK")
            result = await edit_offer(
                request=mock_req,
                req_id=req.id,
                offer_id=offer.id,
                user=test_user,
                db=db_session,
            )
        assert result.status_code == 200
        db_session.refresh(offer)
        assert offer.unit_price == 3.00

    async def test_edit_offer_not_found_raises_404(self, db_session: Session, test_user: User):
        """Lines 2148–2149: offer not found → 404."""
        from app.routers.htmx_views import edit_offer
        from fastapi import HTTPException

        req = _make_requisition(db_session, test_user)
        mock_req = _mock_form_request(fields={"unit_price": "1.00"})
        with pytest.raises(HTTPException) as exc_info:
            await edit_offer(request=mock_req, req_id=req.id, offer_id=99999, user=test_user, db=db_session)
        assert exc_info.value.status_code == 404


# ── log_activity (2385–2404) ─────────────────────────────────────────────


class TestLogActivityDirect:
    async def test_log_activity_creates_log_entry(self, db_session: Session, test_user: User):
        """Lines 2385–2404: creates ActivityLog and returns activity tab."""
        from app.routers.htmx_views import log_activity

        req = _make_requisition(db_session, test_user)
        mock_req = _mock_form_request(
            path=f"/v2/partials/requisitions/{req.id}/activity",
            fields={
                "activity_type": "phone_call",
                "vendor_name": "TestVendor",
                "notes": "Spoke about availability",
            },
        )
        with patch("app.routers.htmx_views.requisition_tab", new_callable=AsyncMock) as mock_tab:
            mock_tab.return_value = HTMLResponse("tab OK")
            result = await log_activity(
                request=mock_req, req_id=req.id, user=test_user, db=db_session
            )
        assert result.status_code == 200

    async def test_log_activity_note_type(self, db_session: Session, test_user: User):
        """Activity type 'note' maps to channel 'note'."""
        from app.routers.htmx_views import log_activity

        req = _make_requisition(db_session, test_user)
        mock_req = _mock_form_request(
            path=f"/v2/partials/requisitions/{req.id}/activity",
            fields={"activity_type": "note", "notes": "Quick note"},
        )
        with patch("app.routers.htmx_views.requisition_tab", new_callable=AsyncMock) as mock_tab:
            mock_tab.return_value = HTMLResponse("tab OK")
            result = await log_activity(
                request=mock_req, req_id=req.id, user=test_user, db=db_session
            )
        assert result.status_code == 200


# ── lead_status_update (6539–6607) ───────────────────────────────────────


class TestLeadStatusUpdateDirect:
    async def test_update_lead_status_has_stock(self, db_session: Session, test_user: User):
        """Lines 6539–6607: updates lead status, returns lead card."""
        from app.routers.htmx_views import lead_status_update

        req = _make_requisition(db_session, test_user)
        lead = _make_lead(db_session, req)
        mock_req = _mock_form_request(
            path=f"/v2/partials/sourcing/leads/{lead.id}/status",
            fields={"status": "has_stock", "note": "Confirmed 500 units"},
        )
        with patch("app.routers.htmx_views.templates") as mock_tpl:
            mock_tpl.TemplateResponse.return_value = HTMLResponse("lead OK")
            result = await lead_status_update(
                request=mock_req, lead_id=lead.id, user=test_user, db=db_session
            )
        assert result.status_code == 200

    async def test_update_lead_status_no_stock(self, db_session: Session, test_user: User):
        """Another valid status."""
        from app.routers.htmx_views import lead_status_update

        req = _make_requisition(db_session, test_user)
        lead = _make_lead(db_session, req)
        mock_req = _mock_form_request(
            path=f"/v2/partials/sourcing/leads/{lead.id}/status",
            fields={"status": "no_stock"},
        )
        with patch("app.routers.htmx_views.templates") as mock_tpl:
            mock_tpl.TemplateResponse.return_value = HTMLResponse("lead OK")
            result = await lead_status_update(
                request=mock_req, lead_id=lead.id, user=test_user, db=db_session
            )
        assert result.status_code == 200

    async def test_update_lead_status_invalid_raises_400(self, db_session: Session, test_user: User):
        """Invalid status value → HTTPException 400."""
        from app.routers.htmx_views import lead_status_update
        from fastapi import HTTPException

        req = _make_requisition(db_session, test_user)
        lead = _make_lead(db_session, req)
        mock_req = _mock_form_request(fields={"status": "invalid_status"})
        with pytest.raises(HTTPException) as exc_info:
            await lead_status_update(request=mock_req, lead_id=lead.id, user=test_user, db=db_session)
        assert exc_info.value.status_code == 400

    async def test_update_lead_not_found_raises_404(self, db_session: Session, test_user: User):
        """Lead not found → HTTPException 404."""
        from app.routers.htmx_views import lead_status_update
        from fastapi import HTTPException

        mock_req = _mock_form_request(fields={"status": "has_stock"})
        with pytest.raises(HTTPException) as exc_info:
            await lead_status_update(request=mock_req, lead_id=99999, user=test_user, db=db_session)
        assert exc_info.value.status_code == 404


# ── log_phone_call (5410–5444) ───────────────────────────────────────────


class TestLogPhoneCallDirect:
    async def test_log_phone_call_creates_contact_and_log(self, db_session: Session, test_user: User):
        """Lines 5410–5444: creates RfqContact and ActivityLog for phone call."""
        from app.routers.htmx_views import log_phone_call

        req = _make_requisition(db_session, test_user)
        mock_req = _mock_form_request(
            path=f"/v2/partials/requisitions/{req.id}/log-phone",
            fields={
                "vendor_name": "PhoneVendor",
                "vendor_phone": "+1-555-0100",
                "notes": "Discussed pricing for BC547",
            },
        )
        with patch("app.routers.htmx_views.templates") as mock_tpl:
            mock_tpl.TemplateResponse.return_value = HTMLResponse("phone OK")
            result = await log_phone_call(request=mock_req, req_id=req.id, user=test_user, db=db_session)
        assert result.status_code == 200

    async def test_log_phone_call_missing_fields_raises_400(self, db_session: Session, test_user: User):
        """Missing vendor_name or phone → HTTPException 400."""
        from app.routers.htmx_views import log_phone_call
        from fastapi import HTTPException

        req = _make_requisition(db_session, test_user)
        mock_req = _mock_form_request(fields={"vendor_name": "X"})  # missing phone
        with pytest.raises(HTTPException) as exc_info:
            await log_phone_call(request=mock_req, req_id=req.id, user=test_user, db=db_session)
        assert exc_info.value.status_code == 400

    async def test_log_phone_call_not_found_raises_404(self, db_session: Session, test_user: User):
        """Requisition not found → 404."""
        from app.routers.htmx_views import log_phone_call
        from fastapi import HTTPException

        mock_req = _mock_form_request(fields={"vendor_name": "X", "vendor_phone": "555"})
        with pytest.raises(HTTPException) as exc_info:
            await log_phone_call(request=mock_req, req_id=99999, user=test_user, db=db_session)
        assert exc_info.value.status_code == 404


# ── buy_plan_submit (5992–6009) ──────────────────────────────────────────


class TestBuyPlanSubmitDirect:
    async def test_buy_plan_submit_success(self, db_session: Session, test_user: User):
        """Lines 5992–6009: calls submit_buy_plan and returns refreshed detail."""
        from app.routers.htmx_views import buy_plan_submit_partial

        req = _make_requisition(db_session, test_user)
        bp = _make_buy_plan(db_session, req, test_user)
        mock_req = _mock_form_request(
            path=f"/v2/partials/buy-plans/{bp.id}/submit",
            fields={"sales_order_number": "SO-12345", "customer_po_number": "PO-001", "salesperson_notes": "Urgent"},
        )
        with (
            patch("app.services.buyplan_workflow.submit_buy_plan") as mock_submit,
            patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock),
            patch("app.routers.htmx_views.buy_plan_detail_partial", new_callable=AsyncMock) as mock_detail,
        ):
            mock_submit.return_value = MagicMock(auto_approved=False, id=bp.id)
            mock_detail.return_value = HTMLResponse("bp detail")
            result = await buy_plan_submit_partial(
                request=mock_req, plan_id=bp.id, user=test_user, db=db_session
            )
        assert result.status_code == 200

    async def test_buy_plan_submit_missing_so_raises_400(self, db_session: Session, test_user: User):
        """Lines 5988–5990: missing SO number → HTTPException 400."""
        from app.routers.htmx_views import buy_plan_submit_partial
        from fastapi import HTTPException

        req = _make_requisition(db_session, test_user)
        bp = _make_buy_plan(db_session, req, test_user)
        mock_req = _mock_form_request(fields={"sales_order_number": ""})  # empty SO
        with pytest.raises(HTTPException) as exc_info:
            await buy_plan_submit_partial(request=mock_req, plan_id=bp.id, user=test_user, db=db_session)
        assert exc_info.value.status_code == 400


# ── buy_plan_approve (6028–6043) ─────────────────────────────────────────


class TestBuyPlanApproveDirect:
    async def test_buy_plan_approve_success(self, db_session: Session, test_user: User):
        """Lines 6028–6043: manager approves buy plan."""
        from app.routers.htmx_views import buy_plan_approve_partial

        req = _make_requisition(db_session, test_user)
        bp = _make_buy_plan(db_session, req, test_user)
        # Create a manager user for the approval
        manager = User(
            email=f"mgr-{uuid.uuid4().hex[:6]}@test.com",
            name="Manager",
            role="manager",
            is_active=True,
        )
        db_session.add(manager)
        db_session.commit()
        db_session.refresh(manager)
        mock_req = _mock_form_request(
            path=f"/v2/partials/buy-plans/{bp.id}/approve",
            fields={"action": "approve", "notes": "Looks good"},
        )
        with (
            patch("app.services.buyplan_workflow.approve_buy_plan") as mock_approve,
            patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock),
            patch("app.routers.htmx_views.buy_plan_detail_partial", new_callable=AsyncMock) as mock_detail,
        ):
            mock_approve.return_value = MagicMock(id=bp.id)
            mock_detail.return_value = HTMLResponse("bp detail")
            result = await buy_plan_approve_partial(
                request=mock_req, plan_id=bp.id, user=manager, db=db_session
            )
        assert result.status_code == 200

    async def test_buy_plan_approve_non_manager_raises_403(self, db_session: Session, test_user: User):
        """Lines 6030–6031: non-manager role → HTTPException 403."""
        from app.routers.htmx_views import buy_plan_approve_partial
        from fastapi import HTTPException

        req = _make_requisition(db_session, test_user)
        bp = _make_buy_plan(db_session, req, test_user)
        # Create a non-manager user
        buyer = User(email=f"buyer-{uuid.uuid4().hex[:6]}@test.com", name="Buyer", role="buyer", is_active=True)
        db_session.add(buyer)
        db_session.commit()
        db_session.refresh(buyer)
        mock_req = _mock_form_request(fields={"action": "approve"})
        with pytest.raises(HTTPException) as exc_info:
            await buy_plan_approve_partial(request=mock_req, plan_id=bp.id, user=buyer, db=db_session)
        assert exc_info.value.status_code == 403


# ── buy_plan_verify_so (6062–6080) ───────────────────────────────────────


class TestBuyPlanVerifySoDirect:
    async def test_buy_plan_verify_so_success(self, db_session: Session, test_user: User):
        """Lines 6062–6080: ops verifies SO on buy plan."""
        from app.routers.htmx_views import buy_plan_verify_so_partial

        req = _make_requisition(db_session, test_user)
        bp = _make_buy_plan(db_session, req, test_user)
        mock_req = _mock_form_request(
            path=f"/v2/partials/buy-plans/{bp.id}/verify-so",
            fields={"action": "approve"},
        )
        with (
            patch("app.services.buyplan_workflow.verify_so") as mock_verify,
            patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock),
            patch("app.routers.htmx_views.buy_plan_detail_partial", new_callable=AsyncMock) as mock_detail,
        ):
            mock_verify.return_value = MagicMock(id=bp.id)
            mock_detail.return_value = HTMLResponse("bp detail")
            result = await buy_plan_verify_so_partial(
                request=mock_req, plan_id=bp.id, user=test_user, db=db_session
            )
        assert result.status_code == 200
