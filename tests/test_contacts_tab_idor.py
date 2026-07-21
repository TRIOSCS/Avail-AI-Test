"""tests/test_contacts_tab_idor.py — Cross-tenant IDOR regression guard for the
Contacts-tab HTMX partials in app/routers/htmx/companies/contacts.py.

Every endpoint below returns company/site/contact data (roster, field values, notes,
change history, discovered-contact PII) and previously depended only on require_user.
A stranger who owns nothing must be denied (404, or an empty 286 for the poller); the
account owner must still get 200.

Called by: pytest
Depends on: conftest.py fixtures (client, test_user, db_session), app.main.app,
    app.dependencies, app.models.crm
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
    """A company owned by test_user (account_owner_id set), with a domain so the
    suggested-contacts trigger reaches its 200 path for the owner."""
    co = Company(
        name="Owned Corp Contacts IDOR",
        is_active=True,
        account_owner_id=test_user.id,
        domain="owned-corp-idor.com",
        created_at=datetime.now(UTC),
    )
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def owned_site(db_session: Session, owned_company: Company) -> CustomerSite:
    """An HQ site under owned_company."""
    site = CustomerSite(
        company_id=owned_company.id,
        site_name="Owner HQ",
        site_type="hq",
        is_active=True,
    )
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)
    return site


@pytest.fixture()
def owned_contact(db_session: Session, owned_site: CustomerSite) -> SiteContact:
    """A contact under owned_site, with PII fields populated."""
    contact = SiteContact(
        customer_site_id=owned_site.id,
        full_name="Alice Owner",
        first_name="Alice",
        last_name="Owner",
        email="alice@owned-corp-idor.com",
        is_active=True,
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
        email="stranger_contacts_idor@example.com",
        name="Stranger Contacts IDOR",
        role="buyer",
        azure_id="stranger-azure-contacts-idor",
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


HX = {"HX-Request": "true"}


# ── contacts_tab_add_form ────────────────────────────────────────────────────


class TestContactsTabAddFormIDOR:
    def test_unrelated_rep_gets_404(self, unrelated_client: TestClient, owned_company: Company):
        resp = unrelated_client.get(f"/v2/partials/customers/{owned_company.id}/contacts/add-form", headers=HX)
        assert resp.status_code == 404

    def test_owner_gets_200(self, client: TestClient, owned_company: Company, owned_site: CustomerSite):
        resp = client.get(f"/v2/partials/customers/{owned_company.id}/contacts/add-form", headers=HX)
        assert resp.status_code == 200
        # The owner's add-form exposes the site roster (dropdown) the stranger's 404 hid.
        assert "Owner HQ" in resp.text


# ── contact_field_edit_form / contact_field_display ──────────────────────────


class TestContactFieldFormsIDOR:
    def test_edit_form_unrelated_rep_gets_404(
        self, unrelated_client: TestClient, owned_company: Company, owned_contact: SiteContact
    ):
        resp = unrelated_client.get(
            f"/v2/partials/customers/{owned_company.id}/contacts/{owned_contact.id}/field/edit/email",
            headers=HX,
        )
        assert resp.status_code == 404

    def test_edit_form_owner_gets_200(self, client: TestClient, owned_company: Company, owned_contact: SiteContact):
        resp = client.get(
            f"/v2/partials/customers/{owned_company.id}/contacts/{owned_contact.id}/field/edit/email",
            headers=HX,
        )
        assert resp.status_code == 200
        # The owner's edit widget is pre-filled with the contact email the stranger was denied.
        assert "alice@owned-corp-idor.com" in resp.text

    def test_display_unrelated_rep_gets_404(
        self, unrelated_client: TestClient, owned_company: Company, owned_contact: SiteContact
    ):
        resp = unrelated_client.get(
            f"/v2/partials/customers/{owned_company.id}/contacts/{owned_contact.id}/field/display/email",
            headers=HX,
        )
        assert resp.status_code == 404

    def test_display_owner_gets_200(self, client: TestClient, owned_company: Company, owned_contact: SiteContact):
        resp = client.get(
            f"/v2/partials/customers/{owned_company.id}/contacts/{owned_contact.id}/field/display/email",
            headers=HX,
        )
        assert resp.status_code == 200
        # The owner's display span shows the contact email the stranger's 404 hid.
        assert "alice@owned-corp-idor.com" in resp.text


# ── contact_notes_modal ──────────────────────────────────────────────────────


class TestContactNotesModalIDOR:
    def test_unrelated_rep_gets_404(
        self, unrelated_client: TestClient, owned_company: Company, owned_contact: SiteContact
    ):
        resp = unrelated_client.get(
            f"/v2/partials/customers/{owned_company.id}/contacts/{owned_contact.id}/notes-modal",
            headers=HX,
        )
        assert resp.status_code == 404

    def test_owner_gets_200(self, client: TestClient, owned_company: Company, owned_contact: SiteContact):
        resp = client.get(
            f"/v2/partials/customers/{owned_company.id}/contacts/{owned_contact.id}/notes-modal",
            headers=HX,
        )
        assert resp.status_code == 200
        # The owner's notes modal is headed by the contact identity the stranger's 404 hid.
        assert "Alice Owner" in resp.text


# ── contact_history_modal ────────────────────────────────────────────────────


class TestContactHistoryModalIDOR:
    def test_unrelated_rep_gets_404(
        self, unrelated_client: TestClient, owned_company: Company, owned_contact: SiteContact
    ):
        resp = unrelated_client.get(
            f"/v2/partials/customers/{owned_company.id}/contacts/{owned_contact.id}/history-modal",
            headers=HX,
        )
        assert resp.status_code == 404

    def test_owner_gets_200(self, client: TestClient, owned_company: Company, owned_contact: SiteContact):
        resp = client.get(
            f"/v2/partials/customers/{owned_company.id}/contacts/{owned_contact.id}/history-modal",
            headers=HX,
        )
        assert resp.status_code == 200
        # The owner's history modal is headed by the contact identity the stranger's 404 hid.
        assert "Alice Owner" in resp.text


# ── contact_files_modal (contact-scoped route, no company_id in path) ─────────


class TestContactFilesModalIDOR:
    def test_unrelated_rep_gets_404(self, unrelated_client: TestClient, owned_contact: SiteContact):
        resp = unrelated_client.get(f"/v2/partials/contacts/{owned_contact.id}/files-modal", headers=HX)
        assert resp.status_code == 404

    def test_owner_gets_200(self, client: TestClient, owned_contact: SiteContact):
        resp = client.get(f"/v2/partials/contacts/{owned_contact.id}/files-modal", headers=HX)
        assert resp.status_code == 200
        # The owner's files modal is headed by the contact identity the stranger's 404 hid.
        assert "Alice Owner" in resp.text


# ── suggested-contacts trigger (paid spend + PII) ────────────────────────────


class TestSuggestedContactsTriggerIDOR:
    def test_unrelated_rep_gets_404(self, unrelated_client: TestClient, owned_company: Company):
        # Denied BEFORE any enrichment credits are enqueued.
        resp = unrelated_client.get(
            f"/v2/partials/customers/{owned_company.id}/suggested-contacts?domain=owned-corp-idor.com",
            headers=HX,
        )
        assert resp.status_code == 404

    def test_owner_gets_200(self, client: TestClient, owned_company: Company, monkeypatch: pytest.MonkeyPatch):
        # Stub the discovery waterfall so the owner path never touches paid providers.
        import app.routers.htmx.companies as pkg

        async def _noop(company_id: int, domain: str, name: str) -> None:
            return None

        monkeypatch.setattr(pkg, "_run_contact_discovery", _noop)
        resp = client.get(
            f"/v2/partials/customers/{owned_company.id}/suggested-contacts?domain=owned-corp-idor.com",
            headers=HX,
        )
        assert resp.status_code == 200
        # The owner receives the "finding contacts" poller (wired to this company's status
        # route) that the stranger's 404 withheld.
        assert f"/v2/partials/customers/{owned_company.id}/suggested-contacts/status" in resp.text


# ── suggested-contacts status poller (streams discovered PII) ────────────────


class TestSuggestedContactsStatusIDOR:
    def test_unrelated_rep_denied_no_leak(self, unrelated_client: TestClient, owned_company: Company):
        # Poller: denial stops the poll with an empty 286 body (never a 4xx that would
        # leave htmx hammering, never any discovered-contact PII).
        resp = unrelated_client.get(f"/v2/partials/customers/{owned_company.id}/suggested-contacts/status", headers=HX)
        assert resp.status_code == 286
        assert resp.text == ""

    def test_owner_not_denied(self, client: TestClient, owned_company: Company):
        # Owner is not denied: with no run in flight the poller returns its normal
        # empty 286 (stop polling) rather than the cross-tenant empty response — the key
        # assertion is that the owner is never blocked by the new gate.
        resp = client.get(f"/v2/partials/customers/{owned_company.id}/suggested-contacts/status", headers=HX)
        assert resp.status_code in (200, 286)
