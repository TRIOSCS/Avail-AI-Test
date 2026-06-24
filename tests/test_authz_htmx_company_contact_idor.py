"""tests/test_authz_htmx_company_contact_idor.py — Cross-tenant company/contact IDOR
guard.

Phase 1b: nine company/contact mutation routes in app/routers/htmx_views.py must gate
on app.dependencies.can_manage_account so a logged-in user can only mutate entities in
accounts they own/manage. A non-owner restricted (SALES/TRADER) rep must get 403; the
account owner and supervisors (manager/admin) must pass.

Routes covered:
  1. POST   /v2/partials/customers/{cid}/segment-tags                 (assign)
  2. DELETE /v2/partials/customers/{cid}/segment-tags/{tag_id}        (unassign)
  3. POST   /v2/partials/customers/{cid}/tier                         (set tier)
  4. POST   /v2/partials/customers/{cid}/apply-name                   (apply name)
  5. POST   /v2/partials/customers/{cid}/contacts                     (contacts-tab create)
  6. POST   /v2/partials/customers/{cid}/suggested-contacts/add       (add suggested)
  7. DELETE /v2/partials/customers/{cid}/sites/{sid}/contacts/{ctid}  (delete contact)
  8. POST   /v2/partials/customers/{cid}/sites/{sid}/contacts/{ctid}/primary
  9. POST   /v2/partials/customers/{cid}/sites/{sid}/contacts/{ctid}/notes (add note)

Mirrors tests/test_site_contact_idor.py for the client/fixture pattern.

Called by: pytest
Depends on: conftest.py fixtures (client, db_session, test_user, sales_user, trader_user,
            manager_user, admin_user), app.routers.htmx_views, app.dependencies.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models.auth import User
from app.models.crm import Company, CustomerSite, SiteContact
from app.models.tags import Tag

# ── shared fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def owned_company(db_session: Session, test_user: User) -> Company:
    """A company owned by test_user (account_owner_id set)."""
    co = Company(
        name="Owned Corp CCIDOR",
        is_active=True,
        account_owner_id=test_user.id,
        created_at=datetime.now(timezone.utc),
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
    """A contact under owned_site."""
    contact = SiteContact(
        customer_site_id=owned_site.id,
        full_name="Alice Owner",
        first_name="Alice",
        last_name="Owner",
        email="alice@owned-ccidor.com",
    )
    db_session.add(contact)
    db_session.commit()
    db_session.refresh(contact)
    return contact


@pytest.fixture()
def owned_tag(db_session: Session, owned_company: Company) -> Tag:
    """A segment tag already assigned to owned_company (for the unassign route)."""
    from app.models.tags import EntityTag

    tag = Tag(name="VIP CCIDOR", tag_type="segment")
    db_session.add(tag)
    db_session.flush()
    db_session.add(EntityTag(entity_type="company", entity_id=owned_company.id, tag_id=tag.id))
    db_session.commit()
    db_session.refresh(tag)
    return tag


def _override_user(user: User):
    """Set require_user override to *user*; returns a no-arg cleanup callable."""
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: user

    def _cleanup():
        app.dependency_overrides.pop(require_user, None)

    return _cleanup


# Each route is exercised three ways:
#   - non-owner restricted (sales, and trader where cheap) → 403
#   - owner (test_user via the `client` fixture, which is the account_owner) → 200
#   - supervisor (manager/admin) → 200
#
# The `client` fixture authenticates as test_user, who IS owned_company.account_owner_id.
# Non-owner / supervisor cases reuse `client` but override require_user for the call.


def _post(client, url, **data):
    return client.post(url, data=data, headers={"HX-Request": "true"})


def _delete(client, url):
    return client.request("DELETE", url, headers={"HX-Request": "true"})


# ── 1. assign segment tag ─────────────────────────────────────────────────────


class TestAssignSegmentTagIDOR:
    @pytest.mark.parametrize("role_fixture", ["sales_user", "trader_user"])
    def test_non_owner_gets_403(self, client, owned_company, request, role_fixture):
        cleanup = _override_user(request.getfixturevalue(role_fixture))
        try:
            resp = _post(client, f"/v2/partials/customers/{owned_company.id}/segment-tags", tag_name="Hacked")
        finally:
            cleanup()
        assert resp.status_code == 403

    def test_owner_gets_200(self, client, owned_company):
        resp = _post(client, f"/v2/partials/customers/{owned_company.id}/segment-tags", tag_name="Legit Tag")
        assert resp.status_code == 200

    @pytest.mark.parametrize("role_fixture", ["manager_user", "admin_user"])
    def test_supervisor_gets_200(self, client, owned_company, request, role_fixture):
        cleanup = _override_user(request.getfixturevalue(role_fixture))
        try:
            resp = _post(client, f"/v2/partials/customers/{owned_company.id}/segment-tags", tag_name="Mgr Tag")
        finally:
            cleanup()
        assert resp.status_code == 200


# ── 2. unassign segment tag ───────────────────────────────────────────────────


class TestUnassignSegmentTagIDOR:
    def test_non_owner_sales_gets_403(self, client, owned_company, owned_tag, sales_user):
        cleanup = _override_user(sales_user)
        try:
            resp = _delete(client, f"/v2/partials/customers/{owned_company.id}/segment-tags/{owned_tag.id}")
        finally:
            cleanup()
        assert resp.status_code == 403

    def test_owner_gets_200(self, client, owned_company, owned_tag):
        resp = _delete(client, f"/v2/partials/customers/{owned_company.id}/segment-tags/{owned_tag.id}")
        assert resp.status_code == 200

    def test_admin_gets_200(self, client, owned_company, owned_tag, admin_user):
        cleanup = _override_user(admin_user)
        try:
            resp = _delete(client, f"/v2/partials/customers/{owned_company.id}/segment-tags/{owned_tag.id}")
        finally:
            cleanup()
        assert resp.status_code == 200


# ── 3. set company tier ───────────────────────────────────────────────────────


class TestSetCompanyTierIDOR:
    @pytest.mark.parametrize("role_fixture", ["sales_user", "trader_user"])
    def test_non_owner_gets_403(self, client, owned_company, request, role_fixture):
        cleanup = _override_user(request.getfixturevalue(role_fixture))
        try:
            resp = _post(client, f"/v2/partials/customers/{owned_company.id}/tier", tier="key")
        finally:
            cleanup()
        assert resp.status_code == 403

    def test_owner_gets_200(self, client, owned_company):
        resp = _post(client, f"/v2/partials/customers/{owned_company.id}/tier", tier="core")
        assert resp.status_code == 200

    def test_manager_gets_200(self, client, owned_company, manager_user):
        cleanup = _override_user(manager_user)
        try:
            resp = _post(client, f"/v2/partials/customers/{owned_company.id}/tier", tier="standard")
        finally:
            cleanup()
        assert resp.status_code == 200


# ── 4. apply company name ─────────────────────────────────────────────────────


class TestApplyCompanyNameIDOR:
    @pytest.mark.parametrize("role_fixture", ["sales_user", "trader_user"])
    def test_non_owner_gets_403(self, client, owned_company, request, role_fixture):
        cleanup = _override_user(request.getfixturevalue(role_fixture))
        try:
            resp = _post(client, f"/v2/partials/customers/{owned_company.id}/apply-name", name="Hacked Corp")
        finally:
            cleanup()
        assert resp.status_code == 403

    def test_owner_gets_200(self, client, owned_company):
        resp = _post(client, f"/v2/partials/customers/{owned_company.id}/apply-name", name="Renamed Corp")
        assert resp.status_code == 200

    def test_admin_gets_200(self, client, owned_company, admin_user):
        cleanup = _override_user(admin_user)
        try:
            resp = _post(client, f"/v2/partials/customers/{owned_company.id}/apply-name", name="Admin Corp")
        finally:
            cleanup()
        assert resp.status_code == 200


# ── 5. contacts-tab create ────────────────────────────────────────────────────


class TestContactsTabCreateIDOR:
    @pytest.mark.parametrize("role_fixture", ["sales_user", "trader_user"])
    def test_non_owner_gets_403(self, client, owned_company, owned_site, request, role_fixture):
        cleanup = _override_user(request.getfixturevalue(role_fixture))
        try:
            resp = _post(
                client,
                f"/v2/partials/customers/{owned_company.id}/contacts",
                first_name="Hacked",
                last_name="Contact",
            )
        finally:
            cleanup()
        assert resp.status_code == 403

    def test_owner_gets_200(self, client, owned_company, owned_site):
        resp = _post(
            client,
            f"/v2/partials/customers/{owned_company.id}/contacts",
            first_name="Bob",
            last_name="Owner",
        )
        assert resp.status_code == 200

    def test_manager_gets_200(self, client, owned_company, owned_site, manager_user):
        cleanup = _override_user(manager_user)
        try:
            resp = _post(
                client,
                f"/v2/partials/customers/{owned_company.id}/contacts",
                first_name="Carol",
                last_name="Mgr",
            )
        finally:
            cleanup()
        assert resp.status_code == 200


# ── 6. add suggested contact ──────────────────────────────────────────────────


class TestAddSuggestedContactIDOR:
    @pytest.mark.parametrize("role_fixture", ["sales_user", "trader_user"])
    def test_non_owner_gets_403(self, client, owned_company, owned_site, request, role_fixture):
        cleanup = _override_user(request.getfixturevalue(role_fixture))
        try:
            resp = _post(
                client,
                f"/v2/partials/customers/{owned_company.id}/suggested-contacts/add",
                full_name="Hacked Suggested",
                site_id=str(owned_site.id),
            )
        finally:
            cleanup()
        assert resp.status_code == 403

    def test_owner_gets_200(self, client, owned_company, owned_site):
        resp = _post(
            client,
            f"/v2/partials/customers/{owned_company.id}/suggested-contacts/add",
            full_name="Dave Suggested",
            site_id=str(owned_site.id),
        )
        assert resp.status_code == 200

    def test_admin_gets_200(self, client, owned_company, owned_site, admin_user):
        cleanup = _override_user(admin_user)
        try:
            resp = _post(
                client,
                f"/v2/partials/customers/{owned_company.id}/suggested-contacts/add",
                full_name="Eve Suggested",
                site_id=str(owned_site.id),
            )
        finally:
            cleanup()
        assert resp.status_code == 200


# ── 7. delete site contact ────────────────────────────────────────────────────


class TestDeleteSiteContactIDOR:
    def test_non_owner_sales_gets_403(self, client, owned_company, owned_site, owned_contact, sales_user):
        cleanup = _override_user(sales_user)
        try:
            resp = _delete(
                client,
                f"/v2/partials/customers/{owned_company.id}/sites/{owned_site.id}/contacts/{owned_contact.id}",
            )
        finally:
            cleanup()
        assert resp.status_code == 403

    def test_owner_gets_200(self, client, owned_company, owned_site, owned_contact):
        resp = _delete(
            client,
            f"/v2/partials/customers/{owned_company.id}/sites/{owned_site.id}/contacts/{owned_contact.id}",
        )
        assert resp.status_code == 200

    def test_manager_gets_200(self, client, owned_company, owned_site, owned_contact, manager_user):
        cleanup = _override_user(manager_user)
        try:
            resp = _delete(
                client,
                f"/v2/partials/customers/{owned_company.id}/sites/{owned_site.id}/contacts/{owned_contact.id}",
            )
        finally:
            cleanup()
        assert resp.status_code == 200


# ── 8. set primary contact ────────────────────────────────────────────────────


class TestSetPrimaryContactIDOR:
    @pytest.mark.parametrize("role_fixture", ["sales_user", "trader_user"])
    def test_non_owner_gets_403(self, client, owned_company, owned_site, owned_contact, request, role_fixture):
        cleanup = _override_user(request.getfixturevalue(role_fixture))
        try:
            resp = _post(
                client,
                f"/v2/partials/customers/{owned_company.id}/sites/{owned_site.id}/contacts/{owned_contact.id}/primary",
            )
        finally:
            cleanup()
        assert resp.status_code == 403

    def test_owner_gets_200(self, client, owned_company, owned_site, owned_contact):
        resp = _post(
            client,
            f"/v2/partials/customers/{owned_company.id}/sites/{owned_site.id}/contacts/{owned_contact.id}/primary",
        )
        assert resp.status_code == 200

    def test_admin_gets_200(self, client, owned_company, owned_site, owned_contact, admin_user):
        cleanup = _override_user(admin_user)
        try:
            resp = _post(
                client,
                f"/v2/partials/customers/{owned_company.id}/sites/{owned_site.id}/contacts/{owned_contact.id}/primary",
            )
        finally:
            cleanup()
        assert resp.status_code == 200


# ── 9. add site contact note ──────────────────────────────────────────────────


class TestAddSiteContactNoteIDOR:
    @pytest.mark.parametrize("role_fixture", ["sales_user", "trader_user"])
    def test_non_owner_gets_403(self, client, owned_company, owned_site, owned_contact, request, role_fixture):
        cleanup = _override_user(request.getfixturevalue(role_fixture))
        try:
            resp = _post(
                client,
                f"/v2/partials/customers/{owned_company.id}/sites/{owned_site.id}/contacts/{owned_contact.id}/notes",
                notes="hack note",
            )
        finally:
            cleanup()
        assert resp.status_code == 403

    def test_owner_gets_200(self, client, owned_company, owned_site, owned_contact):
        resp = _post(
            client,
            f"/v2/partials/customers/{owned_company.id}/sites/{owned_site.id}/contacts/{owned_contact.id}/notes",
            notes="real note",
        )
        assert resp.status_code == 200

    def test_manager_gets_200(self, client, owned_company, owned_site, owned_contact, manager_user):
        cleanup = _override_user(manager_user)
        try:
            resp = _post(
                client,
                f"/v2/partials/customers/{owned_company.id}/sites/{owned_site.id}/contacts/{owned_contact.id}/notes",
                notes="mgr note",
            )
        finally:
            cleanup()
        assert resp.status_code == 200

    def test_note_cross_site_chain_404(self, client, db_session, owned_company, owned_site, owned_contact):
        """IDOR scope: a site_id that does NOT belong to company_id must 404 (not write a
        note against the mismatched chain). Owner auth, mismatched site path."""
        # A second company+site the owner also owns, but whose site is passed under the
        # WRONG company id in the URL.
        other_co = Company(name="Other Owned CCIDOR", is_active=True, account_owner_id=owned_company.account_owner_id)
        db_session.add(other_co)
        db_session.flush()
        other_site = CustomerSite(company_id=other_co.id, site_name="Other HQ", is_active=True)
        db_session.add(other_site)
        db_session.commit()
        # other_site belongs to other_co, NOT owned_company → 404 on the site check.
        resp = _post(
            client,
            f"/v2/partials/customers/{owned_company.id}/sites/{other_site.id}/contacts/{owned_contact.id}/notes",
            notes="cross-site",
        )
        assert resp.status_code == 404
