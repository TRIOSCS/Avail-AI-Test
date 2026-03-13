"""
test_htmx_companies.py — Tests for Phase 3 Task 6: Companies list + detail drawer.
Verifies companies list page, rows partial with search/owner filter/pagination,
detail drawer rendering, and tab content loading.
Called by: pytest
Depends on: app/routers/views.py, app/templates/partials/companies/
"""

import os

os.environ.setdefault("TESTING", "1")
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("USE_HTMX", "true")

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.models import ActivityLog, Company, CustomerSite, Requisition, SiteContact, User


@pytest.fixture()
def htmx_client(db_session, test_user):
    """TestClient with views router registered and auth overridden."""
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app
    from app.routers.views import router as views_router

    route_paths = [r.path for r in app.routes]
    if "/views/companies" not in route_paths:
        app.include_router(views_router)

    def _override_db():
        yield db_session

    def _override_user():
        return test_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


@pytest.fixture()
def owner_user(db_session):
    """A second user to serve as account owner."""
    user = User(
        email="owner@trioscs.com",
        name="Jane Owner",
        role="buyer",
        azure_id="test-azure-owner",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def sample_companies(db_session, test_user, owner_user):
    """Create multiple companies for list/filter/pagination tests."""
    companies = []
    for name, industry, owner_id in [
        ("Acme Corp", "Electronics", test_user.id),
        ("Globex Inc", "Semiconductors", owner_user.id),
        ("Initech Ltd", "Electronics", None),
        ("Umbrella Co", "Defense", owner_user.id),
        ("Inactive Co", "Other", None),
    ]:
        c = Company(
            name=name,
            industry=industry,
            account_owner_id=owner_id,
            is_active=(name != "Inactive Co"),
            site_count=2 if name == "Acme Corp" else 0,
            open_req_count=1 if name == "Acme Corp" else 0,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(c)
        companies.append(c)
    db_session.commit()
    for c in companies:
        db_session.refresh(c)
    return companies


class TestCompaniesListPage:
    """Tests for GET /views/companies — full page HTML."""

    def test_companies_list_page_returns_html(self, htmx_client):
        resp = htmx_client.get("/views/companies")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_companies_list_page_contains_table(self, htmx_client):
        resp = htmx_client.get("/views/companies")
        assert "company-table-body" in resp.text
        assert "Companies" in resp.text

    def test_companies_list_page_has_new_button(self, htmx_client):
        resp = htmx_client.get("/views/companies")
        assert "New Company" in resp.text

    def test_companies_list_shows_active_data(self, htmx_client, sample_companies):
        resp = htmx_client.get("/views/companies")
        assert resp.status_code == 200
        assert "Acme Corp" in resp.text
        assert "Globex Inc" in resp.text
        # Inactive company should not appear
        assert "Inactive Co" not in resp.text

    def test_companies_list_has_search_input(self, htmx_client):
        resp = htmx_client.get("/views/companies")
        assert 'type="search"' in resp.text
        assert "delay:300ms" in resp.text

    def test_companies_list_has_owner_filter(self, htmx_client):
        resp = htmx_client.get("/views/companies")
        assert "All Owners" in resp.text


class TestCompaniesRowsPartial:
    """Tests for GET /views/companies/rows — HTMX rows swap target."""

    def test_rows_returns_html(self, htmx_client, sample_companies):
        resp = htmx_client.get("/views/companies/rows")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_rows_contain_company_names(self, htmx_client, sample_companies):
        resp = htmx_client.get("/views/companies/rows")
        assert "Acme Corp" in resp.text
        assert "Globex Inc" in resp.text
        assert "Initech Ltd" in resp.text
        assert "Umbrella Co" in resp.text

    def test_rows_exclude_inactive(self, htmx_client, sample_companies):
        resp = htmx_client.get("/views/companies/rows")
        assert "Inactive Co" not in resp.text


class TestCompaniesSearchFilter:
    """Tests for search filtering on companies rows."""

    def test_search_by_name(self, htmx_client, sample_companies):
        resp = htmx_client.get("/views/companies/rows?q=Acme")
        assert "Acme Corp" in resp.text
        assert "Globex Inc" not in resp.text

    def test_search_by_industry(self, htmx_client, sample_companies):
        resp = htmx_client.get("/views/companies/rows?q=Semiconductors")
        assert "Globex Inc" in resp.text
        assert "Acme Corp" not in resp.text

    def test_search_no_results(self, htmx_client, sample_companies):
        resp = htmx_client.get("/views/companies/rows?q=nonexistent_xyz")
        assert resp.status_code == 200
        assert "No companies found" in resp.text


class TestCompaniesOwnerFilter:
    """Tests for owner filter on companies rows."""

    def test_filter_by_owner(self, htmx_client, sample_companies):
        resp = htmx_client.get("/views/companies/rows?owner=Jane Owner")
        assert "Globex Inc" in resp.text
        assert "Umbrella Co" in resp.text
        # Acme is owned by test_user, not Jane Owner
        assert "Acme Corp" not in resp.text

    def test_owner_filter_via_hx_include(self, htmx_client, sample_companies):
        """The owner_filter param (from hx-include) also works."""
        resp = htmx_client.get("/views/companies/rows?owner_filter=Jane Owner")
        assert "Globex Inc" in resp.text
        assert "Acme Corp" not in resp.text


class TestCompaniesPagination:
    """Tests for page param on companies rows."""

    def test_page_1_returns_results(self, htmx_client, sample_companies):
        resp = htmx_client.get("/views/companies/rows?page=1")
        assert resp.status_code == 200
        assert "Acme Corp" in resp.text

    def test_page_beyond_range_clamps(self, htmx_client, sample_companies):
        resp = htmx_client.get("/views/companies/rows?page=999")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]


class TestCompanyDetailDrawer:
    """Tests for GET /views/companies/{id} — detail drawer partial."""

    def test_detail_returns_html(self, htmx_client, sample_companies):
        company = sample_companies[0]  # Acme Corp
        resp = htmx_client.get(f"/views/companies/{company.id}")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_detail_contains_company_name(self, htmx_client, sample_companies):
        company = sample_companies[0]
        resp = htmx_client.get(f"/views/companies/{company.id}")
        assert "Acme Corp" in resp.text

    def test_detail_has_tabs(self, htmx_client, sample_companies):
        company = sample_companies[0]
        resp = htmx_client.get(f"/views/companies/{company.id}")
        for tab in ["Overview", "Sites", "Contacts", "Activity", "Pipeline"]:
            assert tab in resp.text

    def test_detail_has_enrich_button(self, htmx_client, sample_companies):
        company = sample_companies[0]
        resp = htmx_client.get(f"/views/companies/{company.id}")
        assert "Enrich" in resp.text

    def test_detail_has_close_button(self, htmx_client, sample_companies):
        company = sample_companies[0]
        resp = htmx_client.get(f"/views/companies/{company.id}")
        assert "close-drawer" in resp.text

    def test_detail_404_for_missing(self, htmx_client):
        resp = htmx_client.get("/views/companies/99999")
        assert resp.status_code == 404


class TestCompanyTabOverview:
    """Tests for GET /views/companies/{id}/tab/overview."""

    def test_overview_returns_html(self, htmx_client, sample_companies):
        company = sample_companies[0]
        resp = htmx_client.get(f"/views/companies/{company.id}/tab/overview")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_overview_shows_fields(self, htmx_client, db_session, sample_companies):
        company = sample_companies[0]
        company.website = "https://acme.com"
        company.phone = "555-1234"
        db_session.commit()
        resp = htmx_client.get(f"/views/companies/{company.id}/tab/overview")
        assert "https://acme.com" in resp.text
        assert "555-1234" in resp.text

    def test_overview_has_notes_section(self, htmx_client, sample_companies):
        company = sample_companies[0]
        resp = htmx_client.get(f"/views/companies/{company.id}/tab/overview")
        assert "Notes" in resp.text


class TestCompanyTabSites:
    """Tests for GET /views/companies/{id}/tab/sites."""

    def test_sites_empty(self, htmx_client, sample_companies):
        company = sample_companies[0]
        resp = htmx_client.get(f"/views/companies/{company.id}/tab/sites")
        assert resp.status_code == 200
        assert "No sites yet" in resp.text

    def test_sites_with_data(self, htmx_client, db_session, sample_companies):
        company = sample_companies[0]
        site = CustomerSite(
            company_id=company.id,
            site_name="Acme HQ",
            contact_name="John",
            city="Dallas",
            site_type="HQ",
        )
        db_session.add(site)
        db_session.commit()
        resp = htmx_client.get(f"/views/companies/{company.id}/tab/sites")
        assert "Acme HQ" in resp.text
        assert "Dallas" in resp.text


class TestCompanyTabPipeline:
    """Tests for GET /views/companies/{id}/tab/pipeline."""

    def test_pipeline_empty(self, htmx_client, sample_companies):
        company = sample_companies[0]
        resp = htmx_client.get(f"/views/companies/{company.id}/tab/pipeline")
        assert resp.status_code == 200
        assert "No open requisitions" in resp.text

    def test_pipeline_with_requisition(self, htmx_client, db_session, test_user, sample_companies):
        company = sample_companies[0]  # Acme Corp
        req = Requisition(
            name="REQ-PIPELINE-001",
            customer_name="Acme Corp",
            status="open",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()
        resp = htmx_client.get(f"/views/companies/{company.id}/tab/pipeline")
        assert "REQ-PIPELINE-001" in resp.text


class TestCompanyTabInvalid:
    """Tests for invalid tab names."""

    def test_invalid_tab_returns_404(self, htmx_client, sample_companies):
        company = sample_companies[0]
        resp = htmx_client.get(f"/views/companies/{company.id}/tab/invalid_tab")
        assert resp.status_code == 404
