"""tests/test_site_read_idor.py - Cross-tenant IDOR regression guard for site READ endpoints.

site_contacts_list (GET .../sites/{id}/contacts) and site_edit_form
(GET .../sites/{id}/edit-form) both returned company/site data (contact PII,
site commercial terms, internal user roster) gated on require_user only. An
unrelated buyer who owns nothing must get 404 (existence hidden); the account
owner must still get 200.

Called by: pytest
Depends on: conftest.py fixtures (client, test_user, db_session), app.dependencies
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.auth import User
from app.models.crm import Company, CustomerSite, SiteContact

# -- shared fixtures ----------------------------------------------------------


@pytest.fixture()
def owned_company(db_session: Session, test_user: User) -> Company:
    """A company owned by test_user (account_owner_id set)."""
    co = Company(
        name="Owned Corp ReadIDOR",
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
    """A site under owned_company with commercial terms populated."""
    site = CustomerSite(
        company_id=owned_company.id,
        site_name="Owner HQ",
        is_active=True,
        payment_terms="NET30",
        shipping_terms="FOB",
        notes="internal pricing note",
    )
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)
    return site


@pytest.fixture()
def owned_contact(db_session: Session, owned_site: CustomerSite) -> SiteContact:
    """A contact under owned_site (the PII that must not leak)."""
    contact = SiteContact(
        customer_site_id=owned_site.id,
        full_name="Alice Owner",
        first_name="Alice",
        last_name="Owner",
        email="alice@owned-corp.com",
        phone="+1-555-0100",
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
        email="stranger_read_idor@example.com",
        name="Stranger Read IDOR",
        role="buyer",
        azure_id="stranger-azure-read-idor",
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


# -- site_contacts_list -------------------------------------------------------


class TestSiteContactsListIDOR:
    """GET .../sites/{id}/contacts must gate on can_manage_account (404 for
    strangers)."""

    def test_unrelated_rep_gets_404(
        self,
        unrelated_client: TestClient,
        owned_company: Company,
        owned_site: CustomerSite,
        owned_contact: SiteContact,
    ):
        resp = unrelated_client.get(
            f"/v2/partials/customers/{owned_company.id}/sites/{owned_site.id}/contacts",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404
        assert "alice@owned-corp.com" not in resp.text

    def test_owner_gets_200(
        self,
        client: TestClient,
        owned_company: Company,
        owned_site: CustomerSite,
        owned_contact: SiteContact,
    ):
        resp = client.get(
            f"/v2/partials/customers/{owned_company.id}/sites/{owned_site.id}/contacts",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        # The owner sees the contact roster the stranger was denied.
        assert "alice@owned-corp.com" in resp.text


# -- site_edit_form -----------------------------------------------------------


class TestSiteEditFormIDOR:
    """GET .../sites/{id}/edit-form must gate on can_manage_account (404 for
    strangers)."""

    def test_unrelated_rep_gets_404(
        self,
        unrelated_client: TestClient,
        owned_company: Company,
        owned_site: CustomerSite,
    ):
        resp = unrelated_client.get(
            f"/v2/partials/customers/{owned_company.id}/sites/{owned_site.id}/edit-form",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404
        assert "internal pricing note" not in resp.text

    def test_owner_gets_200(
        self,
        client: TestClient,
        owned_company: Company,
        owned_site: CustomerSite,
    ):
        resp = client.get(
            f"/v2/partials/customers/{owned_company.id}/sites/{owned_site.id}/edit-form",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        # The owner's edit form actually renders (site-name field present).
        assert "site_name" in resp.text
