"""tests/test_vendors_router_gaps2.py — Second coverage-gap pass for vendors router.

Covers lines 161-202 (vendor_contacts_partial — global vendor contacts list endpoint).
That 42-statement block is the single largest uncovered region and crossing it alone
pushes vendors.py from 83 % to 85 %+.

Called by: pytest
Depends on: app/routers/htmx/vendors.py, conftest.py fixtures
"""

import os

os.environ["TESTING"] = "1"

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User, VendorCard, VendorContact

# ── Client fixture ─────────────────────────────────────────────────────────────


@pytest.fixture()
def vendor_user(db_session: Session) -> User:
    """Standard buyer user for vendor contacts tests."""
    user = User(
        email="vendor_gaps2@trioscs.com",
        name="Gaps2 User",
        role="buyer",
        azure_id="test-azure-gaps2-buyer",
        created_at=datetime.now(UTC),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def gaps2_client(db_session: Session, vendor_user: User) -> TestClient:
    """TestClient with auth overridden to vendor_user."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    overridden = [get_db, require_user, require_admin, require_buyer, require_fresh_token]
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: vendor_user
    app.dependency_overrides[require_admin] = lambda: vendor_user
    app.dependency_overrides[require_buyer] = lambda: vendor_user
    app.dependency_overrides[require_fresh_token] = lambda: "token"
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in overridden:
            app.dependency_overrides.pop(dep, None)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _active_card(db: Session, name: str = "Active Supplier") -> VendorCard:
    """Seed a vendor card that satisfies the is_active/not-blacklisted filter."""
    card = VendorCard(
        normalized_name=name.lower(),
        display_name=name,
        is_active=True,
        is_blacklisted=False,
        created_at=datetime.now(UTC),
    )
    db.add(card)
    db.flush()
    return card


def _contact(db: Session, card: VendorCard, email: str, name: str) -> VendorContact:
    """Seed a VendorContact linked to *card*."""
    vc = VendorContact(
        vendor_card_id=card.id,
        email=email,
        full_name=name,
        source="manual",
        is_verified=True,
        confidence=80,
    )
    db.add(vc)
    db.flush()
    return vc


# ── Lines 161-202: vendor_contacts_partial ────────────────────────────────────


class TestVendorContactsPartial:
    """GET /v2/partials/vendor-contacts — global vendor contacts list."""

    def test_returns_200_with_contacts(self, gaps2_client, db_session):
        """Lines 163-202: endpoint queries contacts, builds ctx, calls template_response."""
        card = _active_card(db_session, "TechParts Inc")
        _contact(db_session, card, "sales@techparts.com", "Alice Smith")
        db_session.commit()

        with patch("app.routers.htmx.vendors.template_response") as mock_tpl:
            mock_tpl.return_value = HTMLResponse("<div>contacts</div>")
            resp = gaps2_client.get("/v2/partials/vendor-contacts")

        assert resp.status_code == 200
        # Verify template was called with the expected context keys
        ctx = mock_tpl.call_args[0][1]
        assert "contacts" in ctx
        assert "total" in ctx
        assert ctx["total"] >= 1

    def test_search_filter_narrows_results(self, gaps2_client, db_session):
        """Lines 169-176: search term is applied as ilike filter on name/email/vendor."""
        card = _active_card(db_session, "FilterVendor")
        # Use a distinctive name prefix that won't appear in the other contact's fields
        _contact(db_session, card, "zephyr@filtervendor.com", "Zephyr Jones")
        _contact(db_session, card, "unrelated@totally.com", "Unrelated Person")
        db_session.commit()

        with patch("app.routers.htmx.vendors.template_response") as mock_tpl:
            mock_tpl.return_value = HTMLResponse("<div>filtered</div>")
            # Search by the unique first name — only the first contact should match
            resp = gaps2_client.get("/v2/partials/vendor-contacts?search=zephyr")

        assert resp.status_code == 200
        ctx = mock_tpl.call_args[0][1]
        assert ctx["search"] == "zephyr"
        emails = [c.email for c in ctx["contacts"]]
        assert "zephyr@filtervendor.com" in emails
        assert "unrelated@totally.com" not in emails

    def test_sort_by_email_desc(self, gaps2_client, db_session):
        """Lines 178-185: sort=email&dir=desc is mapped to the correct column/order."""
        card = _active_card(db_session, "SortVendor")
        _contact(db_session, card, "aaa@sort.com", "A Person")
        _contact(db_session, card, "zzz@sort.com", "Z Person")
        db_session.commit()

        with patch("app.routers.htmx.vendors.template_response") as mock_tpl:
            mock_tpl.return_value = HTMLResponse("<div>sorted</div>")
            resp = gaps2_client.get("/v2/partials/vendor-contacts?sort=email&dir=desc")

        assert resp.status_code == 200
        ctx = mock_tpl.call_args[0][1]
        assert ctx["sort"] == "email"
        assert ctx["dir"] == "desc"

    def test_sort_by_vendor(self, gaps2_client, db_session):
        """Lines 178-185: sort=vendor maps to VendorCard.display_name."""
        card = _active_card(db_session, "VendorSortTest")
        _contact(db_session, card, "contact@vendorsort.com", "Sort Contact")
        db_session.commit()

        with patch("app.routers.htmx.vendors.template_response") as mock_tpl:
            mock_tpl.return_value = HTMLResponse("<div>ok</div>")
            resp = gaps2_client.get("/v2/partials/vendor-contacts?sort=vendor&dir=asc")

        assert resp.status_code == 200
        ctx = mock_tpl.call_args[0][1]
        assert ctx["sort"] == "vendor"

    def test_sort_by_score(self, gaps2_client, db_session):
        """Lines 178-185: sort=score maps to VendorContact.relationship_score."""
        card = _active_card(db_session, "ScoreSortVendor")
        _contact(db_session, card, "scored@vendor.com", "Scored Contact")
        db_session.commit()

        with patch("app.routers.htmx.vendors.template_response") as mock_tpl:
            mock_tpl.return_value = HTMLResponse("<div>ok</div>")
            resp = gaps2_client.get("/v2/partials/vendor-contacts?sort=score&dir=asc")

        assert resp.status_code == 200
        ctx = mock_tpl.call_args[0][1]
        assert ctx["sort"] == "score"

    def test_unknown_sort_key_falls_back_to_name(self, gaps2_client, db_session):
        """Line 184: unknown sort key → default VendorContact.full_name column."""
        card = _active_card(db_session, "FallbackVendor")
        _contact(db_session, card, "fb@fallback.com", "FB Contact")
        db_session.commit()

        with patch("app.routers.htmx.vendors.template_response") as mock_tpl:
            mock_tpl.return_value = HTMLResponse("<div>ok</div>")
            resp = gaps2_client.get("/v2/partials/vendor-contacts?sort=unknown_col")

        assert resp.status_code == 200

    def test_pagination_limit_and_offset(self, gaps2_client, db_session):
        """Lines 187-188: limit/offset are passed to the query and reflected in ctx."""
        card = _active_card(db_session, "PaginationVendor")
        for i in range(5):
            _contact(db_session, card, f"contact{i}@page.com", f"Contact {i}")
        db_session.commit()

        with patch("app.routers.htmx.vendors.template_response") as mock_tpl:
            mock_tpl.return_value = HTMLResponse("<div>paged</div>")
            resp = gaps2_client.get("/v2/partials/vendor-contacts?limit=2&offset=1")

        assert resp.status_code == 200
        ctx = mock_tpl.call_args[0][1]
        assert ctx["limit"] == 2
        assert ctx["offset"] == 1
        assert len(ctx["contacts"]) <= 2

    def test_blacklisted_vendor_contacts_excluded(self, gaps2_client, db_session):
        """Lines 165-168: contacts from blacklisted vendors are filtered out."""
        blacklisted = VendorCard(
            normalized_name="blacklisted co",
            display_name="Blacklisted Co",
            is_active=True,
            is_blacklisted=True,
            created_at=datetime.now(UTC),
        )
        db_session.add(blacklisted)
        db_session.flush()
        _contact(db_session, blacklisted, "bl@blacklisted.com", "BL Contact")
        db_session.commit()

        with patch("app.routers.htmx.vendors.template_response") as mock_tpl:
            mock_tpl.return_value = HTMLResponse("<div>ok</div>")
            resp = gaps2_client.get("/v2/partials/vendor-contacts")

        assert resp.status_code == 200
        ctx = mock_tpl.call_args[0][1]
        emails = [c.email for c in ctx["contacts"]]
        assert "bl@blacklisted.com" not in emails

    def test_inactive_vendor_contacts_excluded(self, gaps2_client, db_session):
        """Lines 165-168: contacts from inactive vendors are filtered out."""
        inactive = VendorCard(
            normalized_name="inactive vendor co",
            display_name="Inactive Vendor Co",
            is_active=False,
            is_blacklisted=False,
            created_at=datetime.now(UTC),
        )
        db_session.add(inactive)
        db_session.flush()
        _contact(db_session, inactive, "inactive@vendor.com", "Inactive Contact")
        db_session.commit()

        with patch("app.routers.htmx.vendors.template_response") as mock_tpl:
            mock_tpl.return_value = HTMLResponse("<div>ok</div>")
            resp = gaps2_client.get("/v2/partials/vendor-contacts")

        assert resp.status_code == 200
        ctx = mock_tpl.call_args[0][1]
        emails = [c.email for c in ctx["contacts"]]
        assert "inactive@vendor.com" not in emails

    def test_empty_search_returns_all_active_contacts(self, gaps2_client, db_session):
        """Line 169: blank search — the strip() branch is skipped, all contacts returned."""
        card = _active_card(db_session, "AllContactsVendor")
        _contact(db_session, card, "one@all.com", "One")
        _contact(db_session, card, "two@all.com", "Two")
        db_session.commit()

        with patch("app.routers.htmx.vendors.template_response") as mock_tpl:
            mock_tpl.return_value = HTMLResponse("<div>all</div>")
            resp = gaps2_client.get("/v2/partials/vendor-contacts?search=")

        assert resp.status_code == 200
        ctx = mock_tpl.call_args[0][1]
        assert ctx["total"] >= 2
