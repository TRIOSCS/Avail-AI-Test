"""tests/test_authz_merge_addsite_idor.py — Account-ownership guard on the merge + add-
to-site routes.

Phase 1: four previously-ungated routes now gate on app.dependencies.can_manage_account
so a logged-in user can only act on accounts they own/manage. The gate fires BEFORE the
404/400/validation branches, so a non-owner restricted (SALES) rep gets 403 even on an
otherwise-valid request; the account owner and supervisors (manager/admin) pass.

Routes covered:
  1. POST /v2/partials/customers/{cid}/merge            (confirmed=true)
  2. GET  /v2/partials/customers/{cid}/merge-preview    (?remove_id=...)
  3. GET  /v2/partials/customers/{cid}/merge-form
  4. POST /api/suggested-contacts/add-to-site           (gated via the site's company)

Mirrors tests/test_authz_htmx_company_contact_idor.py for the client/fixture pattern.

Called by: pytest
Depends on: conftest.py fixtures (client, db_session, test_user, sales_user,
            manager_user, admin_user), app.routers.htmx_views,
            app.routers.crm.enrichment, app.dependencies.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models.auth import User
from app.models.crm import Company, CustomerSite, SiteContact

# ── shared fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def keep_company(db_session: Session, test_user: User) -> Company:
    """The merge keeper, owned by test_user (the `client` fixture's user)."""
    co = Company(
        name="Keep Corp MAIDOR",
        is_active=True,
        account_owner_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def remove_company(db_session: Session, test_user: User) -> Company:
    """The merge duplicate, also owned by test_user (both keeper and duplicate are
    gated)."""
    co = Company(
        name="Remove Corp MAIDOR",
        is_active=True,
        account_owner_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def owned_site(db_session: Session, keep_company: Company) -> CustomerSite:
    """An HQ site under keep_company — gives add-to-site a real target whose owning
    company test_user owns."""
    site = CustomerSite(
        company_id=keep_company.id,
        site_name="Owner HQ MAIDOR",
        site_type="hq",
        is_active=True,
    )
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)
    return site


def _override_user(user: User):
    """Set require_user override to *user*; returns a no-arg cleanup callable."""
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: user

    def _cleanup():
        app.dependency_overrides.pop(require_user, None)

    return _cleanup


# Each route is exercised three ways:
#   - non-owner restricted (sales) → 403
#   - owner (test_user via the `client` fixture, which is the account_owner) → non-403
#   - supervisor (manager/admin) → non-403
#
# The `client` fixture authenticates as test_user, who IS the account owner of the
# fixture companies. Non-owner / supervisor cases reuse `client` but override
# require_user for the call.


# ── 1. company merge (POST, confirmed=true) ──────────────────────────────────


class TestCompanyMergeIDOR:
    def test_non_owner_sales_gets_403(self, client, keep_company, remove_company, sales_user):
        cleanup = _override_user(sales_user)
        try:
            resp = client.post(
                f"/v2/partials/customers/{keep_company.id}/merge",
                data={"remove_id": str(remove_company.id), "confirmed": "true"},
                headers={"HX-Request": "true"},
            )
        finally:
            cleanup()
        assert resp.status_code == 403

    def test_owner_merges(self, client, keep_company, remove_company):
        resp = client.post(
            f"/v2/partials/customers/{keep_company.id}/merge",
            data={"remove_id": str(remove_company.id), "confirmed": "true"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

    def test_manager_merges(self, client, db_session, keep_company, remove_company, manager_user):
        cleanup = _override_user(manager_user)
        try:
            resp = client.post(
                f"/v2/partials/customers/{keep_company.id}/merge",
                data={"remove_id": str(remove_company.id), "confirmed": "true"},
                headers={"HX-Request": "true"},
            )
        finally:
            cleanup()
        assert resp.status_code == 200


# ── 2. merge preview (GET) ───────────────────────────────────────────────────


class TestMergePreviewIDOR:
    def test_non_owner_sales_gets_403(self, client, keep_company, remove_company, sales_user):
        cleanup = _override_user(sales_user)
        try:
            resp = client.get(
                f"/v2/partials/customers/{keep_company.id}/merge-preview",
                params={"remove_id": remove_company.id},
            )
        finally:
            cleanup()
        assert resp.status_code == 403

    def test_owner_gets_200(self, client, keep_company, remove_company):
        resp = client.get(
            f"/v2/partials/customers/{keep_company.id}/merge-preview",
            params={"remove_id": remove_company.id},
        )
        assert resp.status_code == 200

    def test_admin_gets_200(self, client, keep_company, remove_company, admin_user):
        cleanup = _override_user(admin_user)
        try:
            resp = client.get(
                f"/v2/partials/customers/{keep_company.id}/merge-preview",
                params={"remove_id": remove_company.id},
            )
        finally:
            cleanup()
        assert resp.status_code == 200


# ── 3. merge form (GET) ──────────────────────────────────────────────────────


class TestMergeFormIDOR:
    def test_non_owner_sales_gets_403(self, client, keep_company, sales_user):
        cleanup = _override_user(sales_user)
        try:
            resp = client.get(f"/v2/partials/customers/{keep_company.id}/merge-form")
        finally:
            cleanup()
        assert resp.status_code == 403

    def test_owner_gets_200(self, client, keep_company):
        resp = client.get(f"/v2/partials/customers/{keep_company.id}/merge-form")
        assert resp.status_code == 200

    def test_manager_gets_200(self, client, keep_company, manager_user):
        cleanup = _override_user(manager_user)
        try:
            resp = client.get(f"/v2/partials/customers/{keep_company.id}/merge-form")
        finally:
            cleanup()
        assert resp.status_code == 200


# ── 4. add suggested contact to site (POST, gated via the site's company) ─────


class TestAddSuggestedToSiteIDOR:
    def test_non_owner_sales_gets_403(self, client, owned_site, sales_user):
        cleanup = _override_user(sales_user)
        try:
            resp = client.post(
                "/api/suggested-contacts/add-to-site",
                json={
                    "site_id": owned_site.id,
                    "contact": {"full_name": "Hacked Suggested", "email": "hacked@maidor.com"},
                },
            )
        finally:
            cleanup()
        assert resp.status_code == 403

    def test_owner_gets_200(self, client, db_session, owned_site):
        resp = client.post(
            "/api/suggested-contacts/add-to-site",
            json={
                "site_id": owned_site.id,
                "contact": {"full_name": "Legit Suggested", "email": "legit@maidor.com"},
            },
        )
        assert resp.status_code == 200
        assert resp.json()["added"] == 1
        sc = db_session.query(SiteContact).filter_by(customer_site_id=owned_site.id, email="legit@maidor.com").first()
        assert sc is not None

    def test_admin_gets_200(self, client, owned_site, admin_user):
        cleanup = _override_user(admin_user)
        try:
            resp = client.post(
                "/api/suggested-contacts/add-to-site",
                json={
                    "site_id": owned_site.id,
                    "contact": {"full_name": "Admin Suggested", "email": "admin@maidor.com"},
                },
            )
        finally:
            cleanup()
        assert resp.status_code == 200
