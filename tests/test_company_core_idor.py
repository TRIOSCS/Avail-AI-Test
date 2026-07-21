"""tests/test_company_core_idor.py — Cross-tenant IDOR guard for the company core read
endpoints (app/routers/htmx/companies/core.py).

Companion to tests/test_site_contact_idor.py. Each ungated GET read must reject a
stranger (a buyer owning nothing) with 404 — matching company_detail_partial, so an
out-of-scope account is indistinguishable from a missing one — while the account
owner still gets 200.

Covered endpoints:
  - company_edit_form        GET /v2/partials/customers/{id}/edit-form
  - company_field_edit_form  GET /v2/partials/customers/{id}/field/edit/{field}
  - company_field_display    GET /v2/partials/customers/{id}/field/display/{field}
  - company_dup_suggestion   GET /v2/partials/customers/{id}/dup-suggestion
  - company_name_suggestion  GET /v2/partials/customers/{id}/name-suggestion

Called by: pytest
Depends on: conftest.py fixtures (client, db_session, test_user), app.main.app,
    app.dependencies
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.auth import User
from app.models.crm import Company

# A field guaranteed to be in EDITABLE_ACCOUNT_FIELDS and to carry sensitive PII.
_PII_FIELD = "credit_terms"

# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def owned_company(db_session: Session, test_user: User) -> Company:
    """A company owned by test_user (account_owner_id set), with sensitive fields."""
    co = Company(
        name="Owned Corp CoreIDOR",
        is_active=True,
        account_owner_id=test_user.id,
        credit_terms="NET-30 SECRET",
        tax_id="TAX-SECRET-001",
        created_at=datetime.now(UTC),
    )
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def unrelated_client(db_session: Session) -> TestClient:
    """TestClient authenticated as a user who owns NO companies/sites."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    stranger = User(
        email="stranger_core_idor@example.com",
        name="Stranger Core IDOR",
        role="buyer",
        azure_id="stranger-azure-core-idor",
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


# ── company_edit_form ─────────────────────────────────────────────────────────


class TestCompanyEditFormIDOR:
    """GET .../edit-form must not leak credit_terms/tax_id + rosters to strangers."""

    def test_unrelated_rep_gets_404(self, unrelated_client: TestClient, owned_company: Company):
        resp = unrelated_client.get(
            f"/v2/partials/customers/{owned_company.id}/edit-form",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404

    def test_owner_gets_200(self, client: TestClient, owned_company: Company):
        resp = client.get(
            f"/v2/partials/customers/{owned_company.id}/edit-form",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        # The owner sees the credit_terms value the stranger's 404 hid.
        assert "NET-30 SECRET" in resp.text


# ── company_field_edit_form ───────────────────────────────────────────────────


class TestCompanyFieldEditFormIDOR:
    """GET .../field/edit/{field} must not render another account's field widget."""

    def test_unrelated_rep_gets_404(self, unrelated_client: TestClient, owned_company: Company):
        resp = unrelated_client.get(
            f"/v2/partials/customers/{owned_company.id}/field/edit/{_PII_FIELD}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404

    def test_owner_gets_200(self, client: TestClient, owned_company: Company):
        resp = client.get(
            f"/v2/partials/customers/{owned_company.id}/field/edit/{_PII_FIELD}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        # The owner's edit widget is pre-filled with the value the stranger was denied.
        assert "NET-30 SECRET" in resp.text


# ── company_field_display ─────────────────────────────────────────────────────


class TestCompanyFieldDisplayIDOR:
    """GET .../field/display/{field} must not return another account's field value."""

    def test_unrelated_rep_gets_404(self, unrelated_client: TestClient, owned_company: Company):
        resp = unrelated_client.get(
            f"/v2/partials/customers/{owned_company.id}/field/display/{_PII_FIELD}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404

    def test_owner_gets_200(self, client: TestClient, owned_company: Company):
        resp = client.get(
            f"/v2/partials/customers/{owned_company.id}/field/display/{_PII_FIELD}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        # The owner's display span shows the field value the stranger's 404 hid.
        assert "NET-30 SECRET" in resp.text


# ── company_dup_suggestion ────────────────────────────────────────────────────


class TestCompanyDupSuggestionIDOR:
    """GET .../dup-suggestion must 404 (before the org-wide scan) for strangers."""

    def test_unrelated_rep_gets_404(self, unrelated_client: TestClient, owned_company: Company):
        resp = unrelated_client.get(
            f"/v2/partials/customers/{owned_company.id}/dup-suggestion",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404

    def test_owner_gets_200(self, client: TestClient, db_session: Session, test_user: User):
        # Seed two near-duplicate accounts the owner manages so the scan yields a match.
        keeper = Company(
            name="Acme Widgets Inc",
            is_active=True,
            account_owner_id=test_user.id,
            created_at=datetime.now(UTC),
        )
        dup = Company(
            name="Acme Widgets LLC",
            is_active=True,
            account_owner_id=test_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add_all([keeper, dup])
        db_session.commit()
        db_session.refresh(keeper)
        resp = client.get(
            f"/v2/partials/customers/{keeper.id}/dup-suggestion",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        # The owner receives the duplicate-account banner the stranger's 404 withheld.
        assert "Possible duplicate account" in resp.text


# ── company_name_suggestion ───────────────────────────────────────────────────


class TestCompanyNameSuggestionIDOR:
    """GET .../name-suggestion must not confirm existence / leak the name."""

    def test_unrelated_rep_gets_404(self, unrelated_client: TestClient, owned_company: Company):
        resp = unrelated_client.get(
            f"/v2/partials/customers/{owned_company.id}/name-suggestion",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404

    def test_owner_gets_200(self, client: TestClient, db_session: Session, test_user: User):
        # Seed an owned account whose name carries a legal suffix so a suggestion renders.
        co = Company(
            name="Suggestible Systems Inc",
            is_active=True,
            account_owner_id=test_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(co)
        db_session.commit()
        db_session.refresh(co)
        resp = client.get(
            f"/v2/partials/customers/{co.id}/name-suggestion",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        # The owner receives the suffix-stripped name suggestion the stranger's 404 withheld.
        assert "Suggested name:" in resp.text
        assert "Suggestible Systems" in resp.text
