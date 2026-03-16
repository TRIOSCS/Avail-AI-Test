"""tests/test_htmx_company_vendor_crud.py — Tests for V2 company & vendor CRUD.

Tests create/update/typeahead for companies and
edit/update/blacklist/delete/typeahead for vendors via HTMX endpoints.

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_user, test_company, test_vendor_card)
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import Company, CustomerSite, User, VendorCard


# ── Company CRUD ──────────────────────────────────────────────────────


class TestCompanyCreateForm:
    def test_returns_form_html(self, client):
        resp = client.get("/partials/companies/create-form")
        assert resp.status_code == 200
        assert "name" in resp.text.lower()
        assert 'hx-post' in resp.text or "hx-post" in resp.text


class TestCompanyCreate:
    def test_create_company_success(self, client, db_session: Session):
        resp = client.post(
            "/partials/companies/create",
            data={"name": "New Test Corp", "industry": "Electronics"},
        )
        assert resp.status_code == 200
        # Verify company created in DB
        co = db_session.query(Company).filter_by(name="New Test Corp").first()
        assert co is not None
        assert co.industry == "Electronics"
        # Verify HQ site auto-created
        site = db_session.query(CustomerSite).filter_by(company_id=co.id).first()
        assert site is not None
        assert site.site_name == "HQ"

    def test_create_company_blank_name_fails(self, client):
        resp = client.post(
            "/partials/companies/create",
            data={"name": "   "},
        )
        # Should return error (either 422 or 200 with error message)
        assert resp.status_code in (200, 422)
        if resp.status_code == 200:
            assert "required" in resp.text.lower() or "error" in resp.text.lower()


class TestCompanyEdit:
    def test_edit_form_returns_html(self, client, test_company):
        resp = client.get(f"/partials/companies/{test_company.id}/edit")
        assert resp.status_code == 200
        assert test_company.name in resp.text

    def test_edit_nonexistent_returns_404(self, client):
        resp = client.get("/partials/companies/99999/edit")
        assert resp.status_code == 404


class TestCompanyUpdate:
    def test_update_company_name(self, client, test_company, db_session: Session):
        resp = client.put(
            f"/partials/companies/{test_company.id}",
            data={"name": "Updated Corp Name"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_company)
        assert test_company.name == "Updated Corp Name"

    def test_update_nonexistent_returns_404(self, client):
        resp = client.put(
            "/partials/companies/99999",
            data={"name": "Ghost"},
        )
        assert resp.status_code == 404


class TestCompanyTypeahead:
    def test_typeahead_returns_json(self, client, test_company):
        resp = client.get("/partials/companies/typeahead", params={"q": "Acme"})
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        assert data[0]["name"] == "Acme Electronics"

    def test_typeahead_short_query(self, client):
        resp = client.get("/partials/companies/typeahead", params={"q": "A"})
        assert resp.status_code == 200
        data = resp.json()
        assert data == []


# ── Vendor CRUD ───────────────────────────────────────────────────────


class TestVendorEdit:
    def test_edit_form_returns_html(self, client, test_vendor_card):
        resp = client.get(f"/partials/vendors/{test_vendor_card.id}/edit")
        assert resp.status_code == 200
        assert test_vendor_card.display_name in resp.text

    def test_edit_nonexistent_returns_404(self, client):
        resp = client.get("/partials/vendors/99999/edit")
        assert resp.status_code == 404


class TestVendorUpdate:
    def test_update_vendor_display_name(self, client, test_vendor_card, db_session: Session):
        resp = client.put(
            f"/partials/vendors/{test_vendor_card.id}",
            data={"display_name": "Arrow Corp"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert test_vendor_card.display_name == "Arrow Corp"

    def test_update_vendor_emails(self, client, test_vendor_card, db_session: Session):
        resp = client.put(
            f"/partials/vendors/{test_vendor_card.id}",
            data={"emails": "new@arrow.com, sales@arrow.com"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert "new@arrow.com" in test_vendor_card.emails

    def test_update_nonexistent_returns_404(self, client):
        resp = client.put(
            "/partials/vendors/99999",
            data={"display_name": "Ghost"},
        )
        assert resp.status_code == 404


class TestVendorBlacklist:
    def test_toggle_blacklist(self, client, test_vendor_card, db_session: Session):
        assert test_vendor_card.is_blacklisted is False
        resp = client.post(f"/partials/vendors/{test_vendor_card.id}/blacklist")
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert test_vendor_card.is_blacklisted is True

    def test_toggle_blacklist_back(self, client, test_vendor_card, db_session: Session):
        test_vendor_card.is_blacklisted = True
        db_session.commit()
        resp = client.post(f"/partials/vendors/{test_vendor_card.id}/blacklist")
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert test_vendor_card.is_blacklisted is False


class TestVendorDelete:
    def test_delete_vendor(self, client, test_vendor_card, db_session: Session):
        vid = test_vendor_card.id
        resp = client.delete(f"/partials/vendors/{vid}")
        assert resp.status_code == 200
        assert db_session.query(VendorCard).filter_by(id=vid).first() is None

    def test_delete_nonexistent_returns_404(self, client):
        resp = client.delete("/partials/vendors/99999")
        assert resp.status_code == 404


class TestVendorTypeahead:
    def test_typeahead_returns_json(self, client, test_vendor_card):
        resp = client.get("/partials/vendors/typeahead", params={"q": "Arrow"})
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        assert data[0]["name"] == "Arrow Electronics"

    def test_typeahead_short_query_rejected(self, client):
        resp = client.get("/partials/vendors/typeahead", params={"q": "A"})
        assert resp.status_code == 422  # min_length=2 validation
