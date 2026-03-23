"""test_sprint3_vendor_crud.py — Tests for Sprint 3 vendor CRUD + contact management.

Verifies: Edit vendor, toggle blacklist, contact timeline, contact nudges,
vendor reviews CRUD.

Called by: pytest
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User, VendorCard
from app.models.vendors import VendorContact

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def vendor(db_session: Session):
    """A vendor card for testing."""
    v = VendorCard(
        normalized_name="arrow electronics",
        display_name="Arrow Electronics",
        emails=["sales@arrow.com"],
        phones=["+1-555-0100"],
        website="https://arrow.com",
        sighting_count=42,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(v)
    db_session.commit()
    db_session.refresh(v)
    return v


@pytest.fixture()
def vendor_contact(db_session: Session, vendor: VendorCard):
    """A vendor contact."""
    c = VendorContact(
        vendor_card_id=vendor.id,
        full_name="John Sales",
        email="john@arrow.com",
        title="Sales Rep",
        phone="+1-555-0101",
        source="manual",
        interaction_count=5,
        last_interaction_at=datetime.now(timezone.utc) - timedelta(days=45),
    )
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


# ── Edit Vendor ───────────────────────────────────────────────────────


class TestEditVendor:
    def test_edit_form_renders(self, client: TestClient, vendor: VendorCard):
        resp = client.get(
            f"/v2/partials/vendors/{vendor.id}/edit-form",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Edit Vendor" in resp.text
        assert vendor.display_name in resp.text

    def test_edit_saves_changes(self, client: TestClient, vendor: VendorCard, db_session: Session):
        resp = client.post(
            f"/v2/partials/vendors/{vendor.id}/edit",
            data={"display_name": "Arrow Global", "website": "https://arrow-global.com"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        db_session.refresh(vendor)
        assert vendor.display_name == "Arrow Global"
        assert vendor.website == "https://arrow-global.com"

    def test_edit_updates_emails(self, client: TestClient, vendor: VendorCard, db_session: Session):
        resp = client.post(
            f"/v2/partials/vendors/{vendor.id}/edit",
            data={"display_name": "Arrow", "emails": "new@arrow.com, info@arrow.com"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        db_session.refresh(vendor)
        assert "new@arrow.com" in vendor.emails


# ── Toggle Blacklist ──────────────────────────────────────────────────


class TestToggleBlacklist:
    def test_blacklist_toggle_on(self, client: TestClient, vendor: VendorCard, db_session: Session):
        assert not vendor.is_blacklisted
        resp = client.post(
            f"/v2/partials/vendors/{vendor.id}/toggle-blacklist",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        db_session.refresh(vendor)
        assert vendor.is_blacklisted is True

    def test_blacklist_toggle_off(self, client: TestClient, vendor: VendorCard, db_session: Session):
        vendor.is_blacklisted = True
        db_session.commit()
        resp = client.post(
            f"/v2/partials/vendors/{vendor.id}/toggle-blacklist",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        db_session.refresh(vendor)
        assert vendor.is_blacklisted is False


# ── Contact Timeline ──────────────────────────────────────────────────


class TestContactTimeline:
    def test_timeline_renders(self, client: TestClient, vendor: VendorCard, vendor_contact: VendorContact):
        resp = client.get(
            f"/v2/partials/vendors/{vendor.id}/contacts/{vendor_contact.id}/timeline",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Timeline" in resp.text

    def test_timeline_nonexistent_contact(self, client: TestClient, vendor: VendorCard):
        resp = client.get(
            f"/v2/partials/vendors/{vendor.id}/contacts/99999/timeline",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404


# ── Contact Nudges ────────────────────────────────────────────────────


class TestContactNudges:
    def test_nudges_shows_dormant(self, client: TestClient, vendor: VendorCard, vendor_contact: VendorContact):
        resp = client.get(
            f"/v2/partials/vendors/{vendor.id}/contact-nudges",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Contact Nudges" in resp.text
        # Contact was last contacted 45 days ago, should be in nudges
        assert vendor_contact.full_name in resp.text

    def test_nudges_empty_when_all_recent(
        self, client: TestClient, vendor: VendorCard, vendor_contact: VendorContact, db_session: Session
    ):
        vendor_contact.last_interaction_at = datetime.now(timezone.utc)
        db_session.commit()
        resp = client.get(
            f"/v2/partials/vendors/{vendor.id}/contact-nudges",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "All contacts have been contacted" in resp.text


# ── Vendor Reviews ────────────────────────────────────────────────────


class TestVendorReviews:
    def test_reviews_empty(self, client: TestClient, vendor: VendorCard):
        resp = client.get(
            f"/v2/partials/vendors/{vendor.id}/reviews",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "No reviews yet" in resp.text

    def test_add_review(self, client: TestClient, vendor: VendorCard, db_session: Session):
        resp = client.post(
            f"/v2/partials/vendors/{vendor.id}/reviews",
            data={"rating": "4", "comment": "Great vendor!"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Great vendor!" in resp.text

    def test_delete_own_review(self, client: TestClient, vendor: VendorCard, test_user: User, db_session: Session):
        from app.models import VendorReview

        review = VendorReview(vendor_card_id=vendor.id, user_id=test_user.id, rating=3)
        db_session.add(review)
        db_session.commit()
        db_session.refresh(review)

        resp = client.delete(
            f"/v2/partials/vendors/{vendor.id}/reviews/{review.id}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert db_session.get(VendorReview, review.id) is None

    def test_reviews_tab_via_vendor_tab(self, client: TestClient, vendor: VendorCard):
        resp = client.get(
            f"/v2/partials/vendors/{vendor.id}/tab/reviews",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Reviews" in resp.text
