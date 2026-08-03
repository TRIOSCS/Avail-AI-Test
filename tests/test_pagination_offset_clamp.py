"""tests/test_pagination_offset_clamp.py — Stale-offset snap-back on paginated vendor
endpoints.

An out-of-range offset (bookmarked/hand-edited URL, or a filter that shrank the
result set) must snap back to page 1 and report the REAL total — never an empty
page with total=0. Ports the crm_service.customer_contacts_context clamp to
GET /api/vendors (routers/vendors_crud.py) and GET /api/vendor-contacts/bulk
(routers/vendor_contacts.py). In-range offsets must be left untouched.

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_vendor_card, test_vendor_contact)
"""

import os

os.environ["TESTING"] = "1"

from datetime import UTC, datetime

from app.models import VendorCard

# ── GET /api/vendors ─────────────────────────────────────────────────


def test_vendors_list_out_of_range_offset_snaps_to_page_one(client, db_session, test_vendor_card):
    """Offset far past the result set → page 1 with the REAL total, not an empty
    page."""
    resp = client.get("/api/vendors", params={"offset": 500})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["offset"] == 0
    assert len(body["vendors"]) == 1
    assert body["vendors"][0]["display_name"] == "Arrow Electronics"


def test_vendors_list_in_range_offset_untouched(client, db_session, test_vendor_card):
    """A valid offset still pages normally — the clamp only fires when offset >=
    total."""
    second = VendorCard(
        normalized_name="zebra components",
        display_name="Zebra Components",
        emails=[],
        phones=[],
        sighting_count=1,
        created_at=datetime.now(UTC),
    )
    db_session.add(second)
    db_session.commit()

    resp = client.get("/api/vendors", params={"limit": 1, "offset": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert body["offset"] == 1
    assert len(body["vendors"]) == 1
    # Default order is display_name asc → page 2 of 1-per-page is Zebra.
    assert body["vendors"][0]["display_name"] == "Zebra Components"


def test_vendors_list_empty_db_reports_zero_total(client, db_session):
    """Genuinely empty result set keeps total=0 and an empty page."""
    resp = client.get("/api/vendors", params={"offset": 300})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["vendors"] == []


# ── GET /api/vendor-contacts/bulk ────────────────────────────────────


def test_bulk_contacts_out_of_range_offset_snaps_to_page_one(client, db_session, test_vendor_card, test_vendor_contact):
    """Offset past the contact count → page 1 with the real total."""
    resp = client.get("/api/vendor-contacts/bulk", params={"offset": 4999})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["offset"] == 0
    assert len(body["items"]) == 1
    assert body["items"][0]["id"] == test_vendor_contact.id


def test_bulk_contacts_in_range_offset_untouched(client, db_session, test_vendor_card, test_vendor_contact):
    """A valid offset still pages normally on the bulk endpoint."""
    from app.models.vendors import VendorContact

    second = VendorContact(
        vendor_card_id=test_vendor_card.id,
        full_name="Second Person",
        email="second@arrow.com",
        source="manual",
        is_verified=True,
        confidence=70,
    )
    db_session.add(second)
    db_session.commit()

    resp = client.get("/api/vendor-contacts/bulk", params={"limit": 1, "offset": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert body["offset"] == 1
    assert len(body["items"]) == 1
