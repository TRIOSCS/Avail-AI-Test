"""tests/test_rubric_h3_validation_lists.py — TDD tests for validation hardening (Part
1) + global customer/vendor contact list views + vendor CSV import UI (Part 2).

Covers:
- contacts_tab_create: over-length wechat_id -> 400
- edit_vendor: non-@ email rejected (400); blank display_name rejected (400)
- edit_company: invalid website URL rejected (400) via apply_company_field
- GET /v2/contacts: rep sees ONLY contacts from companies they can manage
  (account-owner / site-owner / collaborator); DENY contacts from un-manageable
  accounts; MANAGER/ADMIN see all
- GET /v2/vendor-contacts: renders + reachable; require_user
- Vendor list "Import Vendors" button renders + posts to the existing import endpoint

Called by: pytest
Depends on: conftest fixtures (client, db_session, sales_user, manager_user,
            admin_user, test_company, test_vendor_card, test_vendor_contact)
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Company, CustomerSite, SiteContact, User, VendorCard

# ── Client builder for an arbitrary user (role-scoping tests) ─────────────────


def _client_for(db_session: Session, user: User):
    """Return (TestClient, overrides) whose auth deps resolve to *user*."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    overrides = [get_db, require_user, require_admin, require_buyer, require_fresh_token]
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: user
    app.dependency_overrides[require_admin] = lambda: user
    app.dependency_overrides[require_buyer] = lambda: user
    app.dependency_overrides[require_fresh_token] = lambda: "mock-token"
    return TestClient(app), overrides


# ── Part 1.1 — contacts_tab_create wechat_id length ───────────────────────────


class TestContactsTabCreateWechatLength:
    def test_overlong_wechat_id_rejected(self, client, test_company: Company):
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/contacts",
            data={"full_name": "Jane Doe", "wechat_id": "x" * 101},
        )
        assert resp.status_code == 400
        assert "wechat" in resp.json()["error"].lower()

    def test_max_length_wechat_id_accepted(self, client, test_company: Company):
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/contacts",
            data={"full_name": "Jane Doe", "wechat_id": "x" * 100},
        )
        assert resp.status_code == 200


# ── Part 1.2 / 1.3 — edit_vendor email + display_name ─────────────────────────


class TestEditVendorValidation:
    def test_non_at_email_rejected(self, client, test_vendor_card: VendorCard):
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/edit",
            data={"display_name": "Arrow", "emails": "not-an-email"},
        )
        assert resp.status_code == 400
        assert "email" in resp.json()["error"].lower()

    def test_mixed_valid_invalid_emails_rejected(self, client, test_vendor_card: VendorCard):
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/edit",
            data={"display_name": "Arrow", "emails": "ok@arrow.com, bad"},
        )
        assert resp.status_code == 400

    def test_valid_emails_accepted(self, client, test_vendor_card: VendorCard):
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/edit",
            data={"display_name": "Arrow", "emails": "a@arrow.com, b@arrow.com"},
        )
        assert resp.status_code == 200

    def test_blank_display_name_rejected(self, client, test_vendor_card: VendorCard):
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/edit",
            data={"display_name": "   "},
        )
        assert resp.status_code == 400
        assert "name" in resp.json()["error"].lower()

    def test_partial_edit_without_display_name_leaves_name_untouched(
        self, client, db_session, test_vendor_card: VendorCard
    ):
        original = test_vendor_card.display_name
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/edit",
            data={"emails": "a@arrow.com"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert test_vendor_card.display_name == original


# ── Part 1.4 — edit_company website ───────────────────────────────────────────


class TestEditCompanyWebsite:
    def test_invalid_website_rejected(self, client, test_company: Company, test_user: User, db_session: Session):
        test_company.account_owner_id = test_user.id
        db_session.commit()
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/edit",
            data={"name": "Acme", "website": "not a url at all !!!"},
        )
        assert resp.status_code == 400
        err = resp.json()["error"].lower()
        assert "website" in err or "url" in err

    def test_valid_website_accepted_and_normalized(
        self, client, db_session: Session, test_company: Company, test_user: User
    ):
        test_company.account_owner_id = test_user.id
        db_session.commit()
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/edit",
            data={"name": "Acme", "website": "acme-electronics.com"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_company)
        assert test_company.website.startswith("https://")


# ── Part 2.1 — GET /v2/contacts role scoping ──────────────────────────────────


@pytest.fixture()
def scoped_contacts(db_session: Session, sales_user: User):
    """Two companies: one owned by sales_user (visible), one owned by a stranger
    (must be DENIED to the rep). Each has a site + one contact."""
    stranger = User(email="stranger@trioscs.com", name="Stranger", role="sales", azure_id="az-stranger")
    db_session.add(stranger)
    db_session.flush()

    mine = Company(name="Mine Corp", is_active=True, account_owner_id=sales_user.id)
    theirs = Company(name="Theirs Corp", is_active=True, account_owner_id=stranger.id)
    db_session.add_all([mine, theirs])
    db_session.flush()

    mine_site = CustomerSite(company_id=mine.id, site_name="HQ", is_active=True)
    theirs_site = CustomerSite(company_id=theirs.id, site_name="HQ", is_active=True)
    db_session.add_all([mine_site, theirs_site])
    db_session.flush()

    db_session.add_all(
        [
            SiteContact(customer_site_id=mine_site.id, full_name="Alice Mine", email="alice@mine.com"),
            SiteContact(customer_site_id=theirs_site.id, full_name="Bob Theirs", email="bob@theirs.com"),
        ]
    )
    db_session.commit()
    return {"mine": mine, "theirs": theirs, "stranger": stranger}


class TestContactsListScoping:
    def test_rep_sees_only_manageable_contacts(self, db_session, sales_user, scoped_contacts):
        client, overrides = _client_for(db_session, sales_user)
        from app.main import app

        try:
            resp = client.get("/v2/partials/contacts")
            assert resp.status_code == 200
            body = resp.text
            assert "Alice Mine" in body
            # DENY: a contact from an account the rep can't manage must NOT appear.
            assert "Bob Theirs" not in body
        finally:
            for dep in overrides:
                app.dependency_overrides.pop(dep, None)

    def test_manager_sees_all_contacts(self, db_session, manager_user, scoped_contacts):
        client, overrides = _client_for(db_session, manager_user)
        from app.main import app

        try:
            resp = client.get("/v2/partials/contacts")
            assert resp.status_code == 200
            body = resp.text
            assert "Alice Mine" in body
            assert "Bob Theirs" in body
        finally:
            for dep in overrides:
                app.dependency_overrides.pop(dep, None)

    def test_full_page_route_reachable(self, db_session, manager_user, scoped_contacts):
        client, overrides = _client_for(db_session, manager_user)
        from app.main import app

        try:
            resp = client.get("/v2/contacts")
            assert resp.status_code == 200
        finally:
            for dep in overrides:
                app.dependency_overrides.pop(dep, None)

    def test_search_filters_contacts(self, db_session, manager_user, scoped_contacts):
        client, overrides = _client_for(db_session, manager_user)
        from app.main import app

        try:
            resp = client.get("/v2/partials/contacts?search=Alice")
            assert resp.status_code == 200
            assert "Alice Mine" in resp.text
            assert "Bob Theirs" not in resp.text
        finally:
            for dep in overrides:
                app.dependency_overrides.pop(dep, None)


# ── Part 2.2 — GET /v2/vendor-contacts ────────────────────────────────────────


class TestVendorContactsList:
    def test_vendor_contacts_partial_renders(self, client, test_vendor_contact):
        resp = client.get("/v2/partials/vendor-contacts")
        assert resp.status_code == 200
        # The vendor contact's name (or its vendor) is surfaced in the list.
        assert test_vendor_contact.full_name in resp.text or "Arrow" in resp.text

    def test_vendor_contacts_full_page_reachable(self, client, test_vendor_contact):
        resp = client.get("/v2/vendor-contacts")
        assert resp.status_code == 200

    def test_vendor_contacts_requires_auth(self, unauthenticated_client):
        resp = unauthenticated_client.get("/v2/partials/vendor-contacts", follow_redirects=False)
        assert resp.status_code in (401, 403, 307)

    def test_vendor_contacts_search(self, client, test_vendor_contact):
        resp = client.get("/v2/partials/vendor-contacts?search=zzz-no-match-zzz")
        assert resp.status_code == 200
        assert test_vendor_contact.full_name not in resp.text


# ── Part 2.3 — Vendor CSV import UI ───────────────────────────────────────────


class TestVendorImportUI:
    def test_import_button_renders_on_vendor_list(self, client):
        resp = client.get("/v2/partials/vendors")
        assert resp.status_code == 200
        assert "Import Vendors" in resp.text
        # The button/modal must wire to the existing import endpoint.
        assert "/v2/partials/admin/import/vendors" in resp.text

    def test_import_endpoint_posts_csv(self, db_session, admin_user):
        client, overrides = _client_for(db_session, admin_user)
        from app.main import app

        try:
            csv_bytes = b"name,email,phone,website\nNewCo Imports,sales@newco.com,,https://newco.com\n"
            resp = client.post(
                "/v2/partials/admin/import/vendors",
                files={"file": ("vendors.csv", csv_bytes, "text/csv")},
            )
            assert resp.status_code == 200
            assert "Imported" in resp.text
            assert db_session.query(VendorCard).filter(VendorCard.display_name == "NewCo Imports").first() is not None
        finally:
            for dep in overrides:
                app.dependency_overrides.pop(dep, None)
