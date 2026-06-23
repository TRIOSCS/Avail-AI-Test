"""tests/test_inline_field_edit.py — WS1 inline click-to-edit field endpoints.

Tests for GET edit widget, POST save, validation, normalization, authz.
Called by: pytest
Depends on: app.routers.htmx_views, conftest.py
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Company, CustomerSite, SiteContact, User

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_client(app, db_session, user):
    """Build a TestClient with dependency overrides for the given user."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user

    def _db():
        yield db_session

    def _user():
        return user

    async def _fresh():
        return "mock-token"

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = _user
    app.dependency_overrides[require_admin] = _user
    app.dependency_overrides[require_buyer] = _user
    app.dependency_overrides[require_fresh_token] = _fresh
    return app


def _clear_overrides(app):
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user

    for dep in [get_db, require_user, require_admin, require_buyer, require_fresh_token]:
        app.dependency_overrides.pop(dep, None)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def owner_client(db_session: Session, test_company: Company, test_user: User):
    """TestClient where the logged-in user owns test_company."""
    test_company.account_owner_id = test_user.id
    db_session.commit()

    from app.main import app

    _make_client(app, db_session, test_user)
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    _clear_overrides(app)


@pytest.fixture()
def non_owner_client(db_session: Session, test_company: Company):
    """TestClient where the logged-in user does NOT own test_company and is not
    admin."""
    other = User(
        email="nonowner@trioscs.com",
        name="Non Owner",
        role="buyer",
        azure_id="non-owner-azure-id",
    )
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)

    from app.main import app

    _make_client(app, db_session, other)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    _clear_overrides(app)


@pytest.fixture()
def site_and_contact(db_session: Session, test_company: Company):
    """A site + contact for test_company."""
    site = CustomerSite(
        company_id=test_company.id,
        site_name="HQ",
        site_type="hq",
        is_active=True,
    )
    db_session.add(site)
    db_session.flush()
    contact = SiteContact(
        customer_site_id=site.id,
        full_name="Jane Doe",
        title="Engineer",
        email="jane@acme.com",
    )
    db_session.add(contact)
    db_session.commit()
    db_session.refresh(contact)
    return site, contact


# ── Company field tests ────────────────────────────────────────────────────────


class TestCompanyFieldEdit:
    def test_get_edit_text_field_returns_input(self, owner_client, test_company):
        resp = owner_client.get(f"/v2/partials/customers/{test_company.id}/field/edit/industry")
        assert resp.status_code == 200
        assert "<input" in resp.text

    def test_get_edit_select_field_returns_select(self, owner_client, test_company):
        resp = owner_client.get(f"/v2/partials/customers/{test_company.id}/field/edit/account_type")
        assert resp.status_code == 200
        assert "<select" in resp.text
        assert "Customer" in resp.text

    def test_get_edit_invalid_field_returns_404(self, owner_client, test_company):
        resp = owner_client.get(f"/v2/partials/customers/{test_company.id}/field/edit/nonexistent")
        assert resp.status_code == 404

    def test_post_field_applies_and_returns_display(self, owner_client, test_company, db_session):
        resp = owner_client.post(
            f"/v2/partials/customers/{test_company.id}/field",
            data={"field": "industry", "value": "Aerospace"},
        )
        assert resp.status_code == 200
        assert "Aerospace" in resp.text
        db_session.refresh(test_company)
        assert test_company.industry == "Aerospace"

    def test_post_phone_normalizes_to_e164(self, owner_client, test_company, db_session):
        resp = owner_client.post(
            f"/v2/partials/customers/{test_company.id}/field",
            data={"field": "phone", "value": "(555) 123-4567"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_company)
        assert test_company.phone == "+15551234567"

    def test_post_invalid_field_returns_404(self, owner_client, test_company):
        resp = owner_client.post(
            f"/v2/partials/customers/{test_company.id}/field",
            data={"field": "nonexistent_field", "value": "x"},
        )
        assert resp.status_code == 404

    def test_post_empty_value_clears_field(self, owner_client, test_company, db_session):
        test_company.credit_terms = "Net 30"
        db_session.commit()
        resp = owner_client.post(
            f"/v2/partials/customers/{test_company.id}/field",
            data={"field": "credit_terms", "value": ""},
        )
        assert resp.status_code == 200
        db_session.refresh(test_company)
        assert test_company.credit_terms is None

    def test_display_endpoint_returns_span(self, owner_client, test_company):
        resp = owner_client.get(f"/v2/partials/customers/{test_company.id}/field/display/industry")
        assert resp.status_code == 200
        assert "field-company" in resp.text

    def test_empty_field_renders_add_placeholder(self, owner_client, test_company, db_session):
        test_company.credit_terms = None
        db_session.commit()
        resp = owner_client.get(f"/v2/partials/customers/{test_company.id}/field/display/credit_terms")
        assert resp.status_code == 200
        assert "Add Credit Terms" in resp.text

    def test_non_owner_post_returns_403(self, non_owner_client, test_company):
        resp = non_owner_client.post(
            f"/v2/partials/customers/{test_company.id}/field",
            data={"field": "industry", "value": "Test"},
        )
        assert resp.status_code == 403


# ── Contact field tests ────────────────────────────────────────────────────────


class TestContactFieldEdit:
    def test_get_edit_text_returns_input(self, owner_client, test_company, site_and_contact):
        _, contact = site_and_contact
        resp = owner_client.get(f"/v2/partials/customers/{test_company.id}/contacts/{contact.id}/field/edit/title")
        assert resp.status_code == 200
        assert "<input" in resp.text

    def test_get_edit_role_returns_select(self, owner_client, test_company, site_and_contact):
        _, contact = site_and_contact
        resp = owner_client.get(
            f"/v2/partials/customers/{test_company.id}/contacts/{contact.id}/field/edit/contact_role"
        )
        assert resp.status_code == 200
        assert "<select" in resp.text

    def test_get_edit_invalid_field_returns_404(self, owner_client, test_company, site_and_contact):
        _, contact = site_and_contact
        resp = owner_client.get(f"/v2/partials/customers/{test_company.id}/contacts/{contact.id}/field/edit/bad_field")
        assert resp.status_code == 404

    def test_post_title_saves_and_returns_display(self, owner_client, test_company, site_and_contact, db_session):
        _, contact = site_and_contact
        resp = owner_client.post(
            f"/v2/partials/customers/{test_company.id}/contacts/{contact.id}/field",
            data={"field": "title", "value": "Director"},
        )
        assert resp.status_code == 200
        assert "Director" in resp.text
        db_session.refresh(contact)
        assert contact.title == "Director"

    def test_post_email_without_at_returns_400(self, owner_client, test_company, site_and_contact):
        _, contact = site_and_contact
        resp = owner_client.post(
            f"/v2/partials/customers/{test_company.id}/contacts/{contact.id}/field",
            data={"field": "email", "value": "notanemail"},
        )
        assert resp.status_code == 400

    def test_display_endpoint_returns_span(self, owner_client, test_company, site_and_contact):
        _, contact = site_and_contact
        resp = owner_client.get(f"/v2/partials/customers/{test_company.id}/contacts/{contact.id}/field/display/title")
        assert resp.status_code == 200
        assert "field-contact" in resp.text

    def test_empty_title_renders_add_placeholder(self, owner_client, test_company, site_and_contact, db_session):
        _, contact = site_and_contact
        contact.title = None
        db_session.commit()
        resp = owner_client.get(f"/v2/partials/customers/{test_company.id}/contacts/{contact.id}/field/display/title")
        assert resp.status_code == 200
        assert "Add Title" in resp.text

    def test_empty_email_renders_add_placeholder(self, owner_client, test_company, site_and_contact, db_session):
        _, contact = site_and_contact
        contact.email = None
        db_session.commit()
        resp = owner_client.get(f"/v2/partials/customers/{test_company.id}/contacts/{contact.id}/field/display/email")
        assert resp.status_code == 200
        assert "Add Email" in resp.text

    def test_empty_wechat_renders_add_placeholder(self, owner_client, test_company, site_and_contact, db_session):
        _, contact = site_and_contact
        contact.wechat_id = None
        db_session.commit()
        resp = owner_client.get(
            f"/v2/partials/customers/{test_company.id}/contacts/{contact.id}/field/display/wechat_id"
        )
        assert resp.status_code == 200
        assert "Add WeChat ID" in resp.text

    def test_post_linkedin_saves(self, owner_client, test_company, site_and_contact, db_session):
        _, contact = site_and_contact
        resp = owner_client.post(
            f"/v2/partials/customers/{test_company.id}/contacts/{contact.id}/field",
            data={"field": "linkedin_url", "value": "https://linkedin.com/in/janedoe"},
        )
        assert resp.status_code == 200
        assert "linkedin.com" in resp.text
        db_session.refresh(contact)
        assert contact.linkedin_url == "https://linkedin.com/in/janedoe"


class TestContactFieldEditAuthz:
    """FIX A: contact inline-edit POST is IDOR-safe and owner-or-admin gated."""

    def test_non_owner_post_returns_403(self, non_owner_client, test_company, site_and_contact):
        """A logged-in user who neither owns the company nor is admin gets 403."""
        _, contact = site_and_contact
        resp = non_owner_client.post(
            f"/v2/partials/customers/{test_company.id}/contacts/{contact.id}/field",
            data={"field": "title", "value": "Hacker"},
        )
        assert resp.status_code == 403

    def test_admin_post_succeeds(self, db_session, test_company, site_and_contact, admin_user):
        """An admin (not the owner) can still edit the contact."""
        from fastapi.testclient import TestClient

        from app.main import app

        _, contact = site_and_contact
        test_company.account_owner_id = None
        db_session.commit()
        _make_client(app, db_session, admin_user)
        try:
            with TestClient(app, raise_server_exceptions=True) as c:
                resp = c.post(
                    f"/v2/partials/customers/{test_company.id}/contacts/{contact.id}/field",
                    data={"field": "title", "value": "Director"},
                )
            assert resp.status_code == 200
            db_session.refresh(contact)
            assert contact.title == "Director"
        finally:
            _clear_overrides(app)

    def test_post_mismatched_company_returns_404(self, owner_client, db_session, test_company, site_and_contact):
        """A contact_id that does not belong to {company_id} → 404 (IDOR guard)."""
        _, contact = site_and_contact
        other = Company(name="Other Co", is_active=True)
        db_session.add(other)
        db_session.commit()
        db_session.refresh(other)
        other.account_owner_id = test_company.account_owner_id
        db_session.commit()
        resp = owner_client.post(
            f"/v2/partials/customers/{other.id}/contacts/{contact.id}/field",
            data={"field": "title", "value": "Nope"},
        )
        assert resp.status_code == 404

    def test_get_edit_mismatched_company_returns_404(self, owner_client, db_session, test_company, site_and_contact):
        """The GET edit widget is also IDOR-scoped."""
        _, contact = site_and_contact
        other = Company(name="Other Co 2", is_active=True)
        db_session.add(other)
        db_session.commit()
        db_session.refresh(other)
        other.account_owner_id = test_company.account_owner_id
        db_session.commit()
        resp = owner_client.get(f"/v2/partials/customers/{other.id}/contacts/{contact.id}/field/edit/title")
        assert resp.status_code == 404


class TestCompanyKnownFields:
    """WS2: new fields added to EDITABLE_ACCOUNT_FIELDS (domain, tax_id, source, notes)."""

    def test_known_fields_include_new_fields(self, owner_client, test_company):
        """GET display endpoint for tax_id returns 200 with a field span."""
        resp = owner_client.get(f"/v2/partials/customers/{test_company.id}/field/display/tax_id")
        assert resp.status_code == 200
        assert "field-company" in resp.text

    def test_post_notes_saves(self, owner_client, test_company, db_session):
        """POST notes field persists and returns display."""
        resp = owner_client.post(
            f"/v2/partials/customers/{test_company.id}/field",
            data={"field": "notes", "value": "Important client"},
        )
        assert resp.status_code == 200
        assert "Important client" in resp.text
        db_session.refresh(test_company)
        assert test_company.notes == "Important client"

    def test_post_domain_saves(self, owner_client, test_company, db_session):
        """POST domain field persists and returns display."""
        resp = owner_client.post(
            f"/v2/partials/customers/{test_company.id}/field",
            data={"field": "domain", "value": "acme.com"},
        )
        assert resp.status_code == 200
        assert "acme.com" in resp.text
        db_session.refresh(test_company)
        assert test_company.domain == "acme.com"
