"""tests/test_htmx_views_nightly25.py — Direct-async coverage for htmx_views.py batch 5.

Target line ranges:
  - promote_offer_htmx     2303–2318  (~16 lines)
  - reject_offer_htmx      2329–2342  (~14 lines)
  - offer_changelog        2353–2366  (~14 lines)
  - rfq_compose GET        2415–2470  (~56 lines)
  - ai_cleanup_email       2482–2511  (~30 lines)
  - send_follow_up_htmx    2712–2774  (~63 lines)
  - search_run             2995–3030  (~36 lines)
  - search_filter          3105–3131  (~27 lines)
  - requisition_picker     3285–3296  (~12 lines)
  - find_by_part_partial   3485–3562  (~78 lines)

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

from app.constants import ContactStatus, OfferStatus
from app.models import Requisition, User
from app.models.offers import Contact as RfqContact
from app.models.offers import Offer

# ── Helpers ────────────────────────────────────────────────────────────────


def _mock_form_request(path: str = "/v2/test", fields: dict | None = None) -> MagicMock:
    mock_req = MagicMock(spec=Request)
    mock_req.url.path = path
    mock_req.headers = {}
    mock_req.query_params = {}
    if fields is not None:
        form_mock = MagicMock()
        form_mock.get = lambda key, default=None: fields.get(key, default)
        form_mock.getlist = lambda key: (
            fields[key] if isinstance(fields.get(key), list) else ([fields[key]] if key in fields else [])
        )
        mock_req.form = AsyncMock(return_value=form_mock)
    return mock_req


def _mock_get_request(path: str = "/v2/test", query_params: dict | None = None) -> MagicMock:
    mock_req = MagicMock(spec=Request)
    mock_req.url.path = path
    mock_req.headers = {}
    qp = MagicMock()
    _qp = query_params or {}
    qp.get = lambda key, default=None: _qp.get(key, default)
    mock_req.query_params = qp
    return mock_req


def _make_req(db: Session, user: User) -> Requisition:
    req = Requisition(
        name="Nightly25 Test Req",
        status="active",
        created_by=user.id,
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


def _make_offer(db: Session, req: Requisition, user: User, status: str = OfferStatus.PENDING_REVIEW) -> Offer:
    o = Offer(
        requisition_id=req.id,
        vendor_name="AlphaElec",
        mpn="LM317T",
        unit_price=3.00,
        qty_available=200,
        status=status,
        source="manual",
        entered_by_id=user.id,
    )
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


def _make_rfq_contact(db: Session, req: Requisition, user: User) -> RfqContact:
    c = RfqContact(
        requisition_id=req.id,
        user_id=user.id,
        contact_type="rfq",
        vendor_name="AlphaElec",
        vendor_name_normalized="alphaelec",
        status=ContactStatus.SENT,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


# ── promote_offer_htmx (2303–2318) ──────────────────────────────────────────


class TestPromoteOfferHtmxDirect:
    async def test_promote_offer_success(self, db_session: Session, test_user: User):
        """Lines 2303–2318: promote pending_review → active."""
        from app.routers.htmx_views import promote_offer_htmx

        req = _make_req(db_session, test_user)
        offer = _make_offer(db_session, req, test_user, OfferStatus.PENDING_REVIEW)
        mock_req = _mock_get_request()
        with patch("app.routers.htmx_views.offer_review_queue", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = HTMLResponse("queue")
            result = await promote_offer_htmx(request=mock_req, offer_id=offer.id, user=test_user, db=db_session)
        assert result.status_code == 200
        db_session.refresh(offer)
        assert offer.status == OfferStatus.ACTIVE

    async def test_promote_offer_not_found(self, db_session: Session, test_user: User):
        """Promote non-existent → 404."""
        from fastapi import HTTPException

        from app.routers.htmx_views import promote_offer_htmx

        mock_req = _mock_get_request()
        with pytest.raises(HTTPException) as exc_info:
            await promote_offer_htmx(request=mock_req, offer_id=99999, user=test_user, db=db_session)
        assert exc_info.value.status_code == 404

    async def test_promote_offer_wrong_status_raises_400(self, db_session: Session, test_user: User):
        """Promote active offer → 400 (only pending_review can be promoted)."""
        from fastapi import HTTPException

        from app.routers.htmx_views import promote_offer_htmx

        req = _make_req(db_session, test_user)
        offer = _make_offer(db_session, req, test_user, OfferStatus.ACTIVE)
        mock_req = _mock_get_request()
        with pytest.raises(HTTPException) as exc_info:
            await promote_offer_htmx(request=mock_req, offer_id=offer.id, user=test_user, db=db_session)
        assert exc_info.value.status_code == 400


# ── reject_offer_htmx (2329–2342) ───────────────────────────────────────────


class TestRejectOfferHtmxDirect:
    async def test_reject_offer_success(self, db_session: Session, test_user: User):
        """Lines 2329–2342: reject pending_review → rejected."""
        from app.routers.htmx_views import reject_offer_htmx

        req = _make_req(db_session, test_user)
        offer = _make_offer(db_session, req, test_user, OfferStatus.PENDING_REVIEW)
        mock_req = _mock_get_request()
        with patch("app.routers.htmx_views.offer_review_queue", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = HTMLResponse("queue")
            result = await reject_offer_htmx(request=mock_req, offer_id=offer.id, user=test_user, db=db_session)
        assert result.status_code == 200
        db_session.refresh(offer)
        assert offer.status == OfferStatus.REJECTED

    async def test_reject_offer_not_found(self, db_session: Session, test_user: User):
        """Reject non-existent → 404."""
        from fastapi import HTTPException

        from app.routers.htmx_views import reject_offer_htmx

        mock_req = _mock_get_request()
        with pytest.raises(HTTPException) as exc_info:
            await reject_offer_htmx(request=mock_req, offer_id=99999, user=test_user, db=db_session)
        assert exc_info.value.status_code == 404


# ── offer_changelog (2353–2366) ─────────────────────────────────────────────


class TestOfferChangelogDirect:
    async def test_offer_changelog_success(self, db_session: Session, test_user: User):
        """Lines 2353–2366: render change history for an offer."""
        from app.routers.htmx_views import offer_changelog

        req = _make_req(db_session, test_user)
        offer = _make_offer(db_session, req, test_user)
        mock_req = _mock_get_request(f"/v2/partials/offers/{offer.id}/changelog")
        with patch("app.routers.htmx_views.templates") as mock_tpl:
            mock_tpl.TemplateResponse.return_value = HTMLResponse("changelog")
            result = await offer_changelog(request=mock_req, offer_id=offer.id, user=test_user, db=db_session)
        assert result.status_code == 200

    async def test_offer_changelog_not_found(self, db_session: Session, test_user: User):
        """Non-existent offer → 404."""
        from fastapi import HTTPException

        from app.routers.htmx_views import offer_changelog

        mock_req = _mock_get_request()
        with pytest.raises(HTTPException) as exc_info:
            await offer_changelog(request=mock_req, offer_id=99999, user=test_user, db=db_session)
        assert exc_info.value.status_code == 404


# ── rfq_compose GET (2415–2470) ──────────────────────────────────────────────


class TestRfqComposeGetDirect:
    async def test_rfq_compose_no_parts(self, db_session: Session, test_user: User):
        """Lines 2415–2470: GET rfq_compose with no parts."""
        from app.routers.htmx_views import rfq_compose

        req = _make_req(db_session, test_user)
        mock_req = _mock_get_request(f"/v2/partials/requisitions/{req.id}/rfq-compose")
        with patch("app.routers.htmx_views.templates") as mock_tpl:
            mock_tpl.TemplateResponse.return_value = HTMLResponse("rfq compose")
            result = await rfq_compose(request=mock_req, req_id=req.id, user=test_user, db=db_session)
        assert result.status_code == 200

    async def test_rfq_compose_not_found(self, db_session: Session, test_user: User):
        """Non-existent req → 404."""
        from fastapi import HTTPException

        from app.routers.htmx_views import rfq_compose

        mock_req = _mock_get_request()
        with pytest.raises(HTTPException) as exc_info:
            await rfq_compose(request=mock_req, req_id=99999, user=test_user, db=db_session)
        assert exc_info.value.status_code == 404


# ── ai_cleanup_email (2482–2511) ─────────────────────────────────────────────


class TestAiCleanupEmailDirect:
    async def test_cleanup_empty_body_returns_warning(self, db_session: Session, test_user: User):
        """Lines 2485–2486: empty body → amber warning HTML."""
        from app.routers.htmx_views import ai_cleanup_email

        req = _make_req(db_session, test_user)
        mock_req = _mock_get_request()
        result = await ai_cleanup_email(request=mock_req, req_id=req.id, body="   ", user=test_user, db=db_session)
        assert result.status_code == 200
        assert b"Write your email first" in result.body

    async def test_cleanup_with_body_calls_claude(self, db_session: Session, test_user: User):
        """Lines 2488–2513: non-empty body → calls claude_text, returns script."""
        from app.routers.htmx_views import ai_cleanup_email

        req = _make_req(db_session, test_user)
        mock_req = _mock_get_request()
        with patch("app.utils.claude_client.claude_text", new_callable=AsyncMock) as mock_claude:
            mock_claude.return_value = "Cleaned up email text."
            result = await ai_cleanup_email(
                request=mock_req,
                req_id=req.id,
                body="pls send rfq for parts",
                user=test_user,
                db=db_session,
            )
        assert result.status_code == 200
        assert b"Cleaned up email text" in result.body

    async def test_cleanup_claude_error_returns_original(self, db_session: Session, test_user: User):
        """Lines 2505–2507: claude error → returns original text."""
        from app.routers.htmx_views import ai_cleanup_email

        req = _make_req(db_session, test_user)
        mock_req = _mock_get_request()
        with patch("app.utils.claude_client.claude_text", new_callable=AsyncMock) as mock_claude:
            mock_claude.side_effect = RuntimeError("API down")
            result = await ai_cleanup_email(
                request=mock_req,
                req_id=req.id,
                body="original text",
                user=test_user,
                db=db_session,
            )
        assert result.status_code == 200
        assert b"original text" in result.body


# ── send_follow_up_htmx (2712–2774) ─────────────────────────────────────────


class TestSendFollowUpHtmxDirect:
    async def test_send_follow_up_testing_mode(self, db_session: Session, test_user: User):
        """Lines 2712–2774: TESTING=1 → marks contact SENT without real email."""
        from app.routers.htmx_views import send_follow_up_htmx

        req = _make_req(db_session, test_user)
        contact = _make_rfq_contact(db_session, req, test_user)
        mock_req = _mock_form_request(
            path=f"/v2/partials/follow-ups/{contact.id}/send",
            fields={"body": "Follow-up message"},
        )
        with patch("app.routers.htmx_views.templates") as mock_tpl:
            mock_tpl.TemplateResponse.return_value = HTMLResponse("sent OK")
            result = await send_follow_up_htmx(request=mock_req, contact_id=contact.id, user=test_user, db=db_session)
        assert result.status_code == 200
        db_session.refresh(contact)
        assert contact.status == ContactStatus.SENT

    async def test_send_follow_up_not_found(self, db_session: Session, test_user: User):
        """Non-existent contact → 404."""
        from fastapi import HTTPException

        from app.routers.htmx_views import send_follow_up_htmx

        mock_req = _mock_form_request(fields={"body": ""})
        with pytest.raises(HTTPException) as exc_info:
            await send_follow_up_htmx(request=mock_req, contact_id=99999, user=test_user, db=db_session)
        assert exc_info.value.status_code == 404


# ── search_run (2995–3030) ────────────────────────────────────────────────────


class TestSearchRunDirect:
    async def test_search_run_with_mpn(self, db_session: Session, test_user: User):
        """Lines 2995–3030: POST with mpn triggers streaming search."""
        from app.routers.htmx_views import search_run

        mock_req = _mock_get_request("/v2/partials/search/run")
        mock_req.query_params = MagicMock()
        mock_req.query_params.get = lambda k, d=None: d
        with (
            patch("app.routers.htmx_views._get_enabled_sources", return_value=[]) as _src,
            patch("app.routers.htmx_views.templates") as mock_tpl,
            patch("app.utils.async_helpers.safe_background_task", new_callable=AsyncMock),
        ):
            mock_tpl.TemplateResponse.return_value = HTMLResponse("results shell")
            result = await search_run(
                request=mock_req,
                mpn="LM317T",
                requirement_id=0,
                user=test_user,
                db=db_session,
            )
        assert result.status_code == 200

    async def test_search_run_empty_mpn_returns_error(self, db_session: Session, test_user: User):
        """Lines 3011–3012: empty mpn → error HTML."""
        from app.routers.htmx_views import search_run

        mock_req = _mock_get_request("/v2/partials/search/run")
        mock_req.query_params = MagicMock()
        mock_req.query_params.get = lambda k, d=None: d
        result = await search_run(
            request=mock_req,
            mpn="",
            requirement_id=0,
            user=test_user,
            db=db_session,
        )
        assert result.status_code == 200
        assert b"Please enter a part number" in result.body


# ── search_filter (3105–3131) ──────────────────────────────────────────────────


class TestSearchFilterDirect:
    async def test_search_filter_cache_miss(self, db_session: Session, test_user: User):
        """Lines 3105–3107: cache miss → expired message."""
        from app.routers.htmx_views import search_filter

        mock_req = _mock_get_request()
        with patch("app.routers.htmx_views._get_cached_search_results", return_value=None):
            result = await search_filter(
                request=mock_req,
                search_id="nonexistent-id",
                confidence="all",
                source="all",
                sort="best",
                user=test_user,
                db=db_session,
            )
        assert result.status_code == 200
        assert b"Search results expired" in result.body

    async def test_search_filter_with_results(self, db_session: Session, test_user: User):
        """Lines 3109–3131: filter + sort applied to cached results."""
        from app.routers.htmx_views import search_filter

        cached = [
            {
                "vendor_name": "VendA",
                "confidence_color": "green",
                "sources_found": ["brokerbin"],
                "unit_price": 1.5,
                "qty_available": 100,
                "score": 80,
                "confidence_pct": 90,
            },
            {
                "vendor_name": "VendB",
                "confidence_color": "amber",
                "sources_found": ["nexar"],
                "unit_price": 0.5,
                "qty_available": 500,
                "score": 60,
                "confidence_pct": 65,
            },
        ]
        mock_req = _mock_get_request()
        card_tpl = MagicMock()
        card_tpl.render.return_value = "<div>card</div>"
        with (
            patch("app.routers.htmx_views._get_cached_search_results", return_value=cached),
            patch("app.routers.htmx_views.templates") as mock_tpl,
        ):
            mock_tpl.get_template.return_value = card_tpl
            result = await search_filter(
                request=mock_req,
                search_id="sid-123",
                confidence="all",
                source="all",
                sort="cheapest",
                user=test_user,
                db=db_session,
            )
        assert result.status_code == 200


# ── requisition_picker (3285–3296) ────────────────────────────────────────────


class TestRequisitionPickerDirect:
    async def test_requisition_picker_renders(self, db_session: Session, test_user: User):
        """Lines 3285–3296: GET renders picker modal."""
        from app.routers.htmx_views import requisition_picker

        req = _make_req(db_session, test_user)
        mock_req = _mock_get_request("/v2/partials/search/requisition-picker")
        with patch("app.routers.htmx_views.templates") as mock_tpl:
            mock_tpl.TemplateResponse.return_value = HTMLResponse("picker")
            result = await requisition_picker(
                request=mock_req,
                mpn="LM317T",
                items="[]",
                action="add",
                user=test_user,
                db=db_session,
            )
        assert result.status_code == 200


# ── find_by_part_partial (3485–3562) ──────────────────────────────────────────


class TestFindByPartPartialDirect:
    async def test_find_by_part_no_mpn(self, db_session: Session, test_user: User):
        """Lines 3490–3491: blank mpn → no results, renders template."""
        from app.routers.htmx_views import find_by_part_partial

        mock_req = _mock_get_request("/v2/partials/vendors/find-by-part")
        with patch("app.routers.htmx_views.templates") as mock_tpl:
            mock_tpl.TemplateResponse.return_value = HTMLResponse("find by part")
            result = await find_by_part_partial(
                request=mock_req,
                mpn="",
                hx_target="#main-content",
                push_url_base="/v2/vendors",
                user=test_user,
                db=db_session,
            )
        assert result.status_code == 200

    async def test_find_by_part_with_mpn(self, db_session: Session, test_user: User):
        """Lines 3493–3561: valid mpn → MVH query, affinity fallback, renders
        template."""
        from app.routers.htmx_views import find_by_part_partial

        mock_req = _mock_get_request("/v2/partials/vendors/find-by-part")
        with (
            patch("app.routers.htmx_views.templates") as mock_tpl,
            patch("app.services.vendor_affinity_service.find_vendor_affinity", return_value=[]),
        ):
            mock_tpl.TemplateResponse.return_value = HTMLResponse("find by part mpn")
            result = await find_by_part_partial(
                request=mock_req,
                mpn="LM317T",
                hx_target="#main-content",
                push_url_base="/v2/vendors",
                user=test_user,
                db=db_session,
            )
        assert result.status_code == 200
