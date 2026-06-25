"""Tests for WS3 — custom_fields on Company and SiteContact.

Tests: model validation, CRUD endpoints, IDOR guard on contact routes,
template rendering, and migration chain.

Called by: pytest
Depends on: app.models.crm (Company, SiteContact), app.routers.htmx_views, conftest
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.auth import User
from app.models.crm import Company, CustomerSite, SiteContact
from tests.conftest import engine  # noqa: F401

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def owned_company(db_session: Session, test_user: User) -> Company:
    """A company owned by test_user so the owner-gate passes."""
    co = Company(
        name="Custom Field Corp",
        is_active=True,
        account_owner_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def company_site(db_session: Session, owned_company: Company) -> CustomerSite:
    """A site under owned_company."""
    site = CustomerSite(
        company_id=owned_company.id,
        site_name="HQ",
        is_active=True,
    )
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)
    return site


@pytest.fixture()
def company_contact(db_session: Session, company_site: CustomerSite) -> SiteContact:
    """A SiteContact under company_site."""
    contact = SiteContact(
        customer_site_id=company_site.id,
        full_name="Custom Fields Person",
        email="cf@example.com",
    )
    db_session.add(contact)
    db_session.commit()
    db_session.refresh(contact)
    return contact


@pytest.fixture()
def other_company(db_session: Session) -> Company:
    """A separate company with its own site + contact (IDOR target)."""
    co = Company(name="Other Corp", is_active=True, created_at=datetime.now(timezone.utc))
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    site = CustomerSite(company_id=co.id, site_name="Main", is_active=True)
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)
    contact = SiteContact(
        customer_site_id=site.id,
        full_name="Other Person",
        email="other@corp.com",
    )
    db_session.add(contact)
    db_session.commit()
    db_session.refresh(contact)
    return co


# ── Model / @validates tests ──────────────────────────────────────────────────


class TestCustomFieldsModelValidation:
    """@validates enforcement on Company and SiteContact."""

    def test_company_accepts_valid_dict(self, db_session: Session):
        co = Company(name="Test", custom_fields={"k": "v"}, created_at=datetime.now(timezone.utc))
        db_session.add(co)
        db_session.commit()
        db_session.refresh(co)
        assert co.custom_fields == {"k": "v"}

    def test_company_none_becomes_empty_dict(self, db_session: Session):
        co = Company(name="None Test", custom_fields=None, created_at=datetime.now(timezone.utc))
        db_session.add(co)
        db_session.commit()
        db_session.refresh(co)
        assert co.custom_fields == {}

    def test_company_rejects_non_dict(self):
        with pytest.raises(ValueError, match="must be a dict"):
            Company(name="Bad", custom_fields=["list"], created_at=datetime.now(timezone.utc))

    def test_company_rejects_too_many_keys(self):
        big = {str(i): "v" for i in range(31)}
        with pytest.raises(ValueError, match="max 30 keys"):
            Company(name="Too Many", custom_fields=big, created_at=datetime.now(timezone.utc))

    def test_company_rejects_long_key(self):
        long_key = "k" * 61
        with pytest.raises(ValueError, match="key too long"):
            Company(
                name="Long Key",
                custom_fields={long_key: "v"},
                created_at=datetime.now(timezone.utc),
            )

    def test_company_rejects_long_value(self):
        with pytest.raises(ValueError, match="value too long"):
            Company(
                name="Long Val",
                custom_fields={"k": "v" * 501},
                created_at=datetime.now(timezone.utc),
            )

    def test_site_contact_accepts_valid_dict(self, db_session: Session, company_site: CustomerSite):
        sc = SiteContact(
            customer_site_id=company_site.id,
            full_name="Val Test",
            custom_fields={"contract": "ABC-123"},
        )
        db_session.add(sc)
        db_session.commit()
        db_session.refresh(sc)
        assert sc.custom_fields == {"contract": "ABC-123"}

    def test_site_contact_rejects_non_dict(self, company_site: CustomerSite):
        with pytest.raises(ValueError, match="must be a dict"):
            SiteContact(
                customer_site_id=company_site.id,
                full_name="Bad",
                custom_fields="string",
            )

    def test_site_contact_rejects_too_many_keys(self, company_site: CustomerSite):
        big = {str(i): "v" for i in range(31)}
        with pytest.raises(ValueError, match="max 30 keys"):
            SiteContact(
                customer_site_id=company_site.id,
                full_name="Big",
                custom_fields=big,
            )

    def test_site_contact_rejects_long_key(self, company_site: CustomerSite):
        with pytest.raises(ValueError, match="key too long"):
            SiteContact(
                customer_site_id=company_site.id,
                full_name="LongKey",
                custom_fields={"k" * 61: "v"},
            )

    def test_site_contact_rejects_long_value(self, company_site: CustomerSite):
        with pytest.raises(ValueError, match="value too long"):
            SiteContact(
                customer_site_id=company_site.id,
                full_name="LongVal",
                custom_fields={"k": "v" * 501},
            )


# ── Company custom-fields endpoint tests ─────────────────────────────────────


class TestCompanyCustomFieldsEndpoints:
    """POST / DELETE /v2/partials/customers/{id}/custom-fields."""

    def test_post_adds_field(
        self,
        client: TestClient,
        db_session: Session,
        owned_company: Company,
    ):
        resp = client.post(
            f"/v2/partials/customers/{owned_company.id}/custom-fields",
            data={"label": "Contract #", "value": "CTR-001"},
        )
        assert resp.status_code == 200
        db_session.refresh(owned_company)
        assert owned_company.custom_fields.get("Contract #") == "CTR-001"

    def test_post_second_field_keeps_existing(
        self,
        client: TestClient,
        db_session: Session,
        owned_company: Company,
    ):
        client.post(
            f"/v2/partials/customers/{owned_company.id}/custom-fields",
            data={"label": "Field A", "value": "val-a"},
        )
        client.post(
            f"/v2/partials/customers/{owned_company.id}/custom-fields",
            data={"label": "Field B", "value": "val-b"},
        )
        db_session.refresh(owned_company)
        assert owned_company.custom_fields.get("Field A") == "val-a"
        assert owned_company.custom_fields.get("Field B") == "val-b"

    def test_post_duplicate_label_overwrites(
        self,
        client: TestClient,
        db_session: Session,
        owned_company: Company,
    ):
        client.post(
            f"/v2/partials/customers/{owned_company.id}/custom-fields",
            data={"label": "Key", "value": "original"},
        )
        client.post(
            f"/v2/partials/customers/{owned_company.id}/custom-fields",
            data={"label": "Key", "value": "overwritten"},
        )
        db_session.refresh(owned_company)
        assert owned_company.custom_fields["Key"] == "overwritten"
        assert len(owned_company.custom_fields) == 1

    def test_post_returns_partial_html(
        self,
        client: TestClient,
        owned_company: Company,
    ):
        resp = client.post(
            f"/v2/partials/customers/{owned_company.id}/custom-fields",
            data={"label": "Note", "value": "test"},
        )
        assert "text/html" in resp.headers.get("content-type", "")
        assert f"custom-fields-company-{owned_company.id}" in resp.text

    def test_post_renders_add_affordance(
        self,
        client: TestClient,
        owned_company: Company,
    ):
        resp = client.post(
            f"/v2/partials/customers/{owned_company.id}/custom-fields",
            data={"label": "x", "value": "y"},
        )
        assert "Add field" in resp.text

    def test_delete_removes_key(
        self,
        client: TestClient,
        db_session: Session,
        owned_company: Company,
    ):
        client.post(
            f"/v2/partials/customers/{owned_company.id}/custom-fields",
            data={"label": "ToRemove", "value": "bye"},
        )
        resp = client.delete(f"/v2/partials/customers/{owned_company.id}/custom-fields/ToRemove")
        assert resp.status_code == 200
        db_session.refresh(owned_company)
        assert "ToRemove" not in owned_company.custom_fields

    def test_delete_nonexistent_label_is_idempotent(
        self,
        client: TestClient,
        owned_company: Company,
    ):
        resp = client.delete(f"/v2/partials/customers/{owned_company.id}/custom-fields/NoSuchLabel")
        assert resp.status_code == 200

    def test_post_missing_label_returns_400(
        self,
        client: TestClient,
        owned_company: Company,
    ):
        resp = client.post(
            f"/v2/partials/customers/{owned_company.id}/custom-fields",
            data={"value": "no label here"},
        )
        assert resp.status_code == 400

    def test_post_unknown_company_returns_404(self, client: TestClient):
        resp = client.post(
            "/v2/partials/customers/999999/custom-fields",
            data={"label": "x", "value": "y"},
        )
        assert resp.status_code == 404

    def test_post_non_owner_returns_403(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
    ):
        """test_user is not the owner of this company (owner_id is None)."""
        unowned = Company(
            name="Unowned Corp",
            is_active=True,
            account_owner_id=None,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(unowned)
        db_session.commit()
        db_session.refresh(unowned)
        resp = client.post(
            f"/v2/partials/customers/{unowned.id}/custom-fields",
            data={"label": "x", "value": "y"},
        )
        assert resp.status_code == 403

    def test_post_exceeding_cap_returns_400_not_500(
        self,
        client: TestClient,
        db_session: Session,
        owned_company: Company,
    ):
        """FIX C: the @validates 30-key cap surfaces as 400, not an uncaught 500."""
        owned_company.custom_fields = {str(i): "v" for i in range(30)}
        db_session.commit()
        resp = client.post(
            f"/v2/partials/customers/{owned_company.id}/custom-fields",
            data={"label": "one-too-many", "value": "x"},
        )
        assert resp.status_code == 400


# ── Contact custom-fields endpoint tests ─────────────────────────────────────


class TestContactCustomFieldsEndpoints:
    """POST / DELETE /v2/partials/customers/{cid}/contacts/{ctid}/custom-fields."""

    def test_post_adds_field_to_contact(
        self,
        client: TestClient,
        db_session: Session,
        owned_company: Company,
        company_contact: SiteContact,
    ):
        resp = client.post(
            f"/v2/partials/customers/{owned_company.id}/contacts/{company_contact.id}/custom-fields",
            data={"label": "Tier", "value": "Gold"},
        )
        assert resp.status_code == 200
        db_session.refresh(company_contact)
        assert company_contact.custom_fields.get("Tier") == "Gold"

    def test_post_second_contact_field_keeps_existing(
        self,
        client: TestClient,
        db_session: Session,
        owned_company: Company,
        company_contact: SiteContact,
    ):
        client.post(
            f"/v2/partials/customers/{owned_company.id}/contacts/{company_contact.id}/custom-fields",
            data={"label": "A", "value": "1"},
        )
        client.post(
            f"/v2/partials/customers/{owned_company.id}/contacts/{company_contact.id}/custom-fields",
            data={"label": "B", "value": "2"},
        )
        db_session.refresh(company_contact)
        assert company_contact.custom_fields["A"] == "1"
        assert company_contact.custom_fields["B"] == "2"

    def test_post_duplicate_contact_label_overwrites(
        self,
        client: TestClient,
        db_session: Session,
        owned_company: Company,
        company_contact: SiteContact,
    ):
        client.post(
            f"/v2/partials/customers/{owned_company.id}/contacts/{company_contact.id}/custom-fields",
            data={"label": "Key", "value": "v1"},
        )
        client.post(
            f"/v2/partials/customers/{owned_company.id}/contacts/{company_contact.id}/custom-fields",
            data={"label": "Key", "value": "v2"},
        )
        db_session.refresh(company_contact)
        assert company_contact.custom_fields["Key"] == "v2"

    def test_delete_removes_contact_field(
        self,
        client: TestClient,
        db_session: Session,
        owned_company: Company,
        company_contact: SiteContact,
    ):
        client.post(
            f"/v2/partials/customers/{owned_company.id}/contacts/{company_contact.id}/custom-fields",
            data={"label": "Remove Me", "value": "gone"},
        )
        resp = client.delete(
            f"/v2/partials/customers/{owned_company.id}/contacts/{company_contact.id}/custom-fields/Remove Me"
        )
        assert resp.status_code == 200
        db_session.refresh(company_contact)
        assert "Remove Me" not in company_contact.custom_fields

    def test_contact_renders_partial_container(
        self,
        client: TestClient,
        owned_company: Company,
        company_contact: SiteContact,
    ):
        resp = client.post(
            f"/v2/partials/customers/{owned_company.id}/contacts/{company_contact.id}/custom-fields",
            data={"label": "x", "value": "y"},
        )
        assert f"custom-fields-contact-{company_contact.id}" in resp.text

    def test_contact_idor_guard_wrong_company(
        self,
        client: TestClient,
        owned_company: Company,
        other_company: Company,
        company_contact: SiteContact,
    ):
        """Contact from owned_company cannot be accessed via other_company's path."""
        resp = client.post(
            f"/v2/partials/customers/{other_company.id}/contacts/{company_contact.id}/custom-fields",
            data={"label": "x", "value": "y"},
        )
        assert resp.status_code == 404

    def test_contact_idor_guard_delete_wrong_company(
        self,
        client: TestClient,
        owned_company: Company,
        other_company: Company,
        company_contact: SiteContact,
    ):
        """DELETE via wrong company path is blocked."""
        resp = client.delete(f"/v2/partials/customers/{other_company.id}/contacts/{company_contact.id}/custom-fields/x")
        assert resp.status_code == 404

    def test_contact_post_unknown_company_returns_404(
        self,
        client: TestClient,
        company_contact: SiteContact,
    ):
        resp = client.post(
            f"/v2/partials/customers/999999/contacts/{company_contact.id}/custom-fields",
            data={"label": "x", "value": "y"},
        )
        assert resp.status_code == 404

    def test_contact_post_non_owner_returns_403(
        self,
        db_session: Session,
        owned_company: Company,
        company_contact: SiteContact,
    ):
        """FIX A: a non-owner, non-admin user cannot add a contact custom field."""
        from fastapi.testclient import TestClient as _TC

        from app.database import get_db
        from app.dependencies import require_user
        from app.main import app

        other = User(email="intruder@trioscs.com", name="Intruder", role="buyer", azure_id="intruder-az")
        db_session.add(other)
        db_session.commit()
        db_session.refresh(other)

        app.dependency_overrides[get_db] = lambda: db_session
        app.dependency_overrides[require_user] = lambda: other
        try:
            with _TC(app, raise_server_exceptions=False) as c:
                resp = c.post(
                    f"/v2/partials/customers/{owned_company.id}/contacts/{company_contact.id}/custom-fields",
                    data={"label": "x", "value": "y"},
                )
            assert resp.status_code == 403
        finally:
            app.dependency_overrides.pop(get_db, None)
            app.dependency_overrides.pop(require_user, None)

    def test_contact_post_exceeding_cap_returns_400_not_500(
        self,
        client: TestClient,
        db_session: Session,
        owned_company: Company,
        company_contact: SiteContact,
    ):
        """FIX C: the contact @validates cap surfaces as 400, not an uncaught 500."""
        company_contact.custom_fields = {str(i): "v" for i in range(30)}
        db_session.commit()
        resp = client.post(
            f"/v2/partials/customers/{owned_company.id}/contacts/{company_contact.id}/custom-fields",
            data={"label": "over", "value": "x"},
        )
        assert resp.status_code == 400


# ── Template rendering tests ──────────────────────────────────────────────────


class TestCustomFieldsTemplateRendering:
    """Rendered HTML contains the right structure."""

    def test_rendered_section_shows_add_affordance(
        self,
        client: TestClient,
        owned_company: Company,
    ):
        resp = client.post(
            f"/v2/partials/customers/{owned_company.id}/custom-fields",
            data={"label": "Label1", "value": "Val1"},
        )
        assert "Add field" in resp.text

    def test_rendered_section_shows_existing_pair(
        self,
        client: TestClient,
        owned_company: Company,
    ):
        client.post(
            f"/v2/partials/customers/{owned_company.id}/custom-fields",
            data={"label": "Contract#", "value": "C-999"},
        )
        resp = client.post(
            f"/v2/partials/customers/{owned_company.id}/custom-fields",
            data={"label": "Status", "value": "Active"},
        )
        assert "Contract#" in resp.text
        assert "C-999" in resp.text

    def test_company_detail_includes_additional_details_section(
        self,
        client: TestClient,
        owned_company: Company,
    ):
        resp = client.get(f"/v2/partials/customers/{owned_company.id}")
        assert resp.status_code == 200
        assert "Additional details" in resp.text
        assert f"custom-fields-company-{owned_company.id}" in resp.text


# ── Migration chain test ──────────────────────────────────────────────────────


class TestMigration132:
    """Verify migration 132 chains correctly and custom_fields columns exist in test
    DB."""

    def test_custom_fields_on_company_model(self, db_session: Session):
        """Company.custom_fields column exists and defaults to empty dict."""
        from sqlalchemy import inspect

        inspector = inspect(db_session.bind)
        cols = {c["name"] for c in inspector.get_columns("companies")}
        assert "custom_fields" in cols

    def test_custom_fields_on_site_contacts_model(self, db_session: Session):
        """site_contacts.custom_fields column exists."""
        from sqlalchemy import inspect

        inspector = inspect(db_session.bind)
        cols = {c["name"] for c in inspector.get_columns("site_contacts")}
        assert "custom_fields" in cols

    def test_migration_down_revision_is_131(self):
        """Migration 132 chains onto 131_tbf_search_tables."""
        import importlib.util
        import pathlib

        migration_path = pathlib.Path(__file__).parent.parent / "alembic" / "versions" / "132_crm_custom_fields.py"
        spec = importlib.util.spec_from_file_location("migration_132", migration_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.down_revision == "131_tbf_search_tables"
        assert mod.revision == "132_crm_custom_fields"
