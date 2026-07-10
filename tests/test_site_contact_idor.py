"""tests/test_site_contact_idor.py — Cross-tenant IDOR regression guard.

F1: edit_site_contact must reject unrelated reps (403).
F2: create_site, create_site_contact, edit_company, edit_site must also
    reject unrelated reps (403).

Each endpoint also has a smoke-200 test that proves the owner path works.

Called by: pytest
Depends on: conftest.py fixtures, app.routers.htmx_views, app.dependencies
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
    """A company owned by test_user (account_owner_id set)."""
    co = Company(
        name="Owned Corp IDOR",
        is_active=True,
        account_owner_id=test_user.id,
        created_at=datetime.now(UTC),
    )
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def owned_site(db_session: Session, owned_company: Company) -> CustomerSite:
    """A site under owned_company."""
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
    """A contact under owned_site."""
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
    """TestClient authenticated as a user who owns NO companies/sites."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    stranger = User(
        email="stranger_idor@example.com",
        name="Stranger IDOR",
        role="buyer",
        azure_id="stranger-azure-idor",
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


# ── F1: edit_site_contact ────────────────────────────────────────────────────


class TestEditSiteContactIDOR:
    """edit_site_contact must gate on can_manage_account."""

    def test_unrelated_rep_gets_403(
        self,
        unrelated_client: TestClient,
        owned_company: Company,
        owned_site: CustomerSite,
        owned_contact: SiteContact,
    ):
        """Unrelated rep editing any company's contact must get 403."""
        resp = unrelated_client.post(
            f"/v2/partials/customers/{owned_company.id}/sites/{owned_site.id}/contacts/{owned_contact.id}/edit",
            data={"first_name": "Hacked"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 403

    def test_owner_gets_200(
        self,
        client: TestClient,
        test_user: User,
        owned_company: Company,
        owned_site: CustomerSite,
        owned_contact: SiteContact,
        db_session: Session,
    ):
        """Account owner editing their own contact must get 200."""
        # test_user IS already set as account_owner_id on owned_company via fixture
        resp = client.post(
            f"/v2/partials/customers/{owned_company.id}/sites/{owned_site.id}/contacts/{owned_contact.id}/edit",
            data={"first_name": "Updated", "last_name": "Owner"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200


# ── F2: create_site ──────────────────────────────────────────────────────────


class TestCreateSiteIDOR:
    """create_site must gate on can_manage_account."""

    def test_unrelated_rep_gets_403(
        self,
        unrelated_client: TestClient,
        owned_company: Company,
    ):
        """Unrelated rep creating a site on another company must get 403."""
        resp = unrelated_client.post(
            f"/v2/partials/customers/{owned_company.id}/sites",
            data={"site_name": "Hacked Site"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 403

    def test_owner_gets_200(
        self,
        client: TestClient,
        owned_company: Company,
    ):
        """Account owner creating a site must get 200."""
        resp = client.post(
            f"/v2/partials/customers/{owned_company.id}/sites",
            data={"site_name": "Legit Branch"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200


# ── F2: create_site_contact ──────────────────────────────────────────────────


class TestCreateSiteContactIDOR:
    """create_site_contact must gate on can_manage_account."""

    def test_unrelated_rep_gets_403(
        self,
        unrelated_client: TestClient,
        owned_company: Company,
        owned_site: CustomerSite,
    ):
        """Unrelated rep creating a contact on another company's site must get 403."""
        resp = unrelated_client.post(
            f"/v2/partials/customers/{owned_company.id}/sites/{owned_site.id}/contacts",
            data={"full_name": "Hacked Contact"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 403

    def test_owner_gets_200(
        self,
        client: TestClient,
        owned_company: Company,
        owned_site: CustomerSite,
    ):
        """Account owner creating a contact must get 200."""
        resp = client.post(
            f"/v2/partials/customers/{owned_company.id}/sites/{owned_site.id}/contacts",
            data={"full_name": "Bob Owner"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200


# ── F2: edit_company ─────────────────────────────────────────────────────────


class TestEditCompanyIDOR:
    """edit_company must gate on can_manage_account."""

    def test_unrelated_rep_gets_403(
        self,
        unrelated_client: TestClient,
        owned_company: Company,
    ):
        """Unrelated rep editing another company must get 403."""
        resp = unrelated_client.post(
            f"/v2/partials/customers/{owned_company.id}/edit",
            data={"name": "Hacked Corp"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 403

    def test_owner_gets_200(
        self,
        client: TestClient,
        owned_company: Company,
    ):
        """Account owner editing their company must get 200."""
        resp = client.post(
            f"/v2/partials/customers/{owned_company.id}/edit",
            data={"name": "Updated Corp"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200


# ── F2: edit_site ────────────────────────────────────────────────────────────


class TestEditSiteIDOR:
    """edit_site must gate on can_manage_account."""

    def test_unrelated_rep_gets_403(
        self,
        unrelated_client: TestClient,
        owned_company: Company,
        owned_site: CustomerSite,
    ):
        """Unrelated rep editing another company's site must get 403."""
        resp = unrelated_client.post(
            f"/v2/partials/customers/{owned_company.id}/sites/{owned_site.id}/edit",
            data={"site_name": "Hacked HQ"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 403

    def test_owner_gets_200(
        self,
        client: TestClient,
        owned_company: Company,
        owned_site: CustomerSite,
    ):
        """Account owner editing their site must get 200."""
        resp = client.post(
            f"/v2/partials/customers/{owned_company.id}/sites/{owned_site.id}/edit",
            data={"site_name": "Updated HQ"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
