"""Tests for HTMX vendor contacts CRUD tab (Phase 2D).

Covers vendor contacts tab rendering with add/edit/delete forms and log-call buttons.

Called by: pytest
Depends on: conftest (client, db_session, test_user fixtures), app.models
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import VendorCard
from app.models.vendors import VendorContact


@pytest.fixture()
def vendor_with_contacts(db_session: Session):
    """Create a vendor card with contacts for testing."""
    vendor = VendorCard(
        normalized_name="test_vendor",
        display_name="Test Vendor Inc",
        domain="testvendor.com",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(vendor)
    db_session.flush()

    c1 = VendorContact(
        vendor_card_id=vendor.id,
        full_name="John Doe",
        title="Sales Manager",
        email="john@testvendor.com",
        phone="+15551234567",
        interaction_count=5,
        source="manual",
    )
    c2 = VendorContact(
        vendor_card_id=vendor.id,
        full_name="Jane Smith",
        title="VP Sales",
        email="jane@testvendor.com",
        interaction_count=12,
        source="website_scrape",
    )
    db_session.add_all([c1, c2])
    db_session.commit()

    return {"vendor": vendor, "contacts": [c1, c2]}


def test_contacts_tab_renders_contacts(client, vendor_with_contacts):
    """Contacts tab shows contact rows with CRUD buttons."""
    vendor = vendor_with_contacts["vendor"]
    resp = client.get(f"/partials/vendors/{vendor.id}/tab/contacts")
    assert resp.status_code == 200
    html = resp.text
    assert "John Doe" in html
    assert "Jane Smith" in html
    assert "john@testvendor.com" in html
    assert "Sales Manager" in html


def test_contacts_tab_has_add_form(client, vendor_with_contacts):
    """Contacts tab includes an add contact form."""
    vendor = vendor_with_contacts["vendor"]
    resp = client.get(f"/partials/vendors/{vendor.id}/tab/contacts")
    html = resp.text
    assert "Add Contact" in html
    assert f'hx-post="/api/vendors/{vendor.id}/contacts"' in html
    assert 'name="email"' in html
    assert 'name="full_name"' in html


def test_contacts_tab_has_edit_buttons(client, vendor_with_contacts):
    """Contacts tab rows have Edit and Delete buttons."""
    vendor = vendor_with_contacts["vendor"]
    resp = client.get(f"/partials/vendors/{vendor.id}/tab/contacts")
    html = resp.text
    assert "Edit" in html
    assert "Delete" in html
    assert "Log Call" in html
    assert "x-data" in html
    assert "editing" in html


def test_contacts_tab_has_inline_edit_form(client, vendor_with_contacts):
    """Contacts tab rows have inline edit form with hx-put."""
    vendor = vendor_with_contacts["vendor"]
    c = vendor_with_contacts["contacts"][0]
    resp = client.get(f"/partials/vendors/{vendor.id}/tab/contacts")
    html = resp.text
    assert f'hx-put="/api/vendors/{vendor.id}/contacts/{c.id}"' in html


def test_contacts_tab_has_delete_confirm(client, vendor_with_contacts):
    """Delete button has hx-confirm for safety."""
    vendor = vendor_with_contacts["vendor"]
    resp = client.get(f"/partials/vendors/{vendor.id}/tab/contacts")
    html = resp.text
    assert 'hx-confirm="Delete this contact?"' in html


def test_contacts_tab_has_log_call(client, vendor_with_contacts):
    """Each contact has a log call button."""
    vendor = vendor_with_contacts["vendor"]
    c = vendor_with_contacts["contacts"][0]
    resp = client.get(f"/partials/vendors/{vendor.id}/tab/contacts")
    html = resp.text
    assert f"/api/vendors/{vendor.id}/contacts/{c.id}/log-call" in html


def test_contacts_tab_empty_state(client, db_session):
    """Empty contacts tab shows helpful message."""
    vendor = VendorCard(
        normalized_name="empty_vendor",
        display_name="Empty Vendor",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(vendor)
    db_session.commit()

    resp = client.get(f"/partials/vendors/{vendor.id}/tab/contacts")
    assert resp.status_code == 200
    assert "No contacts found" in resp.text


def test_contacts_tab_shows_interaction_count(client, vendor_with_contacts):
    """Contact rows show interaction count."""
    vendor = vendor_with_contacts["vendor"]
    resp = client.get(f"/partials/vendors/{vendor.id}/tab/contacts")
    html = resp.text
    # Jane has 12 interactions
    assert "12" in html
