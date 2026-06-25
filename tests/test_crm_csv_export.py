"""Tests for role-scoped CRM CSV export endpoints.

Verifies:
- GET /v2/customers/export.csv → companies CSV (header + rows)
- GET /v2/customers/contacts/export.csv → contacts CSV (header + rows)
- Manager sees all companies; sales rep sees only owned companies
- Content-Type is text/csv with attachment disposition
- CSV has correct column headers

Called by: pytest
Depends on: app.routers.crm.export (export_companies_csv, export_contacts_csv)
"""

import csv
import io
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Company, CustomerSite, SiteContact, User


@pytest.fixture()
def manager_client(db_session: Session, manager_user: User) -> TestClient:
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: manager_user
    app.dependency_overrides[require_admin] = lambda: manager_user
    app.dependency_overrides[require_buyer] = lambda: manager_user
    app.dependency_overrides[require_fresh_token] = lambda: "mock-token"

    with TestClient(app) as c:
        yield c

    for dep in [get_db, require_user, require_admin, require_buyer, require_fresh_token]:
        app.dependency_overrides.pop(dep, None)


@pytest.fixture()
def sales_client(db_session: Session, sales_user: User) -> TestClient:
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: sales_user
    app.dependency_overrides[require_admin] = lambda: sales_user
    app.dependency_overrides[require_buyer] = lambda: sales_user
    app.dependency_overrides[require_fresh_token] = lambda: "mock-token"

    with TestClient(app) as c:
        yield c

    for dep in [get_db, require_user, require_admin, require_buyer, require_fresh_token]:
        app.dependency_overrides.pop(dep, None)


@pytest.fixture()
def owned_company(db_session: Session, sales_user: User) -> Company:
    """An active company owned by the sales_user."""
    co = Company(
        name="Owned Corp",
        domain="owned.com",
        industry="Tech",
        account_type="customer",
        is_active=True,
        account_owner_id=None,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(co)
    db_session.flush()
    co.account_owner_id = sales_user.id
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def other_company(db_session: Session) -> Company:
    """An active company owned by nobody (not visible to sales_user)."""
    co = Company(
        name="Other Corp",
        domain="other.com",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def contact_for_owned(db_session: Session, owned_company: Company) -> SiteContact:
    """A site + contact under owned_company."""
    site = CustomerSite(
        company_id=owned_company.id,
        site_name="HQ",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(site)
    db_session.flush()

    contact = SiteContact(
        customer_site_id=site.id,
        full_name="Jane Doe",
        title="VP Sales",
        email="jane@owned.com",
        phone="555-1234",
        contact_role="decision_maker",
        is_primary=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(contact)
    db_session.commit()
    db_session.refresh(contact)
    return contact


def _parse_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    return list(reader)


class TestCompaniesCSVExport:
    def test_returns_200_with_csv_content_type(self, manager_client: TestClient, owned_company: Company):
        resp = manager_client.get("/v2/customers/export.csv")
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]

    def test_attachment_content_disposition(self, manager_client: TestClient, owned_company: Company):
        resp = manager_client.get("/v2/customers/export.csv")
        assert "attachment" in resp.headers.get("content-disposition", "")
        assert "customers.csv" in resp.headers.get("content-disposition", "")

    def test_csv_has_required_headers(self, manager_client: TestClient, owned_company: Company):
        resp = manager_client.get("/v2/customers/export.csv")
        rows = _parse_csv(resp.text)
        assert len(rows) >= 1
        expected_headers = {
            "name",
            "domain",
            "phone",
            "industry",
            "account_type",
            "owner_name",
            "hq_city",
            "hq_state",
            "created_at",
        }
        assert expected_headers == set(rows[0].keys())

    def test_manager_sees_all_active_companies(
        self, manager_client: TestClient, owned_company: Company, other_company: Company
    ):
        resp = manager_client.get("/v2/customers/export.csv")
        rows = _parse_csv(resp.text)
        names = {r["name"] for r in rows}
        assert "Owned Corp" in names
        assert "Other Corp" in names

    def test_sales_rep_sees_only_owned_companies(
        self, sales_client: TestClient, owned_company: Company, other_company: Company
    ):
        resp = sales_client.get("/v2/customers/export.csv")
        rows = _parse_csv(resp.text)
        names = {r["name"] for r in rows}
        assert "Owned Corp" in names
        assert "Other Corp" not in names

    def test_inactive_companies_excluded(self, manager_client: TestClient, db_session: Session):
        inactive = Company(
            name="Inactive Corp",
            is_active=False,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(inactive)
        db_session.commit()
        resp = manager_client.get("/v2/customers/export.csv")
        rows = _parse_csv(resp.text)
        names = {r["name"] for r in rows}
        assert "Inactive Corp" not in names


class TestContactsCSVExport:
    def test_returns_200_with_csv_content_type(self, manager_client: TestClient, contact_for_owned: SiteContact):
        resp = manager_client.get("/v2/customers/contacts/export.csv")
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]

    def test_attachment_content_disposition(self, manager_client: TestClient, contact_for_owned: SiteContact):
        resp = manager_client.get("/v2/customers/contacts/export.csv")
        assert "attachment" in resp.headers.get("content-disposition", "")
        assert "contacts.csv" in resp.headers.get("content-disposition", "")

    def test_csv_has_required_headers(self, manager_client: TestClient, contact_for_owned: SiteContact):
        resp = manager_client.get("/v2/customers/contacts/export.csv")
        rows = _parse_csv(resp.text)
        assert len(rows) >= 1
        expected_headers = {
            "full_name",
            "title",
            "email",
            "phone",
            "contact_role",
            "company_name",
            "site_name",
            "is_primary",
        }
        assert expected_headers == set(rows[0].keys())

    def test_manager_sees_all_contacts(self, manager_client: TestClient, contact_for_owned: SiteContact):
        resp = manager_client.get("/v2/customers/contacts/export.csv")
        rows = _parse_csv(resp.text)
        names = {r["full_name"] for r in rows}
        assert "Jane Doe" in names

    def test_sales_rep_sees_only_owned_contacts(
        self,
        sales_client: TestClient,
        contact_for_owned: SiteContact,
        other_company: Company,
        db_session: Session,
    ):
        # Add a contact under other_company (not owned by sales_user)
        other_site = CustomerSite(
            company_id=other_company.id,
            site_name="Other HQ",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(other_site)
        db_session.flush()
        other_contact = SiteContact(
            customer_site_id=other_site.id,
            full_name="Ghost Contact",
            email="ghost@other.com",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(other_contact)
        db_session.commit()

        resp = sales_client.get("/v2/customers/contacts/export.csv")
        rows = _parse_csv(resp.text)
        names = {r["full_name"] for r in rows}
        assert "Jane Doe" in names
        assert "Ghost Contact" not in names

    def test_deactivated_contact_excluded(
        self,
        manager_client: TestClient,
        owned_company: Company,
        db_session: Session,
    ):
        """Fix 1: SiteContact.is_active=False must be filtered out of contacts export."""
        site = CustomerSite(
            company_id=owned_company.id,
            site_name="Branch",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(site)
        db_session.flush()

        inactive_contact = SiteContact(
            customer_site_id=site.id,
            full_name="Deactivated Person",
            email="gone@owned.com",
            is_active=False,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(inactive_contact)
        db_session.commit()

        resp = manager_client.get("/v2/customers/contacts/export.csv")
        rows = _parse_csv(resp.text)
        names = {r["full_name"] for r in rows}
        assert "Deactivated Person" not in names


class TestCSVFormulaSafety:
    """Fix 3: String cells starting with formula chars must be escaped."""

    def test_formula_prefix_company_name_escaped(
        self,
        manager_client: TestClient,
        db_session: Session,
    ):
        """A company named '=cmd()' must appear as \"'=cmd()\" in the CSV."""
        evil = Company(
            name="=cmd()",
            domain="+evil.com",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(evil)
        db_session.commit()

        resp = manager_client.get("/v2/customers/export.csv")
        # Raw CSV text — the escaped cell value is literally '=cmd()
        assert "'=cmd()" in resp.text
        assert "'+evil.com" in resp.text

    def test_formula_prefix_contact_fields_escaped(
        self,
        manager_client: TestClient,
        owned_company: Company,
        db_session: Session,
    ):
        """Contact fields starting with formula chars must be escaped."""
        site = CustomerSite(
            company_id=owned_company.id,
            site_name="@BadSite",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(site)
        db_session.flush()

        contact = SiteContact(
            customer_site_id=site.id,
            full_name="-Injected",
            title="@Title",
            email="=user@evil.com",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(contact)
        db_session.commit()

        resp = manager_client.get("/v2/customers/contacts/export.csv")
        assert "'-Injected" in resp.text
        assert "'@Title" in resp.text
        assert "'=user@evil.com" in resp.text
        assert "'@BadSite" in resp.text
