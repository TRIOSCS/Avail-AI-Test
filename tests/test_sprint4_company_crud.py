"""test_sprint4_company_crud.py — Tests for Sprint 4 company CRUD + site contacts.

Verifies: Create company, edit company, edit site, typeahead,
duplicate check, site contact notes.

Called by: pytest
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Company, CustomerSite, SiteContact, User


# ── Create Company ───────────────────────────────────────────────────


class TestCreateCompany:
    def test_create_form_renders(self, client: TestClient):
        resp = client.get(
            "/v2/partials/companies/create-form",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Create Company" in resp.text

    def test_create_saves_company(self, client: TestClient, db_session: Session):
        resp = client.post(
            "/v2/partials/companies/create",
            data={"name": "NewCo Electronics", "website": "https://newco.com", "industry": "Semiconductors"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        co = db_session.query(Company).filter(Company.name == "NewCo Electronics").first()
        assert co is not None
        assert co.website == "https://newco.com"

    def test_create_auto_creates_hq_site(self, client: TestClient, db_session: Session):
        client.post(
            "/v2/partials/companies/create",
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
            "/v2/partials/companies/create",
            data={"name": ""},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400

    def test_create_rejects_duplicate(self, client: TestClient, test_company: Company):
        resp = client.post(
            "/v2/partials/companies/create",
            data={"name": test_company.name},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 409


# ── Edit Company ─────────────────────────────────────────────────────


class TestEditCompany:
    def test_edit_form_renders(self, client: TestClient, test_company: Company):
        resp = client.get(
            f"/v2/partials/companies/{test_company.id}/edit-form",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Edit Company" in resp.text
        assert test_company.name in resp.text

    def test_edit_saves_changes(self, client: TestClient, test_company: Company, db_session: Session):
        resp = client.post(
            f"/v2/partials/companies/{test_company.id}/edit",
            data={"name": "Acme Global", "website": "https://acme-global.com"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_company)
        assert test_company.name == "Acme Global"
        assert test_company.website == "https://acme-global.com"

    def test_edit_nonexistent_404(self, client: TestClient):
        resp = client.post(
            "/v2/partials/companies/99999/edit",
            data={"name": "Ghost"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404


# ── Edit Site ────────────────────────────────────────────────────────


class TestEditSite:
    def test_edit_site(self, client: TestClient, test_company: Company, test_customer_site: CustomerSite, db_session: Session):
        resp = client.post(
            f"/v2/partials/companies/{test_company.id}/sites/{test_customer_site.id}/edit",
            data={"site_name": "Branch Office", "city": "Dallas", "country": "US"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_customer_site)
        assert test_customer_site.site_name == "Branch Office"
        assert test_customer_site.city == "Dallas"

    def test_edit_site_nonexistent(self, client: TestClient, test_company: Company):
        resp = client.post(
            f"/v2/partials/companies/{test_company.id}/sites/99999/edit",
            data={"site_name": "Ghost"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404


# ── Typeahead ────────────────────────────────────────────────────────


class TestTypeahead:
    def test_typeahead_returns_matches(self, client: TestClient, test_company: Company):
        resp = client.get(
            f"/v2/partials/companies/typeahead?q={test_company.name[:4]}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert test_company.name in resp.text

    def test_typeahead_short_query_empty(self, client: TestClient):
        resp = client.get(
            "/v2/partials/companies/typeahead?q=A",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert resp.text == ""


# ── Duplicate Check ──────────────────────────────────────────────────


class TestDuplicateCheck:
    def test_detects_duplicate(self, client: TestClient, test_company: Company):
        resp = client.get(
            f"/v2/partials/companies/check-duplicate?name={test_company.name}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "already exists" in resp.text

    def test_no_duplicate(self, client: TestClient):
        resp = client.get(
            "/v2/partials/companies/check-duplicate?name=Unique Corp XYZ",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert resp.text == ""


# ── Contact Notes ────────────────────────────────────────────────────


class TestContactNotes:
    @pytest.fixture()
    def site_contact(self, db_session: Session, test_customer_site: CustomerSite):
        """A site contact for testing notes."""
        c = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="Jane Doe",
            email="jane@acme-electronics.com",
            title="Buyer",
        )
        db_session.add(c)
        db_session.commit()
        db_session.refresh(c)
        return c

    def test_get_notes_empty(self, client: TestClient, test_company: Company, test_customer_site: CustomerSite, site_contact: SiteContact):
        resp = client.get(
            f"/v2/partials/companies/{test_company.id}/sites/{test_customer_site.id}/contacts/{site_contact.id}/notes",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "No notes yet" in resp.text

    def test_add_note(self, client: TestClient, test_company: Company, test_customer_site: CustomerSite, site_contact: SiteContact):
        resp = client.post(
            f"/v2/partials/companies/{test_company.id}/sites/{test_customer_site.id}/contacts/{site_contact.id}/notes",
            data={"notes": "Called about RFQ, very responsive."},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Called about RFQ" in resp.text

    def test_add_empty_note_rejected(self, client: TestClient, test_company: Company, test_customer_site: CustomerSite, site_contact: SiteContact):
        resp = client.post(
            f"/v2/partials/companies/{test_company.id}/sites/{test_customer_site.id}/contacts/{site_contact.id}/notes",
            data={"notes": ""},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400

    def test_notes_nonexistent_contact(self, client: TestClient, test_company: Company, test_customer_site: CustomerSite):
        resp = client.get(
            f"/v2/partials/companies/{test_company.id}/sites/{test_customer_site.id}/contacts/99999/notes",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404
