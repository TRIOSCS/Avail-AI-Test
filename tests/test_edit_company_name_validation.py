"""test_edit_company_name_validation.py — Regression tests closing two fake-success gaps
in the edit-account flow now that its form is user-reachable.

1. The edit form's Company Name input must carry the ``required`` attribute the create
   form has, so the browser can't silently submit a blank name.
2. POST /v2/partials/customers/{id}/edit must reject renaming an account onto another
   existing account's name with the same 409 the create path uses (Company.name is
   nullable=False and NOT unique, so nothing else stops a colliding rename).

Called by: pytest
Depends on: conftest.py fixtures (client auths as test_user; db_session, test_company, test_user)
"""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Company, User


class TestEditCompanyNameRequiredAttr:
    def test_edit_form_name_input_has_required(self, client: TestClient, test_company: Company):
        """The rendered edit form's name input must include ``required`` so a blank name
        can't be silently submitted (parity with the create form)."""
        resp = client.get(
            f"/v2/partials/customers/{test_company.id}/edit-form",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        html = resp.text
        # Locate the name input and assert it carries the required attribute.
        idx = html.find('name="name"')
        assert idx != -1, "edit form is missing the company name input"
        # The tag runs from the opening '<input' up to its closing '>'.
        tag_start = html.rfind("<input", 0, idx)
        tag_end = html.find(">", idx)
        name_tag = html[tag_start:tag_end]
        assert "required" in name_tag, f"name input is missing 'required': {name_tag!r}"


class TestEditCompanyDuplicateName:
    def test_edit_rejects_rename_onto_existing_name(
        self, client: TestClient, test_company: Company, test_user: User, db_session: Session
    ):
        """Renaming an account onto another account's existing name must 409 and leave
        the name unchanged (mirrors create_company's duplicate guard)."""
        # test_user owns test_company so can_manage_account passes.
        test_company.account_owner_id = test_user.id
        other = Company(name="Globex Corporation", is_active=True)
        db_session.add(other)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/edit",
            data={"name": "globex corporation"},  # case-insensitive collision
            headers={"HX-Request": "true"},
        )

        assert resp.status_code == 409
        db_session.refresh(test_company)
        assert test_company.name == "Acme Electronics"

    def test_edit_allows_saving_same_name(
        self, client: TestClient, test_company: Company, test_user: User, db_session: Session
    ):
        """A no-op save (name unchanged) must NOT be blocked by the self row — the
        duplicate guard excludes the company being edited."""
        test_company.account_owner_id = test_user.id
        db_session.commit()

        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/edit",
            data={"name": test_company.name},
            headers={"HX-Request": "true"},
        )

        assert resp.status_code == 200
        db_session.refresh(test_company)
        assert test_company.name == "Acme Electronics"
