"""test_phase4_sites_contacts.py — Tests for Phase 4: Sites & Customer Contacts.

Verifies: sites tab rendering, site CRUD (add/delete), site contacts
CRUD (add/delete/set-primary), contacts tab rendering, dedup guard.

Called by: pytest
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Company, CustomerSite, SiteContact, User

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def company_with_site(db_session: Session) -> Company:
    """A company with one active site."""
    co = Company(
        name="Test Corp",
        website="https://testcorp.com",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(co)
    db_session.flush()

    site = CustomerSite(
        company_id=co.id,
        site_name="HQ Office",
        site_type="hq",
        city="Dallas",
        country="US",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(site)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def site(db_session: Session, company_with_site: Company) -> CustomerSite:
    """The first site of company_with_site."""
    return db_session.query(CustomerSite).filter(CustomerSite.company_id == company_with_site.id).first()


@pytest.fixture()
def site_contact(db_session: Session, site: CustomerSite) -> SiteContact:
    """A contact on the test site."""
    c = SiteContact(
        customer_site_id=site.id,
        full_name="John Buyer",
        title="Purchasing Manager",
        email="john@testcorp.com",
        phone="+1-555-0100",
        is_primary=False,
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


# ── Sites Tab ─────────────────────────────────────────────────────────


class TestSitesTab:
    """Tests for the sites tab in company detail."""

    def test_sites_tab_loads(self, client: TestClient, company_with_site: Company):
        resp = client.get(f"/v2/partials/customers/{company_with_site.id}/tab/sites")
        assert resp.status_code == 200
        assert "HQ Office" in resp.text
        assert "Add Site" in resp.text

    def test_sites_tab_empty_state(self, client: TestClient, test_company: Company):
        resp = client.get(f"/v2/partials/customers/{test_company.id}/tab/sites")
        assert resp.status_code == 200
        assert "No sites" in resp.text


# ── Site CRUD ─────────────────────────────────────────────────────────


class TestSiteCrud:
    """Tests for creating and deleting sites."""

    def test_create_site(self, client: TestClient, db_session: Session, company_with_site: Company):
        resp = client.post(
            f"/v2/partials/customers/{company_with_site.id}/sites",
            data={
                "site_name": "Branch Office",
                "site_type": "branch",
                "city": "Austin",
                "country": "US",
            },
        )
        assert resp.status_code == 200
        assert "Branch Office" in resp.text

        count = db_session.query(CustomerSite).filter(CustomerSite.company_id == company_with_site.id).count()
        assert count == 2

    def test_create_site_requires_name(self, client: TestClient, company_with_site: Company):
        resp = client.post(
            f"/v2/partials/customers/{company_with_site.id}/sites",
            data={"site_name": ""},
        )
        assert resp.status_code == 200
        assert "required" in resp.text.lower()

    def test_create_site_with_owner(
        self,
        client: TestClient,
        db_session: Session,
        company_with_site: Company,
        test_user: User,
    ):
        resp = client.post(
            f"/v2/partials/customers/{company_with_site.id}/sites",
            data={
                "site_name": "Owned Site",
                "owner_id": str(test_user.id),
            },
        )
        assert resp.status_code == 200
        site = db_session.query(CustomerSite).filter(CustomerSite.site_name == "Owned Site").first()
        assert site is not None
        assert site.owner_id == test_user.id

    def test_delete_site(
        self,
        client: TestClient,
        db_session: Session,
        company_with_site: Company,
        site: CustomerSite,
    ):
        resp = client.delete(f"/v2/partials/customers/{company_with_site.id}/sites/{site.id}")
        assert resp.status_code == 200
        assert resp.text.strip() == ""

        db_session.refresh(site)
        assert site.is_active is False

    def test_delete_site_404(self, client: TestClient, company_with_site: Company):
        resp = client.delete(f"/v2/partials/customers/{company_with_site.id}/sites/99999")
        assert resp.status_code == 404


# ── Site Contacts ─────────────────────────────────────────────────────


class TestSiteContacts:
    """Tests for site contacts CRUD."""

    def test_load_contacts(
        self,
        client: TestClient,
        company_with_site: Company,
        site: CustomerSite,
        site_contact: SiteContact,
    ):
        resp = client.get(f"/v2/partials/customers/{company_with_site.id}/sites/{site.id}/contacts")
        assert resp.status_code == 200
        assert "John Buyer" in resp.text
        assert "Purchasing Manager" in resp.text

    def test_create_contact(
        self,
        client: TestClient,
        db_session: Session,
        company_with_site: Company,
        site: CustomerSite,
    ):
        resp = client.post(
            f"/v2/partials/customers/{company_with_site.id}/sites/{site.id}/contacts",
            data={
                "full_name": "Jane Smith",
                "email": "jane@testcorp.com",
                "title": "VP Sales",
                "phone": "+1-555-0200",
            },
        )
        assert resp.status_code == 200
        assert "Jane Smith" in resp.text

        count = db_session.query(SiteContact).filter(SiteContact.customer_site_id == site.id).count()
        assert count >= 1

    def test_create_contact_dedup_by_email(
        self,
        client: TestClient,
        db_session: Session,
        company_with_site: Company,
        site: CustomerSite,
        site_contact: SiteContact,
    ):
        """Adding a contact with the same email should not create a duplicate."""
        client.post(
            f"/v2/partials/customers/{company_with_site.id}/sites/{site.id}/contacts",
            data={
                "full_name": "John B",
                "email": "john@testcorp.com",
            },
        )
        count = (
            db_session.query(SiteContact)
            .filter(
                SiteContact.customer_site_id == site.id,
                SiteContact.email == "john@testcorp.com",
            )
            .count()
        )
        assert count == 1

    def test_create_contact_requires_name(self, client: TestClient, company_with_site: Company, site: CustomerSite):
        resp = client.post(
            f"/v2/partials/customers/{company_with_site.id}/sites/{site.id}/contacts",
            data={"full_name": "", "email": "test@test.com"},
        )
        assert resp.status_code == 200
        assert "required" in resp.text.lower()

    def test_delete_contact(
        self,
        client: TestClient,
        db_session: Session,
        company_with_site: Company,
        site: CustomerSite,
        site_contact: SiteContact,
    ):
        resp = client.delete(
            f"/v2/partials/customers/{company_with_site.id}/sites/{site.id}/contacts/{site_contact.id}"
        )
        assert resp.status_code == 200
        assert resp.text.strip() == ""

        remaining = db_session.query(SiteContact).filter(SiteContact.id == site_contact.id).first()
        assert remaining is None

    def test_delete_contact_404(self, client: TestClient, company_with_site: Company, site: CustomerSite):
        resp = client.delete(f"/v2/partials/customers/{company_with_site.id}/sites/{site.id}/contacts/99999")
        assert resp.status_code == 404

    def test_set_primary_contact(
        self,
        client: TestClient,
        db_session: Session,
        company_with_site: Company,
        site: CustomerSite,
        site_contact: SiteContact,
    ):
        resp = client.post(
            f"/v2/partials/customers/{company_with_site.id}/sites/{site.id}/contacts/{site_contact.id}/primary"
        )
        assert resp.status_code == 200
        assert "Primary" in resp.text

        db_session.refresh(site_contact)
        assert site_contact.is_primary is True

    def test_set_primary_unsets_others(
        self,
        client: TestClient,
        db_session: Session,
        company_with_site: Company,
        site: CustomerSite,
    ):
        """Setting primary should unset any other primary contact on the same site."""
        c1 = SiteContact(customer_site_id=site.id, full_name="Contact A", is_primary=True, is_active=True)
        c2 = SiteContact(customer_site_id=site.id, full_name="Contact B", is_primary=False, is_active=True)
        db_session.add_all([c1, c2])
        db_session.commit()

        client.post(f"/v2/partials/customers/{company_with_site.id}/sites/{site.id}/contacts/{c2.id}/primary")

        db_session.refresh(c1)
        db_session.refresh(c2)
        assert c1.is_primary is False
        assert c2.is_primary is True


# ── Contacts Tab (Company-level) ─────────────────────────────────────


class TestContactsTab:
    """Tests for the company-level contacts tab."""

    def test_contacts_tab_shows_site_contacts(
        self,
        client: TestClient,
        company_with_site: Company,
        site_contact: SiteContact,
    ):
        resp = client.get(f"/v2/partials/customers/{company_with_site.id}/tab/contacts")
        assert resp.status_code == 200
        assert "John Buyer" in resp.text

    def test_contacts_tab_empty(self, client: TestClient, test_company: Company):
        resp = client.get(f"/v2/partials/customers/{test_company.id}/tab/contacts")
        assert resp.status_code == 200
        assert "No contacts" in resp.text
