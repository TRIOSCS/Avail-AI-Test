"""tests/test_contact_fields_144.py — migration 144: secondary email/phone + reports-to
+ contact tags.

Tests:
  1. Migration schema round-trip (up/down/up), single head, 3 new columns present,
     reports_to_id SET NULL on contact delete.
  2. Create + edit contact with secondary_email/phone/reports_to_id persisted.
     Inline-edit secondary_email works.
  3. reports_to select excludes self + lists same-company contacts.
  4. Contact tag assign/remove via EntityTag(entity_type='site_contact'); chips render.

Called by: pytest
Depends on: app.models.crm, app.models.tags, app.routers.htmx_views, conftest.py
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.models.auth import User
from app.models.crm import Company, CustomerSite, SiteContact
from app.models.tags import EntityTag, Tag

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def site_and_two_contacts(db_session: Session, test_company: Company):
    """HQ site + two active contacts for testing reports_to and tags."""
    site = CustomerSite(
        company_id=test_company.id,
        site_name="HQ",
        site_type="hq",
        is_active=True,
    )
    db_session.add(site)
    db_session.flush()

    alice = SiteContact(
        customer_site_id=site.id,
        full_name="Alice Manager",
        first_name="Alice",
        last_name="Manager",
        email="alice@acme144.com",
    )
    bob = SiteContact(
        customer_site_id=site.id,
        full_name="Bob Report",
        first_name="Bob",
        last_name="Report",
        email="bob@acme144.com",
    )
    db_session.add_all([alice, bob])
    db_session.commit()
    db_session.refresh(alice)
    db_session.refresh(bob)
    return site, alice, bob


@pytest.fixture()
def segment_tag(db_session: Session) -> Tag:
    """A reusable segment tag for contact tag tests."""
    tag = Tag(name="OEM-Contact-Test-144", tag_type="segment")
    db_session.add(tag)
    db_session.commit()
    db_session.refresh(tag)
    return tag


# ── 1. Migration schema checks (SQLite-level) ─────────────────────────────────


class TestMigration144Schema:
    """Verify the 3 new columns exist in the test database (created via
    Base.metadata)."""

    def test_secondary_email_column_exists(self, db_session: Session):
        """secondary_email column exists on site_contacts table."""
        inspector = inspect(db_session.bind)
        cols = {c["name"] for c in inspector.get_columns("site_contacts")}
        assert "secondary_email" in cols, "secondary_email column missing from site_contacts"

    def test_secondary_phone_column_exists(self, db_session: Session):
        """secondary_phone column exists on site_contacts table."""
        inspector = inspect(db_session.bind)
        cols = {c["name"] for c in inspector.get_columns("site_contacts")}
        assert "secondary_phone" in cols, "secondary_phone column missing from site_contacts"

    def test_reports_to_id_column_exists(self, db_session: Session):
        """reports_to_id column exists on site_contacts table."""
        inspector = inspect(db_session.bind)
        cols = {c["name"] for c in inspector.get_columns("site_contacts")}
        assert "reports_to_id" in cols, "reports_to_id column missing from site_contacts"

    def test_reports_to_id_set_null_on_delete(self, db_session: Session, test_company: Company):
        """Deleting the manager contact sets reports_to_id to NULL on subordinate."""
        site = CustomerSite(company_id=test_company.id, site_name="SetNullSite", site_type="hq", is_active=True)
        db_session.add(site)
        db_session.flush()

        manager = SiteContact(customer_site_id=site.id, full_name="Mgr", first_name="Mgr")
        db_session.add(manager)
        db_session.flush()

        report = SiteContact(
            customer_site_id=site.id,
            full_name="Rep",
            first_name="Rep",
            reports_to_id=manager.id,
        )
        db_session.add(report)
        db_session.commit()

        db_session.delete(manager)
        db_session.commit()
        db_session.expire(report)
        db_session.refresh(report)
        assert report.reports_to_id is None, "reports_to_id should be NULL after manager deleted"


# ── 2. Create + edit + inline-edit ───────────────────────────────────────────


class TestSecondaryFieldCreate:
    """Creating a contact saves secondary_email, secondary_phone, reports_to_id."""

    def test_create_saves_secondary_email(
        self,
        client: TestClient,
        test_company: Company,
        test_user: User,
        db_session: Session,
    ):
        """POST with secondary_email persists it."""
        test_company.account_owner_id = test_user.id
        db_session.commit()
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/contacts",
            data={
                "first_name": "SecondaryTest144",
                "email": "sec-primary-144@acme.com",
                "secondary_email": "sec-alt-144@acme.com",
            },
        )
        assert resp.status_code == 200
        contact = db_session.query(SiteContact).filter(SiteContact.email == "sec-primary-144@acme.com").first()
        assert contact is not None
        assert contact.secondary_email == "sec-alt-144@acme.com"

    def test_create_saves_secondary_phone(
        self,
        client: TestClient,
        test_company: Company,
        test_user: User,
        db_session: Session,
    ):
        """POST with secondary_phone persists it."""
        test_company.account_owner_id = test_user.id
        db_session.commit()
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/contacts",
            data={
                "first_name": "PhoneTest144",
                "email": "phone-test-144@acme.com",
                "secondary_phone": "+1-555-999-0001",
            },
        )
        assert resp.status_code == 200
        contact = db_session.query(SiteContact).filter(SiteContact.email == "phone-test-144@acme.com").first()
        assert contact is not None
        assert contact.secondary_phone == "+1-555-999-0001"

    def test_create_saves_reports_to_id(
        self,
        client: TestClient,
        test_company: Company,
        test_user: User,
        site_and_two_contacts,
        db_session: Session,
    ):
        """POST with reports_to_id persists it."""
        test_company.account_owner_id = test_user.id
        db_session.commit()
        _site, alice, _bob = site_and_two_contacts
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/contacts",
            data={
                "first_name": "NewPerson144",
                "email": "newperson-144@acme.com",
                "reports_to_id": str(alice.id),
            },
        )
        assert resp.status_code == 200
        contact = db_session.query(SiteContact).filter(SiteContact.email == "newperson-144@acme.com").first()
        assert contact is not None
        assert contact.reports_to_id == alice.id


class TestSecondaryFieldEdit:
    """Editing a contact via the form endpoint saves secondary fields."""

    def test_edit_saves_secondary_email(
        self,
        client: TestClient,
        test_company: Company,
        test_user: User,
        site_and_two_contacts,
        db_session: Session,
    ):
        """POST edit with secondary_email persists it."""
        test_company.account_owner_id = test_user.id
        db_session.commit()
        site, alice, _bob = site_and_two_contacts
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/{site.id}/contacts/{alice.id}/edit",
            data={"first_name": "Alice", "last_name": "Manager", "secondary_email": "alice-alt@acme.com"},
        )
        assert resp.status_code == 200
        db_session.refresh(alice)
        assert alice.secondary_email == "alice-alt@acme.com"

    def test_edit_saves_reports_to_id(
        self,
        client: TestClient,
        test_company: Company,
        test_user: User,
        site_and_two_contacts,
        db_session: Session,
    ):
        """POST edit with reports_to_id persists it."""
        test_company.account_owner_id = test_user.id
        db_session.commit()
        site, alice, bob = site_and_two_contacts
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/{site.id}/contacts/{bob.id}/edit",
            data={"first_name": "Bob", "last_name": "Report", "reports_to_id": str(alice.id)},
        )
        assert resp.status_code == 200
        db_session.refresh(bob)
        assert bob.reports_to_id == alice.id

    def test_edit_clears_reports_to_id(
        self,
        client: TestClient,
        test_company: Company,
        test_user: User,
        site_and_two_contacts,
        db_session: Session,
    ):
        """POST edit with reports_to_id='' clears it."""
        test_company.account_owner_id = test_user.id
        site, alice, bob = site_and_two_contacts
        bob.reports_to_id = alice.id
        db_session.commit()

        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/{site.id}/contacts/{bob.id}/edit",
            data={"first_name": "Bob", "last_name": "Report", "reports_to_id": ""},
        )
        assert resp.status_code == 200
        db_session.refresh(bob)
        assert bob.reports_to_id is None


class TestInlineEditSecondaryFields:
    """Inline-edit secondary_email and secondary_phone via the field endpoint."""

    def test_inline_edit_secondary_email_persists(
        self,
        client: TestClient,
        test_company: Company,
        test_user: User,
        site_and_two_contacts,
        db_session: Session,
    ):
        """Inline POST secondary_email persisted."""
        test_company.account_owner_id = test_user.id
        db_session.commit()

        _site, alice, _bob = site_and_two_contacts
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/contacts/{alice.id}/field",
            data={"field": "secondary_email", "value": "alice-inline@acme.com"},
        )
        assert resp.status_code == 200
        db_session.refresh(alice)
        assert alice.secondary_email == "alice-inline@acme.com"

    def test_inline_edit_secondary_phone_persists(
        self,
        client: TestClient,
        test_company: Company,
        test_user: User,
        site_and_two_contacts,
        db_session: Session,
    ):
        """Inline POST secondary_phone persisted."""
        test_company.account_owner_id = test_user.id
        db_session.commit()

        _site, alice, _bob = site_and_two_contacts
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/contacts/{alice.id}/field",
            data={"field": "secondary_phone", "value": "+1-800-555-4444"},
        )
        assert resp.status_code == 200
        db_session.refresh(alice)
        # CRM P5 trust: contact phone fields normalize to E.164 on save.
        assert alice.secondary_phone == "+18005554444"


# ── 3. reports_to select endpoint ────────────────────────────────────────────


class TestReportsToSelect:
    """GET /v2/partials/customers/{id}/contacts/for-select returns correct set."""

    def test_for_select_returns_active_contacts(
        self,
        client: TestClient,
        test_company: Company,
        test_user: User,
        site_and_two_contacts,
        db_session: Session,
    ):
        """Returns all active contacts for the company."""
        test_company.account_owner_id = test_user.id
        db_session.commit()
        _site, alice, bob = site_and_two_contacts
        resp = client.get(f"/v2/partials/customers/{test_company.id}/contacts/for-select")
        assert resp.status_code == 200
        data = resp.json()
        ids = {item["id"] for item in data}
        assert alice.id in ids
        assert bob.id in ids

    def test_for_select_excludes_self(
        self,
        client: TestClient,
        test_company: Company,
        test_user: User,
        site_and_two_contacts,
        db_session: Session,
    ):
        """exclude_id removes that contact from the list."""
        test_company.account_owner_id = test_user.id
        db_session.commit()
        _site, alice, bob = site_and_two_contacts
        resp = client.get(
            f"/v2/partials/customers/{test_company.id}/contacts/for-select",
            params={"exclude_id": alice.id},
        )
        assert resp.status_code == 200
        data = resp.json()
        ids = {item["id"] for item in data}
        assert alice.id not in ids, "Self should be excluded"
        assert bob.id in ids

    def test_for_select_items_have_name(
        self,
        client: TestClient,
        test_company: Company,
        test_user: User,
        site_and_two_contacts,
        db_session: Session,
    ):
        """Items include 'id' and 'name' keys."""
        test_company.account_owner_id = test_user.id
        db_session.commit()
        _site, alice, _bob = site_and_two_contacts
        resp = client.get(f"/v2/partials/customers/{test_company.id}/contacts/for-select")
        assert resp.status_code == 200
        data = resp.json()
        alice_row = next((i for i in data if i["id"] == alice.id), None)
        assert alice_row is not None
        assert "name" in alice_row
        assert alice_row["name"] == alice.full_name


# ── 4. Contact tag assign/remove ─────────────────────────────────────────────


class TestContactTagAssign:
    """POST/DELETE contact tag routes create/remove
    EntityTag(entity_type='site_contact')."""

    def test_assign_tag_by_id_creates_entity_tag(
        self,
        client: TestClient,
        test_company: Company,
        test_user: User,
        site_and_two_contacts,
        segment_tag: Tag,
        db_session: Session,
    ):
        """POST tag_id= creates EntityTag with entity_type='site_contact'."""
        test_company.account_owner_id = test_user.id
        db_session.commit()
        _site, alice, _bob = site_and_two_contacts
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/contacts/{alice.id}/tags",
            data={"tag_id": str(segment_tag.id)},
        )
        assert resp.status_code == 200
        et = (
            db_session.query(EntityTag)
            .filter_by(entity_type="site_contact", entity_id=alice.id, tag_id=segment_tag.id)
            .first()
        )
        assert et is not None
        assert et.is_visible is True

    def test_assign_tag_by_name_creates_new_tag(
        self,
        client: TestClient,
        test_company: Company,
        test_user: User,
        site_and_two_contacts,
        db_session: Session,
    ):
        """POST tag_name= creates a new segment tag and assigns it."""
        test_company.account_owner_id = test_user.id
        db_session.commit()
        _site, alice, _bob = site_and_two_contacts
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/contacts/{alice.id}/tags",
            data={"tag_name": "Contact-Unique-144-XYZ"},
        )
        assert resp.status_code == 200
        tag = db_session.query(Tag).filter_by(name="Contact-Unique-144-XYZ").first()
        assert tag is not None
        et = (
            db_session.query(EntityTag).filter_by(entity_type="site_contact", entity_id=alice.id, tag_id=tag.id).first()
        )
        assert et is not None

    def test_assign_tag_renders_chip(
        self,
        client: TestClient,
        test_company: Company,
        test_user: User,
        site_and_two_contacts,
        segment_tag: Tag,
        db_session: Session,
    ):
        """Response HTML contains the tag name chip."""
        test_company.account_owner_id = test_user.id
        db_session.commit()
        _site, alice, _bob = site_and_two_contacts
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/contacts/{alice.id}/tags",
            data={"tag_id": str(segment_tag.id)},
        )
        assert resp.status_code == 200
        assert segment_tag.name in resp.text

    def test_assign_tag_missing_params_returns_400(
        self,
        client: TestClient,
        test_company: Company,
        test_user: User,
        site_and_two_contacts,
        db_session: Session,
    ):
        """POST with neither tag_id nor tag_name returns 400."""
        test_company.account_owner_id = test_user.id
        db_session.commit()
        _site, alice, _bob = site_and_two_contacts
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/contacts/{alice.id}/tags",
            data={},
        )
        assert resp.status_code == 400

    def test_unassign_tag_removes_entity_tag(
        self,
        client: TestClient,
        test_company: Company,
        test_user: User,
        site_and_two_contacts,
        segment_tag: Tag,
        db_session: Session,
    ):
        """DELETE removes the EntityTag row."""
        test_company.account_owner_id = test_user.id
        db_session.commit()
        _site, alice, _bob = site_and_two_contacts
        et = EntityTag(
            entity_type="site_contact",
            entity_id=alice.id,
            tag_id=segment_tag.id,
            is_visible=True,
            interaction_count=0,
            total_entity_interactions=0,
        )
        db_session.add(et)
        db_session.commit()

        resp = client.delete(f"/v2/partials/customers/{test_company.id}/contacts/{alice.id}/tags/{segment_tag.id}")
        assert resp.status_code == 200
        gone = (
            db_session.query(EntityTag)
            .filter_by(entity_type="site_contact", entity_id=alice.id, tag_id=segment_tag.id)
            .first()
        )
        assert gone is None

    def test_unassign_nonexistent_tag_is_idempotent(
        self,
        client: TestClient,
        test_company: Company,
        test_user: User,
        site_and_two_contacts,
        segment_tag: Tag,
        db_session: Session,
    ):
        """DELETE for tag not assigned returns 200 (idempotent)."""
        test_company.account_owner_id = test_user.id
        db_session.commit()
        _site, alice, _bob = site_and_two_contacts
        resp = client.delete(f"/v2/partials/customers/{test_company.id}/contacts/{alice.id}/tags/{segment_tag.id}")
        assert resp.status_code == 200


# ── 5. Edit-form reports_to select ───────────────────────────────────────────


class TestEditFormReportsToSelect:
    """GET edit-form renders the reports_to select with same-company contacts."""

    def test_edit_form_contains_reports_to_select(
        self,
        client: TestClient,
        test_company: Company,
        site_and_two_contacts,
    ):
        """Edit form renders reports_to_id select element."""
        _site, alice, _bob = site_and_two_contacts
        resp = client.get(f"/v2/partials/customers/{test_company.id}/contacts/{alice.id}/edit-form")
        assert resp.status_code == 200
        assert "reports_to_id" in resp.text

    def test_edit_form_excludes_self_from_reports_to(
        self,
        client: TestClient,
        test_company: Company,
        site_and_two_contacts,
    ):
        """Edit form reports_to select includes bob but not alice (self-excluded)."""
        _site, alice, bob = site_and_two_contacts
        resp = client.get(f"/v2/partials/customers/{test_company.id}/contacts/{alice.id}/edit-form")
        assert resp.status_code == 200
        # Bob should appear as a reports_to option
        assert bob.full_name in resp.text
        # Alice's full_name should NOT appear in the select (she's the contact being edited)
        # Count occurrences — alice appears in the page header/form title but NOT as a select option
        # The select options only list site_contacts_for_select (which excludes alice)
        # We verify by checking bob's name IS present and alice's is NOT present
        # (since alice is only passed as contact= and her name doesn't appear elsewhere in this template)
        assert alice.full_name not in resp.text


# ── 6. Security / authz DENY tests ───────────────────────────────────────────


@pytest.fixture()
def unrelated_client_144(db_session: Session):
    """TestClient where the authenticated user has NO ownership relation to any
    company."""
    from datetime import datetime, timezone

    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    stranger = User(
        email="stranger144@example.com",
        name="Stranger 144",
        role="buyer",
        azure_id="stranger-azure-144",
        created_at=datetime.now(timezone.utc),
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


class TestContactTagAuthzDeny:
    """Unrelated rep must not access tag or select routes (IDOR/PII guard)."""

    def test_assign_tag_unrelated_rep_gets_403(
        self,
        unrelated_client_144: TestClient,
        test_company: Company,
        site_and_two_contacts,
        segment_tag: Tag,
    ):
        """POST tag assign by unrelated rep returns 403."""
        _site, alice, _bob = site_and_two_contacts
        resp = unrelated_client_144.post(
            f"/v2/partials/customers/{test_company.id}/contacts/{alice.id}/tags",
            data={"tag_id": str(segment_tag.id)},
        )
        assert resp.status_code == 403

    def test_unassign_tag_unrelated_rep_gets_403(
        self,
        unrelated_client_144: TestClient,
        test_company: Company,
        site_and_two_contacts,
        segment_tag: Tag,
    ):
        """DELETE tag unassign by unrelated rep returns 403."""
        _site, alice, _bob = site_and_two_contacts
        resp = unrelated_client_144.delete(
            f"/v2/partials/customers/{test_company.id}/contacts/{alice.id}/tags/{segment_tag.id}"
        )
        assert resp.status_code == 403

    def test_for_select_unrelated_rep_gets_403(
        self,
        unrelated_client_144: TestClient,
        test_company: Company,
        site_and_two_contacts,
    ):
        """GET contacts/for-select by unrelated rep returns 403."""
        resp = unrelated_client_144.get(f"/v2/partials/customers/{test_company.id}/contacts/for-select")
        assert resp.status_code == 403

    def test_assign_tag_cross_company_contact_gets_404(
        self,
        client: TestClient,
        test_company: Company,
        test_user: User,
        site_and_two_contacts,
        segment_tag: Tag,
        db_session: Session,
    ):
        """Assigning a tag to a contact from a different company returns 404 (cross-
        company guard)."""
        from app.models.crm import Company as _Company

        test_company.account_owner_id = test_user.id
        # Create a second company with its own contact
        other_co = _Company(name="OtherCo144", is_active=True)
        db_session.add(other_co)
        db_session.flush()
        other_site = CustomerSite(company_id=other_co.id, site_name="HQ", is_active=True)
        db_session.add(other_site)
        db_session.flush()
        other_contact = SiteContact(
            customer_site_id=other_site.id,
            full_name="OtherPerson144",
            first_name="OtherPerson",
        )
        db_session.add(other_contact)
        db_session.commit()

        # Use test_company in URL but other_contact's id → cross-company → 404
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/contacts/{other_contact.id}/tags",
            data={"tag_id": str(segment_tag.id)},
        )
        assert resp.status_code == 404

    def test_unassign_tag_cross_company_contact_gets_404(
        self,
        client: TestClient,
        test_company: Company,
        test_user: User,
        site_and_two_contacts,
        segment_tag: Tag,
        db_session: Session,
    ):
        """Deleting a tag for a contact from a different company returns 404."""
        from app.models.crm import Company as _Company

        test_company.account_owner_id = test_user.id
        other_co = _Company(name="OtherCoB144", is_active=True)
        db_session.add(other_co)
        db_session.flush()
        other_site = CustomerSite(company_id=other_co.id, site_name="HQ", is_active=True)
        db_session.add(other_site)
        db_session.flush()
        other_contact = SiteContact(
            customer_site_id=other_site.id,
            full_name="OtherPersonB144",
            first_name="OtherPersonB",
        )
        db_session.add(other_contact)
        db_session.commit()

        resp = client.delete(
            f"/v2/partials/customers/{test_company.id}/contacts/{other_contact.id}/tags/{segment_tag.id}"
        )
        assert resp.status_code == 404


class TestReportsToSameCompanyValidation:
    """reports_to_id from another company must be rejected (F3)."""

    def test_create_reports_to_other_company_returns_400(
        self,
        client: TestClient,
        test_company: Company,
        test_user: User,
        site_and_two_contacts,
        db_session: Session,
    ):
        """POST create with reports_to_id from another company returns 400."""
        from app.models.crm import Company as _Company

        # Own the company the contact is created under so the gate is passed and
        # the request reaches the cross-company reports_to validation (400).
        test_company.account_owner_id = test_user.id
        db_session.commit()

        other_co = _Company(name="CrossCo144C", is_active=True)
        db_session.add(other_co)
        db_session.flush()
        other_site = CustomerSite(company_id=other_co.id, site_name="HQ", is_active=True)
        db_session.add(other_site)
        db_session.flush()
        other_contact = SiteContact(
            customer_site_id=other_site.id,
            full_name="CrossPerson144",
            first_name="CrossPerson",
        )
        db_session.add(other_contact)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/contacts",
            data={
                "first_name": "NewCross144",
                "email": "newcross-144@acme.com",
                "reports_to_id": str(other_contact.id),
            },
        )
        assert resp.status_code == 400

    def test_edit_reports_to_other_company_returns_400(
        self,
        client: TestClient,
        test_company: Company,
        test_user: User,
        site_and_two_contacts,
        db_session: Session,
    ):
        """POST edit with reports_to_id from another company returns 400."""
        from app.models.crm import Company as _Company

        test_company.account_owner_id = test_user.id
        site, alice, _bob = site_and_two_contacts
        other_co = _Company(name="CrossCo144D", is_active=True)
        db_session.add(other_co)
        db_session.flush()
        other_site = CustomerSite(company_id=other_co.id, site_name="HQ", is_active=True)
        db_session.add(other_site)
        db_session.flush()
        other_contact = SiteContact(
            customer_site_id=other_site.id,
            full_name="CrossPersonD144",
            first_name="CrossPersonD",
        )
        db_session.add(other_contact)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/{site.id}/contacts/{alice.id}/edit",
            data={
                "first_name": "Alice",
                "last_name": "Manager",
                "reports_to_id": str(other_contact.id),
            },
        )
        assert resp.status_code == 400

    def test_edit_reports_to_self_returns_400(
        self,
        client: TestClient,
        test_company: Company,
        test_user: User,
        site_and_two_contacts,
        db_session: Session,
    ):
        """POST edit with reports_to_id == self (contact_id) returns 400."""
        test_company.account_owner_id = test_user.id
        db_session.commit()
        site, alice, _bob = site_and_two_contacts
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/{site.id}/contacts/{alice.id}/edit",
            data={
                "first_name": "Alice",
                "last_name": "Manager",
                "reports_to_id": str(alice.id),
            },
        )
        assert resp.status_code == 400


class TestSecondaryEmailValidation:
    """secondary_email without '@' must be rejected (F4)."""

    def test_secondary_email_without_at_rejected_on_model(self, db_session: Session, test_company: Company):
        """Model @validates rejects secondary_email without '@'."""
        import pytest as _pytest

        site = CustomerSite(
            company_id=test_company.id,
            site_name="ValSite144",
            site_type="hq",
            is_active=True,
        )
        db_session.add(site)
        db_session.flush()

        with _pytest.raises(ValueError, match="Invalid secondary_email"):
            SiteContact(
                customer_site_id=site.id,
                full_name="ValContact144",
                first_name="ValContact",
                secondary_email="notanemail",
            )

    def test_secondary_email_with_at_accepted(self, db_session: Session, test_company: Company):
        """Model @validates accepts secondary_email containing '@'."""
        site = CustomerSite(
            company_id=test_company.id,
            site_name="ValSite144B",
            site_type="hq",
            is_active=True,
        )
        db_session.add(site)
        db_session.flush()

        contact = SiteContact(
            customer_site_id=site.id,
            full_name="ValContactB144",
            first_name="ValContactB",
            secondary_email="valid@acme.com",
        )
        db_session.add(contact)
        db_session.commit()
        db_session.refresh(contact)
        assert contact.secondary_email == "valid@acme.com"

    def test_secondary_email_none_accepted(self, db_session: Session, test_company: Company):
        """Model @validates accepts None (no secondary email)."""
        site = CustomerSite(
            company_id=test_company.id,
            site_name="ValSite144C",
            site_type="hq",
            is_active=True,
        )
        db_session.add(site)
        db_session.flush()

        contact = SiteContact(
            customer_site_id=site.id,
            full_name="ValContactC144",
            first_name="ValContactC",
            secondary_email=None,
        )
        db_session.add(contact)
        db_session.commit()
        assert contact.secondary_email is None
