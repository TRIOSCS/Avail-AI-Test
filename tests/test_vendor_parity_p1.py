"""tests/test_vendor_parity_p1.py — TDD tests for vendor-parity P1.

Covers:
- Migration 145 schema: is_primary + custom_fields columns exist (round-trip)
- Vendor contact CRUD via /v2/partials/vendors routes + DENY tests
- Vendor contact set-primary (clears others)
- Vendor ownership claim/release UI routes + auth match service rules
- Vendor custom fields add/remove + require_user enforced

Called by: pytest
Depends on: conftest fixtures (client, unauthenticated_client, admin_user,
            test_vendor_card, test_vendor_contact, db_session)
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User, VendorCard, VendorContact
from app.models.strategic import StrategicVendor

# ── Admin client fixture ─────────────────────────────────────────────────────


@pytest.fixture()
def admin_client(db_session: Session, admin_user: User) -> TestClient:
    """TestClient that resolves require_admin to admin_user."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_user
    from app.main import app

    def _db():
        yield db_session

    def _user():
        return admin_user

    overrides = [get_db, require_user, require_admin, require_buyer]
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = _user
    app.dependency_overrides[require_admin] = _user
    app.dependency_overrides[require_buyer] = _user
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in overrides:
            app.dependency_overrides.pop(dep, None)


# ── Migration 145 round-trip ─────────────────────────────────────────────────


class TestMigration145Schema:
    """VendorContact.is_primary and VendorCard.custom_fields columns exist."""

    def test_vendor_contact_has_is_primary(self, db_session: Session, test_vendor_card: VendorCard):
        vc = VendorContact(
            vendor_card_id=test_vendor_card.id,
            email="primary@test.com",
            source="manual",
            is_primary=True,
            confidence=80,
        )
        db_session.add(vc)
        db_session.commit()
        db_session.refresh(vc)
        assert vc.is_primary is True

    def test_vendor_contact_is_primary_default_false(self, db_session: Session, test_vendor_card: VendorCard):
        vc = VendorContact(
            vendor_card_id=test_vendor_card.id,
            email="normal@test.com",
            source="manual",
            confidence=80,
        )
        db_session.add(vc)
        db_session.commit()
        db_session.refresh(vc)
        assert vc.is_primary is False

    def test_vendor_card_has_custom_fields(self, db_session: Session, test_vendor_card: VendorCard):
        test_vendor_card.custom_fields = {"Contract": "1234", "Region": "APAC"}
        db_session.commit()
        db_session.refresh(test_vendor_card)
        assert test_vendor_card.custom_fields["Contract"] == "1234"
        assert test_vendor_card.custom_fields["Region"] == "APAC"

    def test_vendor_card_custom_fields_default_empty(self, db_session: Session):
        card = VendorCard(
            normalized_name="empty-cf-vendor",
            display_name="Empty CF Vendor",
            emails=[],
            phones=[],
        )
        db_session.add(card)
        db_session.commit()
        db_session.refresh(card)
        # Should be None or {} — either is acceptable (JSONB default)
        assert card.custom_fields is None or card.custom_fields == {}

    def test_vendor_card_custom_fields_validator_cap(self, test_vendor_card: VendorCard):
        """Max 30 keys validator fires before DB write."""
        with pytest.raises(ValueError, match="max 30"):
            test_vendor_card.custom_fields = {str(i): "v" for i in range(31)}

    def test_vendor_card_custom_fields_validator_key_length(self, test_vendor_card: VendorCard):
        with pytest.raises(ValueError, match="key too long"):
            test_vendor_card.custom_fields = {"k" * 61: "value"}

    def test_vendor_card_custom_fields_validator_value_length(self, test_vendor_card: VendorCard):
        with pytest.raises(ValueError, match="value too long"):
            test_vendor_card.custom_fields = {"key": "v" * 501}


# ── Vendor Contact Add (require_user) ────────────────────────────────────────


class TestVendorContactAdd:
    def test_add_contact_success(self, client: TestClient, db_session: Session, test_vendor_card: VendorCard):
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/contacts",
            data={"email": "new@vendor.com", "full_name": "New Person", "title": "Sales"},
        )
        assert resp.status_code == 200
        # Returns the contact row HTML
        assert "new@vendor.com" in resp.text

    def test_add_first_contact_appears_in_contacts_tab(
        self, client: TestClient, db_session: Session, test_vendor_card: VendorCard
    ):
        """I2: adding the FIRST contact to a zero-contact vendor must render it in the list.

        The add-contact form targets #contacts-table-body with hx-swap="beforeend".
        Before this fix the empty-state used a <div id="contacts-table-body">, so a <tr>
        was inserted into a div — invalid HTML that browsers silently drop.  The fix
        always renders a real <tbody id="contacts-table-body">, so the <tr> is valid.

        Here we verify the contacts tab (re-rendered after the POST) actually contains
        the new email, proving the row is not silently dropped.
        """
        # Confirm the vendor starts with zero contacts.
        db_session.refresh(test_vendor_card)
        assert test_vendor_card.vendor_contacts == []

        # Add the first contact.
        add_resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/contacts",
            data={"email": "first@vendor.com", "full_name": "First Contact"},
        )
        assert add_resp.status_code == 200
        # The route returns contact_row.html — the email must be present.
        assert "first@vendor.com" in add_resp.text

        # Now render the contacts tab and confirm the contact appears.
        tab_resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/tab/contacts")
        assert tab_resp.status_code == 200
        assert "first@vendor.com" in tab_resp.text
        # The tbody must always be present (even after contacts are added).
        assert 'id="contacts-table-body"' in tab_resp.text

    def test_add_contact_missing_email_400(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/contacts",
            data={"full_name": "No Email"},
        )
        assert resp.status_code == 400

    def test_add_contact_duplicate_email_409(
        self, client: TestClient, db_session: Session, test_vendor_card: VendorCard, test_vendor_contact: VendorContact
    ):
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/contacts",
            data={"email": test_vendor_contact.email},
        )
        assert resp.status_code == 409

    def test_add_contact_not_found_vendor(self, client: TestClient):
        resp = client.post(
            "/v2/partials/vendors/99999/contacts",
            data={"email": "x@y.com"},
        )
        assert resp.status_code == 404

    def test_add_contact_anon_denied(self, unauthenticated_client: TestClient, test_vendor_card: VendorCard):
        resp = unauthenticated_client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/contacts",
            data={"email": "x@y.com"},
            follow_redirects=False,
        )
        # unauthenticated → redirect to login or 401
        assert resp.status_code in (401, 302, 307)


# ── Vendor Contact Edit (require_user) ───────────────────────────────────────


class TestVendorContactEdit:
    def test_edit_contact_success(
        self, client: TestClient, db_session: Session, test_vendor_card: VendorCard, test_vendor_contact: VendorContact
    ):
        resp = client.put(
            f"/v2/partials/vendors/{test_vendor_card.id}/contacts/{test_vendor_contact.id}",
            data={"full_name": "Updated Name", "title": "New Title"},
        )
        assert resp.status_code == 200
        assert "Updated Name" in resp.text

    def test_edit_contact_not_found(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.put(
            f"/v2/partials/vendors/{test_vendor_card.id}/contacts/99999",
            data={"full_name": "X"},
        )
        assert resp.status_code == 404

    def test_edit_contact_email_conflict(
        self, client: TestClient, db_session: Session, test_vendor_card: VendorCard, test_vendor_contact: VendorContact
    ):
        vc2 = VendorContact(
            vendor_card_id=test_vendor_card.id,
            email="other@vendor.com",
            source="manual",
            confidence=80,
        )
        db_session.add(vc2)
        db_session.commit()
        resp = client.put(
            f"/v2/partials/vendors/{test_vendor_card.id}/contacts/{test_vendor_contact.id}",
            data={"email": "other@vendor.com"},
        )
        assert resp.status_code == 409

    def test_edit_contact_anon_denied(
        self, unauthenticated_client: TestClient, test_vendor_card: VendorCard, test_vendor_contact: VendorContact
    ):
        resp = unauthenticated_client.put(
            f"/v2/partials/vendors/{test_vendor_card.id}/contacts/{test_vendor_contact.id}",
            data={"full_name": "Hax"},
            follow_redirects=False,
        )
        assert resp.status_code in (401, 302, 307)


# ── Vendor Contact Delete (require_admin) ────────────────────────────────────


class TestVendorContactDelete:
    def test_delete_contact_admin_succeeds(
        self,
        admin_client: TestClient,
        db_session: Session,
        test_vendor_card: VendorCard,
        test_vendor_contact: VendorContact,
    ):
        resp = admin_client.delete(f"/v2/partials/vendors/{test_vendor_card.id}/contacts/{test_vendor_contact.id}")
        assert resp.status_code == 200
        # Row is gone from DB
        assert db_session.get(VendorContact, test_vendor_contact.id) is None

    def test_delete_contact_not_found(self, admin_client: TestClient, test_vendor_card: VendorCard):
        resp = admin_client.delete(f"/v2/partials/vendors/{test_vendor_card.id}/contacts/99999")
        assert resp.status_code == 404

    def test_delete_contact_non_admin_denied(
        self, db_session: Session, test_user: User, test_vendor_card: VendorCard, test_vendor_contact: VendorContact
    ):
        """DENY: a non-admin user gets 403 from the require_admin-gated delete.

        Overrides require_admin to raise 403 (simulating a real non-admin caller)
        while leaving the rest of auth wired so the route runs but is blocked.
        This is a structural test: it proves require_admin is the gate and that
        a non-admin cannot delete contacts regardless of other auth state.
        """
        from fastapi import HTTPException

        from app.database import get_db
        from app.dependencies import require_admin, require_buyer, require_user
        from app.main import app

        def _db():
            yield db_session

        def _user():
            return test_user

        def _deny_admin():
            raise HTTPException(403, "Admin access required")

        overrides = [get_db, require_user, require_admin, require_buyer]
        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[require_user] = _user
        app.dependency_overrides[require_admin] = _deny_admin
        app.dependency_overrides[require_buyer] = _user
        try:
            with TestClient(app) as non_admin_client:
                resp = non_admin_client.delete(
                    f"/v2/partials/vendors/{test_vendor_card.id}/contacts/{test_vendor_contact.id}"
                )
        finally:
            for dep in overrides:
                app.dependency_overrides.pop(dep, None)

        assert resp.status_code == 403

    def test_delete_contact_anon_denied(
        self, unauthenticated_client: TestClient, test_vendor_card: VendorCard, test_vendor_contact: VendorContact
    ):
        """DENY: unauthenticated caller is rejected from admin-gated delete."""
        resp = unauthenticated_client.delete(
            f"/v2/partials/vendors/{test_vendor_card.id}/contacts/{test_vendor_contact.id}",
            follow_redirects=False,
        )
        assert resp.status_code in (401, 302, 307)


# ── Vendor Contact Set-Primary (require_user) ────────────────────────────────


class TestVendorContactSetPrimary:
    def test_set_primary_clears_others(
        self,
        client: TestClient,
        db_session: Session,
        test_vendor_card: VendorCard,
        test_vendor_contact: VendorContact,
    ):
        # Create a second contact
        vc2 = VendorContact(
            vendor_card_id=test_vendor_card.id,
            email="second@vendor.com",
            source="manual",
            confidence=80,
            is_primary=True,  # Start as primary
        )
        db_session.add(vc2)
        db_session.commit()

        # Set the first contact as primary
        resp = client.post(f"/v2/partials/vendors/{test_vendor_card.id}/contacts/{test_vendor_contact.id}/set-primary")
        assert resp.status_code == 200

        db_session.refresh(test_vendor_contact)
        db_session.refresh(vc2)
        assert test_vendor_contact.is_primary is True
        assert vc2.is_primary is False  # cleared

    def test_set_primary_not_found(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.post(f"/v2/partials/vendors/{test_vendor_card.id}/contacts/99999/set-primary")
        assert resp.status_code == 404

    def test_set_primary_anon_denied(
        self, unauthenticated_client: TestClient, test_vendor_card: VendorCard, test_vendor_contact: VendorContact
    ):
        """DENY: unauthenticated caller cannot set primary."""
        resp = unauthenticated_client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/contacts/{test_vendor_contact.id}/set-primary",
            follow_redirects=False,
        )
        assert resp.status_code in (401, 302, 307)


# ── Vendor Ownership Claim/Release ───────────────────────────────────────────


class TestVendorOwnership:
    def test_ownership_badge_get(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/ownership")
        assert resp.status_code == 200
        # Should render the Claim button since no claim exists
        assert "Claim Strategic" in resp.text or "claim" in resp.text.lower()

    def test_claim_vendor(self, client: TestClient, db_session: Session, test_vendor_card: VendorCard):
        resp = client.post(f"/v2/partials/vendors/{test_vendor_card.id}/claim")
        assert resp.status_code == 200
        # Should now show ownership info
        assert "Strategic" in resp.text or "Release" in resp.text

        claim = (
            db_session.query(StrategicVendor).filter_by(vendor_card_id=test_vendor_card.id, released_at=None).first()
        )
        assert claim is not None

    def test_claim_vendor_not_found(self, client: TestClient):
        resp = client.post("/v2/partials/vendors/99999/claim")
        assert resp.status_code == 404

    def test_release_vendor(
        self, client: TestClient, db_session: Session, test_vendor_card: VendorCard, test_user: User
    ):
        # First claim
        client.post(f"/v2/partials/vendors/{test_vendor_card.id}/claim")

        # Now release
        resp = client.post(f"/v2/partials/vendors/{test_vendor_card.id}/release")
        assert resp.status_code == 200
        assert "Claim Strategic" in resp.text or "claim" in resp.text.lower()

    def test_release_not_claimed_400(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.post(f"/v2/partials/vendors/{test_vendor_card.id}/release")
        assert resp.status_code == 400

    def test_claim_anon_denied(self, unauthenticated_client: TestClient, test_vendor_card: VendorCard):
        """DENY: unauthenticated caller cannot claim a vendor."""
        resp = unauthenticated_client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/claim",
            follow_redirects=False,
        )
        assert resp.status_code in (401, 302, 307)

    def test_release_anon_denied(self, unauthenticated_client: TestClient, test_vendor_card: VendorCard):
        """DENY: unauthenticated caller cannot release a vendor."""
        resp = unauthenticated_client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/release",
            follow_redirects=False,
        )
        assert resp.status_code in (401, 302, 307)


# ── Vendor Custom Fields ─────────────────────────────────────────────────────


class TestVendorCustomFields:
    def test_add_custom_field(self, client: TestClient, db_session: Session, test_vendor_card: VendorCard):
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/custom-fields",
            data={"label": "Contract #", "value": "C-1234"},
        )
        assert resp.status_code == 200
        assert "Contract #" in resp.text
        assert "C-1234" in resp.text

        db_session.refresh(test_vendor_card)
        assert test_vendor_card.custom_fields.get("Contract #") == "C-1234"

    def test_add_custom_field_overwrites(self, client: TestClient, db_session: Session, test_vendor_card: VendorCard):
        test_vendor_card.custom_fields = {"Tier": "Silver"}
        db_session.commit()

        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/custom-fields",
            data={"label": "Tier", "value": "Gold"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert test_vendor_card.custom_fields["Tier"] == "Gold"

    def test_add_custom_field_missing_label(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/custom-fields",
            data={"label": "", "value": "x"},
        )
        assert resp.status_code == 400

    def test_add_custom_field_not_found_vendor(self, client: TestClient):
        resp = client.post(
            "/v2/partials/vendors/99999/custom-fields",
            data={"label": "k", "value": "v"},
        )
        assert resp.status_code == 404

    def test_delete_custom_field(self, client: TestClient, db_session: Session, test_vendor_card: VendorCard):
        test_vendor_card.custom_fields = {"Region": "APAC", "Owner": "Bob"}
        db_session.commit()

        resp = client.delete(f"/v2/partials/vendors/{test_vendor_card.id}/custom-fields/Region")
        assert resp.status_code == 200
        assert "Region" not in resp.text

        db_session.refresh(test_vendor_card)
        assert "Region" not in test_vendor_card.custom_fields
        assert "Owner" in test_vendor_card.custom_fields

    def test_delete_missing_label_noop(self, client: TestClient, test_vendor_card: VendorCard):
        """Deleting a non-existent label is a no-op (200)."""
        resp = client.delete(f"/v2/partials/vendors/{test_vendor_card.id}/custom-fields/NoSuchKey")
        assert resp.status_code == 200

    def test_add_custom_field_anon_denied(self, unauthenticated_client: TestClient, test_vendor_card: VendorCard):
        """DENY: unauthenticated cannot add custom fields."""
        resp = unauthenticated_client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/custom-fields",
            data={"label": "k", "value": "v"},
            follow_redirects=False,
        )
        assert resp.status_code in (401, 302, 307)

    def test_delete_custom_field_anon_denied(self, unauthenticated_client: TestClient, test_vendor_card: VendorCard):
        """DENY: unauthenticated cannot delete custom fields."""
        resp = unauthenticated_client.delete(
            f"/v2/partials/vendors/{test_vendor_card.id}/custom-fields/Region",
            follow_redirects=False,
        )
        assert resp.status_code in (401, 302, 307)
