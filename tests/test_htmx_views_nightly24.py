"""tests/test_htmx_views_nightly24.py — Additional direct-async coverage for htmx_views.py.

Targets more high-impact uncovered line ranges using direct async invocation.

Target line ranges:
  - create_quote_from_offers  1916–1971  (~56 lines) form with offer_ids
  - review_offer              1987–2010  (~24 lines) form approve/reject
  - update_requirement        2881–2962  (~82 lines) Form params + form body
  - add_offers_to_draft_quote 7683–7733  (~51 lines) raw body JSON
  - buy_plan_verify_po        6136–6148  (~13 lines) form
  - buy_plan_flag_issue       6162–6172  (~11 lines) form
  - buy_plan_cancel           6191–6197  (~7 lines) no form

Called by: pytest autodiscovery (asyncio_mode = auto)
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

import os

os.environ["TESTING"] = "1"

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import BackgroundTasks
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import HTMLResponse

from app.constants import BuyPlanStatus, OfferStatus, QuoteStatus, SOVerificationStatus
from app.models import Requirement, Requisition, User
from app.models.offers import Offer
from app.models.quotes import Quote

# ── Helpers ────────────────────────────────────────────────────────────────


def _mock_form_request(path: str = "/v2/test", fields: dict | None = None, headers: dict | None = None) -> MagicMock:
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


def _mock_body_request(path: str = "/v2/test", body_bytes: bytes = b"") -> MagicMock:
    mock_req = MagicMock(spec=Request)
    mock_req.url.path = path
    mock_req.headers = {}
    mock_req.body = AsyncMock(return_value=body_bytes)
    return mock_req


def _make_req(db: Session, user: User) -> Requisition:
    req = Requisition(
        name=f"REQ-{uuid.uuid4().hex[:6]}",
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


def _make_draft_quote(db: Session, req: Requisition, user: User) -> Quote:
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


def _make_buy_plan(db: Session, req: Requisition, user: User):
    from app.models.buy_plan import BuyPlan

    q = _make_draft_quote(db, req, user)
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


# ── create_quote_from_offers (1916–1971) ─────────────────────────────────


class TestCreateQuoteFromOffersDirect:
    async def test_creates_quote_from_offer_ids(self, db_session: Session, test_user: User):
        """Lines 1916–1971: creates Quote + QuoteLines from offer_ids list."""
        from app.routers.htmx_views import create_quote_from_offers

        req = _make_req(db_session, test_user)
        offer = _make_offer(db_session, req, test_user)
        mock_req = _mock_form_request(
            path=f"/v2/partials/requisitions/{req.id}/create-quote",
            fields={"offer_ids": [str(offer.id)]},
        )
        with patch("app.routers.htmx_views.templates") as mock_tpl:
            mock_tpl.TemplateResponse.return_value = HTMLResponse("quote OK")
            result = await create_quote_from_offers(request=mock_req, req_id=req.id, user=test_user, db=db_session)
        assert result.status_code == 200

    async def test_creates_quote_with_multiple_offers(self, db_session: Session, test_user: User):
        """Multiple offers create multiple QuoteLines."""
        from app.routers.htmx_views import create_quote_from_offers

        req = _make_req(db_session, test_user)
        o1 = _make_offer(db_session, req, test_user, "BC547")
        o2 = _make_offer(db_session, req, test_user, "LM317T")
        mock_req = _mock_form_request(
            path=f"/v2/partials/requisitions/{req.id}/create-quote",
            fields={"offer_ids": [str(o1.id), str(o2.id)]},
        )
        with patch("app.routers.htmx_views.templates") as mock_tpl:
            mock_tpl.TemplateResponse.return_value = HTMLResponse("quote OK")
            result = await create_quote_from_offers(request=mock_req, req_id=req.id, user=test_user, db=db_session)
        assert result.status_code == 200

    async def test_no_offer_ids_raises_400(self, db_session: Session, test_user: User):
        """Lines 1913–1914: empty offer_ids → HTTPException 400."""
        from fastapi import HTTPException

        from app.routers.htmx_views import create_quote_from_offers

        req = _make_req(db_session, test_user)
        mock_req = _mock_form_request(fields={"offer_ids": []})
        with pytest.raises(HTTPException) as exc_info:
            await create_quote_from_offers(request=mock_req, req_id=req.id, user=test_user, db=db_session)
        assert exc_info.value.status_code == 400

    async def test_offer_not_on_requisition_raises_404(self, db_session: Session, test_user: User):
        """Lines 1919–1920: offer not on this requisition → 404."""
        from fastapi import HTTPException

        from app.routers.htmx_views import create_quote_from_offers

        req = _make_req(db_session, test_user)
        # Offer ID 99999 doesn't exist
        mock_req = _mock_form_request(fields={"offer_ids": ["99999"]})
        with pytest.raises(HTTPException) as exc_info:
            await create_quote_from_offers(request=mock_req, req_id=req.id, user=test_user, db=db_session)
        assert exc_info.value.status_code == 404


# ── review_offer (1987–2010) ─────────────────────────────────────────────


class TestReviewOfferDirect:
    async def test_approve_offer(self, db_session: Session, test_user: User):
        """Lines 1987–2010: approve action transitions offer to APPROVED."""
        from app.routers.htmx_views import review_offer

        req = _make_req(db_session, test_user)
        offer = _make_offer(db_session, req, test_user)
        # PENDING_REVIEW → APPROVED is a valid transition (ACTIVE → APPROVED is not)
        offer.status = OfferStatus.PENDING_REVIEW
        db_session.commit()
        mock_req = _mock_form_request(
            path=f"/v2/partials/requisitions/{req.id}/offers/{offer.id}/review",
            fields={"action": "approve"},
        )
        with patch("app.routers.htmx_views.requisition_tab", new_callable=AsyncMock) as mock_tab:
            mock_tab.return_value = HTMLResponse("tab OK")
            result = await review_offer(
                request=mock_req,
                req_id=req.id,
                offer_id=offer.id,
                user=test_user,
                db=db_session,
            )
        assert result.status_code == 200
        db_session.refresh(offer)
        assert offer.status == OfferStatus.APPROVED

    async def test_reject_offer(self, db_session: Session, test_user: User):
        """Lines 2002–2005: reject action transitions offer to REJECTED."""
        from app.routers.htmx_views import review_offer

        req = _make_req(db_session, test_user)
        offer = _make_offer(db_session, req, test_user)
        mock_req = _mock_form_request(fields={"action": "reject"})
        with patch("app.routers.htmx_views.requisition_tab", new_callable=AsyncMock) as mock_tab:
            mock_tab.return_value = HTMLResponse("tab OK")
            result = await review_offer(
                request=mock_req,
                req_id=req.id,
                offer_id=offer.id,
                user=test_user,
                db=db_session,
            )
        assert result.status_code == 200
        db_session.refresh(offer)
        assert offer.status == OfferStatus.REJECTED

    async def test_invalid_action_raises_400(self, db_session: Session, test_user: User):
        """Lines 1989–1990: invalid action → 400."""
        from fastapi import HTTPException

        from app.routers.htmx_views import review_offer

        req = _make_req(db_session, test_user)
        offer = _make_offer(db_session, req, test_user)
        mock_req = _mock_form_request(fields={"action": "invalid"})
        with pytest.raises(HTTPException) as exc_info:
            await review_offer(
                request=mock_req,
                req_id=req.id,
                offer_id=offer.id,
                user=test_user,
                db=db_session,
            )
        assert exc_info.value.status_code == 400


# ── update_requirement (2881–2962) ───────────────────────────────────────


class TestUpdateRequirementDirect:
    async def test_update_requirement_basic(self, db_session: Session, test_user: User):
        """Lines 2881–2962: updates requirement fields, skips auto-search in TESTING."""
        from app.routers.htmx_views import update_requirement

        req = _make_req(db_session, test_user)
        item = db_session.query(Requirement).filter(Requirement.requisition_id == req.id).first()
        mock_req = _mock_form_request(
            path=f"/v2/partials/requisitions/{req.id}/requirements/{item.id}",
            fields={"sub_mpn": ["LM741"], "sub_manufacturer": ["TI"]},
        )
        with patch("app.routers.htmx_views.templates") as mock_tpl:
            mock_tpl.TemplateResponse.return_value = HTMLResponse("row OK")
            result = await update_requirement(
                request=mock_req,
                req_id=req.id,
                item_id=item.id,
                background_tasks=BackgroundTasks(),
                primary_mpn="BC547",
                manufacturer="Fairchild",
                target_qty=200,
                brand="",
                target_price=None,
                substitutes="",
                customer_pn="CP-001",
                need_by_date="",
                condition="new",
                date_codes="",
                firmware="",
                hardware_codes="",
                packaging="",
                notes="Test update",
                user=test_user,
                db=db_session,
            )
        assert result.status_code == 200
        db_session.refresh(item)
        assert item.manufacturer == "Fairchild"

    async def test_update_requirement_empty_manufacturer_raises_422(self, db_session: Session, test_user: User):
        """Lines 2885–2886: empty manufacturer → HTTPException 422."""
        from fastapi import HTTPException

        from app.routers.htmx_views import update_requirement

        req = _make_req(db_session, test_user)
        item = db_session.query(Requirement).filter(Requirement.requisition_id == req.id).first()
        mock_req = _mock_form_request(fields={"sub_mpn": [], "sub_manufacturer": []})
        with pytest.raises(HTTPException) as exc_info:
            await update_requirement(
                request=mock_req,
                req_id=req.id,
                item_id=item.id,
                background_tasks=BackgroundTasks(),
                primary_mpn="BC547",
                manufacturer="  ",  # empty after strip
                target_qty=100,
                brand="",
                target_price=None,
                substitutes="",
                customer_pn="",
                need_by_date="",
                condition="",
                date_codes="",
                firmware="",
                hardware_codes="",
                packaging="",
                notes="",
                user=test_user,
                db=db_session,
            )
        assert exc_info.value.status_code == 422

    async def test_update_requirement_item_not_found_raises_404(self, db_session: Session, test_user: User):
        """Lines 2890–2891: requirement not found → 404."""
        from fastapi import HTTPException

        from app.routers.htmx_views import update_requirement

        req = _make_req(db_session, test_user)
        mock_req = _mock_form_request(fields={"sub_mpn": [], "sub_manufacturer": []})
        with pytest.raises(HTTPException) as exc_info:
            await update_requirement(
                request=mock_req,
                req_id=req.id,
                item_id=99999,
                background_tasks=BackgroundTasks(),
                primary_mpn="BC547",
                manufacturer="TI",
                target_qty=100,
                brand="",
                target_price=None,
                substitutes="",
                customer_pn="",
                need_by_date="",
                condition="",
                date_codes="",
                firmware="",
                hardware_codes="",
                packaging="",
                notes="",
                user=test_user,
                db=db_session,
            )
        assert exc_info.value.status_code == 404

    async def test_update_requirement_with_need_by_date(self, db_session: Session, test_user: User):
        """Lines 2920–2926: need_by_date parsing branch."""
        from app.routers.htmx_views import update_requirement

        req = _make_req(db_session, test_user)
        item = db_session.query(Requirement).filter(Requirement.requisition_id == req.id).first()
        mock_req = _mock_form_request(fields={"sub_mpn": [], "sub_manufacturer": []})
        with patch("app.routers.htmx_views.templates") as mock_tpl:
            mock_tpl.TemplateResponse.return_value = HTMLResponse("row OK")
            result = await update_requirement(
                request=mock_req,
                req_id=req.id,
                item_id=item.id,
                background_tasks=BackgroundTasks(),
                primary_mpn="BC547",
                manufacturer="TI",
                target_qty=100,
                brand="",
                target_price=1.50,
                substitutes="",
                customer_pn="",
                need_by_date="2025-06-30",  # valid date
                condition="",
                date_codes="",
                firmware="",
                hardware_codes="",
                packaging="",
                notes="",
                user=test_user,
                db=db_session,
            )
        assert result.status_code == 200


# ── add_offers_to_draft_quote (7683–7733) ────────────────────────────────


class TestAddOffersToDraftQuoteDirect:
    async def test_add_offers_to_draft_quote(self, db_session: Session, test_user: User):
        """Lines 7683–7733: parses body, creates QuoteLines."""
        from app.routers.htmx_views import add_offers_to_draft_quote

        req = _make_req(db_session, test_user)
        offer = _make_offer(db_session, req, test_user)
        quote = _make_draft_quote(db_session, req, test_user)

        import json

        body = json.dumps({"offer_ids": [offer.id], "quote_id": quote.id}).encode()
        mock_req = _mock_body_request(
            path=f"/v2/partials/requisitions/{req.id}/add-offers-to-quote",
            body_bytes=body,
        )
        result = await add_offers_to_draft_quote(request=mock_req, req_id=req.id, user=test_user, db=db_session)
        assert result.status_code == 200

    async def test_add_offers_invalid_json_raises_400(self, db_session: Session, test_user: User):
        """Lines 7685–7686: invalid JSON body → HTTPException 400."""
        from fastapi import HTTPException

        from app.routers.htmx_views import add_offers_to_draft_quote

        req = _make_req(db_session, test_user)
        mock_req = _mock_body_request(body_bytes=b"not json")
        with pytest.raises(HTTPException) as exc_info:
            await add_offers_to_draft_quote(request=mock_req, req_id=req.id, user=test_user, db=db_session)
        assert exc_info.value.status_code == 400

    async def test_add_offers_empty_list_raises_400(self, db_session: Session, test_user: User):
        """Lines 7694–7695: empty offer_ids or missing quote_id → 400."""
        import json

        from fastapi import HTTPException

        from app.routers.htmx_views import add_offers_to_draft_quote

        req = _make_req(db_session, test_user)
        body = json.dumps({"offer_ids": [], "quote_id": 0}).encode()
        mock_req = _mock_body_request(body_bytes=body)
        with pytest.raises(HTTPException) as exc_info:
            await add_offers_to_draft_quote(request=mock_req, req_id=req.id, user=test_user, db=db_session)
        assert exc_info.value.status_code == 400

    async def test_add_offers_non_draft_quote_raises_400(self, db_session: Session, test_user: User):
        """Lines 7700–7701: non-draft quote → 400."""
        import json

        from fastapi import HTTPException

        from app.routers.htmx_views import add_offers_to_draft_quote

        req = _make_req(db_session, test_user)
        offer = _make_offer(db_session, req, test_user)
        # Create a non-draft quote
        quote = Quote(
            requisition_id=req.id,
            quote_number=f"Q-{uuid.uuid4().hex[:8]}",
            status=QuoteStatus.SENT,  # Not draft
            created_by_id=test_user.id,
        )
        db_session.add(quote)
        db_session.commit()
        db_session.refresh(quote)

        body = json.dumps({"offer_ids": [offer.id], "quote_id": quote.id}).encode()
        mock_req = _mock_body_request(body_bytes=body)
        with pytest.raises(HTTPException) as exc_info:
            await add_offers_to_draft_quote(request=mock_req, req_id=req.id, user=test_user, db=db_session)
        assert exc_info.value.status_code == 400


# ── buy_plan_verify_po (6136–6148) ───────────────────────────────────────


class TestBuyPlanVerifyPoDirect:
    async def test_buy_plan_verify_po_success(self, db_session: Session, test_user: User):
        """Lines 6136–6148: verify PO on buy plan line."""
        from app.routers.htmx_views import buy_plan_verify_po_partial

        req = _make_req(db_session, test_user)
        bp = _make_buy_plan(db_session, req, test_user)
        mock_req = _mock_form_request(
            path=f"/v2/partials/buy-plans/{bp.id}/lines/1/verify-po",
            fields={"action": "approve", "rejection_note": ""},
        )
        with (
            patch("app.services.buyplan_workflow.verify_po"),
            patch("app.services.buyplan_workflow.check_completion", return_value=None),
            patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock),
            patch("app.routers.htmx_views.buy_plan_detail_partial", new_callable=AsyncMock) as mock_detail,
        ):
            mock_detail.return_value = HTMLResponse("bp detail")
            result = await buy_plan_verify_po_partial(
                request=mock_req, plan_id=bp.id, line_id=1, user=test_user, db=db_session
            )
        assert result.status_code == 200


# ── buy_plan_flag_issue (6162–6172) ──────────────────────────────────────


class TestBuyPlanFlagIssueDirect:
    async def test_buy_plan_flag_issue(self, db_session: Session, test_user: User):
        """Lines 6162–6172: flag issue on a buy plan line."""
        from app.routers.htmx_views import buy_plan_flag_issue_partial

        req = _make_req(db_session, test_user)
        bp = _make_buy_plan(db_session, req, test_user)
        mock_req = _mock_form_request(
            path=f"/v2/partials/buy-plans/{bp.id}/lines/1/issue",
            fields={"issue_type": "damaged", "note": "Package arrived damaged"},
        )
        with (
            patch("app.services.buyplan_workflow.flag_line_issue"),
            patch("app.routers.htmx_views.buy_plan_detail_partial", new_callable=AsyncMock) as mock_detail,
        ):
            mock_detail.return_value = HTMLResponse("bp detail")
            result = await buy_plan_flag_issue_partial(
                request=mock_req, plan_id=bp.id, line_id=1, user=test_user, db=db_session
            )
        assert result.status_code == 200


# ── buy_plan_cancel (6191–6197) ──────────────────────────────────────────


class TestBuyPlanCancelDirect:
    async def test_buy_plan_cancel_success(self, db_session: Session, test_user: User):
        """Lines 6191–6197: cancel a buy plan."""
        from app.routers.htmx_views import buy_plan_cancel_partial

        req = _make_req(db_session, test_user)
        bp = _make_buy_plan(db_session, req, test_user)
        form_mock = MagicMock()
        form_mock.get = lambda key, default=None: {"reason": "test cancel"}.get(key, default)
        mock_req = MagicMock(spec=Request)
        mock_req.url.path = f"/v2/partials/buy-plans/{bp.id}/cancel"
        mock_req.headers = {}
        mock_req.form = AsyncMock(return_value=form_mock)
        with patch("app.routers.htmx_views.buy_plan_detail_partial", new_callable=AsyncMock) as mock_detail:
            mock_detail.return_value = HTMLResponse("bp detail")
            result = await buy_plan_cancel_partial(request=mock_req, plan_id=bp.id, user=test_user, db=db_session)
        assert result.status_code == 200

    async def test_buy_plan_cancel_not_found(self, db_session: Session, test_user: User):
        """buy_plan not found → 404."""
        from fastapi import HTTPException

        from app.routers.htmx_views import buy_plan_cancel_partial

        mock_req = MagicMock(spec=Request)
        mock_req.url.path = "/v2/partials/buy-plans/99999/cancel"
        mock_req.headers = {}
        with pytest.raises(HTTPException) as exc_info:
            await buy_plan_cancel_partial(request=mock_req, plan_id=99999, user=test_user, db=db_session)
        assert exc_info.value.status_code == 404
