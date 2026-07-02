"""test_crm_account_crud_buttons.py — F7: surface the create/edit-account forms as
modals.

Verifies the buttons that wire the previously-orphaned create-account and edit-account
forms into the CRM: the "+ New account" button on the account list, the "Edit account"
kebab item on company detail, that both forms are modal-shaped (refresh the list/detail
via the correct hx-target, never a #main-content wipe), and that a successful create/edit
fires HX-Trigger=cdmListRefresh so the left account list refreshes.

Called by: pytest
Depends on: conftest.py fixtures (client, db_session, test_company, test_user),
            app.routers.htmx.companies
"""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Company, User

_HX = {"HX-Request": "true"}


# ── Account list: "+ New account" button + refresh listener ──────────────────


class TestNewAccountButton:
    def test_list_has_new_account_button(self, client: TestClient, test_company: Company):
        resp = client.get("/v2/partials/customers", headers=_HX)
        assert resp.status_code == 200
        # Button opens the create-account form as a modal (loads into #modal-content).
        assert "/v2/partials/customers/create-form" in resp.text
        assert "+ New account" in resp.text
        assert 'hx-target="#modal-content"' in resp.text

    def test_list_has_refresh_listener(self, client: TestClient, test_company: Company):
        """The workspace carries the hidden listener that reloads #cdm-list when a
        create/edit modal fires cdmListRefresh."""
        resp = client.get("/v2/partials/customers", headers=_HX)
        assert resp.status_code == 200
        assert "cdmListRefresh from:body" in resp.text
        assert 'hx-target="#cdm-list"' in resp.text


# ── Company detail: "Edit account" affordance + stable root id ───────────────


class TestEditAffordance:
    def test_detail_has_edit_affordance(
        self, client: TestClient, test_company: Company, test_user: User, db_session: Session
    ):
        test_company.account_owner_id = test_user.id
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{test_company.id}", headers=_HX)
        assert resp.status_code == 200
        assert f"/v2/partials/customers/{test_company.id}/edit-form" in resp.text
        assert 'hx-target="#modal-content"' in resp.text

    def test_detail_root_has_stable_id(
        self, client: TestClient, test_company: Company, test_user: User, db_session: Session
    ):
        """The edit modal (outside the detail root) re-swaps the detail via this id."""
        test_company.account_owner_id = test_user.id
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{test_company.id}", headers=_HX)
        assert resp.status_code == 200
        assert f'id="company-detail-{test_company.id}"' in resp.text


# ── Forms are modal-shaped (refresh detail/list, never wipe #main-content) ───


class TestFormsAreModalShaped:
    def test_create_form_targets_detail_not_shell(self, client: TestClient):
        resp = client.get("/v2/partials/customers/create-form", headers=_HX)
        assert resp.status_code == 200
        assert 'hx-target="#cdm-detail"' in resp.text
        assert 'hx-target="#main-content"' not in resp.text
        # Closes the modal on a successful submit.
        assert "close-modal" in resp.text

    def test_edit_form_targets_detail_root_not_shell(
        self, client: TestClient, test_company: Company, test_user: User, db_session: Session
    ):
        test_company.account_owner_id = test_user.id
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{test_company.id}/edit-form", headers=_HX)
        assert resp.status_code == 200
        assert f'hx-target="#company-detail-{test_company.id}"' in resp.text
        assert 'hx-swap="outerHTML"' in resp.text
        assert 'hx-target="#main-content"' not in resp.text
        assert "close-modal" in resp.text


# ── Successful create/edit fires the list-refresh trigger ────────────────────


class TestListRefreshTrigger:
    def test_create_fires_list_refresh(self, client: TestClient, db_session: Session):
        resp = client.post(
            "/v2/partials/customers/create",
            data={"name": "Refresh Trigger Co"},
            headers=_HX,
        )
        assert resp.status_code == 200
        assert resp.headers.get("HX-Trigger") == "cdmListRefresh"
        assert db_session.query(Company).filter(Company.name == "Refresh Trigger Co").first() is not None

    def test_edit_fires_list_refresh(
        self, client: TestClient, test_company: Company, test_user: User, db_session: Session
    ):
        test_company.account_owner_id = test_user.id
        db_session.commit()
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/edit",
            data={"name": "Renamed Account Co"},
            headers=_HX,
        )
        assert resp.status_code == 200
        assert resp.headers.get("HX-Trigger") == "cdmListRefresh"
        db_session.refresh(test_company)
        assert test_company.name == "Renamed Account Co"
