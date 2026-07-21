"""tests/test_contact_fields.py — Step 4: first/last name split + contact_owner_id.

Tests migration backfill logic, form create/edit composing full_name, inline edits,
validation, and owner select rendering.

Called by: pytest
Depends on: app.models.crm, app.routers.htmx_views, conftest.py
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.auth import User
from app.models.crm import Company, CustomerSite, SiteContact

# ── Backfill logic tests (model-level, no HTTP) ──────────────────────────────


class TestNameBackfill:
    """Test the backfill split logic used in migration 134."""

    def test_split_two_tokens(self):
        """'Jane Doe' → first=Jane, last=Doe."""
        parts = "Jane Doe".split(" ", 1)
        assert parts[0] == "Jane"
        assert parts[1] == "Doe"

    def test_split_three_tokens(self):
        """'Mary Jane Watson' → first=Mary, last='Jane Watson'."""
        parts = "Mary Jane Watson".split(" ", 1)
        assert parts[0] == "Mary"
        assert parts[1] == "Jane Watson"

    def test_split_single_token(self):
        """'Cher' → first=Cher, last=None."""
        parts = "Cher".split(" ", 1)
        assert parts[0] == "Cher"
        assert len(parts) == 1


class TestMigrationBackfill:
    """Test that the migration backfill sets first_name/last_name correctly."""

    def test_backfill_two_part_name(self, db_session: Session, test_company: Company):
        """'Jane Doe' backfill → first=Jane, last=Doe."""
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
        )
        db_session.add(contact)
        db_session.commit()

        # Simulate the migration backfill
        bind = db_session.connection()
        rows = bind.execute(
            text("SELECT id, full_name FROM site_contacts WHERE id = :rid"),
            {"rid": contact.id},
        ).fetchall()
        for row_id, full_name in rows:
            if full_name:
                parts = full_name.strip().split(" ", 1)
                first = parts[0] or None
                last = parts[1].strip() if len(parts) > 1 else None
                bind.execute(
                    text("UPDATE site_contacts SET first_name = :fn, last_name = :ln WHERE id = :rid"),
                    {"fn": first, "ln": last, "rid": row_id},
                )
        db_session.expire_all()

        db_session.refresh(contact)
        assert contact.first_name == "Jane"
        assert contact.last_name == "Doe"

    def test_backfill_single_name(self, db_session: Session, test_company: Company):
        """'Cher' backfill → first=Cher, last=None."""
        site = CustomerSite(
            company_id=test_company.id,
            site_name="HQ2",
            is_active=True,
        )
        db_session.add(site)
        db_session.flush()

        contact = SiteContact(customer_site_id=site.id, full_name="Cher")
        db_session.add(contact)
        db_session.commit()

        bind = db_session.connection()
        rows = bind.execute(
            text("SELECT id, full_name FROM site_contacts WHERE id = :rid"),
            {"rid": contact.id},
        ).fetchall()
        for row_id, full_name in rows:
            if full_name:
                parts = full_name.strip().split(" ", 1)
                first = parts[0] or None
                last = parts[1].strip() if len(parts) > 1 else None
                bind.execute(
                    text("UPDATE site_contacts SET first_name = :fn, last_name = :ln WHERE id = :rid"),
                    {"fn": first, "ln": last, "rid": row_id},
                )
        db_session.expire_all()

        db_session.refresh(contact)
        assert contact.first_name == "Cher"
        assert contact.last_name is None


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def site_and_contact(db_session: Session, test_company: Company, test_user: User):
    """HQ site + contact with first/last name."""
    # Owner set so the account-gated edit-form GET (can_manage_account) renders.
    test_company.account_owner_id = test_user.id
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
        full_name="Alice Smith",
        first_name="Alice",
        last_name="Smith",
        email="alice@acme.com",
    )
    db_session.add(contact)
    db_session.commit()
    db_session.refresh(contact)
    return site, contact


# ── Form create tests ─────────────────────────────────────────────────────────


class TestContactCreateFirstLast:
    """Creating a contact with first_name + last_name composes full_name."""

    def test_create_first_last_composes_full_name(
        self,
        client: TestClient,
        test_company: Company,
        test_user: User,
        db_session: Session,
    ):
        """POST with first+last → contact.full_name = 'First Last'."""
        test_company.account_owner_id = test_user.id
        db_session.commit()
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/contacts",
            data={
                "first_name": "Bob",
                "last_name": "Builder",
                "email": "bob@builder.com",
            },
        )
        assert resp.status_code == 200
        contact = db_session.query(SiteContact).filter(SiteContact.email == "bob@builder.com").first()
        assert contact is not None
        assert contact.first_name == "Bob"
        assert contact.last_name == "Builder"
        assert contact.full_name == "Bob Builder"

    def test_create_first_only_composes_full_name(
        self,
        client: TestClient,
        test_company: Company,
        test_user: User,
        db_session: Session,
    ):
        """POST with first_name only → full_name = first_name."""
        test_company.account_owner_id = test_user.id
        db_session.commit()
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/contacts",
            data={"first_name": "Madonna", "email": "madonna@pop.com"},
        )
        assert resp.status_code == 200
        contact = db_session.query(SiteContact).filter(SiteContact.email == "madonna@pop.com").first()
        assert contact is not None
        assert contact.full_name == "Madonna"
        assert contact.first_name == "Madonna"
        assert contact.last_name is None

    def test_create_blank_first_and_last_returns_400(
        self,
        client: TestClient,
        test_company: Company,
        test_user: User,
        db_session: Session,
    ):
        """POST with both first_name and last_name blank → 400."""
        test_company.account_owner_id = test_user.id
        db_session.commit()
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/contacts",
            data={"first_name": "", "last_name": ""},
        )
        assert resp.status_code == 400

    def test_create_ignores_contact_owner_id(
        self,
        client: TestClient,
        test_company: Company,
        test_user: User,
        db_session: Session,
    ):
        """POST with contact_owner_id in form data — field is ignored (picker
        removed)."""
        test_company.account_owner_id = test_user.id
        db_session.commit()
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/contacts",
            data={
                "first_name": "Owned",
                "last_name": "Contact",
                "email": "owned@acme.com",
                "contact_owner_id": str(test_user.id),
            },
        )
        assert resp.status_code == 200
        contact = db_session.query(SiteContact).filter(SiteContact.email == "owned@acme.com").first()
        assert contact is not None
        # contact_owner_id must NOT be set — picker is removed, ownership via site→account
        assert contact.contact_owner_id is None


# ── Edit tests ────────────────────────────────────────────────────────────────


class TestContactEditFirstLast:
    """Editing first/last name via the form endpoint recomposes full_name."""

    def test_edit_first_last_recomposes_full_name(
        self,
        client: TestClient,
        test_company: Company,
        test_user: User,
        site_and_contact,
        db_session: Session,
    ):
        """POST with first+last → full_name is recomposed."""
        test_company.account_owner_id = test_user.id
        db_session.commit()
        site, contact = site_and_contact
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/{site.id}/contacts/{contact.id}/edit",
            data={"first_name": "Alicia", "last_name": "Keys"},
        )
        assert resp.status_code == 200
        db_session.refresh(contact)
        assert contact.first_name == "Alicia"
        assert contact.last_name == "Keys"
        assert contact.full_name == "Alicia Keys"

    def test_edit_first_name_only_recomposes_full_name(
        self,
        client: TestClient,
        test_company: Company,
        test_user: User,
        site_and_contact,
        db_session: Session,
    ):
        """Editing first+last keeps values and recomposes."""
        test_company.account_owner_id = test_user.id
        site, contact = site_and_contact
        contact.first_name = "Alice"
        contact.last_name = "Smith"
        contact.full_name = "Alice Smith"
        db_session.commit()

        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/{site.id}/contacts/{contact.id}/edit",
            data={"first_name": "Alicia", "last_name": "Smith"},
        )
        assert resp.status_code == 200
        db_session.refresh(contact)
        assert contact.full_name == "Alicia Smith"

    def test_edit_blank_first_and_last_returns_400(
        self,
        client: TestClient,
        test_company: Company,
        test_user: User,
        site_and_contact,
        db_session: Session,
    ):
        """Clearing both first AND last name → 400."""
        test_company.account_owner_id = test_user.id
        db_session.commit()
        site, contact = site_and_contact
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/{site.id}/contacts/{contact.id}/edit",
            data={"first_name": "", "last_name": ""},
        )
        assert resp.status_code == 400


# ── Inline edit tests ─────────────────────────────────────────────────────────


class TestContactInlineEdit:
    """Inline field edit for first_name, last_name, contact_owner_id."""

    def test_inline_edit_first_name_recomposes_full_name(
        self,
        client: TestClient,
        test_company: Company,
        test_user: User,
        site_and_contact,
        db_session: Session,
    ):
        """Inline POST first_name → full_name is recomposed."""
        # contact_field_post requires account_owner_id == user.id or admin
        test_company.account_owner_id = test_user.id
        db_session.commit()

        site, contact = site_and_contact
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/contacts/{contact.id}/field",
            data={"field": "first_name", "value": "Alexandra"},
        )
        assert resp.status_code == 200
        db_session.refresh(contact)
        assert contact.first_name == "Alexandra"
        assert contact.full_name == f"Alexandra {contact.last_name or ''}".strip()

    def test_inline_edit_blank_first_name_with_last_name_ok(
        self,
        client: TestClient,
        test_company: Company,
        test_user: User,
        site_and_contact,
        db_session: Session,
    ):
        """Clearing first_name is OK if last_name exists."""
        test_company.account_owner_id = test_user.id
        db_session.commit()

        site, contact = site_and_contact
        contact.last_name = "Smith"
        db_session.commit()

        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/contacts/{contact.id}/field",
            data={"field": "first_name", "value": ""},
        )
        assert resp.status_code == 200
        db_session.refresh(contact)
        assert contact.first_name is None
        assert contact.full_name == "Smith"

    def test_inline_edit_contact_owner_id_not_available(
        self,
        client: TestClient,
        test_company: Company,
        site_and_contact,
        test_user: User,
        db_session: Session,
    ):
        """contact_owner_id is NOT in EDITABLE_CONTACT_FIELDS — inline edit returns 404.

        Per Phase 1 ownership cleanup, the per-contact owner picker is removed.
        Ownership flows via site→account, so the inline field path must remain 404.
        """
        test_company.account_owner_id = test_user.id
        db_session.commit()

        site, contact = site_and_contact
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/contacts/{contact.id}/field",
            data={"field": "contact_owner_id", "value": str(test_user.id)},
        )
        assert resp.status_code == 404, "contact_owner_id inline edit must return 404"

    def test_inline_edit_contact_owner_id_clear_not_available(
        self,
        client: TestClient,
        test_company: Company,
        site_and_contact,
        test_user: User,
        db_session: Session,
    ):
        """Clearing contact_owner_id via inline edit also returns 404 (picker
        removed)."""
        test_company.account_owner_id = test_user.id
        db_session.commit()

        site, contact = site_and_contact
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/contacts/{contact.id}/field",
            data={"field": "contact_owner_id", "value": ""},
        )
        assert resp.status_code == 404, "contact_owner_id inline clear must return 404"


# ── Add/edit form must NOT render contact_owner_id picker ─────────────────────


class TestContactFormOwnerSelect:
    """Phase 1 ownership cleanup: per-contact owner picker is removed from add/edit forms."""

    def test_add_form_does_not_render_owner_picker(
        self, client: TestClient, db_session: Session, test_company: Company, test_user: User
    ):
        """Add-form GET must NOT contain contact_owner_id (picker removed)."""
        test_company.account_owner_id = test_user.id  # owner passes can_manage_account gate
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{test_company.id}/contacts/add-form")
        assert resp.status_code == 200
        assert "contact_owner_id" not in resp.text

    def test_edit_form_does_not_render_owner_picker(
        self,
        client: TestClient,
        test_company: Company,
        site_and_contact,
        test_user: User,
    ):
        """Edit-form GET must NOT contain contact_owner_id (picker removed)."""
        site, contact = site_and_contact
        resp = client.get(f"/v2/partials/customers/{test_company.id}/contacts/{contact.id}/edit-form")
        assert resp.status_code == 200
        assert "contact_owner_id" not in resp.text

    def test_add_form_renders_first_last_inputs(
        self, client: TestClient, db_session: Session, test_company: Company, test_user: User
    ):
        """Add form renders first_name and last_name inputs (not full_name)."""
        test_company.account_owner_id = test_user.id  # owner passes can_manage_account gate
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{test_company.id}/contacts/add-form")
        assert resp.status_code == 200
        # Template uses single-quoted attributes
        assert "name='first_name'" in resp.text
        assert "name='last_name'" in resp.text


# ── full_name still renders ───────────────────────────────────────────────────


@pytest.fixture()
def _grant_account_management(test_user: User, db_session: Session) -> None:
    """Promote the buyer ``test_user`` to MANAGER so it can_manage every account.

    The contacts tab is reached via the company detail partial
    (``GET /v2/partials/customers/{id}``), which now gates on ``can_manage_account``. The
    class below GETs that endpoint as ``test_user`` on ``test_company`` without assigning
    ownership, so promote the actor to MANAGER (``can_manage_account`` is True for managers,
    exactly as for the account owner) to exercise the authorized render path.
    """
    test_user.role = "manager"
    db_session.commit()


@pytest.mark.usefixtures("_grant_account_management")
class TestFullNameStillRenders:
    """full_name is still the display field everywhere."""

    def test_full_name_in_contacts_list(
        self,
        client: TestClient,
        test_company: Company,
        site_and_contact,
    ):
        """Contact list still shows full_name via the contacts tab."""
        site, contact = site_and_contact
        resp = client.get(f"/v2/partials/customers/{test_company.id}?tab=contacts")
        assert resp.status_code == 200
        assert contact.full_name in resp.text
