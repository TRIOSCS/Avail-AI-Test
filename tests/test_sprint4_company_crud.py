"""test_sprint4_company_crud.py — Tests for Sprint 4 company CRUD + site contacts.

Verifies: Create company, edit company, edit site, typeahead,
duplicate check, site contact notes.

Called by: pytest
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Company, CustomerSite, User

# ── Create Company ───────────────────────────────────────────────────


class TestCreateCompany:
    def test_create_form_renders(self, client: TestClient):
        resp = client.get(
            "/v2/partials/customers/create-form",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Create Company" in resp.text

    def test_create_saves_company(self, client: TestClient, db_session: Session):
        resp = client.post(
            "/v2/partials/customers/create",
            data={"name": "NewCo Electronics", "website": "https://newco.com", "industry": "Semiconductors"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        co = db_session.query(Company).filter(Company.name == "NewCo Electronics").first()
        assert co is not None
        assert co.website == "https://newco.com"

    def test_create_auto_creates_hq_site(self, client: TestClient, db_session: Session):
        client.post(
            "/v2/partials/customers/create",
            data={"name": "SiteCo Inc"},
            headers={"HX-Request": "true"},
        )
        co = db_session.query(Company).filter(Company.name == "SiteCo Inc").first()
        assert co is not None
        site = db_session.query(CustomerSite).filter(CustomerSite.company_id == co.id).first()
        assert site is not None
        assert site.site_name == "HQ"

    def test_create_rejects_empty_name(self, client: TestClient):
        resp = client.post(
            "/v2/partials/customers/create",
            data={"name": ""},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400

    def test_create_rejects_duplicate(self, client: TestClient, test_company: Company):
        resp = client.post(
            "/v2/partials/customers/create",
            data={"name": test_company.name},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 409


# ── Edit Company ─────────────────────────────────────────────────────


class TestEditCompany:
    def test_edit_form_renders(self, client: TestClient, test_company: Company):
        resp = client.get(
            f"/v2/partials/customers/{test_company.id}/edit-form",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Edit Company" in resp.text
        assert test_company.name in resp.text

    def test_edit_saves_changes(self, client: TestClient, test_company: Company, test_user: User, db_session: Session):
        test_company.account_owner_id = test_user.id
        db_session.commit()
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/edit",
            data={"name": "Acme Global", "website": "https://acme-global.com"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_company)
        assert test_company.name == "Acme Global"
        assert test_company.website == "https://acme-global.com"

    def test_edit_nonexistent_404(self, client: TestClient):
        resp = client.post(
            "/v2/partials/customers/99999/edit",
            data={"name": "Ghost"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404


# ── Edit Site ────────────────────────────────────────────────────────


class TestEditSite:
    def test_edit_site(
        self,
        client: TestClient,
        test_company: Company,
        test_customer_site: CustomerSite,
        test_user: User,
        db_session: Session,
    ):
        test_company.account_owner_id = test_user.id
        db_session.commit()
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/{test_customer_site.id}/edit",
            data={"site_name": "Branch Office", "city": "Dallas", "country": "US"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_customer_site)
        assert test_customer_site.site_name == "Branch Office"
        assert test_customer_site.city == "Dallas"

    def test_edit_site_nonexistent(self, client: TestClient, test_company: Company):
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/99999/edit",
            data={"site_name": "Ghost"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404

    def test_edit_form_targets_tab_panel_not_shell(
        self, client: TestClient, test_company: Company, test_customer_site: CustomerSite
    ):
        """F1: the site-edit modal must swap the Sites tab panel (#company-tab-content),
        NOT #main-content. The edit handler returns the sites_tab fragment (same as a
        Sites-tab click); targeting #main-content replaced the whole page/workspace shell
        with a bare sites list, wiping the header, tabs, and account list."""
        resp = client.get(
            f"/v2/partials/customers/{test_company.id}/sites/{test_customer_site.id}/edit-form",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "hx-target='#company-tab-content'" in resp.text
        assert "hx-target='#main-content'" not in resp.text


# ── Typeahead ────────────────────────────────────────────────────────


class TestTypeahead:
    def test_typeahead_returns_matches(self, client: TestClient, test_company: Company):
        resp = client.get(
            f"/v2/partials/customers/typeahead?q={test_company.name[:4]}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert test_company.name in resp.text

    def test_typeahead_short_query_empty(self, client: TestClient):
        resp = client.get(
            "/v2/partials/customers/typeahead?q=A",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert resp.text == ""


# ── Duplicate Check ──────────────────────────────────────────────────


class TestDuplicateCheck:
    def test_detects_duplicate(self, client: TestClient, test_company: Company):
        resp = client.get(
            f"/v2/partials/customers/check-duplicate?name={test_company.name}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "already exists" in resp.text

    def test_no_duplicate(self, client: TestClient):
        resp = client.get(
            "/v2/partials/customers/check-duplicate?name=Unique Corp XYZ",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert resp.text == ""
