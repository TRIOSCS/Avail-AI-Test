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
