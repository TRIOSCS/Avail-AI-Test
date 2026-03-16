"""Tests for HTMX vendor contact CRUD endpoints (add-form, create, edit, update, delete).

Covers the 5 new endpoints on /v2/partials/vendors/{vendor_id}/contacts/*.

Called by: pytest
Depends on: conftest (client, db_session, test_user fixtures), app.models
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import VendorCard
from app.models.vendors import VendorContact


@pytest.fixture()
def vendor_for_contacts(db_session: Session):
    """Create a vendor card with one existing contact for CRUD testing."""
    vendor = VendorCard(
        normalized_name="crud_vendor",
        display_name="CRUD Vendor Inc",
        domain="crudvendor.com",
        emails=["existing@crudvendor.com"],
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(vendor)
    db_session.flush()

    contact = VendorContact(
        vendor_card_id=vendor.id,
        full_name="Existing Contact",
        email="existing@crudvendor.com",
        phone="+15559999999",
        title="Director",
        label="Sales",
        source="manual",
        confidence=100,
        is_verified=True,
    )
    db_session.add(contact)
    db_session.commit()

    return {"vendor": vendor, "contact": contact}


# ── Add form ─────────────────────────────────────────────────────────


def test_add_form_returns_html(client, vendor_for_contacts):
    """GET add-form returns an HTML form with required fields."""
    vendor = vendor_for_contacts["vendor"]
    resp = client.get(f"/v2/partials/vendors/{vendor.id}/contacts/add-form")
    assert resp.status_code == 200
    html = resp.text
    assert "Add Contact" in html
    assert 'name="full_name"' in html
    assert 'name="email"' in html
    assert 'name="phone"' in html
    assert 'name="title"' in html
    assert 'name="label"' in html
    assert f'hx-post="/v2/partials/vendors/{vendor.id}/contacts"' in html


def test_add_form_vendor_not_found(client):
    """GET add-form for non-existent vendor returns 404."""
    resp = client.get("/v2/partials/vendors/99999/contacts/add-form")
    assert resp.status_code == 404


# ── Create ───────────────────────────────────────────────────────────


def test_create_contact_success(client, vendor_for_contacts, db_session):
    """POST creates a new contact with source=manual and confidence=100."""
    vendor = vendor_for_contacts["vendor"]
    resp = client.post(
        f"/v2/partials/vendors/{vendor.id}/contacts",
        data={
            "full_name": "New Person",
            "email": "new@crudvendor.com",
            "phone": "+15550001111",
            "title": "Engineer",
            "label": "Technical",
        },
    )
    assert resp.status_code == 200
    assert "New Person" in resp.text
    assert "added successfully" in resp.text
    assert resp.headers.get("HX-Trigger") == "refreshContacts"

    created = (
        db_session.query(VendorContact)
        .filter(VendorContact.email == "new@crudvendor.com")
        .first()
    )
    assert created is not None
    assert created.source == "manual"
    assert created.confidence == 100
    assert created.is_verified is True


def test_create_contact_syncs_email_to_vendor(client, vendor_for_contacts, db_session):
    """Creating a contact syncs the email into vendor.emails."""
    vendor = vendor_for_contacts["vendor"]
    client.post(
        f"/v2/partials/vendors/{vendor.id}/contacts",
        data={"full_name": "Sync Test", "email": "synced@crudvendor.com"},
    )
    db_session.refresh(vendor)
    assert "synced@crudvendor.com" in vendor.emails


def test_create_contact_email_dedup(client, vendor_for_contacts):
    """Creating a contact with a duplicate email returns 409."""
    vendor = vendor_for_contacts["vendor"]
    resp = client.post(
        f"/v2/partials/vendors/{vendor.id}/contacts",
        data={"full_name": "Duplicate", "email": "existing@crudvendor.com"},
    )
    assert resp.status_code == 409
    assert "already exists" in resp.text


def test_create_contact_missing_name(client, vendor_for_contacts):
    """Creating a contact without full_name returns 422."""
    vendor = vendor_for_contacts["vendor"]
    resp = client.post(
        f"/v2/partials/vendors/{vendor.id}/contacts",
        data={"full_name": "", "email": "noname@test.com"},
    )
    assert resp.status_code == 422


def test_create_contact_vendor_not_found(client):
    """POST to non-existent vendor returns 404."""
    resp = client.post(
        "/v2/partials/vendors/99999/contacts",
        data={"full_name": "Ghost"},
    )
    assert resp.status_code == 404


# ── Edit form ────────────────────────────────────────────────────────


def test_edit_form_prefilled(client, vendor_for_contacts):
    """GET edit form is pre-filled with current contact values."""
    vendor = vendor_for_contacts["vendor"]
    contact = vendor_for_contacts["contact"]
    resp = client.get(
        f"/v2/partials/vendors/{vendor.id}/contacts/{contact.id}/edit"
    )
    assert resp.status_code == 200
    html = resp.text
    assert "Edit Contact" in html
    assert "Existing Contact" in html
    assert "existing@crudvendor.com" in html
    assert "Director" in html
    assert f'hx-put="/v2/partials/vendors/{vendor.id}/contacts/{contact.id}"' in html


def test_edit_form_contact_not_found(client, vendor_for_contacts):
    """GET edit form for non-existent contact returns 404."""
    vendor = vendor_for_contacts["vendor"]
    resp = client.get(
        f"/v2/partials/vendors/{vendor.id}/contacts/99999/edit"
    )
    assert resp.status_code == 404


# ── Update ───────────────────────────────────────────────────────────


def test_update_contact_success(client, vendor_for_contacts, db_session):
    """PUT updates contact fields and returns updated row HTML."""
    vendor = vendor_for_contacts["vendor"]
    contact = vendor_for_contacts["contact"]
    resp = client.put(
        f"/v2/partials/vendors/{vendor.id}/contacts/{contact.id}",
        data={
            "full_name": "Updated Name",
            "email": "updated@crudvendor.com",
            "phone": "+15550002222",
            "title": "VP Sales",
            "label": "Executive",
        },
    )
    assert resp.status_code == 200
    assert "Updated Name" in resp.text

    db_session.refresh(contact)
    assert contact.full_name == "Updated Name"
    assert contact.email == "updated@crudvendor.com"


def test_update_contact_email_dedup(client, vendor_for_contacts, db_session):
    """PUT with a duplicate email returns 409."""
    vendor = vendor_for_contacts["vendor"]
    contact = vendor_for_contacts["contact"]

    other = VendorContact(
        vendor_card_id=vendor.id,
        full_name="Other",
        email="other@crudvendor.com",
        source="manual",
    )
    db_session.add(other)
    db_session.commit()

    resp = client.put(
        f"/v2/partials/vendors/{vendor.id}/contacts/{contact.id}",
        data={"full_name": "Existing Contact", "email": "other@crudvendor.com"},
    )
    assert resp.status_code == 409


def test_update_contact_syncs_email_to_vendor(client, vendor_for_contacts, db_session):
    """Updating email syncs old removal and new addition in vendor.emails."""
    vendor = vendor_for_contacts["vendor"]
    contact = vendor_for_contacts["contact"]
    client.put(
        f"/v2/partials/vendors/{vendor.id}/contacts/{contact.id}",
        data={"full_name": "Existing Contact", "email": "newemail@crudvendor.com"},
    )
    db_session.refresh(vendor)
    assert "newemail@crudvendor.com" in vendor.emails
    assert "existing@crudvendor.com" not in vendor.emails


def test_update_contact_not_found(client, vendor_for_contacts):
    """PUT to non-existent contact returns 404."""
    vendor = vendor_for_contacts["vendor"]
    resp = client.put(
        f"/v2/partials/vendors/{vendor.id}/contacts/99999",
        data={"full_name": "Nobody"},
    )
    assert resp.status_code == 404


# ── Delete ───────────────────────────────────────────────────────────


def test_delete_contact_success(client, vendor_for_contacts, db_session):
    """DELETE removes contact and returns empty response with HX-Trigger."""
    vendor = vendor_for_contacts["vendor"]
    contact = vendor_for_contacts["contact"]
    resp = client.delete(
        f"/v2/partials/vendors/{vendor.id}/contacts/{contact.id}"
    )
    assert resp.status_code == 200
    assert resp.headers.get("HX-Trigger") == "refreshContacts"

    deleted = db_session.query(VendorContact).filter(VendorContact.id == contact.id).first()
    assert deleted is None


def test_delete_contact_removes_email_from_vendor(client, vendor_for_contacts, db_session):
    """Deleting a contact also removes the email from vendor.emails."""
    vendor = vendor_for_contacts["vendor"]
    contact = vendor_for_contacts["contact"]
    client.delete(f"/v2/partials/vendors/{vendor.id}/contacts/{contact.id}")
    db_session.refresh(vendor)
    assert "existing@crudvendor.com" not in (vendor.emails or [])


def test_delete_contact_not_found(client, vendor_for_contacts):
    """DELETE non-existent contact returns 404."""
    vendor = vendor_for_contacts["vendor"]
    resp = client.delete(
        f"/v2/partials/vendors/{vendor.id}/contacts/99999"
    )
    assert resp.status_code == 404
