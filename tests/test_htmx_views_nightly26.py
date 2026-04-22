"""tests/test_htmx_views_nightly26.py — Direct-async coverage for htmx_views.py batch 6.

Target line ranges:
  - create_company          4384–4417  (~34 lines)
  - edit_company            5038–5057  (~20 lines)
  - edit_site               5074–5083  (~10 lines)
  - add_site_contact_note   5108–5125  (~18 lines)
  - edit_quote_metadata     5315–5328  (~14 lines)
  - buy_plan_confirm_po     6098–6120  (~23 lines)
  - update_material_card    7359–7380  (~22 lines)
  - update_quote_line       7456–7480  (~25 lines)
  - proactive_draft         8310–8386  (~77 lines)

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

from app.models import Requisition, User
from app.models.crm import Company, CustomerSite, SiteContact
from app.models.intelligence import MaterialCard
from app.models.quotes import Quote, QuoteLine

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
    req = Requisition(name="N26 Test Req", status="active", created_by=user.id)
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


def _make_company(db: Session) -> Company:
    co = Company(name="TestCo N26", is_active=True)
    db.add(co)
    db.commit()
    db.refresh(co)
    return co


def _make_site(db: Session, company: Company) -> CustomerSite:
    site = CustomerSite(company_id=company.id, site_name="HQ", is_active=True)
    db.add(site)
    db.commit()
    db.refresh(site)
    return site


def _make_site_contact(db: Session, site: CustomerSite) -> SiteContact:
    contact = SiteContact(
        customer_site_id=site.id,
        full_name="Jane Tester",
        email="jane@testco.com",
        is_active=True,
    )
    db.add(contact)
    db.commit()
    db.refresh(contact)
    return contact


def _make_quote(db: Session, req: Requisition, user: User) -> Quote:
    q = Quote(
        requisition_id=req.id,
        quote_number="Q-N26-0001",
        created_by_id=user.id,
    )
    db.add(q)
    db.commit()
    db.refresh(q)
    return q


def _make_quote_line(db: Session, quote: Quote) -> QuoteLine:
    line = QuoteLine(
        quote_id=quote.id,
        mpn="BC547",
        manufacturer="Onsemi",
        qty=100,
        cost_price=0.10,
        sell_price=0.15,
    )
    db.add(line)
    db.commit()
    db.refresh(line)
    return line


def _make_material_card(db: Session) -> MaterialCard:
    card = MaterialCard(
        normalized_mpn="bc547",
        display_mpn="BC547",
        manufacturer="Onsemi",
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


# ── create_company (4384–4417) ───────────────────────────────────────────────


class TestCreateCompanyDirect:
    async def test_create_company_success(self, db_session: Session, test_user: User):
        """Lines 4384–4417: POST form creates Company + default site."""
        from app.routers.htmx_views import create_company

        mock_req = _mock_form_request(
            fields={"name": "NewCo N26", "website": "https://newco.com", "industry": "Electronics"}
        )
        with patch("app.routers.htmx_views.company_detail_partial", new_callable=AsyncMock) as mock_detail:
            mock_detail.return_value = HTMLResponse("company detail")
            result = await create_company(request=mock_req, user=test_user, db=db_session)
        assert result.status_code == 200
        # Company row created
        co = db_session.query(Company).filter(Company.name == "NewCo N26").first()
        assert co is not None

    async def test_create_company_empty_name_raises_400(self, db_session: Session, test_user: User):
        """empty name → 400."""
        from fastapi import HTTPException

        from app.routers.htmx_views import create_company

        mock_req = _mock_form_request(fields={"name": ""})
        with pytest.raises(HTTPException) as exc_info:
            await create_company(request=mock_req, user=test_user, db=db_session)
        assert exc_info.value.status_code == 400

    async def test_create_company_duplicate_raises_409(self, db_session: Session, test_user: User):
        """duplicate name → 409."""
        from fastapi import HTTPException

        from app.routers.htmx_views import create_company

        _make_company(db_session)  # creates "TestCo N26"
        mock_req = _mock_form_request(fields={"name": "TestCo N26"})
        with pytest.raises(HTTPException) as exc_info:
            await create_company(request=mock_req, user=test_user, db=db_session)
        assert exc_info.value.status_code == 409


# ── edit_company (5038–5057) ─────────────────────────────────────────────────


class TestEditCompanyDirect:
    async def test_edit_company_success(self, db_session: Session, test_user: User):
        """Lines 5038–5057: POST form updates Company fields."""
        from app.routers.htmx_views import edit_company

        company = _make_company(db_session)
        mock_req = _mock_form_request(
            fields={"name": "Updated N26", "website": "https://updated.com", "industry": "Tech"}
        )
        with patch("app.routers.htmx_views.company_detail_partial", new_callable=AsyncMock) as mock_detail:
            mock_detail.return_value = HTMLResponse("company detail")
            result = await edit_company(request=mock_req, company_id=company.id, user=test_user, db=db_session)
        assert result.status_code == 200
        db_session.refresh(company)
        assert company.name == "Updated N26"

    async def test_edit_company_not_found(self, db_session: Session, test_user: User):
        """non-existent company → 404."""
        from fastapi import HTTPException

        from app.routers.htmx_views import edit_company

        mock_req = _mock_form_request(fields={"name": "X"})
        with pytest.raises(HTTPException) as exc_info:
            await edit_company(request=mock_req, company_id=99999, user=test_user, db=db_session)
        assert exc_info.value.status_code == 404


# ── edit_site (5074–5083) ────────────────────────────────────────────────────


class TestEditSiteDirect:
    async def test_edit_site_success(self, db_session: Session, test_user: User):
        """Lines 5074–5083: POST form updates CustomerSite fields."""
        from app.routers.htmx_views import edit_site

        company = _make_company(db_session)
        site = _make_site(db_session, company)
        mock_req = _mock_form_request(fields={"site_name": "Branch Office", "city": "Austin", "country": "US"})
        with patch("app.routers.htmx_views.company_tab", new_callable=AsyncMock) as mock_tab:
            mock_tab.return_value = HTMLResponse("sites tab")
            result = await edit_site(
                request=mock_req,
                company_id=company.id,
                site_id=site.id,
                user=test_user,
                db=db_session,
            )
        assert result.status_code == 200
        db_session.refresh(site)
        assert site.site_name == "Branch Office"

    async def test_edit_site_not_found(self, db_session: Session, test_user: User):
        """non-existent site → 404."""
        from fastapi import HTTPException

        from app.routers.htmx_views import edit_site

        company = _make_company(db_session)
        mock_req = _mock_form_request(fields={"site_name": "X"})
        with pytest.raises(HTTPException) as exc_info:
            await edit_site(request=mock_req, company_id=company.id, site_id=99999, user=test_user, db=db_session)
        assert exc_info.value.status_code == 404


# ── add_site_contact_note (5108–5125) ────────────────────────────────────────


class TestAddSiteContactNoteDirect:
    async def test_add_note_success(self, db_session: Session, test_user: User):
        """Lines 5108–5125: POST note → creates ActivityLog, returns notes."""
        from app.routers.htmx_views import add_site_contact_note

        company = _make_company(db_session)
        site = _make_site(db_session, company)
        contact = _make_site_contact(db_session, site)
        mock_req = _mock_form_request(fields={"notes": "Called, left voicemail"})
        with patch("app.routers.htmx_views.get_site_contact_notes", new_callable=AsyncMock) as mock_notes:
            mock_notes.return_value = HTMLResponse("notes list")
            result = await add_site_contact_note(
                request=mock_req,
                company_id=company.id,
                site_id=site.id,
                contact_id=contact.id,
                user=test_user,
                db=db_session,
            )
        assert result.status_code == 200

    async def test_add_note_empty_raises_400(self, db_session: Session, test_user: User):
        """empty notes → 400."""
        from fastapi import HTTPException

        from app.routers.htmx_views import add_site_contact_note

        company = _make_company(db_session)
        site = _make_site(db_session, company)
        contact = _make_site_contact(db_session, site)
        mock_req = _mock_form_request(fields={"notes": ""})
        with pytest.raises(HTTPException) as exc_info:
            await add_site_contact_note(
                request=mock_req,
                company_id=company.id,
                site_id=site.id,
                contact_id=contact.id,
                user=test_user,
                db=db_session,
            )
        assert exc_info.value.status_code == 400


# ── edit_quote_metadata (5315–5328) ──────────────────────────────────────────


class TestEditQuoteMetadataDirect:
    async def test_edit_quote_metadata_success(self, db_session: Session, test_user: User):
        """Lines 5315–5328: POST form updates quote metadata fields."""
        from app.routers.htmx_views import edit_quote_metadata

        req = _make_req(db_session, test_user)
        quote = _make_quote(db_session, req, test_user)
        mock_req = _mock_form_request(fields={"payment_terms": "Net30", "shipping_terms": "FOB", "notes": "Rush order"})
        with patch("app.routers.htmx_views.quote_detail_partial", new_callable=AsyncMock) as mock_detail:
            mock_detail.return_value = HTMLResponse("quote detail")
            result = await edit_quote_metadata(request=mock_req, quote_id=quote.id, user=test_user, db=db_session)
        assert result.status_code == 200
        db_session.refresh(quote)
        assert quote.payment_terms == "Net30"

    async def test_edit_quote_not_found(self, db_session: Session, test_user: User):
        """non-existent quote → 404."""
        from fastapi import HTTPException

        from app.routers.htmx_views import edit_quote_metadata

        mock_req = _mock_form_request(fields={"payment_terms": "Net30"})
        with pytest.raises(HTTPException) as exc_info:
            await edit_quote_metadata(request=mock_req, quote_id=99999, user=test_user, db=db_session)
        assert exc_info.value.status_code == 404


# ── buy_plan_confirm_po (6098–6120) ──────────────────────────────────────────


class TestBuyPlanConfirmPoDirect:
    async def test_confirm_po_success(self, db_session: Session, test_user: User):
        """Lines 6098–6120: POST form with po_number → calls confirm_po."""
        from app.models.buy_plan import BuyPlan, BuyPlanLine
        from app.routers.htmx_views import buy_plan_confirm_po_partial

        req = _make_req(db_session, test_user)
        quote = _make_quote(db_session, req, test_user)
        bp = BuyPlan(
            quote_id=quote.id,
            requisition_id=req.id,
            status="active",
        )
        db_session.add(bp)
        db_session.flush()
        line = BuyPlanLine(
            buy_plan_id=bp.id,
            quantity=10,
            status="awaiting_po",
        )
        db_session.add(line)
        db_session.commit()
        db_session.refresh(bp)
        db_session.refresh(line)

        mock_req = _mock_form_request(fields={"po_number": "PO-999", "estimated_ship_date": "2026-06-01"})
        with (
            patch("app.services.buyplan_workflow.confirm_po") as mock_confirm,
            patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock),
            patch("app.routers.htmx_views.buy_plan_detail_partial", new_callable=AsyncMock) as mock_detail,
        ):
            mock_confirm.return_value = None
            mock_detail.return_value = HTMLResponse("bp detail")
            result = await buy_plan_confirm_po_partial(
                request=mock_req,
                plan_id=bp.id,
                line_id=line.id,
                user=test_user,
                db=db_session,
            )
        assert result.status_code == 200

    async def test_confirm_po_empty_po_number_raises_400(self, db_session: Session, test_user: User):
        """empty po_number → 400."""
        from fastapi import HTTPException

        from app.models.buy_plan import BuyPlan
        from app.routers.htmx_views import buy_plan_confirm_po_partial

        req = _make_req(db_session, test_user)
        quote = _make_quote(db_session, req, test_user)
        bp = BuyPlan(quote_id=quote.id, requisition_id=req.id, status="active")
        db_session.add(bp)
        db_session.commit()
        db_session.refresh(bp)

        mock_req = _mock_form_request(fields={"po_number": ""})
        with pytest.raises(HTTPException) as exc_info:
            await buy_plan_confirm_po_partial(request=mock_req, plan_id=bp.id, line_id=1, user=test_user, db=db_session)
        assert exc_info.value.status_code == 400


# ── update_material_card (7359–7380) ─────────────────────────────────────────


class TestUpdateMaterialCardDirect:
    async def test_update_material_card_success(self, db_session: Session, test_user: User):
        """Lines 7359–7380: POST form updates material card fields."""
        from app.routers.htmx_views import update_material_card

        card = _make_material_card(db_session)
        mock_req = _mock_form_request(
            fields={"manufacturer": "TI", "description": "Voltage regulator", "category": "Linear"}
        )
        with patch("app.routers.htmx_views.material_detail_partial", new_callable=AsyncMock) as mock_detail:
            mock_detail.return_value = HTMLResponse("card detail")
            result = await update_material_card(request=mock_req, card_id=card.id, user=test_user, db=db_session)
        assert result.status_code == 200
        db_session.refresh(card)
        assert card.manufacturer == "TI"

    async def test_update_material_card_not_found(self, db_session: Session, test_user: User):
        """non-existent card → 404."""
        from fastapi import HTTPException

        from app.routers.htmx_views import update_material_card

        mock_req = _mock_form_request(fields={"manufacturer": "TI"})
        with pytest.raises(HTTPException) as exc_info:
            await update_material_card(request=mock_req, card_id=99999, user=test_user, db=db_session)
        assert exc_info.value.status_code == 404


# ── update_quote_line (7456–7480) ─────────────────────────────────────────────


class TestUpdateQuoteLineDirect:
    async def test_update_quote_line_success(self, db_session: Session, test_user: User):
        """Lines 7456–7480: POST form updates quote line fields."""
        from app.routers.htmx_views import update_quote_line

        req = _make_req(db_session, test_user)
        quote = _make_quote(db_session, req, test_user)
        line = _make_quote_line(db_session, quote)
        mock_req = _mock_form_request(fields={"qty": "200", "cost_price": "0.08", "sell_price": "0.12"})
        with patch("app.routers.htmx_views.templates") as mock_tpl:
            mock_tpl.TemplateResponse.return_value = HTMLResponse("line row")
            result = await update_quote_line(
                request=mock_req,
                quote_id=quote.id,
                line_id=line.id,
                user=test_user,
                db=db_session,
            )
        assert result.status_code == 200
        db_session.refresh(line)
        assert line.qty == 200

    async def test_update_quote_line_not_found(self, db_session: Session, test_user: User):
        """non-existent line → 404."""
        from fastapi import HTTPException

        from app.routers.htmx_views import update_quote_line

        req = _make_req(db_session, test_user)
        quote = _make_quote(db_session, req, test_user)
        mock_req = _mock_form_request(fields={"qty": "5"})
        with pytest.raises(HTTPException) as exc_info:
            await update_quote_line(request=mock_req, quote_id=quote.id, line_id=99999, user=test_user, db=db_session)
        assert exc_info.value.status_code == 404

    async def test_update_quote_line_invalid_qty_raises_400(self, db_session: Session, test_user: User):
        """non-integer qty → 400."""
        from fastapi import HTTPException

        from app.routers.htmx_views import update_quote_line

        req = _make_req(db_session, test_user)
        quote = _make_quote(db_session, req, test_user)
        line = _make_quote_line(db_session, quote)
        mock_req = _mock_form_request(fields={"qty": "not-a-number"})
        with pytest.raises(HTTPException) as exc_info:
            await update_quote_line(request=mock_req, quote_id=quote.id, line_id=line.id, user=test_user, db=db_session)
        assert exc_info.value.status_code == 400


# ── proactive_draft_for_prepare (8310–8386) ──────────────────────────────────


class TestProactiveDraftDirect:
    async def test_draft_no_match_ids_returns_error(self, db_session: Session, test_user: User):
        """Lines 8307–8308: empty match_ids → error HTML."""
        from app.routers.htmx_views import proactive_draft_for_prepare

        mock_req = _mock_form_request(fields={"match_ids": [], "contact_ids": []})
        result = await proactive_draft_for_prepare(request=mock_req, user=test_user, db=db_session)
        assert result.status_code == 200
        assert b"No matches selected" in result.body

    async def test_draft_invalid_match_ids_returns_error(self, db_session: Session, test_user: User):
        """Lines 8315–8316: match_ids that don't belong to user → error HTML."""
        from app.routers.htmx_views import proactive_draft_for_prepare

        mock_req = _mock_form_request(fields={"match_ids": ["99999"], "contact_ids": []})
        result = await proactive_draft_for_prepare(request=mock_req, user=test_user, db=db_session)
        assert result.status_code == 200
        assert b"No valid matches found" in result.body
