"""tests/test_contact_role_write_in.py — ISS-029 contact role write-in coverage.

Covers the OTHER free-text write-in on the two full contact create/edit handlers
(POST /v2/partials/customers/{id}/contacts, POST .../sites/{sid}/contacts/{cid}/edit):
canonical role acceptance, custom write-in persistence, empty-custom fallback to
plain "other", the String(50) cap, and the add-form's write-in toggle markup.

Called by: pytest
Depends on: app.routers.htmx.companies._registries (resolve_contact_role),
    app.routers.htmx.companies.contacts (contacts_tab_create, edit_site_contact),
    conftest fixtures (db_session, client, test_user)
"""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.auth import User
from app.models.crm import Company, CustomerSite, SiteContact


def _make_company_with_hq(db_session: Session, owner: User) -> tuple[Company, CustomerSite]:
    company = Company(name="Write-In Co", is_active=True, account_owner_id=owner.id)
    db_session.add(company)
    db_session.flush()
    site = CustomerSite(company_id=company.id, site_name="HQ", site_type="hq", is_active=True)
    db_session.add(site)
    db_session.commit()
    return company, site


class TestResolveContactRoleUnit:
    """Unit coverage of the resolver itself (no HTTP round-trip)."""

    def test_blank_role_returns_none(self):
        from app.routers.htmx.companies._registries import resolve_contact_role

        assert resolve_contact_role("", "") is None
        assert resolve_contact_role("   ", "anything") is None

    def test_canonical_role_returned_as_is(self):
        from app.routers.htmx.companies._registries import resolve_contact_role

        assert resolve_contact_role("buyer_po", "") == "buyer_po"
        assert resolve_contact_role("decision_maker", "ignored") == "decision_maker"

    def test_other_with_custom_text_returns_trimmed_custom(self):
        from app.routers.htmx.companies._registries import resolve_contact_role

        assert resolve_contact_role("other", "  Regional Buyer  ") == "Regional Buyer"

    def test_other_with_empty_custom_falls_back_to_plain_other(self):
        from app.routers.htmx.companies._registries import resolve_contact_role

        assert resolve_contact_role("other", "") == "other"
        assert resolve_contact_role("other", "   ") == "other"

    def test_other_custom_capped_to_50_chars(self):
        from app.routers.htmx.companies._registries import resolve_contact_role

        long_custom = "X" * 80
        result = resolve_contact_role("other", long_custom)
        assert result == "X" * 50
        assert len(result) == 50

    def test_unknown_role_raises_400(self):
        from fastapi import HTTPException

        from app.routers.htmx.companies._registries import resolve_contact_role

        try:
            resolve_contact_role("wizard", "")
            raise AssertionError("expected HTTPException")
        except HTTPException as exc:
            assert exc.status_code == 400


class TestCreateContactRoleWriteIn:
    def test_create_with_canonical_role(self, client: TestClient, db_session: Session, test_user: User):
        company, site = _make_company_with_hq(db_session, test_user)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/contacts",
            data={
                "site_id": str(site.id),
                "first_name": "Pat",
                "last_name": "Canonical",
                "email": "pat@writein.com",
                "contact_role": "logistics",
            },
        )
        assert resp.status_code == 200
        contact = db_session.query(SiteContact).filter(SiteContact.email == "pat@writein.com").first()
        assert contact.contact_role == "logistics"

    def test_create_with_other_custom_write_in(self, client: TestClient, db_session: Session, test_user: User):
        company, site = _make_company_with_hq(db_session, test_user)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/contacts",
            data={
                "site_id": str(site.id),
                "first_name": "Robin",
                "last_name": "Custom",
                "email": "robin@writein.com",
                "contact_role": "other",
                "contact_role_custom": "Quality Auditor",
            },
        )
        assert resp.status_code == 200
        contact = db_session.query(SiteContact).filter(SiteContact.email == "robin@writein.com").first()
        assert contact.contact_role == "Quality Auditor"

    def test_create_with_other_empty_custom_falls_back(self, client: TestClient, db_session: Session, test_user: User):
        company, site = _make_company_with_hq(db_session, test_user)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/contacts",
            data={
                "site_id": str(site.id),
                "first_name": "Sam",
                "last_name": "Fallback",
                "email": "sam@writein.com",
                "contact_role": "other",
                "contact_role_custom": "   ",
            },
        )
        assert resp.status_code == 200
        contact = db_session.query(SiteContact).filter(SiteContact.email == "sam@writein.com").first()
        assert contact.contact_role == "other"

    def test_create_with_other_custom_capped_at_50_chars(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        company, site = _make_company_with_hq(db_session, test_user)
        long_custom = "Extremely Long Custom Contact Role Description Text" * 2  # > 50 chars
        resp = client.post(
            f"/v2/partials/customers/{company.id}/contacts",
            data={
                "site_id": str(site.id),
                "first_name": "Cap",
                "last_name": "Test",
                "email": "cap@writein.com",
                "contact_role": "other",
                "contact_role_custom": long_custom,
            },
        )
        assert resp.status_code == 200
        contact = db_session.query(SiteContact).filter(SiteContact.email == "cap@writein.com").first()
        assert contact.contact_role == long_custom.strip()[:50]
        assert len(contact.contact_role) == 50


class TestEditContactRoleWriteIn:
    def _make_contact(self, db_session: Session, company: Company, site: CustomerSite) -> SiteContact:
        contact = SiteContact(
            customer_site_id=site.id,
            full_name="Existing Contact",
            email="existing@writein.com",
            contact_role="buyer",
        )
        db_session.add(contact)
        db_session.commit()
        db_session.refresh(contact)
        return contact

    def test_edit_to_custom_write_in(self, client: TestClient, db_session: Session, test_user: User):
        company, site = _make_company_with_hq(db_session, test_user)
        contact = self._make_contact(db_session, company, site)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/sites/{site.id}/contacts/{contact.id}/edit",
            data={
                "full_name": "Existing Contact",
                "email": "existing@writein.com",
                "contact_role": "other",
                "contact_role_custom": "Compliance Lead",
            },
        )
        assert resp.status_code == 200
        db_session.refresh(contact)
        assert contact.contact_role == "Compliance Lead"

    def test_edit_other_empty_custom_falls_back_to_plain_other(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        company, site = _make_company_with_hq(db_session, test_user)
        contact = self._make_contact(db_session, company, site)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/sites/{site.id}/contacts/{contact.id}/edit",
            data={
                "full_name": "Existing Contact",
                "email": "existing@writein.com",
                "contact_role": "other",
                "contact_role_custom": "",
            },
        )
        assert resp.status_code == 200
        db_session.refresh(contact)
        assert contact.contact_role == "other"

    def test_edit_custom_capped_at_50_chars(self, client: TestClient, db_session: Session, test_user: User):
        company, site = _make_company_with_hq(db_session, test_user)
        contact = self._make_contact(db_session, company, site)
        long_custom = "Y" * 75
        resp = client.post(
            f"/v2/partials/customers/{company.id}/sites/{site.id}/contacts/{contact.id}/edit",
            data={
                "full_name": "Existing Contact",
                "email": "existing@writein.com",
                "contact_role": "other",
                "contact_role_custom": long_custom,
            },
        )
        assert resp.status_code == 200
        db_session.refresh(contact)
        assert contact.contact_role == "Y" * 50

    def test_edit_field_not_submitted_leaves_role_unchanged(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """contact_role omitted from the form entirely (not blank) must not touch the
        stored role — mirrors the "field was submitted" guard the other registry fields
        use."""
        company, site = _make_company_with_hq(db_session, test_user)
        contact = self._make_contact(db_session, company, site)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/sites/{site.id}/contacts/{contact.id}/edit",
            data={"full_name": "Existing Contact", "email": "existing@writein.com"},
        )
        assert resp.status_code == 200
        db_session.refresh(contact)
        assert contact.contact_role == "buyer"


class TestContactFormWriteInMarkup:
    """The add/edit modal form renders the write-in toggle markup — the Alpine
    x-data/x-show scaffolding that reveals contact_role_custom when 'other' is selected,
    and (in edit mode) prefills the custom text for an existing non-canonical stored
    role."""

    def test_add_form_renders_role_other_toggle(self, client: TestClient, db_session: Session, test_user: User):
        company, site = _make_company_with_hq(db_session, test_user)
        resp = client.get(f"/v2/partials/customers/{company.id}/contacts/add-form")
        assert resp.status_code == 200
        html = resp.text
        assert "roleOther" in html
        assert "contact_role_custom" in html
        assert "x-show='roleOther'" in html

    def test_add_form_lists_all_canonical_roles_as_options(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        company, site = _make_company_with_hq(db_session, test_user)
        html = client.get(f"/v2/partials/customers/{company.id}/contacts/add-form").text
        for role in (
            "buyer",
            "manager",
            "engineer",
            "planner",
            "buyer_po",
            "specifier",
            "ap_payer",
            "logistics",
            "exec",
            "technical",
            "decision_maker",
            "operations",
            "other",
        ):
            assert f"value='{role}'" in html, f"missing <option> for role={role}"

    def test_edit_form_prefills_custom_role_and_preselects_other(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        company, site = _make_company_with_hq(db_session, test_user)
        contact = SiteContact(
            customer_site_id=site.id,
            full_name="Custom Role Contact",
            email="customrole@writein.com",
            contact_role="Regional Buyer",
        )
        db_session.add(contact)
        db_session.commit()
        db_session.refresh(contact)

        resp = client.get(f"/v2/partials/customers/{company.id}/contacts/{contact.id}/edit-form")
        assert resp.status_code == 200
        html = resp.text
        assert "roleOther&quot;: true" in html or '"roleOther": true' in html
        assert "Regional Buyer" in html
