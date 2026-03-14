"""
test_htmx_vendors.py — Tests for Phase 3 Task 7: Vendor Cards + Contacts HTMX views.
Verifies vendor list page, rows partial with search/sort/pagination,
detail drawer rendering, tab content (overview, contacts, analytics, offers),
and contact row rendering.
Called by: pytest
Depends on: app/routers/views.py, app/templates/partials/vendors/
"""

import os

os.environ.setdefault("TESTING", "1")
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("USE_HTMX", "true")

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.models import Offer, Requisition, User, VendorCard, VendorContact, VendorReview


@pytest.fixture()
def htmx_client(db_session, test_user):
    """TestClient with views router registered and auth overridden."""
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app
    from app.routers.views import router as views_router

    route_paths = [r.path for r in app.routes]
    if "/views/vendors" not in route_paths:
        app.include_router(views_router)

    def _override_db():
        yield db_session

    def _override_user():
        return test_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


@pytest.fixture()
def sample_vendors(db_session):
    """Create multiple vendor cards for list/filter tests."""
    vendors = []
    for name, domain, score in [
        ("Arrow Electronics", "arrow.com", 85.0),
        ("Mouser Electronics", "mouser.com", 92.5),
        ("Digi-Key", "digikey.com", None),
        ("Newark Element14", "newark.com", 70.0),
    ]:
        v = VendorCard(
            normalized_name=name.lower(),
            display_name=name,
            domain=domain,
            vendor_score=score,
            sighting_count=10,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(v)
        vendors.append(v)
    db_session.commit()
    for v in vendors:
        db_session.refresh(v)
    return vendors


@pytest.fixture()
def vendor_with_contacts(db_session, sample_vendors):
    """Add contacts to the first vendor (Arrow Electronics)."""
    vendor = sample_vendors[0]
    contacts = []
    for name, email in [
        ("John Sales", "john@arrow.com"),
        ("Jane Buyer", "jane@arrow.com"),
    ]:
        vc = VendorContact(
            vendor_card_id=vendor.id,
            full_name=name,
            email=email,
            phone="+1-555-0100",
            title="Sales Rep",
            source="manual",
            is_verified=True,
        )
        db_session.add(vc)
        contacts.append(vc)
    db_session.commit()
    for c in contacts:
        db_session.refresh(c)
    return vendor, contacts


class TestVendorsListPage:
    """Tests for GET /views/vendors — full page HTML."""

    def test_vendors_list_page_returns_html(self, htmx_client):
        resp = htmx_client.get("/views/vendors")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_vendors_list_page_contains_table(self, htmx_client):
        resp = htmx_client.get("/views/vendors")
        assert "vendor-table-body" in resp.text
        assert "Vendors" in resp.text

    def test_vendors_list_page_has_new_button(self, htmx_client):
        resp = htmx_client.get("/views/vendors")
        assert "New Vendor" in resp.text

    def test_vendors_list_shows_data(self, htmx_client, sample_vendors):
        resp = htmx_client.get("/views/vendors")
        assert resp.status_code == 200
        assert "Arrow Electronics" in resp.text
        assert "Mouser Electronics" in resp.text

    def test_vendors_list_has_search_input(self, htmx_client):
        resp = htmx_client.get("/views/vendors")
        assert 'type="search"' in resp.text
        assert "delay:300ms" in resp.text


class TestVendorsRowsPartial:
    """Tests for GET /views/vendors/rows — HTMX rows swap target."""

    def test_rows_returns_html(self, htmx_client, sample_vendors):
        resp = htmx_client.get("/views/vendors/rows")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_rows_contain_vendor_names(self, htmx_client, sample_vendors):
        resp = htmx_client.get("/views/vendors/rows")
        assert "Arrow Electronics" in resp.text
        assert "Mouser Electronics" in resp.text
        assert "Digi-Key" in resp.text
        assert "Newark Element14" in resp.text


class TestVendorsSearch:
    """Tests for search filtering on vendor rows."""

    def test_search_by_name(self, htmx_client, sample_vendors):
        resp = htmx_client.get("/views/vendors/rows?q=Arrow")
        assert "Arrow Electronics" in resp.text
        assert "Mouser Electronics" not in resp.text

    def test_search_by_domain(self, htmx_client, sample_vendors):
        resp = htmx_client.get("/views/vendors/rows?q=mouser.com")
        assert "Mouser Electronics" in resp.text
        assert "Arrow Electronics" not in resp.text

    def test_search_no_results(self, htmx_client, sample_vendors):
        resp = htmx_client.get("/views/vendors/rows?q=nonexistent_xyz")
        assert resp.status_code == 200
        assert "No vendors found" in resp.text


class TestVendorsPagination:
    """Tests for page param on vendor rows."""

    def test_page_1_returns_results(self, htmx_client, sample_vendors):
        resp = htmx_client.get("/views/vendors/rows?page=1")
        assert resp.status_code == 200
        assert "Arrow Electronics" in resp.text

    def test_page_beyond_range_clamps(self, htmx_client, sample_vendors):
        resp = htmx_client.get("/views/vendors/rows?page=999")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]


class TestVendorDetailDrawer:
    """Tests for GET /views/vendors/{id} — detail drawer partial."""

    def test_detail_returns_html(self, htmx_client, sample_vendors):
        vendor = sample_vendors[0]
        resp = htmx_client.get(f"/views/vendors/{vendor.id}")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_detail_contains_vendor_name(self, htmx_client, sample_vendors):
        vendor = sample_vendors[0]
        resp = htmx_client.get(f"/views/vendors/{vendor.id}")
        assert "Arrow Electronics" in resp.text

    def test_detail_has_tabs(self, htmx_client, sample_vendors):
        vendor = sample_vendors[0]
        resp = htmx_client.get(f"/views/vendors/{vendor.id}")
        for tab in ["Overview", "Contacts", "Analytics", "Offers"]:
            assert tab in resp.text

    def test_detail_has_enrich_button(self, htmx_client, sample_vendors):
        vendor = sample_vendors[0]
        resp = htmx_client.get(f"/views/vendors/{vendor.id}")
        assert "Enrich" in resp.text

    def test_detail_has_close_button(self, htmx_client, sample_vendors):
        vendor = sample_vendors[0]
        resp = htmx_client.get(f"/views/vendors/{vendor.id}")
        assert "close-drawer" in resp.text

    def test_detail_shows_score(self, htmx_client, sample_vendors):
        vendor = sample_vendors[0]  # Arrow, score=85.0
        resp = htmx_client.get(f"/views/vendors/{vendor.id}")
        assert "85.0" in resp.text

    def test_detail_404_for_missing(self, htmx_client):
        resp = htmx_client.get("/views/vendors/99999")
        assert resp.status_code == 404


class TestVendorTabOverview:
    """Tests for GET /views/vendors/{id}/tab/overview."""

    def test_overview_returns_html(self, htmx_client, sample_vendors):
        vendor = sample_vendors[0]
        resp = htmx_client.get(f"/views/vendors/{vendor.id}/tab/overview")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_overview_shows_fields(self, htmx_client, db_session, sample_vendors):
        vendor = sample_vendors[0]
        vendor.website = "https://arrow.com"
        vendor.industry = "Electronics Distribution"
        db_session.commit()
        resp = htmx_client.get(f"/views/vendors/{vendor.id}/tab/overview")
        assert "https://arrow.com" in resp.text
        assert "Electronics Distribution" in resp.text

    def test_overview_shows_no_reviews(self, htmx_client, sample_vendors):
        vendor = sample_vendors[0]
        resp = htmx_client.get(f"/views/vendors/{vendor.id}/tab/overview")
        assert "No reviews yet" in resp.text

    def test_overview_shows_rating(self, htmx_client, db_session, test_user, sample_vendors):
        vendor = sample_vendors[0]
        review = VendorReview(
            vendor_card_id=vendor.id,
            user_id=test_user.id,
            rating=4,
            comment="Great vendor",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(review)
        db_session.commit()
        resp = htmx_client.get(f"/views/vendors/{vendor.id}/tab/overview")
        assert "4.0" in resp.text
        assert "1 review" in resp.text

    def test_overview_has_blacklist_toggle(self, htmx_client, sample_vendors):
        vendor = sample_vendors[0]
        resp = htmx_client.get(f"/views/vendors/{vendor.id}/tab/overview")
        assert "Blacklist" in resp.text


class TestVendorTabContacts:
    """Tests for GET /views/vendors/{id}/tab/contacts."""

    def test_contacts_empty(self, htmx_client, sample_vendors):
        vendor = sample_vendors[0]
        resp = htmx_client.get(f"/views/vendors/{vendor.id}/tab/contacts")
        assert resp.status_code == 200
        assert "No contacts found" in resp.text

    def test_contacts_with_data(self, htmx_client, vendor_with_contacts):
        vendor, contacts = vendor_with_contacts
        resp = htmx_client.get(f"/views/vendors/{vendor.id}/tab/contacts")
        assert "John Sales" in resp.text
        assert "Jane Buyer" in resp.text
        assert "john@arrow.com" in resp.text

    def test_contacts_has_add_button(self, htmx_client, sample_vendors):
        vendor = sample_vendors[0]
        resp = htmx_client.get(f"/views/vendors/{vendor.id}/tab/contacts")
        assert "Add Contact" in resp.text


class TestVendorTabAnalytics:
    """Tests for GET /views/vendors/{id}/tab/analytics."""

    def test_analytics_returns_html(self, htmx_client, sample_vendors):
        vendor = sample_vendors[0]
        resp = htmx_client.get(f"/views/vendors/{vendor.id}/tab/analytics")
        assert resp.status_code == 200
        assert "Vendor Scorecard" in resp.text

    def test_analytics_shows_fields(self, htmx_client, db_session, sample_vendors):
        vendor = sample_vendors[0]
        vendor.response_rate = 0.85
        vendor.ghost_rate = 0.1
        vendor.total_outreach = 50
        vendor.total_responses = 42
        db_session.commit()
        resp = htmx_client.get(f"/views/vendors/{vendor.id}/tab/analytics")
        assert "85.0%" in resp.text
        assert "10.0%" in resp.text
        assert "50" in resp.text


class TestVendorTabOffers:
    """Tests for GET /views/vendors/{id}/tab/offers."""

    def test_offers_empty(self, htmx_client, sample_vendors):
        vendor = sample_vendors[0]
        resp = htmx_client.get(f"/views/vendors/{vendor.id}/tab/offers")
        assert resp.status_code == 200
        assert "No offers from this vendor yet" in resp.text

    def test_offers_with_data(self, htmx_client, db_session, test_user, sample_vendors):
        vendor = sample_vendors[0]  # Arrow Electronics
        req = Requisition(
            name="REQ-VTEST-001",
            customer_name="Test Customer",
            status="open",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        offer = Offer(
            requisition_id=req.id,
            vendor_name="Arrow Electronics",
            mpn="LM317T",
            qty_available=1000,
            unit_price=0.50,
            entered_by_id=test_user.id,
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.commit()
        resp = htmx_client.get(f"/views/vendors/{vendor.id}/tab/offers")
        assert "LM317T" in resp.text
        assert "active" in resp.text


class TestVendorTabInvalid:
    """Tests for invalid tab names."""

    def test_invalid_tab_returns_404(self, htmx_client, sample_vendors):
        vendor = sample_vendors[0]
        resp = htmx_client.get(f"/views/vendors/{vendor.id}/tab/invalid_tab")
        assert resp.status_code == 404
