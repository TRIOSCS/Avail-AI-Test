"""tests/test_company_api_idor.py — Cross-tenant IDOR regression guard for the JSON
companies API.

Mirrors tests/test_site_contact_idor.py.

Guards:
  - GET /api/companies/{id}  must 404 for an unrelated rep, 200 for the owner.
  - GET /api/companies (list) must NOT leak an out-of-scope company to an unrelated
    rep (rep-scoped visibility), while the owner still sees it.

Called by: pytest
Depends on: conftest.py fixtures (client, db_session, test_user), app.main, app.dependencies
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.auth import User
from app.models.crm import Company, CustomerSite, SiteContact

# ── shared fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def owned_company(db_session: Session, test_user: User) -> Company:
    """A company owned by test_user, carrying PII the API would leak."""
    co = Company(
        name="Owned Corp API IDOR",
        is_active=True,
        account_owner_id=test_user.id,
        notes="private account notes",
        tax_id="SECRET-TAX-99",
        credit_terms="NET-30 confidential",
        phone="+1-555-0100",
        created_at=datetime.now(UTC),
    )
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def owned_site(db_session: Session, owned_company: Company) -> CustomerSite:
    site = CustomerSite(
        company_id=owned_company.id,
        site_name="Owner HQ",
        is_active=True,
    )
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)
    return site


@pytest.fixture()
def owned_contact(db_session: Session, owned_site: CustomerSite) -> SiteContact:
    contact = SiteContact(
        customer_site_id=owned_site.id,
        full_name="Alice Owner",
        first_name="Alice",
        last_name="Owner",
        email="alice@owned-corp.com",
    )
    db_session.add(contact)
    db_session.commit()
    db_session.refresh(contact)
    return contact


@pytest.fixture()
def unrelated_client(db_session: Session) -> TestClient:
    """TestClient authenticated as a buyer who owns NO companies/sites."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    stranger = User(
        email="stranger_company_idor@example.com",
        name="Stranger Company IDOR",
        role="buyer",
        azure_id="stranger-azure-company-idor",
        created_at=datetime.now(UTC),
    )
    db_session.add(stranger)
    db_session.commit()

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: stranger
    app.dependency_overrides[require_admin] = lambda: stranger
    app.dependency_overrides[require_buyer] = lambda: stranger
    app.dependency_overrides[require_fresh_token] = lambda: "mock-token"

    with TestClient(app) as c:
        yield c

    for dep in [get_db, require_user, require_admin, require_buyer, require_fresh_token]:
        app.dependency_overrides.pop(dep, None)


# ── GET /api/companies/{id} — detail leak ────────────────────────────────────


class TestGetCompanyIDOR:
    """get_company must 404 an out-of-scope account (indistinguishable from missing)."""

    def test_unrelated_rep_gets_404(
        self,
        unrelated_client: TestClient,
        owned_company: Company,
        owned_site: CustomerSite,
        owned_contact: SiteContact,
    ):
        resp = unrelated_client.get(f"/api/companies/{owned_company.id}")
        assert resp.status_code == 404

    def test_owner_gets_200(
        self,
        client: TestClient,
        owned_company: Company,
        owned_site: CustomerSite,
        owned_contact: SiteContact,
    ):
        resp = client.get(f"/api/companies/{owned_company.id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == owned_company.id
        assert body["tax_id"] == "SECRET-TAX-99"


# ── GET /api/companies — list leak ───────────────────────────────────────────


class TestListCompaniesIDOR:
    """list_companies must scope to rep-visible accounts for non-managers."""

    def test_unrelated_rep_cannot_see_company(
        self,
        unrelated_client: TestClient,
        owned_company: Company,
    ):
        resp = unrelated_client.get("/api/companies")
        assert resp.status_code == 200
        ids = [item["id"] for item in resp.json()["items"]]
        assert owned_company.id not in ids

    def test_owner_sees_company(
        self,
        client: TestClient,
        owned_company: Company,
    ):
        resp = client.get("/api/companies")
        assert resp.status_code == 200
        ids = [item["id"] for item in resp.json()["items"]]
        assert owned_company.id in ids
