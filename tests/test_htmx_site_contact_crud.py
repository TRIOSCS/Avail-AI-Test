"""tests/test_htmx_site_contact_crud.py — Tests for site & site-contact CRUD endpoints.

Tests create/update sites, and create/update/delete/add-form for site contacts
via the HTMX partials in routers/htmx/companies.py.

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_company, test_customer_site)
"""

import pytest
from sqlalchemy.orm import Session

from app.models import Company, CustomerSite
from app.models.crm import SiteContact


# ── Site CRUD ────────────────────────────────────────────────────────


class TestSiteCreate:
    def test_create_site_success(self, client, test_company, db_session: Session):
        resp = client.post(
            f"/v2/partials/companies/{test_company.id}/sites",
            data={"site_name": "West Coast Warehouse", "city": "Portland", "state": "OR"},
        )
        assert resp.status_code == 200
        assert "created successfully" in resp.text
        site = db_session.query(CustomerSite).filter_by(
            company_id=test_company.id, site_name="West Coast Warehouse"
        ).first()
        assert site is not None
        assert site.city == "Portland"
        assert site.state == "OR"

    def test_create_site_blank_name_fails(self, client, test_company):
        resp = client.post(
            f"/v2/partials/companies/{test_company.id}/sites",
            data={"site_name": "   "},
        )
        assert resp.status_code == 422
        assert "required" in resp.text.lower()

    def test_create_site_nonexistent_company(self, client):
        resp = client.post(
            "/v2/partials/companies/99999/sites",
            data={"site_name": "Ghost Site"},
        )
        assert resp.status_code == 404

    def test_create_site_has_hx_trigger(self, client, test_company):
        resp = client.post(
            f"/v2/partials/companies/{test_company.id}/sites",
            data={"site_name": "Triggered Site"},
        )
        assert resp.status_code == 200
        assert resp.headers.get("HX-Trigger") == "refreshSites"


class TestSiteUpdate:
    def test_update_site_fields(self, client, test_customer_site, db_session: Session):
        resp = client.put(
            f"/v2/partials/sites/{test_customer_site.id}",
            data={"city": "Austin", "state": "TX", "payment_terms": "Net 30"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_customer_site)
        assert test_customer_site.city == "Austin"
        assert test_customer_site.state == "TX"
        assert test_customer_site.payment_terms == "Net 30"

    def test_update_site_blank_name_fails(self, client, test_customer_site):
        resp = client.put(
            f"/v2/partials/sites/{test_customer_site.id}",
            data={"site_name": "   "},
        )
        assert resp.status_code == 422

    def test_update_site_nonexistent(self, client):
        resp = client.put("/v2/partials/sites/99999", data={"city": "Nowhere"})
        assert resp.status_code == 404

    def test_update_site_has_hx_trigger(self, client, test_customer_site):
        resp = client.put(
            f"/v2/partials/sites/{test_customer_site.id}",
            data={"notes": "Updated notes"},
        )
        assert resp.status_code == 200
        assert resp.headers.get("HX-Trigger") == "refreshSites"


# ── Site Contact CRUD ────────────────────────────────────────────────


class TestSiteContactAddForm:
    def test_returns_form_html(self, client, test_customer_site):
        resp = client.get(f"/v2/partials/sites/{test_customer_site.id}/contacts/add-form")
        assert resp.status_code == 200
        assert "full_name" in resp.text
        assert "hx-post" in resp.text

    def test_nonexistent_site_returns_404(self, client):
        resp = client.get("/v2/partials/sites/99999/contacts/add-form")
        assert resp.status_code == 404


class TestSiteContactCreate:
    def test_create_contact_success(self, client, test_customer_site, db_session: Session):
        resp = client.post(
            f"/v2/partials/sites/{test_customer_site.id}/contacts",
            data={"full_name": "Bob Smith", "email": "bob@acme.com", "title": "Buyer"},
        )
        assert resp.status_code == 200
        assert "added successfully" in resp.text
        contact = db_session.query(SiteContact).filter_by(
            customer_site_id=test_customer_site.id, full_name="Bob Smith"
        ).first()
        assert contact is not None
        assert contact.email == "bob@acme.com"

    def test_create_contact_blank_name_fails(self, client, test_customer_site):
        resp = client.post(
            f"/v2/partials/sites/{test_customer_site.id}/contacts",
            data={"full_name": "  "},
        )
        assert resp.status_code == 422
        assert "required" in resp.text.lower()

    def test_create_contact_duplicate_email(self, client, test_customer_site, db_session: Session):
        # Create first contact
        c = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="Existing Contact",
            email="dupe@acme.com",
            is_active=True,
        )
        db_session.add(c)
        db_session.commit()

        # Try creating with same email
        resp = client.post(
            f"/v2/partials/sites/{test_customer_site.id}/contacts",
            data={"full_name": "New Contact", "email": "dupe@acme.com"},
        )
        assert resp.status_code == 422
        assert "already exists" in resp.text.lower()

    def test_create_contact_primary_clears_others(self, client, test_customer_site, db_session: Session):
        # Create existing primary contact
        c = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="Old Primary",
            email="old@acme.com",
            is_primary=True,
            is_active=True,
        )
        db_session.add(c)
        db_session.commit()

        # Create new primary
        resp = client.post(
            f"/v2/partials/sites/{test_customer_site.id}/contacts",
            data={"full_name": "New Primary", "email": "new@acme.com", "is_primary": "true"},
        )
        assert resp.status_code == 200
        db_session.refresh(c)
        assert c.is_primary is False

    def test_create_contact_has_hx_trigger(self, client, test_customer_site):
        resp = client.post(
            f"/v2/partials/sites/{test_customer_site.id}/contacts",
            data={"full_name": "Trigger Test"},
        )
        assert resp.status_code == 200
        assert resp.headers.get("HX-Trigger") == "refreshContacts"

    def test_create_contact_nonexistent_site(self, client):
        resp = client.post(
            "/v2/partials/sites/99999/contacts",
            data={"full_name": "Ghost"},
        )
        assert resp.status_code == 404


class TestSiteContactUpdate:
    def _make_contact(self, db_session, site_id):
        c = SiteContact(
            customer_site_id=site_id,
            full_name="Update Me",
            email="update@acme.com",
            is_active=True,
        )
        db_session.add(c)
        db_session.commit()
        db_session.refresh(c)
        return c

    def test_update_contact_fields(self, client, test_customer_site, db_session: Session):
        contact = self._make_contact(db_session, test_customer_site.id)
        resp = client.put(
            f"/v2/partials/sites/{test_customer_site.id}/contacts/{contact.id}",
            data={"title": "Director", "phone": "555-1234"},
        )
        assert resp.status_code == 200
        db_session.refresh(contact)
        assert contact.title == "Director"
        assert contact.phone == "555-1234"

    def test_update_contact_blank_name_fails(self, client, test_customer_site, db_session: Session):
        contact = self._make_contact(db_session, test_customer_site.id)
        resp = client.put(
            f"/v2/partials/sites/{test_customer_site.id}/contacts/{contact.id}",
            data={"full_name": "   "},
        )
        assert resp.status_code == 422

    def test_update_contact_primary_clears_others(self, client, test_customer_site, db_session: Session):
        old = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="Old Primary",
            is_primary=True,
            is_active=True,
        )
        db_session.add(old)
        db_session.commit()
        new = self._make_contact(db_session, test_customer_site.id)

        resp = client.put(
            f"/v2/partials/sites/{test_customer_site.id}/contacts/{new.id}",
            data={"is_primary": "true"},
        )
        assert resp.status_code == 200
        db_session.refresh(old)
        assert old.is_primary is False
        db_session.refresh(new)
        assert new.is_primary is True

    def test_update_nonexistent_contact(self, client, test_customer_site):
        resp = client.put(
            f"/v2/partials/sites/{test_customer_site.id}/contacts/99999",
            data={"title": "Ghost"},
        )
        assert resp.status_code == 404


class TestSiteContactDelete:
    def test_delete_contact(self, client, test_customer_site, db_session: Session):
        contact = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="Delete Me",
            is_active=True,
        )
        db_session.add(contact)
        db_session.commit()
        cid = contact.id

        resp = client.delete(
            f"/v2/partials/sites/{test_customer_site.id}/contacts/{cid}"
        )
        assert resp.status_code == 200
        assert resp.text == ""
        assert resp.headers.get("HX-Trigger") == "refreshContacts"
        assert db_session.query(SiteContact).filter_by(id=cid).first() is None

    def test_delete_nonexistent_contact(self, client, test_customer_site):
        resp = client.delete(
            f"/v2/partials/sites/{test_customer_site.id}/contacts/99999"
        )
        assert resp.status_code == 404
