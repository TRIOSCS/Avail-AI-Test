"""tests/test_htmx_views_nightly27.py — Direct-async coverage for htmx_views.py batch 7.

Target line ranges (small, targeted):
  - edit_offer ValueError branches  2179–2192  (~9 lines) invalid qty/price/date
  - edit_offer requirement_id        2208        (1 line)
  - rfq_compose with vendors         2448–2454   (~5 lines)
  - review_response_htmx             2803–2815   (~13 lines)

Called by: pytest autodiscovery (asyncio_mode = auto)
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import HTMLResponse

from app.constants import OfferStatus
from app.models import Requisition, User
from app.models.offers import Offer, VendorResponse

# ── Helpers ───────────────────────────────────────────────────────────────


def _mock_form_request(path: str = "/v2/test", fields: dict | None = None) -> MagicMock:
    mock_req = MagicMock(spec=Request)
    mock_req.url.path = path
    mock_req.headers = {}
    mock_req.query_params = MagicMock()
    mock_req.query_params.get = lambda k, d=None: d
    form_mock = MagicMock()
    _fields = fields or {}
    form_mock.get = lambda key, default=None: _fields.get(key, default)
    form_mock.__getitem__ = lambda self, key: _fields[key]
    form_mock.__contains__ = lambda self, key: key in _fields
    form_mock.getlist = lambda key: (
        _fields[key] if isinstance(_fields.get(key), list) else ([_fields[key]] if key in _fields else [])
    )
    mock_req.form = AsyncMock(return_value=form_mock)
    return mock_req


def _make_req(db: Session, user: User) -> Requisition:
    req = Requisition(name="N27 Test Req", status="active", created_by=user.id)
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


def _make_offer(db: Session, req: Requisition, user: User) -> Offer:
    o = Offer(
        requisition_id=req.id,
        vendor_name="VendN27",
        mpn="LM317T",
        unit_price=1.00,
        qty_available=100,
        status=OfferStatus.ACTIVE,
        source="manual",
        entered_by_id=user.id,
    )
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


def _make_vendor_response(db: Session, req: Requisition) -> VendorResponse:
    vr = VendorResponse(
        requisition_id=req.id,
        vendor_name="VendN27",
        vendor_email="vend@n27.com",
        status="new",
    )
    db.add(vr)
    db.commit()
    db.refresh(vr)
    return vr


# ── edit_offer ValueError branches (2179–2192) ───────────────────────────────


class TestEditOfferValueErrorBranchesDirect:
    """Cover the try/except ValueError continue branches in edit_offer."""

    async def test_edit_offer_invalid_qty_continues(self, db_session: Session, test_user: User):
        """Lines 2179–2180: invalid qty_available → ValueError caught, field skipped."""
        from app.routers.htmx_views import edit_offer

        req = _make_req(db_session, test_user)
        offer = _make_offer(db_session, req, test_user)

        # Pass invalid (non-integer) value for qty_available
        mock_req = _mock_form_request(
            fields={
                "qty_available": "not-a-number",
                "vendor_name": "UpdatedVend",
            }
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
        # qty should NOT have changed
        db_session.refresh(offer)
        assert offer.qty_available == 100

    async def test_edit_offer_invalid_unit_price_continues(self, db_session: Session, test_user: User):
        """Lines 2184–2185: invalid unit_price → ValueError caught, field skipped."""
        from app.routers.htmx_views import edit_offer

        req = _make_req(db_session, test_user)
        offer = _make_offer(db_session, req, test_user)
        mock_req = _mock_form_request(fields={"unit_price": "bad-price", "vendor_name": "V2"})
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
        assert float(offer.unit_price) == 1.00  # unchanged

    async def test_edit_offer_invalid_valid_until_continues(self, db_session: Session, test_user: User):
        """Lines 2187–2192: invalid valid_until date → ValueError caught, field skipped."""
        from app.routers.htmx_views import edit_offer

        req = _make_req(db_session, test_user)
        offer = _make_offer(db_session, req, test_user)
        mock_req = _mock_form_request(fields={"valid_until": "not-a-date", "vendor_name": "V3"})
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

    async def test_edit_offer_with_requirement_id(self, db_session: Session, test_user: User):
        """Line 2208: requirement_id form field sets offer.requirement_id."""
        from app.models.sourcing import Requirement
        from app.routers.htmx_views import edit_offer

        req = _make_req(db_session, test_user)
        item = Requirement(
            requisition_id=req.id,
            primary_mpn="LM317T",
            manufacturer="TI",
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)

        offer = _make_offer(db_session, req, test_user)
        mock_req = _mock_form_request(fields={"requirement_id": str(item.id), "vendor_name": "Vend"})
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
        assert offer.requirement_id == item.id


# ── review_response_htmx (2803–2815) ─────────────────────────────────────────


class TestReviewResponseHtmxDirect:
    async def test_mark_reviewed_success(self, db_session: Session, test_user: User):
        """Lines 2803–2815: POST status=reviewed → vr.status = 'reviewed'."""
        from app.routers.htmx_views import review_response_htmx

        req = _make_req(db_session, test_user)
        vr = _make_vendor_response(db_session, req)
        mock_req = _mock_form_request(fields={"status": "reviewed"})
        with patch("app.routers.htmx_views.templates") as mock_tpl:
            mock_tpl.TemplateResponse.return_value = HTMLResponse("response card")
            result = await review_response_htmx(
                request=mock_req,
                req_id=req.id,
                response_id=vr.id,
                user=test_user,
                db=db_session,
            )
        assert result.status_code == 200
        db_session.refresh(vr)
        assert vr.status == "reviewed"

    async def test_mark_rejected_success(self, db_session: Session, test_user: User):
        """POST status=rejected → vr.status = 'rejected'."""
        from app.routers.htmx_views import review_response_htmx

        req = _make_req(db_session, test_user)
        vr = _make_vendor_response(db_session, req)
        mock_req = _mock_form_request(fields={"status": "rejected"})
        with patch("app.routers.htmx_views.templates") as mock_tpl:
            mock_tpl.TemplateResponse.return_value = HTMLResponse("response card")
            result = await review_response_htmx(
                request=mock_req,
                req_id=req.id,
                response_id=vr.id,
                user=test_user,
                db=db_session,
            )
        assert result.status_code == 200
        db_session.refresh(vr)
        assert vr.status == "rejected"

    async def test_invalid_status_raises_400(self, db_session: Session, test_user: User):
        """invalid status → 400."""
        from fastapi import HTTPException

        from app.routers.htmx_views import review_response_htmx

        req = _make_req(db_session, test_user)
        vr = _make_vendor_response(db_session, req)
        mock_req = _mock_form_request(fields={"status": "bogus"})
        with pytest.raises(HTTPException) as exc_info:
            await review_response_htmx(
                request=mock_req,
                req_id=req.id,
                response_id=vr.id,
                user=test_user,
                db=db_session,
            )
        assert exc_info.value.status_code == 400

    async def test_response_not_found_raises_404(self, db_session: Session, test_user: User):
        """non-existent response → 404."""
        from fastapi import HTTPException

        from app.routers.htmx_views import review_response_htmx

        req = _make_req(db_session, test_user)
        mock_req = _mock_form_request(fields={"status": "reviewed"})
        with pytest.raises(HTTPException) as exc_info:
            await review_response_htmx(
                request=mock_req,
                req_id=req.id,
                response_id=99999,
                user=test_user,
                db=db_session,
            )
        assert exc_info.value.status_code == 404
