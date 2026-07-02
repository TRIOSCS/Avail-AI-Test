"""Tests for Company.primary_contact_id and parent_company_id (Step 3).

Covers:
- Setting primary contact via endpoint — persists + header shows name.
- Non-owner gets 403; contact from another company gets 404.
- Setting parent_company persists + shows Parent + child count on parent.
- Cycle guard rejects self-parent (400) and descendant-parent (400).

Called by: pytest test suite
Depends on: tests/conftest.py fixtures (db_session, test_user, admin_user, client)
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.auth import User
from app.models.crm import Company, CustomerSite, SiteContact


@pytest.fixture()
def company_a(db_session: Session) -> Company:
    co = Company(name="Company A", is_active=True)
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def company_b(db_session: Session) -> Company:
    co = Company(name="Company B", is_active=True)
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def company_c(db_session: Session) -> Company:
    co = Company(name="Company C", is_active=True)
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def site_a(db_session: Session, company_a: Company) -> CustomerSite:
    site = CustomerSite(company_id=company_a.id, site_name="HQ A", is_active=True)
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)
    return site


@pytest.fixture()
def site_b(db_session: Session, company_b: Company) -> CustomerSite:
    site = CustomerSite(company_id=company_b.id, site_name="HQ B", is_active=True)
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)
    return site


@pytest.fixture()
def contact_a(db_session: Session, site_a: CustomerSite) -> SiteContact:
    contact = SiteContact(
        customer_site_id=site_a.id,
        full_name="Alice Smith",
        email="alice@a.com",
    )
    db_session.add(contact)
    db_session.commit()
    db_session.refresh(contact)
    return contact


@pytest.fixture()
def contact_b(db_session: Session, site_b: CustomerSite) -> SiteContact:
    contact = SiteContact(
        customer_site_id=site_b.id,
        full_name="Bob Jones",
        email="bob@b.com",
    )
    db_session.add(contact)
    db_session.commit()
    db_session.refresh(contact)
    return contact


@pytest.fixture()
def owner_client(db_session: Session, company_a: Company, test_user: User) -> TestClient:
    """TestClient where test_user is the owner of company_a."""
    company_a.account_owner_id = test_user.id
    db_session.commit()

    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: test_user
    app.dependency_overrides[require_admin] = lambda: test_user
    app.dependency_overrides[require_buyer] = lambda: test_user
    app.dependency_overrides[require_fresh_token] = lambda: "mock-token"

    with TestClient(app) as c:
        yield c

    for dep in [get_db, require_user, require_admin, require_buyer, require_fresh_token]:
        app.dependency_overrides.pop(dep, None)


@pytest.fixture()
def non_owner_client(db_session: Session, company_a: Company) -> TestClient:
    """TestClient where user is NOT the owner of company_a."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    other = User(email="other@trioscs.com", name="Other User", role="buyer", azure_id="other-azure")
    db_session.add(other)
    db_session.commit()

    # company_a.account_owner_id is None or another user — not 'other'
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: other
    app.dependency_overrides[require_admin] = lambda: other
    app.dependency_overrides[require_buyer] = lambda: other
    app.dependency_overrides[require_fresh_token] = lambda: "mock-token"

    with TestClient(app) as c:
        yield c

    for dep in [get_db, require_user, require_admin, require_buyer, require_fresh_token]:
        app.dependency_overrides.pop(dep, None)


class TestPrimaryContactEndpoint:
    def test_set_primary_contact_persists(
        self, owner_client: TestClient, db_session: Session, company_a: Company, contact_a: SiteContact
    ):
        resp = owner_client.post(f"/v2/partials/customers/{company_a.id}/primary-contact/{contact_a.id}")
        assert resp.status_code == 200
        db_session.expire(company_a)
        db_session.refresh(company_a)
        assert company_a.primary_contact_id == contact_a.id

    def test_set_primary_contact_shows_in_header(
        self, owner_client: TestClient, db_session: Session, company_a: Company, contact_a: SiteContact
    ):
        resp = owner_client.post(f"/v2/partials/customers/{company_a.id}/primary-contact/{contact_a.id}")
        assert resp.status_code == 200
        assert "Alice Smith" in resp.text

    def test_non_owner_gets_403(self, non_owner_client: TestClient, company_a: Company, contact_a: SiteContact):
        resp = non_owner_client.post(f"/v2/partials/customers/{company_a.id}/primary-contact/{contact_a.id}")
        assert resp.status_code == 403

    def test_contact_from_other_company_gets_404(
        self,
        owner_client: TestClient,
        db_session: Session,
        company_a: Company,
        contact_b: SiteContact,
    ):
        """contact_b belongs to company_b, not company_a — IDOR guard → 404."""
        resp = owner_client.post(f"/v2/partials/customers/{company_a.id}/primary-contact/{contact_b.id}")
        assert resp.status_code == 404

    def test_kebab_button_targets_detail_root_not_main_content(
        self, owner_client: TestClient, company_a: Company, contact_a: SiteContact
    ):
        """F8: the 'Set account primary' kebab action must refresh the detail via
        `closest [data-detail-root]` (outerHTML) like the sibling Reactivate/Archive
        actions — NOT swap the whole #main-content and destroy the CDM split workspace.

        The account-primary shows in the detail header (not the contacts list), so the
        handler returns the full detail partial; targeting the detail root re-swaps it
        both inside the CDM workspace and on a deep-linked full-page detail.
        """
        resp = owner_client.get(f"/v2/partials/customers/{company_a.id}/tab/contacts")
        assert resp.status_code == 200
        m = re.search(
            r"<button[^>]*primary-contact/" + str(contact_a.id) + r"\b.*?Set account primary</button>",
            resp.text,
            re.S,
        )
        assert m, "Set account primary button not found in contacts tab"
        btn = m.group(0)
        assert "closest [data-detail-root]" in btn
        assert "outerHTML" in btn
        assert "#main-content" not in btn


class TestParentCompanyEndpoint:
    def test_set_parent_persists(
        self,
        owner_client: TestClient,
        db_session: Session,
        company_a: Company,
        company_b: Company,
    ):
        resp = owner_client.post(
            f"/v2/partials/customers/{company_a.id}/parent",
            data={"parent_company_id": str(company_b.id)},
        )
        assert resp.status_code == 200
        db_session.expire(company_a)
        db_session.refresh(company_a)
        assert company_a.parent_company_id == company_b.id

    def test_parent_name_shown_in_response(
        self,
        owner_client: TestClient,
        db_session: Session,
        company_a: Company,
        company_b: Company,
    ):
        resp = owner_client.post(
            f"/v2/partials/customers/{company_a.id}/parent",
            data={"parent_company_id": str(company_b.id)},
        )
        assert resp.status_code == 200
        assert "Company B" in resp.text

    def test_child_count_shown_on_parent(
        self,
        owner_client: TestClient,
        db_session: Session,
        company_a: Company,
        company_b: Company,
        test_user: User,
    ):
        """After setting company_a's parent to company_b, company_b has 1 child."""
        company_a.parent_company_id = company_b.id
        # company detail now gates on can_manage_account; test_user owns company_a but the
        # rendered account here is company_b, so make test_user its owner too.
        company_b.account_owner_id = test_user.id
        db_session.commit()

        # Render company_b detail — should mention 1 child account
        resp = owner_client.get(f"/v2/partials/customers/{company_b.id}")
        assert resp.status_code == 200
        # We just verify a child account reference exists
        assert "child account" in resp.text or "Company A" in resp.text

    def test_cycle_guard_self_parent(
        self,
        owner_client: TestClient,
        company_a: Company,
    ):
        resp = owner_client.post(
            f"/v2/partials/customers/{company_a.id}/parent",
            data={"parent_company_id": str(company_a.id)},
        )
        assert resp.status_code == 400

    def test_cycle_guard_descendant_parent(
        self,
        owner_client: TestClient,
        db_session: Session,
        company_a: Company,
        company_b: Company,
        company_c: Company,
    ):
        """A → B → C; setting C's parent to A would form a cycle."""
        company_b.parent_company_id = company_a.id
        company_c.parent_company_id = company_b.id
        db_session.commit()

        # Now try to set company_a's parent to company_c (would create A→B→C→A)
        resp = owner_client.post(
            f"/v2/partials/customers/{company_a.id}/parent",
            data={"parent_company_id": str(company_c.id)},
        )
        assert resp.status_code == 400

    def test_non_owner_gets_403(
        self,
        non_owner_client: TestClient,
        company_a: Company,
        company_b: Company,
    ):
        resp = non_owner_client.post(
            f"/v2/partials/customers/{company_a.id}/parent",
            data={"parent_company_id": str(company_b.id)},
        )
        assert resp.status_code == 403


class TestEditCompanyParentCycleGuard:
    """edit_company (POST /v2/partials/customers/{id}/edit) must enforce the same cycle
    guard as set_parent_company — both now delegate to _set_parent_company."""

    def test_edit_company_setting_descendant_as_parent_rejected(
        self,
        owner_client: TestClient,
        db_session: Session,
        company_a: Company,
        company_b: Company,
        company_c: Company,
    ):
        """A → B → C; using the edit modal to set company_a's parent to company_c (which
        is already a descendant via set_parent_company) must return 400."""
        # Establish A → B → C hierarchy first
        company_b.parent_company_id = company_a.id
        company_c.parent_company_id = company_b.id
        db_session.commit()

        # Attempt to make A a child of C via the edit form (would create A→B→C→A)
        resp = owner_client.post(
            f"/v2/partials/customers/{company_a.id}/edit",
            data={"parent_company_id": str(company_c.id)},
        )
        assert resp.status_code == 400, (
            "edit_company must reject a parent that would create a cycle — "
            "the inline parent field now goes through _set_parent_company"
        )

    def test_edit_company_self_parent_rejected(
        self,
        owner_client: TestClient,
        company_a: Company,
    ):
        resp = owner_client.post(
            f"/v2/partials/customers/{company_a.id}/edit",
            data={"parent_company_id": str(company_a.id)},
        )
        assert resp.status_code == 400
