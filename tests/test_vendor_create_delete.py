"""test_vendor_create_delete.py — Tests for vendor create/delete CRUD affordances.

Verifies:
  - POST /api/vendors creates a VendorCard (201)
  - POST /api/vendors returns 409 on duplicate normalized name
  - POST /api/vendors returns 422 on missing display_name
  - GET /v2/partials/vendors/create-form renders form with display_name field
  - GET /v2/partials/vendors/list.html contains an "Add Vendor" button
  - POST /v2/partials/vendors/create creates vendor and returns detail HTML
  - DELETE /v2/partials/vendors/{id} deletes vendor and returns list
  - DELETE /v2/partials/vendors/{id} returns 400 if vendor has active offers
  - detail.html contains a delete control pointing at the delete route
  - Auth: unauthenticated callers are rejected (401/403)

Called by: pytest
Depends on: conftest.py fixtures (client, db_session, test_user)
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Offer, VendorCard

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def vendor(db_session: Session) -> VendorCard:
    """A vendor card for testing."""
    v = VendorCard(
        normalized_name="test vendor co",
        display_name="Test Vendor Co",
        emails=["sales@testvendor.com"],
        phones=["+1-555-0199"],
        website="https://testvendor.com",
        sighting_count=5,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(v)
    db_session.commit()
    db_session.refresh(v)
    return v


# ── POST /api/vendors ─────────────────────────────────────────────────


def test_create_vendor_api_success(client: TestClient):
    """POST /api/vendors creates a VendorCard and returns 201 with the card data."""
    resp = client.post(
        "/api/vendors",
        json={"display_name": "Brand New Vendor"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["display_name"] == "Brand New Vendor"
    assert "id" in data


def test_create_vendor_api_with_all_fields(client: TestClient):
    """POST /api/vendors accepts optional fields."""
    resp = client.post(
        "/api/vendors",
        json={
            "display_name": "Full Vendor LLC",
            "website": "https://fullvendor.com",
            "emails": ["info@fullvendor.com"],
            "phones": ["+1-555-0200"],
            "industry": "Electronic Components",
            "hq_city": "Chicago",
            "hq_country": "US",
            "employee_size": "51-200",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["display_name"] == "Full Vendor LLC"


def test_create_vendor_api_duplicate_name_returns_409(client: TestClient, vendor: VendorCard):
    """POST /api/vendors returns 409 when the normalized name already exists."""
    resp = client.post(
        "/api/vendors",
        json={"display_name": "Test Vendor Co"},  # same normalized name as fixture
    )
    assert resp.status_code == 409


def test_create_vendor_api_missing_name_returns_422(client: TestClient):
    """POST /api/vendors returns 422 when display_name is absent."""
    resp = client.post("/api/vendors", json={})
    assert resp.status_code == 422


def test_create_vendor_api_blank_name_returns_422(client: TestClient):
    """POST /api/vendors returns 422 when display_name is blank."""
    resp = client.post("/api/vendors", json={"display_name": "   "})
    assert resp.status_code == 422


# ── GET /v2/partials/vendors/create-form ─────────────────────────────


def test_create_form_route_renders(client: TestClient):
    """GET /v2/partials/vendors/create-form returns HTML with the name input."""
    resp = client.get("/v2/partials/vendors/create-form")
    assert resp.status_code == 200
    assert b'name="display_name"' in resp.content


def test_create_form_has_duplicate_check_wiring(client: TestClient):
    """create_form.html wires the vendor name input to the duplicate-check endpoint."""
    resp = client.get("/v2/partials/vendors/create-form")
    assert resp.status_code == 200
    assert b"/api/vendors/check-duplicate" in resp.content


# ── vendors/list.html — Add Vendor button ────────────────────────────


def test_vendor_list_has_add_vendor_button(client: TestClient):
    """GET /v2/partials/vendors includes an Add Vendor button linking to create-form."""
    resp = client.get("/v2/partials/vendors")
    assert resp.status_code == 200
    assert b"/v2/partials/vendors/create-form" in resp.content
    assert b"Add Vendor" in resp.content


# ── POST /v2/partials/vendors/create ─────────────────────────────────


def test_create_vendor_partial_creates_and_returns_detail(client: TestClient):
    """POST /v2/partials/vendors/create creates the vendor and returns detail HTML."""
    resp = client.post(
        "/v2/partials/vendors/create",
        data={"display_name": "HTMX New Vendor"},
    )
    assert resp.status_code == 200
    # detail page shows the vendor name
    assert b"HTMX New Vendor" in resp.content


def test_create_vendor_partial_duplicate_returns_409(client: TestClient, vendor: VendorCard):
    """POST /v2/partials/vendors/create returns 409 on duplicate name."""
    resp = client.post(
        "/v2/partials/vendors/create",
        data={"display_name": "Test Vendor Co"},
    )
    assert resp.status_code == 409


def test_create_vendor_partial_missing_name_returns_400(client: TestClient):
    """POST /v2/partials/vendors/create returns 400 if display_name is empty."""
    resp = client.post("/v2/partials/vendors/create", data={"display_name": ""})
    assert resp.status_code == 400


# ── DELETE /v2/partials/vendors/{id} ─────────────────────────────────


def test_delete_vendor_partial_removes_card(client: TestClient, vendor: VendorCard, db_session: Session):
    """DELETE /v2/partials/vendors/{id} deletes the card and returns the vendor list."""
    resp = client.delete(f"/v2/partials/vendors/{vendor.id}")
    assert resp.status_code == 200
    # After delete, the vendor list is returned; vendor name should not appear
    deleted = db_session.get(VendorCard, vendor.id)
    assert deleted is None


def test_delete_vendor_partial_missing_vendor_returns_404(client: TestClient):
    """DELETE /v2/partials/vendors/{id} returns 404 for a non-existent vendor."""
    resp = client.delete("/v2/partials/vendors/999999")
    assert resp.status_code == 404


def test_delete_vendor_partial_with_offers_returns_400(client: TestClient, vendor: VendorCard, db_session: Session):
    """DELETE /v2/partials/vendors/{id} returns 400 when vendor has active offers."""
    from app.models import MaterialCard

    mc = MaterialCard(
        normalized_mpn="LM317T",
        display_mpn="LM317T",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(mc)
    db_session.flush()

    offer = Offer(
        vendor_card_id=vendor.id,
        material_card_id=mc.id,
        vendor_name="Test Vendor Co",
        mpn="LM317T",
        unit_price=1.23,
        qty_available=100,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer)
    db_session.commit()

    resp = client.delete(f"/v2/partials/vendors/{vendor.id}")
    assert resp.status_code == 400


# ── vendors/detail.html — Delete button rendering ────────────────────


def test_vendor_detail_has_delete_button(client: TestClient, vendor: VendorCard):
    """GET /v2/partials/vendors/{id} includes a delete control targeting the delete
    route."""
    resp = client.get(f"/v2/partials/vendors/{vendor.id}")
    assert resp.status_code == 200
    assert f"/v2/partials/vendors/{vendor.id}".encode() in resp.content
    assert b"Delete" in resp.content


# ── vendors/detail.html — header consistency with account header ─────


def test_vendor_detail_header_matches_account_header(client: TestClient, db_session: Session):
    """The vendor detail h1 uses the design-system record-hero recipe (.h1) and the
    header metadata uses middot `·` separators (not pipe `|`), mirroring the account
    header."""
    v = VendorCard(
        normalized_name="header consistency co",
        display_name="Header Consistency Co",
        hq_city="Chicago",
        hq_country="US",
        industry="Electronic Components",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(v)
    db_session.commit()
    db_session.refresh(v)

    resp = client.get(f"/v2/partials/vendors/{v.id}")
    assert resp.status_code == 200
    body = resp.text

    # h1 must use the shared record-hero recipe (account-header parity via .h1),
    # not a hand-rolled utility stack.
    assert '<h1 class="h1">Header Consistency Co</h1>' in body
    assert '<h1 class="text-2xl font-bold text-gray-900">' not in body
    # Header metadata separators must be the middot, not the pipe.
    assert '<span class="text-gray-300">·</span>' in body
    assert '<span class="text-gray-300">|</span>' not in body


# ── Auth: unauthenticated caller is rejected ─────────────────────────


def test_create_vendor_api_requires_auth():
    """POST /api/vendors without auth returns 401/403."""
    from app.main import app

    with TestClient(app) as unauthenticated:
        resp = unauthenticated.post("/api/vendors", json={"display_name": "No Auth Vendor"})
    assert resp.status_code in (401, 403)


def test_create_form_route_requires_auth():
    """GET /v2/partials/vendors/create-form without auth returns 401/403."""
    from app.main import app

    with TestClient(app) as unauthenticated:
        resp = unauthenticated.get("/v2/partials/vendors/create-form")
    assert resp.status_code in (401, 403)
