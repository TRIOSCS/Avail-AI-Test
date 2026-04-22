"""test_htmx_views.py — Comprehensive tests for app/routers/htmx_views.py.

Targets 85%+ line coverage across all route groups: full-page views,
requisitions, vendors, customers, buy-plans, quotes, search, settings,
prospecting, proactive, materials, trouble-tickets, sourcing, parts,
knowledge, and admin endpoints.

Called by: pytest
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import (
    BuyPlanStatus,
    OfferStatus,
    QuoteStatus,
    RequisitionStatus,
    SourcingStatus,
)
from app.models import (
    BuyPlan,
    Company,
    CustomerSite,
    Offer,
    Quote,
    Requirement,
    Requisition,
    User,
    VendorCard,
)

# ── Helpers ───────────────────────────────────────────────────────────────


def _make_requisition(db: Session, user: User, **kw) -> Requisition:
    defaults = dict(
        name="REQ-TEST",
        customer_name="Acme",
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
        target_qty=1000,
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
        normalized_name="arrow electronics",
        display_name="Arrow Electronics",
        emails=["sales@arrow.com"],
        phones=["+1-555-0100"],
        sighting_count=42,
        is_blacklisted=False,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    vc = VendorCard(**defaults)
    db.add(vc)
    db.flush()
    return vc


def _make_offer(db: Session, req: Requisition, user: User, **kw) -> Offer:
    defaults = dict(
        requisition_id=req.id,
        vendor_name="Arrow Electronics",
        mpn="LM317T",
        qty_available=1000,
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


def _make_company(db: Session, **kw) -> Company:
    defaults = dict(
        name="Acme Electronics",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    co = Company(**defaults)
    db.add(co)
    db.flush()
    return co


def _make_customer_site(db: Session, company: Company, **kw) -> CustomerSite:
    defaults = dict(
        company_id=company.id,
        site_name="HQ",
    )
    defaults.update(kw)
    site = CustomerSite(**defaults)
    db.add(site)
    db.flush()
    return site


def _make_quote(db: Session, req: Requisition, user: User, **kw) -> Quote:
    defaults = dict(
        requisition_id=req.id,
        quote_number=f"Q-{req.id}-1",
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
        total_cost=500.0,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    bp = BuyPlan(**defaults)
    db.add(bp)
    db.flush()
    return bp


# ══════════════════════════════════════════════════════════════════════════
# Full Page Entry Points
# ══════════════════════════════════════════════════════════════════════════


class TestV2FullPages:
    """Test the multi-decorated v2_page handler for all entry URLs."""

    def test_v2_root(self, client: TestClient):
        resp = client.get("/v2")
        assert resp.status_code == 200

    def test_v2_requisitions(self, client: TestClient):
        resp = client.get("/v2/requisitions")
        assert resp.status_code == 200

    def test_v2_requisitions_detail(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/requisitions/{req.id}")
        assert resp.status_code == 200

    def test_v2_search(self, client: TestClient):
        resp = client.get("/v2/search")
        assert resp.status_code == 200

    def test_v2_vendors(self, client: TestClient):
        resp = client.get("/v2/vendors")
        assert resp.status_code == 200

    def test_v2_vendors_detail(self, client: TestClient, db_session: Session):
        vc = _make_vendor_card(db_session)
        db_session.commit()
        resp = client.get(f"/v2/vendors/{vc.id}")
        assert resp.status_code == 200

    def test_v2_customers(self, client: TestClient):
        resp = client.get("/v2/customers")
        assert resp.status_code == 200

    def test_v2_customers_detail(self, client: TestClient, db_session: Session):
        co = _make_company(db_session)
        db_session.commit()
        resp = client.get(f"/v2/customers/{co.id}")
        assert resp.status_code == 200

    def test_v2_buy_plans(self, client: TestClient):
        resp = client.get("/v2/buy-plans")
        assert resp.status_code == 200

    def test_v2_excess(self, client: TestClient):
        resp = client.get("/v2/excess")
        assert resp.status_code == 200

    def test_v2_quotes(self, client: TestClient):
        resp = client.get("/v2/quotes")
        assert resp.status_code == 200

    def test_v2_settings(self, client: TestClient):
        resp = client.get("/v2/settings")
        assert resp.status_code == 200

    def test_v2_prospecting(self, client: TestClient):
        resp = client.get("/v2/prospecting")
        assert resp.status_code == 200

    def test_v2_proactive(self, client: TestClient):
        resp = client.get("/v2/proactive")
        assert resp.status_code == 200

    def test_v2_materials(self, client: TestClient):
        resp = client.get("/v2/materials")
        assert resp.status_code == 200

    def test_v2_follow_ups(self, client: TestClient):
        resp = client.get("/v2/follow-ups")
        assert resp.status_code == 200

    def test_v2_sightings(self, client: TestClient):
        resp = client.get("/v2/sightings")
        assert resp.status_code == 200

    def test_v2_trouble_tickets(self, client: TestClient):
        resp = client.get("/v2/trouble-tickets")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Helper / Utility Functions
# ══════════════════════════════════════════════════════════════════════════


class TestHelperFunctions:
    """Test _parse_filter_json, _safe_int, _safe_float, _parse_date_safe."""

    def test_parse_filter_json_empty(self):
        from app.routers.htmx_views import _parse_filter_json

        assert _parse_filter_json("") == {}
        assert _parse_filter_json(None) == {}

    def test_parse_filter_json_valid(self):
        from app.routers.htmx_views import _parse_filter_json

        result = _parse_filter_json('{"key": "val"}')
        assert result == {"key": "val"}

    def test_parse_filter_json_invalid(self):
        from app.routers.htmx_views import _parse_filter_json

        assert _parse_filter_json("not json") == {}

    def test_parse_filter_json_coerce_numeric(self):
        from app.routers.htmx_views import _parse_filter_json

        result = _parse_filter_json('{"price_min": "10.5", "price_max": "bad", "name": "test"}', coerce_numeric=True)
        assert result["price_min"] == 10.5
        assert "price_max" not in result  # bad value dropped
        assert result["name"] == "test"

    def test_safe_int(self):
        from app.routers.htmx_views import _safe_int

        assert _safe_int("42") == 42
        assert _safe_int("") is None
        assert _safe_int(None) is None
        assert _safe_int("abc") is None

    def test_safe_float(self):
        from app.routers.htmx_views import _safe_float

        assert _safe_float("3.14") == 3.14
        assert _safe_float("") is None
        assert _safe_float(None) is None
        assert _safe_float("abc") is None

    def test_parse_date_safe(self):
        from datetime import date

        from app.routers.htmx_views import _parse_date_safe

        assert _parse_date_safe("", date) is None
        assert _parse_date_safe(None, date) is None
        assert _parse_date_safe("bad-date", date) is None
        assert _parse_date_safe("2026-03-28", date) == date(2026, 3, 28)

    def test_is_htmx(self):
        from app.routers.htmx_views import _is_htmx

        class FakeReq:
            def __init__(self, headers):
                self.headers = headers

        assert _is_htmx(FakeReq({"HX-Request": "true"})) is True
        assert _is_htmx(FakeReq({})) is False


# ══════════════════════════════════════════════════════════════════════════
# Global Search
# ══════════════════════════════════════════════════════════════════════════


class TestGlobalSearch:
    """Test the global search endpoints."""

    def test_global_search_empty(self, client: TestClient):
        resp = client.get("/v2/partials/search/global?q=")
        assert resp.status_code == 200

    def test_global_search_with_query(self, client: TestClient):
        resp = client.get("/v2/partials/search/global?q=arrow")
        assert resp.status_code == 200

    def test_ai_search_endpoint(self, client: TestClient):
        mock_result = {"best_match": None, "groups": {}, "total_count": 0}
        with patch("app.services.global_search_service.ai_search", new_callable=AsyncMock, return_value=mock_result):
            resp = client.post("/v2/partials/search/ai", data={"q": "test search"})
            assert resp.status_code == 200

    def test_search_results_page_empty(self, client: TestClient):
        resp = client.get("/v2/partials/search/results?q=")
        assert resp.status_code == 200

    def test_search_results_page_with_query(self, client: TestClient):
        resp = client.get("/v2/partials/search/results?q=test")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Parts Workspace
# ══════════════════════════════════════════════════════════════════════════


class TestPartsWorkspace:
    """Test the parts workspace partial."""

    def test_workspace(self, client: TestClient):
        resp = client.get("/v2/partials/parts/workspace")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Requisitions
# ══════════════════════════════════════════════════════════════════════════


class TestRequisitionsListPartial:
    """Test the requisitions list partial with filters."""

    def test_list_no_filters(self, client: TestClient, db_session: Session, test_user: User):
        _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.get("/v2/partials/requisitions")
        assert resp.status_code == 200

    def test_list_with_search(self, client: TestClient, db_session: Session, test_user: User):
        _make_requisition(db_session, test_user, name="RFQ-ARROW-001")
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?q=arrow")
        assert resp.status_code == 200

    def test_list_with_status_filter(self, client: TestClient, db_session: Session, test_user: User):
        _make_requisition(db_session, test_user, status=RequisitionStatus.ACTIVE)
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?status=active")
        assert resp.status_code == 200

    def test_list_with_owner_filter(self, client: TestClient, db_session: Session, test_user: User):
        _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions?owner={test_user.id}")
        assert resp.status_code == 200

    def test_list_with_urgency_filter(self, client: TestClient, db_session: Session, test_user: User):
        _make_requisition(db_session, test_user, urgency="hot")
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?urgency=hot")
        assert resp.status_code == 200

    def test_list_with_date_filters(self, client: TestClient, db_session: Session, test_user: User):
        _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?date_from=2020-01-01&date_to=2030-12-31")
        assert resp.status_code == 200

    def test_list_with_invalid_date(self, client: TestClient, db_session: Session, test_user: User):
        _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?date_from=bad&date_to=bad")
        assert resp.status_code == 200

    def test_list_sort_by_name_asc(self, client: TestClient, db_session: Session, test_user: User):
        _make_requisition(db_session, test_user, name="AAA")
        _make_requisition(db_session, test_user, name="ZZZ")
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?sort=name&dir=asc")
        assert resp.status_code == 200

    def test_list_sort_desc(self, client: TestClient, db_session: Session, test_user: User):
        _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?sort=created_at&dir=desc")
        assert resp.status_code == 200

    def test_list_sort_by_req_count(self, client: TestClient, db_session: Session, test_user: User):
        """Sort by parts count (correlated subquery)."""
        r1 = _make_requisition(db_session, test_user, name="FEW-PARTS")
        r2 = _make_requisition(db_session, test_user, name="MANY-PARTS")
        _make_requirement(db_session, r1)
        for i in range(3):
            _make_requirement(db_session, r2, primary_mpn=f"MPN-{i}")
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?sort=req_count&dir=desc")
        assert resp.status_code == 200
        assert resp.text.index("MANY-PARTS") < resp.text.index("FEW-PARTS")

    def test_list_sort_by_offer_count(self, client: TestClient, db_session: Session, test_user: User):
        """Sort by offers count (correlated subquery)."""
        r1 = _make_requisition(db_session, test_user, name="NO-OFFERS")
        r2 = _make_requisition(db_session, test_user, name="HAS-OFFERS")
        _make_offer(db_session, r2, test_user)
        _make_offer(db_session, r2, test_user, mpn="MPN-2")
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?sort=offer_count&dir=desc")
        assert resp.status_code == 200
        assert resp.text.index("HAS-OFFERS") < resp.text.index("NO-OFFERS")

    def test_list_sort_by_deadline(self, client: TestClient, db_session: Session, test_user: User):
        """Sort by deadline — ASAP sorts before dates, NULLs last."""
        _make_requisition(db_session, test_user, name="ASAP-REQ", deadline="ASAP")
        _make_requisition(db_session, test_user, name="DATE-REQ", deadline="2026-12-31")
        _make_requisition(db_session, test_user, name="NO-DEADLINE")
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?sort=deadline&dir=asc")
        assert resp.status_code == 200
        # ASAP should appear before dated deadlines, NULLs last
        assert resp.text.index("ASAP-REQ") < resp.text.index("DATE-REQ")
        assert resp.text.index("DATE-REQ") < resp.text.index("NO-DEADLINE")

    def test_list_sort_by_updated_at(self, client: TestClient, db_session: Session, test_user: User):
        """Sort by updated_at — updated rows first, NULLs sort last."""
        _make_requisition(db_session, test_user, name="UPDATED-REQ", updated_at=datetime.now(timezone.utc))
        _make_requisition(db_session, test_user, name="NEVER-UPDATED")
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?sort=updated_at&dir=desc")
        assert resp.status_code == 200
        assert resp.text.index("UPDATED-REQ") < resp.text.index("NEVER-UPDATED")

    def test_list_sort_invalid_key_falls_back(self, client: TestClient, db_session: Session, test_user: User):
        """Invalid sort key falls back to created_at without crashing."""
        _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?sort=bogus&dir=desc")
        assert resp.status_code == 200

    def test_list_sort_invalid_dir_returns_422(self, client: TestClient, db_session: Session, test_user: User):
        """Invalid dir value is rejected by FastAPI Literal validation."""
        _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?sort=name&dir=bogus")
        assert resp.status_code == 422

    def test_list_deadline_asap_renders_amber(self, client: TestClient, db_session: Session, test_user: User):
        """ASAP deadline renders with amber styling."""
        _make_requisition(db_session, test_user, deadline="ASAP")
        db_session.commit()
        resp = client.get("/v2/partials/requisitions")
        assert resp.status_code == 200
        assert "text-amber-600" in resp.text
        assert "ASAP" in resp.text

    def test_list_pagination(self, client: TestClient, db_session: Session, test_user: User):
        for i in range(5):
            _make_requisition(db_session, test_user, name=f"REQ-{i}")
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?limit=2&offset=2")
        assert resp.status_code == 200

    def test_list_search_match_reason_customer(self, client: TestClient, db_session: Session, test_user: User):
        _make_requisition(db_session, test_user, customer_name="Acme Corp")
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?q=acme")
        assert resp.status_code == 200

    def test_list_search_match_reason_part(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user, name="RFQ-99", customer_name="NotThis")
        _make_requirement(db_session, req, primary_mpn="LM317T")
        db_session.commit()
        resp = client.get("/v2/partials/requisitions?q=LM317T")
        assert resp.status_code == 200


class TestRequisitionCreateForm:
    """Test the create/import form endpoints."""

    def test_create_form(self, client: TestClient):
        resp = client.get("/v2/partials/requisitions/create-form")
        assert resp.status_code == 200

    def test_import_form(self, client: TestClient):
        resp = client.get("/v2/partials/requisitions/import-form")
        assert resp.status_code == 200


class TestRequisitionCreate:
    """Test creating a requisition via POST."""

    def test_create_basic(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/create",
            data={"name": "Test Req", "customer_name": "Acme", "urgency": "normal", "parts_text": ""},
        )
        assert resp.status_code == 200

    def test_create_with_parts(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/create",
            data={
                "name": "Test Req Parts",
                "customer_name": "Acme",
                "urgency": "normal",
                "parts_text": "LM317T, 1000\nNE555P, 500",
            },
        )
        assert resp.status_code == 200

    def test_create_with_invalid_qty(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/create",
            data={
                "name": "Test Req Bad Qty",
                "parts_text": "LM317T, notanumber",
            },
        )
        assert resp.status_code == 200


class TestRequisitionDetail:
    """Test requisition detail and tabs."""

    def test_detail(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        _make_requirement(db_session, req)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}")
        assert resp.status_code == 200
        # Lazy insights hx-get must pair with hx-target="this" so swaps do not inherit
        # <main id="main-content" hx-target="this"> (which would replace the whole main column).
        marker = f'hx-get="/v2/partials/requisitions/{req.id}/insights"'
        assert marker in resp.text
        start = resp.text.index(marker)
        assert 'hx-target="this"' in resp.text[start : start + 280]

    def test_detail_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/requisitions/999999")
        assert resp.status_code == 404

    def test_tab_parts(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        _make_requirement(db_session, req)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/parts")
        assert resp.status_code == 200

    def test_tab_offers(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        _make_offer(db_session, req, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/offers")
        assert resp.status_code == 200

    def test_tab_quotes(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/quotes")
        assert resp.status_code == 200

    def test_tab_buy_plans(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/buy_plans")
        assert resp.status_code == 200

    def test_tab_tasks(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/tasks")
        assert resp.status_code == 200

    def test_tab_activity(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/activity")
        assert resp.status_code == 200

    def test_tab_responses(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/responses")
        assert resp.status_code == 200

    def test_tab_invalid(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/invalid_tab")
        assert resp.status_code == 404


class TestRequisitionInlineEdit:
    """Test inline edit cell and save."""

    def test_edit_cell_name(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/edit/name")
        assert resp.status_code == 200

    def test_edit_cell_status(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/edit/status")
        assert resp.status_code == 200

    def test_edit_cell_owner(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/edit/owner")
        assert resp.status_code == 200

    def test_edit_cell_invalid_field(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/edit/bogus")
        assert resp.status_code == 400

    def test_edit_cell_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/requisitions/999999/edit/name")
        assert resp.status_code == 404

    def test_inline_save_name(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.patch(
            f"/v2/partials/requisitions/{req.id}/inline",
            data={"field": "name", "value": "New Name", "context": "row"},
        )
        assert resp.status_code == 200

    def test_inline_save_urgency(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.patch(
            f"/v2/partials/requisitions/{req.id}/inline",
            data={"field": "urgency", "value": "hot", "context": "row"},
        )
        assert resp.status_code == 200

    def test_inline_save_deadline(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.patch(
            f"/v2/partials/requisitions/{req.id}/inline",
            data={"field": "deadline", "value": "2026-04-01", "context": "row"},
        )
        assert resp.status_code == 200

    def test_inline_save_deadline_clear(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.patch(
            f"/v2/partials/requisitions/{req.id}/inline",
            data={"field": "deadline", "value": "", "context": "row"},
        )
        assert resp.status_code == 200

    def test_inline_save_owner(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.patch(
            f"/v2/partials/requisitions/{req.id}/inline",
            data={"field": "owner", "value": str(test_user.id), "context": "row"},
        )
        assert resp.status_code == 200

    def test_inline_save_status(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        with patch("app.services.requisition_state.transition"):
            resp = client.patch(
                f"/v2/partials/requisitions/{req.id}/inline",
                data={"field": "status", "value": "archived", "context": "row"},
            )
            assert resp.status_code == 200

    def test_inline_save_header_context(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.patch(
            f"/v2/partials/requisitions/{req.id}/inline",
            data={"field": "name", "value": "Renamed", "context": "header"},
        )
        assert resp.status_code == 200

    def test_inline_save_tab_context(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.patch(
            f"/v2/partials/requisitions/{req.id}/inline",
            data={"field": "name", "value": "Renamed Tab", "context": "tab"},
        )
        assert resp.status_code == 200

    def test_inline_save_not_found(self, client: TestClient):
        resp = client.patch(
            "/v2/partials/requisitions/999999/inline",
            data={"field": "name", "value": "X", "context": "row"},
        )
        assert resp.status_code == 404


class TestRequisitionRowActions:
    """Test row-level actions (archive, activate, claim, unclaim, clone)."""

    def test_action_archive(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        with patch("app.services.requisition_state.transition"):
            resp = client.post(f"/v2/partials/requisitions/{req.id}/action/archive", data={})
            assert resp.status_code == 200

    def test_action_activate(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user, status=RequisitionStatus.ARCHIVED)
        db_session.commit()
        with patch("app.services.requisition_state.transition"):
            resp = client.post(f"/v2/partials/requisitions/{req.id}/action/activate", data={})
            assert resp.status_code == 200

    def test_action_claim(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user, claimed_by_id=None)
        db_session.commit()
        with patch("app.services.requirement_status.claim_requisition"):
            resp = client.post(f"/v2/partials/requisitions/{req.id}/action/claim", data={})
            assert resp.status_code == 200

    def test_action_unclaim(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        with patch("app.services.requirement_status.unclaim_requisition"):
            resp = client.post(f"/v2/partials/requisitions/{req.id}/action/unclaim", data={})
            assert resp.status_code == 200

    def test_action_clone(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        mock_new = MagicMock(id=999)
        with patch("app.services.requisition_service.clone_requisition", return_value=mock_new):
            resp = client.post(f"/v2/partials/requisitions/{req.id}/action/clone", data={})
            assert resp.status_code == 200

    def test_action_invalid(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.post(f"/v2/partials/requisitions/{req.id}/action/invalid", data={})
        assert resp.status_code == 400

    def test_action_not_found(self, client: TestClient):
        resp = client.post("/v2/partials/requisitions/999999/action/archive", data={})
        assert resp.status_code == 404

    def test_action_return_format_detail(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        with patch("app.services.requisition_state.transition"):
            resp = client.post(
                f"/v2/partials/requisitions/{req.id}/action/archive",
                data={"return": "detail"},
            )
            assert resp.status_code == 200


class TestRequisitionBulkActions:
    """Test bulk actions on requisitions."""

    def test_bulk_archive(self, client: TestClient, db_session: Session, test_user: User):
        r1 = _make_requisition(db_session, test_user, name="Bulk1")
        r2 = _make_requisition(db_session, test_user, name="Bulk2")
        db_session.commit()
        resp = client.post(
            "/v2/partials/requisitions/bulk/archive",
            data={"ids": f"{r1.id},{r2.id}"},
        )
        assert resp.status_code == 200

    def test_bulk_activate(self, client: TestClient, db_session: Session, test_user: User):
        r1 = _make_requisition(db_session, test_user, status=RequisitionStatus.ARCHIVED)
        db_session.commit()
        resp = client.post(
            "/v2/partials/requisitions/bulk/activate",
            data={"ids": str(r1.id)},
        )
        assert resp.status_code == 200

    def test_bulk_assign(self, client: TestClient, db_session: Session, test_user: User):
        r1 = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.post(
            "/v2/partials/requisitions/bulk/assign",
            data={"ids": str(r1.id), "owner_id": str(test_user.id)},
        )
        assert resp.status_code == 200

    def test_bulk_no_ids(self, client: TestClient):
        resp = client.post("/v2/partials/requisitions/bulk/archive", data={"ids": ""})
        assert resp.status_code == 400

    def test_bulk_invalid_ids(self, client: TestClient):
        resp = client.post("/v2/partials/requisitions/bulk/archive", data={"ids": "abc,def"})
        assert resp.status_code == 400

    def test_bulk_invalid_action(self, client: TestClient):
        resp = client.post("/v2/partials/requisitions/bulk/delete", data={"ids": "1"})
        assert resp.status_code == 400

    def test_bulk_too_many(self, client: TestClient):
        ids = ",".join(str(i) for i in range(201))
        resp = client.post("/v2/partials/requisitions/bulk/archive", data={"ids": ids})
        assert resp.status_code == 400


class TestAddRequirement:
    """Test adding a requirement to a requisition."""

    def test_add_requirement_success(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/requirements",
            data={
                "primary_mpn": "NE555P",
                "manufacturer": "Texas Instruments",
                "target_qty": "100",
            },
        )
        assert resp.status_code == 200

    def test_add_requirement_missing_manufacturer(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/requirements",
            data={"primary_mpn": "NE555P", "manufacturer": "", "target_qty": "100"},
        )
        assert resp.status_code == 422

    def test_add_requirement_not_found(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/999999/requirements",
            data={"primary_mpn": "NE555P", "manufacturer": "TI", "target_qty": "100"},
        )
        assert resp.status_code == 404


class TestSearchAll:
    """Test search-all requirements in a requisition."""

    def test_search_all(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        _make_requirement(db_session, req)
        db_session.commit()
        resp = client.post(f"/v2/partials/requisitions/{req.id}/search-all")
        assert resp.status_code == 200

    def test_search_all_no_requirements(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.post(f"/v2/partials/requisitions/{req.id}/search-all")
        assert resp.status_code == 200
        assert "No requirements to search" in resp.text


class TestRequisitionImport:
    """Test AI import parse and save."""

    def test_import_parse_no_data(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/import-parse",
            data={"name": "Test", "raw_text": ""},
        )
        assert resp.status_code == 200
        assert "No data" in resp.text

    def test_import_parse_json_mode_no_data(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/import-parse?format=json",
            data={"name": "Test", "raw_text": ""},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["error"] == "No data provided"

    def test_import_parse_with_text(self, client: TestClient):
        mock_result = {"requirements": [{"primary_mpn": "LM317T", "target_qty": 100}], "name": "AI Name"}
        with patch("app.routers.htmx_views.parse_freeform_rfq", new_callable=AsyncMock, return_value=mock_result):
            resp = client.post(
                "/v2/partials/requisitions/import-parse",
                data={"name": "Import", "raw_text": "LM317T 100pcs"},
                files={"file": ("", b"", "application/octet-stream")},
            )
            assert resp.status_code == 200

    def test_import_parse_json_mode(self, client: TestClient):
        mock_result = {
            "requirements": [{"primary_mpn": "LM317T", "target_qty": 100}],
            "name": "AI Name",
            "customer_name": "AI Customer",
        }
        with patch("app.routers.htmx_views.parse_freeform_rfq", new_callable=AsyncMock, return_value=mock_result):
            resp = client.post(
                "/v2/partials/requisitions/import-parse?format=json",
                data={"name": "Import", "raw_text": "LM317T 100pcs"},
                files={"file": ("", b"", "application/octet-stream")},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "requirements" in data

    def test_import_save_no_parts(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/import-save",
            data={"name": "Test"},
        )
        assert resp.status_code == 200
        assert "No valid parts" in resp.text

    def test_import_save_with_parts(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/import-save",
            data={
                "name": "Import Test",
                "customer_name": "Acme",
                "customer_site_id": "",
                "deadline": "",
                "urgency": "normal",
                "reqs[0].primary_mpn": "LM317T",
                "reqs[0].target_qty": "100",
                "reqs[0].brand": "",
                "reqs[0].target_price": "",
                "reqs[0].condition": "new",
                "reqs[0].customer_pn": "",
                "reqs[0].date_codes": "",
                "reqs[0].packaging": "",
                "reqs[0].manufacturer": "TI",
                "reqs[0].substitutes": "",
                "reqs[0].firmware": "",
                "reqs[0].hardware_codes": "",
                "reqs[0].description": "",
                "reqs[0].package_type": "",
                "reqs[0].revision": "",
                "reqs[0].need_by_date": "",
                "reqs[0].sale_notes": "",
            },
        )
        assert resp.status_code == 200


class TestParseEmailOffer:
    """Test email/offer parsing endpoints."""

    def test_parse_email_form(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/parse-email-form")
        assert resp.status_code == 200

    def test_paste_offer_form(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/paste-offer-form")
        assert resp.status_code == 200

    def test_parse_email_empty_body(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/parse-email",
            data={"email_body": "", "vendor_name": "Arrow"},
        )
        assert resp.status_code == 200
        assert "paste the email" in resp.text.lower()

    def test_parse_email_success(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        mock_result = {
            "quotes": [{"mpn": "LM317T", "qty": 100, "price": 0.5}],
            "overall_confidence": 0.95,
            "email_type": "quote",
        }
        with patch("app.services.ai_email_parser.parse_email", new_callable=AsyncMock, return_value=mock_result):
            resp = client.post(
                f"/v2/partials/requisitions/{req.id}/parse-email",
                data={"email_body": "We can offer LM317T at $0.50", "vendor_name": "Arrow"},
            )
            assert resp.status_code == 200

    def test_parse_email_no_result(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        with patch("app.services.ai_email_parser.parse_email", new_callable=AsyncMock, return_value=None):
            resp = client.post(
                f"/v2/partials/requisitions/{req.id}/parse-email",
                data={"email_body": "Hello", "vendor_name": "Arrow"},
            )
            assert resp.status_code == 200

    def test_parse_email_exception(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        with patch(
            "app.services.ai_email_parser.parse_email", new_callable=AsyncMock, side_effect=Exception("AI error")
        ):
            resp = client.post(
                f"/v2/partials/requisitions/{req.id}/parse-email",
                data={"email_body": "Hello", "vendor_name": "Arrow"},
            )
            assert resp.status_code == 200
            assert "Parse failed" in resp.text

    def test_parse_offer_empty(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/parse-offer",
            data={"raw_text": ""},
        )
        assert resp.status_code == 200
        assert "paste vendor text" in resp.text.lower()

    def test_parse_offer_success(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        _make_requirement(db_session, req)
        db_session.commit()
        mock_result = {"offers": [{"mpn": "LM317T", "qty_available": 100, "unit_price": 0.5, "vendor_name": "Arrow"}]}
        with patch(
            "app.services.freeform_parser_service.parse_freeform_offer",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            resp = client.post(
                f"/v2/partials/requisitions/{req.id}/parse-offer",
                data={"raw_text": "LM317T 100pcs $0.50"},
            )
            assert resp.status_code == 200

    def test_parse_offer_exception(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        with patch(
            "app.services.freeform_parser_service.parse_freeform_offer",
            new_callable=AsyncMock,
            side_effect=Exception("fail"),
        ):
            resp = client.post(
                f"/v2/partials/requisitions/{req.id}/parse-offer",
                data={"raw_text": "LM317T 100pcs"},
            )
            assert resp.status_code == 200
            assert "Parse failed" in resp.text


class TestSaveParsedOffers:
    """Test saving parsed offers."""

    def test_save_parsed_offers(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        _make_requirement(db_session, req, primary_mpn="LM317T")
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/save-parsed-offers",
            data={
                "vendor_name": "Arrow",
                "offers[0].mpn": "LM317T",
                "offers[0].vendor_name": "Arrow",
                "offers[0].qty_available": "1000",
                "offers[0].unit_price": "0.50",
                "offers[0].condition": "new",
            },
        )
        assert resp.status_code == 200

    def test_save_parsed_offers_no_data(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/save-parsed-offers",
            data={},
        )
        assert resp.status_code == 200
        assert "No offers to save" in resp.text


# ══════════════════════════════════════════════════════════════════════════
# Quote Endpoints
# ══════════════════════════════════════════════════════════════════════════


class TestCreateQuoteFromOffers:
    """Test creating a quote from offers."""

    def test_create_quote_no_offers(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/create-quote",
            data={},
        )
        assert resp.status_code == 400

    def test_create_quote_success(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        offer = _make_offer(db_session, req, test_user)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/create-quote",
            data={"offer_ids": str(offer.id)},
        )
        assert resp.status_code == 200


class TestDeleteRequirement:
    """Test deleting a requirement."""

    def test_delete_requirement(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.delete(f"/v2/partials/requisitions/{req.id}/requirements/{item.id}")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Vendor Endpoints
# ══════════════════════════════════════════════════════════════════════════


class TestVendorsList:
    """Test the vendors list partial."""

    def test_list(self, client: TestClient, db_session: Session):
        _make_vendor_card(db_session)
        db_session.commit()
        resp = client.get("/v2/partials/vendors")
        assert resp.status_code == 200

    def test_list_with_search(self, client: TestClient, db_session: Session):
        _make_vendor_card(db_session, display_name="Arrow Electronics")
        db_session.commit()
        resp = client.get("/v2/partials/vendors?q=arrow")
        assert resp.status_code == 200

    def test_list_show_blacklisted(self, client: TestClient, db_session: Session):
        _make_vendor_card(db_session, is_blacklisted=True)
        db_session.commit()
        resp = client.get("/v2/partials/vendors?hide_blacklisted=false")
        assert resp.status_code == 200

    def test_list_sort_by_name(self, client: TestClient, db_session: Session):
        _make_vendor_card(db_session)
        db_session.commit()
        resp = client.get("/v2/partials/vendors?sort=display_name&dir=asc")
        assert resp.status_code == 200


class TestVendorDetail:
    """Test vendor detail."""

    def test_detail(self, client: TestClient, db_session: Session):
        vc = _make_vendor_card(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/vendors/{vc.id}")
        assert resp.status_code == 200
        marker = f'hx-get="/v2/partials/vendors/{vc.id}/insights"'
        assert marker in resp.text
        start = resp.text.index(marker)
        assert 'hx-target="this"' in resp.text[start : start + 280]

    def test_detail_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/vendors/999999")
        assert resp.status_code == 404


class TestVendorTabs:
    """Test vendor tab endpoints."""

    def test_tab_contacts(self, client: TestClient, db_session: Session):
        vc = _make_vendor_card(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/vendors/{vc.id}/tab/contacts")
        assert resp.status_code == 200

    def test_tab_sightings(self, client: TestClient, db_session: Session):
        vc = _make_vendor_card(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/vendors/{vc.id}/tab/overview")
        assert resp.status_code == 200

    def test_tab_offers(self, client: TestClient, db_session: Session):
        vc = _make_vendor_card(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/vendors/{vc.id}/tab/offers")
        assert resp.status_code == 200

    def test_tab_invalid(self, client: TestClient, db_session: Session):
        vc = _make_vendor_card(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/vendors/{vc.id}/tab/bogus")
        assert resp.status_code == 404


class TestVendorEdit:
    """Test vendor edit form and save."""

    def test_edit_form(self, client: TestClient, db_session: Session):
        vc = _make_vendor_card(db_session)
        db_session.commit()
        resp = client.get(f"/v2/partials/vendors/{vc.id}/edit-form")
        assert resp.status_code == 200

    def test_edit_save(self, client: TestClient, db_session: Session):
        vc = _make_vendor_card(db_session)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/vendors/{vc.id}/edit",
            data={"display_name": "Updated Name", "website": "https://new.com"},
        )
        assert resp.status_code == 200

    def test_toggle_blacklist(self, client: TestClient, db_session: Session):
        vc = _make_vendor_card(db_session, is_blacklisted=False)
        db_session.commit()
        resp = client.post(f"/v2/partials/vendors/{vc.id}/toggle-blacklist")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Customer Endpoints
# ══════════════════════════════════════════════════════════════════════════


class TestCompaniesRedirect:
    """Test legacy /v2/companies redirect to /v2/customers."""

    def test_companies_redirect(self, client: TestClient):
        resp = client.get("/v2/companies", follow_redirects=False)
        assert resp.status_code == 301

    def test_companies_redirect_with_path(self, client: TestClient):
        resp = client.get("/v2/companies/123", follow_redirects=False)
        assert resp.status_code == 301

    def test_partials_companies_redirect(self, client: TestClient):
        resp = client.get("/v2/partials/companies", follow_redirects=False)
        assert resp.status_code == 301


class TestCustomersList:
    """Test customers list partial."""

    def test_list(self, client: TestClient, db_session: Session):
        _make_company(db_session)
        db_session.commit()
        resp = client.get("/v2/partials/customers")
        assert resp.status_code == 200

    def test_list_with_search(self, client: TestClient, db_session: Session):
        _make_company(db_session, name="Acme Electronics")
        db_session.commit()
        resp = client.get("/v2/partials/customers?search=acme")
        assert resp.status_code == 200


class TestCustomerDetail:
    """Test customer detail and tabs."""

    def test_detail(self, client: TestClient, db_session: Session):
        co = _make_company(db_session)
        _make_customer_site(db_session, co)
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{co.id}")
        assert resp.status_code == 200
        marker = f'hx-get="/v2/partials/customers/{co.id}/insights"'
        assert marker in resp.text
        start = resp.text.index(marker)
        assert 'hx-target="this"' in resp.text[start : start + 280]

    def test_detail_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/customers/999999")
        assert resp.status_code == 404


class TestCustomerCRUD:
    """Test create company, typeahead, duplicate check."""

    def test_create_form(self, client: TestClient):
        resp = client.get("/v2/partials/customers/create-form")
        assert resp.status_code == 200

    def test_create_company(self, client: TestClient):
        resp = client.post(
            "/v2/partials/customers/create",
            data={"name": "New Corp", "website": "https://newcorp.com"},
        )
        assert resp.status_code == 200

    def test_create_company_no_name(self, client: TestClient):
        resp = client.post("/v2/partials/customers/create", data={"name": ""})
        assert resp.status_code == 400

    def test_create_company_duplicate(self, client: TestClient, db_session: Session):
        _make_company(db_session, name="Dupe Corp")
        db_session.commit()
        resp = client.post("/v2/partials/customers/create", data={"name": "Dupe Corp"})
        assert resp.status_code == 409

    def test_typeahead_short_query(self, client: TestClient):
        resp = client.get("/v2/partials/customers/typeahead?q=a")
        assert resp.status_code == 200
        assert resp.text == ""

    def test_typeahead_valid(self, client: TestClient, db_session: Session):
        _make_company(db_session, name="Acme Corp")
        db_session.commit()
        resp = client.get("/v2/partials/customers/typeahead?q=acme")
        assert resp.status_code == 200

    def test_check_duplicate(self, client: TestClient, db_session: Session):
        _make_company(db_session, name="Dup Check Inc")
        db_session.commit()
        resp = client.get("/v2/partials/customers/check-duplicate?name=Dup+Check+Inc")
        assert resp.status_code == 200


class TestCustomerQuickCreate:
    """Test quick-create from AI lookup."""

    def test_quick_create_new(self, client: TestClient):
        resp = client.post(
            "/v2/partials/customers/quick-create",
            data={"company_name": "Quick Corp", "website": "https://quick.com", "city": "Austin"},
        )
        assert resp.status_code == 200
        assert "Created" in resp.text

    def test_quick_create_duplicate(self, client: TestClient, db_session: Session):
        co = _make_company(db_session, name="Existing Corp")
        _make_customer_site(db_session, co)
        db_session.commit()
        resp = client.post(
            "/v2/partials/customers/quick-create",
            data={"company_name": "Existing Corp"},
        )
        assert resp.status_code == 200
        assert "already exists" in resp.text


class TestCustomerLookup:
    """Test AI customer lookup."""

    def test_lookup_success(self, client: TestClient):
        mock_result = {
            "company_name": "Acme",
            "website": "https://acme.com",
            "phone": "555-1234",
            "address_line1": "123 Main",
            "city": "Austin",
            "state": "TX",
            "zip": "78701",
            "country": "US",
        }
        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=mock_result):
            resp = client.post(
                "/v2/partials/customers/lookup",
                data={"company_name": "Acme", "location": "Austin TX"},
            )
            assert resp.status_code == 200
            assert "Acme" in resp.text

    def test_lookup_failure(self, client: TestClient):
        from app.utils.claude_errors import ClaudeUnavailableError

        with patch(
            "app.utils.claude_client.claude_json", new_callable=AsyncMock, side_effect=ClaudeUnavailableError("down")
        ):
            resp = client.post(
                "/v2/partials/customers/lookup",
                data={"company_name": "Acme", "location": ""},
            )
            assert resp.status_code == 200
            assert "Could not look up" in resp.text

    def test_lookup_no_result(self, client: TestClient):
        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=None):
            resp = client.post(
                "/v2/partials/customers/lookup",
                data={"company_name": "Acme", "location": ""},
            )
            assert resp.status_code == 200
            assert "Could not look up" in resp.text


# ══════════════════════════════════════════════════════════════════════════
# Settings Endpoints
# ══════════════════════════════════════════════════════════════════════════


class TestSettings:
    """Test settings partials."""

    def test_settings_index(self, client: TestClient):
        resp = client.get("/v2/partials/settings")
        assert resp.status_code == 200

    def test_settings_sources(self, client: TestClient):
        resp = client.get("/v2/partials/settings/sources")
        assert resp.status_code == 200

    def test_settings_system_admin(self, client: TestClient, test_user: User, db_session: Session):
        test_user.role = "admin"
        db_session.commit()
        with patch("app.services.admin_service.get_all_config", return_value={}):
            resp = client.get("/v2/partials/settings/system")
            assert resp.status_code == 200

    def test_settings_profile(self, client: TestClient):
        resp = client.get("/v2/partials/settings/profile")
        assert resp.status_code == 200

    def test_toggle_8x8(self, client: TestClient):
        resp = client.post("/api/user/toggle-8x8")
        assert resp.status_code == 200

    def test_settings_data_ops(self, client: TestClient, test_user: User, db_session: Session):
        test_user.role = "admin"
        db_session.commit()
        with patch("app.vendor_utils.find_vendor_dedup_candidates", return_value=[]):
            with patch("app.company_utils.find_company_dedup_candidates", return_value=[]):
                resp = client.get("/v2/partials/settings/data-ops")
                assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Buy Plans
# ══════════════════════════════════════════════════════════════════════════


class TestBuyPlans:
    """Test buy plan list and detail partials."""

    def test_list(self, client: TestClient):
        resp = client.get("/v2/partials/buy-plans")
        assert resp.status_code == 200

    def test_list_with_status(self, client: TestClient):
        resp = client.get("/v2/partials/buy-plans?status=pending")
        assert resp.status_code == 200

    def test_list_mine(self, client: TestClient):
        resp = client.get("/v2/partials/buy-plans?mine=true")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Quotes
# ══════════════════════════════════════════════════════════════════════════


class TestQuotesList:
    """Test quotes list partial."""

    def test_list(self, client: TestClient):
        resp = client.get("/v2/partials/quotes")
        assert resp.status_code == 200

    def test_list_with_status(self, client: TestClient):
        resp = client.get("/v2/partials/quotes?status=draft")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Offer Endpoints
# ══════════════════════════════════════════════════════════════════════════


class TestOfferEndpoints:
    """Test add, edit, delete, review offer endpoints."""

    def test_add_offer_form(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/add-offer-form")
        assert resp.status_code == 200

    def test_add_offer(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        _make_requirement(db_session, req, primary_mpn="LM317T")
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/add-offer",
            data={
                "vendor_name": "Arrow",
                "mpn": "LM317T",
                "qty_available": "500",
                "unit_price": "0.55",
            },
        )
        assert resp.status_code == 200

    def test_edit_offer_form(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        offer = _make_offer(db_session, req, test_user)
        db_session.commit()
        resp = client.get(f"/v2/partials/requisitions/{req.id}/offers/{offer.id}/edit-form")
        assert resp.status_code == 200

    def test_edit_offer(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        offer = _make_offer(db_session, req, test_user)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/offers/{offer.id}/edit",
            data={"vendor_name": "Arrow", "mpn": "LM317T", "qty_available": "999", "unit_price": "0.60"},
        )
        assert resp.status_code == 200

    def test_delete_offer(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        offer = _make_offer(db_session, req, test_user)
        db_session.commit()
        resp = client.delete(f"/v2/partials/requisitions/{req.id}/offers/{offer.id}")
        assert resp.status_code == 200

    def test_review_offer(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        offer = _make_offer(db_session, req, test_user, status=OfferStatus.PENDING_REVIEW)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/offers/{offer.id}/review",
            data={"action": "approve"},
        )
        assert resp.status_code == 200

    def test_reconfirm_offer(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        offer = _make_offer(db_session, req, test_user)
        db_session.commit()
        resp = client.post(f"/v2/partials/requisitions/{req.id}/offers/{offer.id}/reconfirm")
        assert resp.status_code == 200

    def test_mark_offer_sold(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        offer = _make_offer(db_session, req, test_user)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/offers/{offer.id}/mark-sold",
            data={},
        )
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Materials
# ══════════════════════════════════════════════════════════════════════════


class TestMaterials:
    """Test materials workspace and list."""

    def test_materials_workspace(self, client: TestClient):
        resp = client.get("/v2/partials/materials/workspace")
        assert resp.status_code == 200

    def test_materials_faceted(self, client: TestClient):
        with patch("app.services.faceted_search_service.search_materials_faceted", return_value=([], 0)):
            with patch("app.services.faceted_search_service.get_facet_counts", return_value={}):
                resp = client.get("/v2/partials/materials/faceted")
                assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Trouble Tickets
# ══════════════════════════════════════════════════════════════════════════


class TestTroubleTickets:
    """Test trouble tickets workspace and list."""

    def test_workspace(self, client: TestClient):
        resp = client.get("/v2/partials/trouble-tickets/workspace")
        assert resp.status_code == 200

    def test_list(self, client: TestClient):
        resp = client.get("/v2/partials/trouble-tickets/list")
        assert resp.status_code == 200

    def test_list_with_status(self, client: TestClient):
        resp = client.get("/v2/partials/trouble-tickets/list?status=open")
        assert resp.status_code == 200

    def test_detail_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/trouble-tickets/999999")
        assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# Prospecting
# ══════════════════════════════════════════════════════════════════════════


class TestProspecting:
    """Test prospecting list."""

    def test_list(self, client: TestClient):
        resp = client.get("/v2/partials/prospecting")
        assert resp.status_code == 200

    def test_stats(self, client: TestClient):
        resp = client.get("/v2/partials/prospecting/stats")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Proactive
# ══════════════════════════════════════════════════════════════════════════


class TestProactive:
    """Test proactive endpoints."""

    def test_list(self, client: TestClient):
        resp = client.get("/v2/partials/proactive")
        assert resp.status_code == 200

    def test_scorecard(self, client: TestClient):
        resp = client.get("/v2/partials/proactive/scorecard")
        assert resp.status_code == 200

    def test_badge(self, client: TestClient):
        resp = client.get("/v2/partials/proactive/badge")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Insights
# ══════════════════════════════════════════════════════════════════════════


class TestInsights:
    """Test AI insights panels."""

    def test_requisition_insights(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        with patch("app.services.knowledge_service.get_cached_insights", return_value=None):
            resp = client.get(f"/v2/partials/requisitions/{req.id}/insights")
            assert resp.status_code == 200

    def test_requisition_insights_refresh(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        with patch("app.services.knowledge_service.generate_insights"):
            with patch("app.services.knowledge_service.get_cached_insights", return_value=None):
                resp = client.post(f"/v2/partials/requisitions/{req.id}/insights/refresh")
                assert resp.status_code == 200

    def test_vendor_insights(self, client: TestClient, db_session: Session):
        vc = _make_vendor_card(db_session)
        db_session.commit()
        with patch("app.services.knowledge_service.get_cached_vendor_insights", return_value=None):
            resp = client.get(f"/v2/partials/vendors/{vc.id}/insights")
            assert resp.status_code == 200

    def test_vendor_insights_refresh(self, client: TestClient, db_session: Session):
        vc = _make_vendor_card(db_session)
        db_session.commit()
        with patch("app.services.knowledge_service.generate_vendor_insights"):
            with patch("app.services.knowledge_service.get_cached_vendor_insights", return_value=None):
                resp = client.post(f"/v2/partials/vendors/{vc.id}/insights/refresh")
                assert resp.status_code == 200

    def test_company_insights(self, client: TestClient, db_session: Session):
        co = _make_company(db_session)
        db_session.commit()
        with patch("app.services.knowledge_service.get_cached_company_insights", return_value=None):
            resp = client.get(f"/v2/partials/customers/{co.id}/insights")
            assert resp.status_code == 200

    def test_company_insights_refresh(self, client: TestClient, db_session: Session):
        co = _make_company(db_session)
        db_session.commit()
        with patch("app.services.knowledge_service.generate_company_insights"):
            with patch("app.services.knowledge_service.get_cached_company_insights", return_value=None):
                resp = client.post(f"/v2/partials/customers/{co.id}/insights/refresh")
                assert resp.status_code == 200

    def test_dashboard_partial_pipeline_loader_targets_self(self, client: TestClient):
        """Pipeline lazy-load must set hx-target so it does not inherit <main hx-
        target="this">."""
        resp = client.get("/v2/partials/dashboard")
        assert resp.status_code == 200
        marker = 'hx-get="/v2/partials/dashboard/pipeline-insights"'
        assert marker in resp.text
        start = resp.text.index(marker)
        assert 'hx-target="this"' in resp.text[start : start + 280]

    def test_pipeline_insights(self, client: TestClient):
        with patch("app.services.knowledge_service.get_cached_pipeline_insights", return_value=None):
            resp = client.get("/v2/partials/dashboard/pipeline-insights")
            assert resp.status_code == 200

    def test_pipeline_insights_refresh(self, client: TestClient):
        with patch("app.services.knowledge_service.generate_pipeline_insights"):
            with patch("app.services.knowledge_service.get_cached_pipeline_insights", return_value=None):
                resp = client.post("/v2/partials/dashboard/pipeline-insights/refresh")
                assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Parts List (split-panel)
# ══════════════════════════════════════════════════════════════════════════


class TestPartsList:
    """Test the parts list partial (the new split-panel view)."""

    def test_list(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        _make_requirement(db_session, req)
        db_session.commit()
        resp = client.get("/v2/partials/parts")
        assert resp.status_code == 200

    def test_list_with_search(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        _make_requirement(db_session, req, primary_mpn="LM317T")
        db_session.commit()
        resp = client.get("/v2/partials/parts?q=LM317T")
        assert resp.status_code == 200

    def test_list_with_status_filter(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        _make_requirement(db_session, req)
        db_session.commit()
        resp = client.get("/v2/partials/parts?status=open")
        assert resp.status_code == 200

    def test_list_sort(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        _make_requirement(db_session, req)
        db_session.commit()
        resp = client.get("/v2/partials/parts?sort=mpn&dir=asc")
        assert resp.status_code == 200


class TestPartTabs:
    """Test part-level tab endpoints."""

    def test_offers_tab(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.get(f"/v2/partials/parts/{item.id}/tab/offers")
        assert resp.status_code == 200

    def test_sourcing_tab(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.get(f"/v2/partials/parts/{item.id}/tab/sourcing")
        assert resp.status_code == 200

    def test_req_details_tab(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.get(f"/v2/partials/parts/{item.id}/tab/req-details")
        assert resp.status_code == 200

    def test_activity_tab(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.get(f"/v2/partials/parts/{item.id}/tab/activity")
        assert resp.status_code == 200

    def test_comms_tab(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.get(f"/v2/partials/parts/{item.id}/tab/comms")
        assert resp.status_code == 200

    def test_notes_tab(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.get(f"/v2/partials/parts/{item.id}/tab/notes")
        assert resp.status_code == 200


class TestPartHeader:
    """Test part header and inline edits."""

    def test_header(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.get(f"/v2/partials/parts/{item.id}/header")
        assert resp.status_code == 200

    def test_header_edit_mpn(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.get(f"/v2/partials/parts/{item.id}/header/edit/brand")
        assert resp.status_code == 200

    def test_header_save(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.patch(
            f"/v2/partials/parts/{item.id}/header",
            data={"field": "brand", "value": "Texas Instruments"},
        )
        assert resp.status_code == 200


class TestPartCellEdit:
    """Test part cell edit and save."""

    def test_cell_edit(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.get(f"/v2/partials/parts/{item.id}/cell/edit/target_qty")
        assert resp.status_code == 200

    def test_cell_display(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.get(f"/v2/partials/parts/{item.id}/cell/display/target_qty")
        assert resp.status_code == 200

    def test_cell_save(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.patch(
            f"/v2/partials/parts/{item.id}/cell",
            data={"field": "target_qty", "value": "500"},
        )
        assert resp.status_code == 200


class TestPartSpecEdit:
    """Test part spec edit/save."""

    def test_spec_edit(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.get(f"/v2/partials/parts/{item.id}/edit-spec/condition")
        assert resp.status_code == 200

    def test_spec_save(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.patch(
            f"/v2/partials/parts/{item.id}/save-spec",
            data={"field": "condition", "value": "new"},
        )
        assert resp.status_code == 200


class TestPartNotes:
    """Test part notes save."""

    def test_save_notes(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.patch(
            f"/v2/partials/parts/{item.id}/notes",
            data={"notes": "This is a test note"},
        )
        assert resp.status_code == 200


class TestPartTasks:
    """Test part task create, done, reopen."""

    def test_create_task(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/parts/{item.id}/tasks",
            data={"title": "Test task", "priority": "1"},
        )
        assert resp.status_code == 200


class TestPartArchive:
    """Test archive/unarchive single parts and bulk."""

    def test_archive_part(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.patch(f"/v2/partials/parts/{item.id}/archive")
        assert resp.status_code == 200

    def test_unarchive_part(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req, sourcing_status="archived")
        db_session.commit()
        resp = client.patch(f"/v2/partials/parts/{item.id}/unarchive")
        assert resp.status_code == 200

    def test_archive_requisition(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        db_session.commit()
        with patch("app.services.requisition_state.transition"):
            resp = client.patch(f"/v2/partials/requisitions/{req.id}/archive")
            assert resp.status_code == 200

    def test_unarchive_requisition(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user, status=RequisitionStatus.ARCHIVED)
        db_session.commit()
        with patch("app.services.requisition_state.transition"):
            resp = client.patch(f"/v2/partials/requisitions/{req.id}/unarchive")
            assert resp.status_code == 200

    def test_bulk_archive(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req)
        db_session.commit()
        resp = client.post(
            "/v2/partials/parts/bulk-archive",
            json={"requirement_ids": [item.id], "requisition_ids": []},
        )
        assert resp.status_code == 200

    def test_bulk_unarchive(self, client: TestClient, db_session: Session, test_user: User):
        req = _make_requisition(db_session, test_user)
        item = _make_requirement(db_session, req, sourcing_status="archived")
        db_session.commit()
        resp = client.post(
            "/v2/partials/parts/bulk-unarchive",
            json={"requirement_ids": [item.id], "requisition_ids": []},
        )
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Knowledge Endpoints
# ══════════════════════════════════════════════════════════════════════════


class TestKnowledge:
    """Test knowledge list and create."""

    def test_list(self, client: TestClient):
        resp = client.get("/v2/partials/knowledge")
        assert resp.status_code == 200

    def test_create(self, client: TestClient):
        resp = client.post(
            "/v2/partials/knowledge",
            data={
                "content": "Some knowledge content",
                "entry_type": "note",
            },
        )
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Admin Endpoints
# ══════════════════════════════════════════════════════════════════════════


class TestAdminEndpoints:
    """Test admin-level endpoints (merge, import, api health)."""

    def test_api_health(self, client: TestClient):
        resp = client.get("/v2/partials/admin/api-health")
        assert resp.status_code == 200

    def test_vendor_merge(self, client: TestClient, db_session: Session, test_user: User):
        test_user.role = "admin"
        db_session.commit()
        v1 = _make_vendor_card(db_session, normalized_name="vendor_a", display_name="Vendor A")
        v2 = _make_vendor_card(db_session, normalized_name="vendor_b", display_name="Vendor B")
        db_session.commit()
        with patch("app.services.vendor_merge_service.merge_vendor_cards") as mock_merge:
            mock_merge.return_value = {"kept_name": "Vendor A", "reassigned": 0}
            resp = client.post(
                "/v2/partials/admin/vendor-merge",
                data={"keep_id": str(v1.id), "remove_id": str(v2.id)},
            )
            assert resp.status_code == 200

    def test_company_merge(self, client: TestClient, db_session: Session, test_user: User):
        test_user.role = "admin"
        db_session.commit()
        c1 = _make_company(db_session, name="Company A")
        c2 = _make_company(db_session, name="Company B")
        db_session.commit()
        with patch("app.services.company_merge_service.merge_companies") as mock_merge:
            mock_merge.return_value = {"kept_name": "Company A"}
            resp = client.post(
                "/v2/partials/admin/company-merge",
                data={"keep_id": str(c1.id), "remove_id": str(c2.id)},
            )
            assert resp.status_code == 200

    def test_admin_data_ops(self, client: TestClient):
        resp = client.get("/v2/partials/admin/data-ops")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Follow-ups
# ══════════════════════════════════════════════════════════════════════════


class TestFollowUps:
    """Test follow-ups list."""

    def test_list(self, client: TestClient):
        resp = client.get("/v2/partials/follow-ups")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Sightings
# ══════════════════════════════════════════════════════════════════════════


class TestSightingsWorkspace:
    """Test sightings workspace."""

    def test_workspace(self, client: TestClient):
        resp = client.get("/v2/partials/sightings/workspace")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Offer Review Queue
# ══════════════════════════════════════════════════════════════════════════


class TestOfferReviewQueue:
    """Test offer review queue."""

    def test_review_queue(self, client: TestClient):
        resp = client.get("/v2/partials/offers/review-queue")
        assert resp.status_code == 200
