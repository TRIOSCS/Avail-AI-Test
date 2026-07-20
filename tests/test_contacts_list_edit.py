"""tests/test_contacts_list_edit.py — Edit-from-the-global-contacts-list flow.

Covers the origin=contacts mode added to the shared contact edit modal:
  - GET edit-form?origin=contacts renders hidden origin + filter_* inputs and
    targets #main-content (so the save swaps the global list, not the tab).
  - POST .../edit with origin=contacts persists the change and re-renders the
    global contacts list scoped to the carried filter_* values.
  - POST .../edit without origin still renders the Contacts-tab grouped list
    (regression guard for the company-tab path).
  - contacts_list.html readability affordances: per-row Edit button, mailto/tel
    links, priority/DNC/Archived flags, site line under company.

Depends on: conftest client/test_user/test_company fixtures;
app/routers/htmx/companies/contacts.py; customers/contacts_list.html;
customers/tabs/_contact_form.html.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.models import Company, CustomerSite, SiteContact, User


@pytest.fixture()
def owned_contact(db_session: Session, test_company: Company, test_user: User) -> SiteContact:
    """A site + contact under test_company, owned by test_user (can_manage passes)."""
    test_company.account_owner_id = test_user.id
    site = CustomerSite(company_id=test_company.id, site_name="HQ", site_type="hq", is_active=True)
    db_session.add(site)
    db_session.flush()
    contact = SiteContact(
        customer_site_id=site.id,
        full_name="Jane Doe",
        first_name="Jane",
        last_name="Doe",
        email="jane@acme-electronics.com",
        phone="+1-555-0101",
        title="Buyer",
        is_priority=True,
        do_not_contact=True,
        created_at=datetime.now(UTC),
    )
    db_session.add(contact)
    db_session.commit()
    db_session.refresh(contact)
    return contact


class TestEditFormOriginContacts:
    def test_edit_form_carries_origin_and_filters(self, client, test_company, owned_contact):
        resp = client.get(
            f"/v2/partials/customers/{test_company.id}/contacts/{owned_contact.id}/edit-form"
            "?origin=contacts&filter_search=jane&filter_company_id=0"
            "&filter_contact_role=&filter_cadence_state=overdue&filter_limit=50&filter_offset=0"
        )
        assert resp.status_code == 200
        body = resp.text
        assert "name='origin' value='contacts'" in body
        assert "name='filter_search' value='jane'" in body
        assert "name='filter_cadence_state' value='overdue'" in body
        assert "#main-content" in body
        assert "#contacts-tab-list" not in body

    def test_edit_form_without_origin_targets_tab_list(self, client, test_company, owned_contact):
        resp = client.get(f"/v2/partials/customers/{test_company.id}/contacts/{owned_contact.id}/edit-form")
        assert resp.status_code == 200
        assert "#contacts-tab-list" in resp.text
        assert "name='origin'" not in resp.text

    def test_edit_form_unknown_origin_ignored(self, client, test_company, owned_contact):
        resp = client.get(f"/v2/partials/customers/{test_company.id}/contacts/{owned_contact.id}/edit-form?origin=evil")
        assert resp.status_code == 200
        assert "name='origin'" not in resp.text
        assert "#contacts-tab-list" in resp.text


class TestEditSaveOriginContacts:
    def test_save_persists_and_rerenders_global_list(self, client, db_session, test_company, owned_contact, test_user):
        site_id = owned_contact.customer_site_id
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/{site_id}/contacts/{owned_contact.id}/edit",
            data={
                "origin": "contacts",
                "first_name": "Janet",
                "last_name": "Doe",
                "title": "Senior Buyer",
                "filter_search": "",
                "filter_company_id": "0",
                "filter_contact_role": "",
                "filter_cadence_state": "",
                "filter_limit": "50",
                "filter_offset": "0",
            },
        )
        assert resp.status_code == 200
        db_session.refresh(owned_contact)
        assert owned_contact.first_name == "Janet"
        assert owned_contact.full_name == "Janet Doe"
        # Global list markers — heading + the edited contact rendered as a row.
        assert "Customer Contacts" in resp.text
        assert "Janet Doe" in resp.text
        assert "showToast" in resp.headers.get("HX-Trigger", "")

    def test_save_honors_carried_filters(self, client, db_session, test_company, owned_contact):
        # A second contact that the carried search filter must exclude from the re-render.
        other = SiteContact(
            customer_site_id=owned_contact.customer_site_id,
            full_name="Zed Other",
            email="zed@acme-electronics.com",
        )
        db_session.add(other)
        db_session.commit()
        site_id = owned_contact.customer_site_id
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/{site_id}/contacts/{owned_contact.id}/edit",
            data={
                "origin": "contacts",
                "first_name": "Jane",
                "last_name": "Doe",
                "filter_search": "Jane",
                "filter_limit": "50",
                "filter_offset": "0",
            },
        )
        assert resp.status_code == 200
        assert "Jane Doe" in resp.text
        assert "Zed Other" not in resp.text

    def test_save_without_origin_renders_tab_list(self, client, db_session, test_company, owned_contact):
        site_id = owned_contact.customer_site_id
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/{site_id}/contacts/{owned_contact.id}/edit",
            data={"first_name": "Jane", "last_name": "Doe"},
        )
        assert resp.status_code == 200
        # Grouped Contacts-tab list, not the global workspace.
        assert "Customer Contacts" not in resp.text


class TestContactsListReadability:
    def test_row_renders_edit_button_and_links(self, client, test_company, owned_contact):
        resp = client.get("/v2/partials/contacts")
        assert resp.status_code == 200
        body = resp.text
        assert f"/v2/partials/customers/{test_company.id}/contacts/{owned_contact.id}/edit-form?origin=contacts" in body
        assert "mailto:jane@acme-electronics.com" in body
        assert "tel:+1-555-0101" in body
        assert "Edit Jane Doe" in body

    def test_row_renders_flags_and_site(self, client, owned_contact):
        resp = client.get("/v2/partials/contacts")
        assert resp.status_code == 200
        body = resp.text
        assert "DNC" in body
        assert "Priority contact" in body
        assert "HQ" in body


@pytest.fixture()
def legacy_contact(db_session: Session, test_company: Company, test_user: User) -> SiteContact:
    """A legacy contact: name lives only in full_name (first/last NULL), all else NULL."""
    test_company.account_owner_id = test_user.id
    site = CustomerSite(company_id=test_company.id, site_name="Legacy Plant", site_type="plant", is_active=True)
    db_session.add(site)
    db_session.flush()
    contact = SiteContact(
        customer_site_id=site.id,
        full_name="David Tuckman",
        created_at=datetime.now(UTC),
    )
    db_session.add(contact)
    db_session.commit()
    db_session.refresh(contact)
    return contact


@pytest.fixture()
def unrelated_client(db_session: Session):
    """TestClient authenticated as a buyer who owns NO companies/sites."""
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    stranger = User(
        email="stranger_contacts_edit@example.com",
        name="Stranger",
        role="buyer",
        azure_id="stranger-azure-contacts-edit",
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


class TestEditFormAuthz:
    """The edit-form GET must gate on can_manage_account like its save-path peer."""

    def test_unrelated_rep_gets_404(self, unrelated_client, test_company, owned_contact):
        resp = unrelated_client.get(f"/v2/partials/customers/{test_company.id}/contacts/{owned_contact.id}/edit-form")
        assert resp.status_code == 404

    def test_owner_still_gets_200(self, client, test_company, owned_contact):
        resp = client.get(f"/v2/partials/customers/{test_company.id}/contacts/{owned_contact.id}/edit-form")
        assert resp.status_code == 200


class TestNullFieldPrefill:
    """NULL columns must prefill as '' — never the literal string 'None'."""

    def test_null_fields_never_render_none(self, client, test_company, legacy_contact):
        resp = client.get(f"/v2/partials/customers/{test_company.id}/contacts/{legacy_contact.id}/edit-form")
        assert resp.status_code == 200
        assert "value='None'" not in resp.text
        assert ">None</textarea>" not in resp.text

    def test_legacy_full_name_prefills_name_fields(self, client, test_company, legacy_contact):
        resp = client.get(f"/v2/partials/customers/{test_company.id}/contacts/{legacy_contact.id}/edit-form")
        assert "value='David'" in resp.text
        assert "value='Tuckman'" in resp.text

    def test_untouched_save_round_trips_legacy_name(self, client, db_session, test_company, legacy_contact):
        """Submitting the form exactly as prefilled must not corrupt the name or 400."""
        site_id = legacy_contact.customer_site_id
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/{site_id}/contacts/{legacy_contact.id}/edit",
            data={
                "first_name": "David",
                "last_name": "Tuckman",
                "title": "",
                "email": "",
                "phone": "",
                "notes": "",
            },
        )
        assert resp.status_code == 200
        db_session.expire_all()
        refreshed = db_session.get(SiteContact, legacy_contact.id)
        assert refreshed.full_name == "David Tuckman"
        assert refreshed.first_name == "David"
        assert refreshed.last_name == "Tuckman"
        assert refreshed.email is None

    def test_single_token_legacy_name_prefills_first_only(self, client, db_session, test_company, test_user):
        test_company.account_owner_id = test_user.id
        site = CustomerSite(company_id=test_company.id, site_name="S2", site_type="office", is_active=True)
        db_session.add(site)
        db_session.flush()
        contact = SiteContact(customer_site_id=site.id, full_name="Cher", created_at=datetime.now(UTC))
        db_session.add(contact)
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{test_company.id}/contacts/{contact.id}/edit-form")
        assert resp.status_code == 200
        assert "value='Cher'" in resp.text
        assert "value='None'" not in resp.text
