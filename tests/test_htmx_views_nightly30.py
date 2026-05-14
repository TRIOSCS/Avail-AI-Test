"""tests/test_htmx_views_nightly30.py — Coverage for htmx_views.py gaps.

Targets:
  line  403: SALES role filter in requisitions_list_partial
  lines 1636-1639: bulk action ValueError (invalid ID format)
  line  1475: parse_offer returns None
  lines 3113-3126: search_filter confidence / source / sort paths
  lines 4778-4791: create_site invalid owner_id + owner already owns site

Called by: pytest autodiscovery
Depends on: conftest.py fixtures (db_session, test_user, sales_user, client)
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.testclient import TestClient

from app.database import get_db
from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
from app.models import Company, CustomerSite, Requisition, User

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_request(path: str = "/v2/partials/requisitions") -> MagicMock:
    req = MagicMock(spec=Request)
    req.url.path = path
    req.headers = {"HX-Request": "true"}
    req.query_params = MagicMock()
    req.query_params.get = lambda k, d=None: d
    req.cookies = {}
    return req


def _sales_client(db_session: Session, sales_user: User) -> TestClient:
    """TestClient with sales_user as the authenticated user."""
    from app.main import app

    def _override_db():
        yield db_session

    def _override_user():
        return sales_user

    async def _override_fresh_token():
        return "mock-token"

    overridden = [get_db, require_user, require_admin, require_buyer, require_fresh_token]
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_admin] = _override_user
    app.dependency_overrides[require_buyer] = _override_user
    app.dependency_overrides[require_fresh_token] = _override_fresh_token

    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in overridden:
            app.dependency_overrides.pop(dep, None)


# ── SALES role filter (line 403) ─────────────────────────────────────────────


class TestSalesRoleFilter:
    """SALES users only see their own requisitions (line 403)."""

    def test_sales_user_sees_only_own_requisitions(self, db_session: Session, sales_user: User, test_user: User):
        req_own = Requisition(name="Sales Own Req", status="active", created_by=sales_user.id)
        req_other = Requisition(name="Other User Req", status="active", created_by=test_user.id)
        db_session.add_all([req_own, req_other])
        db_session.commit()

        for client in _sales_client(db_session, sales_user):
            resp = client.get("/v2/partials/requisitions")
            assert resp.status_code == 200
            text = resp.text
            assert "Sales Own Req" in text
            assert "Other User Req" not in text


# ── Bulk action invalid ID format (lines 1636-1639) ─────────────────────────


class TestBulkActionInvalidIds:
    """POST /v2/partials/requisitions/bulk/archive with non-integer IDs → 400."""

    def test_invalid_id_format_returns_400(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/bulk/archive",
            data={"ids": "abc,def"},
        )
        assert resp.status_code == 400

    def test_empty_ids_returns_400(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/bulk/archive",
            data={"ids": ""},
        )
        assert resp.status_code == 400

    def test_invalid_action_returns_400(self, client: TestClient, test_requisition: Requisition):
        resp = client.post(
            "/v2/partials/requisitions/bulk/delete",
            data={"ids": str(test_requisition.id)},
        )
        assert resp.status_code == 400


# ── Parse offer returns None (line 1475) ─────────────────────────────────────


class TestParseOfferNoneResult:
    """When parse_freeform_offer returns None, ctx['offers'] is set to [] (line 1475)."""

    async def test_parse_offer_none_result(self, db_session: Session, test_user: User, test_requisition: Requisition):
        from app.routers.htmx_views import parse_offer_action

        mock_req = _make_request(f"/v2/partials/requisitions/{test_requisition.id}/parse-offer")

        with patch(
            "app.services.freeform_parser_service.parse_freeform_offer",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await parse_offer_action(
                request=mock_req,
                req_id=test_requisition.id,
                raw_text="LM317T 100",
                user=test_user,
                db=db_session,
            )
        assert result is not None


# ── Search filter confidence / source / sort (lines 3113-3126) ───────────────


class TestSearchFilterPaths:
    """Direct coroutine calls to cover confidence/source/sort filter branches."""

    async def test_confidence_filter_high(self, db_session: Session, test_user: User):
        from app.routers.htmx_views import search_filter

        sample_results = [
            {
                "confidence_color": "green",
                "score": 0.9,
                "confidence_pct": 90,
                "unit_price": 1.0,
                "qty_available": 50,
                "sources_found": ["brokerbin"],
            },
            {
                "confidence_color": "red",
                "score": 0.3,
                "confidence_pct": 30,
                "unit_price": 0.5,
                "qty_available": 10,
                "sources_found": [],
            },
        ]

        mock_req = _make_request("/v2/partials/search/filter")

        with patch("app.routers.htmx_views._get_cached_search_results", return_value=sample_results):
            with patch("app.routers.htmx_views.templates") as mock_tpl:
                mock_tpl.get_template.return_value.render.return_value = "<div>card</div>"
                result = await search_filter(
                    request=mock_req,
                    search_id="test-123",
                    confidence="high",
                    source="all",
                    sort="best",
                    user=test_user,
                    db=db_session,
                )
        assert isinstance(result, HTMLResponse)

    async def test_source_filter(self, db_session: Session, test_user: User):
        from app.routers.htmx_views import search_filter

        sample_results = [
            {
                "confidence_color": "green",
                "score": 0.9,
                "confidence_pct": 90,
                "unit_price": 1.0,
                "qty_available": 50,
                "sources_found": ["brokerbin"],
            },
            {
                "confidence_color": "amber",
                "score": 0.7,
                "confidence_pct": 70,
                "unit_price": 0.8,
                "qty_available": 20,
                "sources_found": ["digikey"],
            },
        ]

        mock_req = _make_request("/v2/partials/search/filter")

        with patch("app.routers.htmx_views._get_cached_search_results", return_value=sample_results):
            with patch("app.routers.htmx_views.templates") as mock_tpl:
                mock_tpl.get_template.return_value.render.return_value = "<div>card</div>"
                result = await search_filter(
                    request=mock_req,
                    search_id="test-123",
                    confidence="all",
                    source="brokerbin",
                    sort="best",
                    user=test_user,
                    db=db_session,
                )
        assert isinstance(result, HTMLResponse)

    async def test_sort_cheapest(self, db_session: Session, test_user: User):
        from app.routers.htmx_views import search_filter

        sample_results = [
            {
                "confidence_color": "green",
                "score": 0.9,
                "confidence_pct": 90,
                "unit_price": 2.0,
                "qty_available": 50,
                "sources_found": [],
            },
            {
                "confidence_color": "amber",
                "score": 0.7,
                "confidence_pct": 70,
                "unit_price": 0.5,
                "qty_available": 20,
                "sources_found": [],
            },
        ]

        mock_req = _make_request("/v2/partials/search/filter")

        with patch("app.routers.htmx_views._get_cached_search_results", return_value=sample_results):
            with patch("app.routers.htmx_views.templates") as mock_tpl:
                mock_tpl.get_template.return_value.render.return_value = "<div>card</div>"
                result = await search_filter(
                    request=mock_req,
                    search_id="test-123",
                    confidence="all",
                    source="all",
                    sort="cheapest",
                    user=test_user,
                    db=db_session,
                )
        assert isinstance(result, HTMLResponse)

    async def test_sort_stock(self, db_session: Session, test_user: User):
        from app.routers.htmx_views import search_filter

        sample_results = [
            {
                "confidence_color": "green",
                "score": 0.9,
                "confidence_pct": 90,
                "unit_price": 1.0,
                "qty_available": 100,
                "sources_found": [],
            },
            {
                "confidence_color": "amber",
                "score": 0.5,
                "confidence_pct": 50,
                "unit_price": 0.8,
                "qty_available": 5,
                "sources_found": [],
            },
        ]

        mock_req = _make_request("/v2/partials/search/filter")

        with patch("app.routers.htmx_views._get_cached_search_results", return_value=sample_results):
            with patch("app.routers.htmx_views.templates") as mock_tpl:
                mock_tpl.get_template.return_value.render.return_value = "<div>card</div>"
                result = await search_filter(
                    request=mock_req,
                    search_id="test-123",
                    confidence="all",
                    source="all",
                    sort="stock",
                    user=test_user,
                    db=db_session,
                )
        assert isinstance(result, HTMLResponse)


# ── Create site: invalid owner_id + existing owner (lines 4778-4791) ─────────


class TestCreateSiteOwnerValidation:
    """POST /v2/partials/customers/{company_id}/sites owner_id edge cases."""

    def test_invalid_owner_id_string_ignored(self, client: TestClient, db_session: Session):
        """Non-integer owner_id is treated as None (lines 4778-4779)."""
        company = Company(name="Test Co", is_active=True)
        db_session.add(company)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/customers/{company.id}/sites",
            data={"site_name": "HQ", "owner_id": "not-a-number"},
        )
        assert resp.status_code == 200
        assert "HQ" in resp.text

    def test_owner_already_owns_site_returns_error(self, client: TestClient, db_session: Session, test_user: User):
        """Owner who already owns a site returns error HTML (lines 4780-4791)."""
        company = Company(name="Test Co 2", is_active=True)
        db_session.add(company)
        db_session.commit()

        existing_site = CustomerSite(
            company_id=company.id,
            site_name="Existing Site",
            owner_id=test_user.id,
        )
        db_session.add(existing_site)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/customers/{company.id}/sites",
            data={"site_name": "New Site", "owner_id": str(test_user.id)},
        )
        assert resp.status_code == 200
        assert "already owns" in resp.text.lower() or "only own one site" in resp.text.lower()

    def test_missing_site_name_returns_error(self, client: TestClient, db_session: Session):
        """Empty site_name returns validation error."""
        company = Company(name="Test Co 3", is_active=True)
        db_session.add(company)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/customers/{company.id}/sites",
            data={"site_name": ""},
        )
        assert resp.status_code == 200
        assert "required" in resp.text.lower()
