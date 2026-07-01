"""test_edit_company_owner_validation.py — Regression tests for edit_company owner gate.

Verifies that POST /v2/partials/customers/{id}/edit validates a new primary owner the
same way create_company and the bulk assign-owner path do: the target user must exist
and be active. Without this guard, ownership can be silently transferred to a
deactivated user, or a bogus id raises an unhandled FK IntegrityError (500) on commit.

Called by: pytest
Depends on: conftest.py fixtures (client auths as test_user; db_session, test_company, test_user)
"""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Company, User


def _make_user(db: Session, *, email: str, azure_id: str, is_active: bool) -> User:
    user = User(email=email, name=email.split("@")[0], role="buyer", azure_id=azure_id)
    user.is_active = is_active
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


class TestEditCompanyOwnerValidation:
    def test_edit_rejects_deactivated_new_owner(
        self, client: TestClient, test_company: Company, test_user: User, db_session: Session
    ):
        """Reassigning ownership to a deactivated user must 400 and leave owner
        unchanged."""
        # test_user is the primary owner so it passes can_manage_account_team.
        test_company.account_owner_id = test_user.id
        db_session.commit()
        deactivated = _make_user(
            db_session, email="ghost@trioscs.com", azure_id="azure-deactivated-001", is_active=False
        )

        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/edit",
            data={"name": test_company.name, "owner_id": str(deactivated.id)},
            headers={"HX-Request": "true"},
        )

        assert resp.status_code == 400
        db_session.refresh(test_company)
        assert test_company.account_owner_id == test_user.id

    def test_edit_rejects_nonexistent_new_owner(
        self, client: TestClient, test_company: Company, test_user: User, db_session: Session
    ):
        """A non-existent owner id must 400 (clean), not raise an FK error on commit."""
        test_company.account_owner_id = test_user.id
        db_session.commit()

        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/edit",
            data={"name": test_company.name, "owner_id": "999999"},
            headers={"HX-Request": "true"},
        )

        assert resp.status_code == 400
        db_session.refresh(test_company)
        assert test_company.account_owner_id == test_user.id

    def test_edit_accepts_active_new_owner(
        self, client: TestClient, test_company: Company, test_user: User, db_session: Session
    ):
        """Reassigning ownership to a valid active user succeeds."""
        test_company.account_owner_id = test_user.id
        db_session.commit()
        active = _make_user(db_session, email="newowner@trioscs.com", azure_id="azure-active-001", is_active=True)

        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/edit",
            data={"name": test_company.name, "owner_id": str(active.id)},
            headers={"HX-Request": "true"},
        )

        assert resp.status_code == 200
        db_session.refresh(test_company)
        assert test_company.account_owner_id == active.id
