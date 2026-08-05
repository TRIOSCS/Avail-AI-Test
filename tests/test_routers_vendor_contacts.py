"""tests/test_routers_vendor_contacts.py — Tests for routers/vendor_contacts.py.

Covers: vendor contacts CRUD, bulk contacts, log-call.

Called by: pytest
Depends on: routers/vendor_contacts.py, utils/vendor_helpers.py
"""

import json

from app.models import VendorContact

# ── Contacts CRUD ────────────────────────────────────────────────────────


def test_list_vendor_contacts(client, db_session, test_vendor_card, test_vendor_contact):
    """GET /api/vendors/{id}/contacts returns the contacts list."""
    resp = client.get(f"/api/vendors/{test_vendor_card.id}/contacts")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    emails = [c["email"] for c in data]
    assert "john@arrow.com" in emails


def test_add_vendor_contact(client, db_session, test_vendor_card):
    """POST /api/vendors/{id}/contacts with email+name succeeds."""
    resp = client.post(
        f"/api/vendors/{test_vendor_card.id}/contacts",
        json={"email": "jane@arrow.com", "full_name": "Jane Buyer"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["duplicate"] is False
    assert "id" in data


def test_add_vendor_contact_duplicate(client, db_session, test_vendor_card, test_vendor_contact):
    """POST same email twice returns duplicate=True."""
    resp = client.post(
        f"/api/vendors/{test_vendor_card.id}/contacts",
        json={"email": "john@arrow.com", "full_name": "John Sales"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["duplicate"] is True


def test_add_vendor_contact_not_found(client):
    """POST /api/vendors/99999/contacts returns 404."""
    resp = client.post(
        "/api/vendors/99999/contacts",
        json={"email": "x@y.com", "full_name": "Nobody"},
    )
    assert resp.status_code == 404


def test_update_vendor_contact(client, db_session, test_vendor_card, test_vendor_contact):
    """PUT /api/vendors/{card_id}/contacts/{contact_id} updates the contact."""
    resp = client.put(
        f"/api/vendors/{test_vendor_card.id}/contacts/{test_vendor_contact.id}",
        json={"full_name": "John Updated", "title": "VP Sales"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True


def test_update_vendor_contact_email_conflict(client, db_session, test_vendor_card, test_vendor_contact):
    """PUT with email that conflicts with another contact returns 409."""
    vc2 = VendorContact(
        vendor_card_id=test_vendor_card.id,
        full_name="Other Person",
        email="other@arrow.com",
        source="manual",
        is_verified=True,
        confidence=80,
    )
    db_session.add(vc2)
    db_session.commit()

    resp = client.put(
        f"/api/vendors/{test_vendor_card.id}/contacts/{vc2.id}",
        json={"email": "john@arrow.com"},
    )
    assert resp.status_code == 409


def test_delete_vendor_contact(client, db_session, test_vendor_card, test_vendor_contact):
    """DELETE /api/vendors/{card_id}/contacts/{contact_id} removes the contact."""
    resp = client.delete(f"/api/vendors/{test_vendor_card.id}/contacts/{test_vendor_contact.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True


def test_delete_vendor_contact_not_found(client, db_session, test_vendor_card):
    """DELETE nonexistent contact returns 404."""
    resp = client.delete(f"/api/vendors/{test_vendor_card.id}/contacts/99999")
    assert resp.status_code == 404


def test_update_vendor_contact_change_email(client, db_session, test_vendor_card, test_vendor_contact):
    """PUT contact with new email updates email and syncs legacy emails[]."""
    old_email = test_vendor_contact.email
    test_vendor_card.emails = [old_email, "other@arrow.com"]
    db_session.commit()

    resp = client.put(
        f"/api/vendors/{test_vendor_card.id}/contacts/{test_vendor_contact.id}",
        json={"email": "newemail@arrow.com"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True

    db_session.refresh(test_vendor_card)
    assert "newemail@arrow.com" in test_vendor_card.emails
    assert old_email not in test_vendor_card.emails


def test_update_vendor_contact_label_and_phone(client, db_session, test_vendor_card, test_vendor_contact):
    """PUT contact with label and phone updates both fields."""
    resp = client.put(
        f"/api/vendors/{test_vendor_card.id}/contacts/{test_vendor_contact.id}",
        json={"label": "Purchasing", "phone": "+1-555-9999"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True


def test_update_vendor_contact_not_found(client, db_session, test_vendor_card):
    """PUT nonexistent contact returns 404."""
    resp = client.put(
        f"/api/vendors/{test_vendor_card.id}/contacts/99999",
        json={"full_name": "Ghost"},
    )
    assert resp.status_code == 404


def test_update_vendor_contact_set_company_type(client, db_session, test_vendor_card, test_vendor_contact):
    """PUT contact with empty full_name sets contact_type to company."""
    resp = client.put(
        f"/api/vendors/{test_vendor_card.id}/contacts/{test_vendor_contact.id}",
        json={"full_name": ""},
    )
    assert resp.status_code == 200


def test_delete_vendor_contact_cleans_legacy_emails(client, db_session, test_vendor_card, test_vendor_contact):
    """DELETE contact removes email from card's legacy emails[] array."""
    test_vendor_card.emails = ["john@arrow.com", "other@arrow.com"]
    db_session.commit()

    resp = client.delete(f"/api/vendors/{test_vendor_card.id}/contacts/{test_vendor_contact.id}")
    assert resp.status_code == 200

    db_session.refresh(test_vendor_card)
    assert "john@arrow.com" not in test_vendor_card.emails
    assert "other@arrow.com" in test_vendor_card.emails


def test_add_vendor_contact_adds_to_legacy_emails(client, db_session, test_vendor_card):
    """POST /api/vendors/{id}/contacts adds email to card's legacy emails[]."""
    original_emails = test_vendor_card.emails or []
    resp = client.post(
        f"/api/vendors/{test_vendor_card.id}/contacts",
        json={"email": "legacy@arrow.com"},
    )
    assert resp.status_code == 200

    db_session.refresh(test_vendor_card)
    assert "legacy@arrow.com" in test_vendor_card.emails


def test_add_vendor_contact_company_type(client, db_session, test_vendor_card):
    """POST /api/vendors/{id}/contacts without full_name sets type to company."""
    resp = client.post(
        f"/api/vendors/{test_vendor_card.id}/contacts",
        json={"email": "company@arrow.com"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["duplicate"] is False


# ── Bulk vendor contacts ─────────────────────────────────────────────────


def test_vendor_contacts_bulk_empty(client, db_session):
    """GET /api/vendor-contacts/bulk with no data returns empty items."""
    resp = client.get("/api/vendor-contacts/bulk")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0
    assert "limit" in data
    assert "offset" in data


def test_vendor_contacts_bulk_with_data(client, db_session, test_vendor_card, test_vendor_contact):
    """GET /api/vendor-contacts/bulk returns contacts with vendor_name."""
    resp = client.get("/api/vendor-contacts/bulk")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert len(data["items"]) >= 1
    item = data["items"][0]
    assert "vendor_name" in item
    assert item["vendor_name"] == "Arrow Electronics"
    assert "email" in item
    assert item["email"] == "john@arrow.com"


def test_vendor_contacts_bulk_pagination(client, db_session, test_vendor_card, test_vendor_contact):
    """Bulk endpoint respects limit and offset."""
    resp = client.get("/api/vendor-contacts/bulk", params={"limit": 1, "offset": 0})
    assert resp.status_code == 200
    data = resp.json()
    assert data["limit"] == 1
    assert data["offset"] == 0
    assert len(data["items"]) <= 1


def test_vendor_contacts_bulk_excludes_blacklisted(client, db_session, test_vendor_card, test_vendor_contact):
    """Blacklisted vendor contacts are excluded from bulk response."""
    test_vendor_card.is_blacklisted = True
    db_session.commit()
    resp = client.get("/api/vendor-contacts/bulk")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0


# ── UX audit dead-control fixes ──────────────────────────────────────────


def test_vendor_list_controls_dispatch_input_event(client, db_session):
    """The vendor-list filter controls (sort / hide-blacklisted / show-archived / cards-
    vs-table) must re-fire the search input via its real ``input`` trigger.

    Previously they dispatched ``'changed'``, which the ``q`` input never listens for
    (``hx-trigger="input delay:300ms"``), so no request fired and the controls were
    silently dead. Assert the fixed event is dispatched and the dead one is gone.
    """
    resp = client.get("/v2/partials/vendors")
    assert resp.status_code == 200
    html = resp.text
    assert "htmx.trigger(document.querySelector('#vendor-filters [name=q]'), 'input')" in html
    assert "'changed')" not in html


def test_log_call_returns_refreshed_row_and_toast(client, db_session, test_vendor_card, test_vendor_contact):
    """Log Call re-renders the contact row (interaction count ticks up) and emits an
    ``HX-Trigger: showToast`` so the click is visibly acknowledged (was ``hx-swap=none``
    against bare JSON → zero feedback)."""
    before = test_vendor_contact.interaction_count or 0

    resp = client.post(f"/api/vendors/{test_vendor_card.id}/contacts/{test_vendor_contact.id}/log-call")
    assert resp.status_code == 200

    # Success toast drives on-screen feedback.
    trigger = resp.headers.get("HX-Trigger")
    assert trigger is not None
    payload = json.loads(trigger)
    assert payload["showToast"]["type"] == "success"
    assert payload["showToast"]["message"]

    # Body is the refreshed row (targets #vendor-contact-<id> with outerHTML swap).
    assert f'id="vendor-contact-{test_vendor_contact.id}"' in resp.text

    # Interaction count actually incremented, so the re-rendered row shows the new value.
    db_session.refresh(test_vendor_contact)
    assert test_vendor_contact.interaction_count == before + 1


def test_log_call_missing_contact_404(client, db_session, test_vendor_card):
    """Log Call on an unknown contact returns 404."""
    resp = client.post(f"/api/vendors/{test_vendor_card.id}/contacts/999999/log-call")
    assert resp.status_code == 404
