"""test_phase3a_ai_contacts.py — Tests for Phase 3A: AI Contact Finder in vendor detail.

Verifies: find_contacts tab rendering, AI search trigger, prospect CRUD
(save/promote/delete), and proper HTML partial responses.

Called by: pytest
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import VendorCard, VendorContact
from app.models.enrichment import ProspectContact

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def vendor_with_domain(db_session: Session) -> VendorCard:
    """A vendor card with a domain for AI contact search."""
    card = VendorCard(
        normalized_name="digikey",
        display_name="DigiKey Electronics",
        domain="digikey.com",
        emails=["sales@digikey.com"],
        sighting_count=100,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.commit()
    db_session.refresh(card)
    return card


@pytest.fixture()
def prospect(db_session: Session, vendor_with_domain: VendorCard) -> ProspectContact:
    """A prospect contact linked to a vendor."""
    pc = ProspectContact(
        vendor_card_id=vendor_with_domain.id,
        full_name="Jane Smith",
        title="Procurement Manager",
        email="jane.smith@digikey.com",
        phone="+1-555-0123",
        linkedin_url="https://linkedin.com/in/janesmith",
        source="web_search",
        confidence="medium",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(pc)
    db_session.commit()
    db_session.refresh(pc)
    return pc


# ── Tab Rendering ─────────────────────────────────────────────────────


class TestFindContactsTab:
    """Tests for the Find Contacts tab in vendor detail."""

    def test_vendor_detail_has_find_contacts_tab(self, client: TestClient, vendor_with_domain: VendorCard):
        """Vendor detail should include the Find Contacts tab button."""
        resp = client.get(f"/v2/partials/vendors/{vendor_with_domain.id}")
        assert resp.status_code == 200
        assert "Find Contacts" in resp.text

    def test_find_contacts_tab_loads(self, client: TestClient, vendor_with_domain: VendorCard):
        """Find Contacts tab partial should render successfully."""
        resp = client.get(f"/v2/partials/vendors/{vendor_with_domain.id}/tab/find_contacts")
        assert resp.status_code == 200
        assert "AI Contact Finder" in resp.text
        assert "Find Contacts" in resp.text

    def test_find_contacts_tab_shows_existing_prospects(
        self,
        client: TestClient,
        vendor_with_domain: VendorCard,
        prospect: ProspectContact,
    ):
        """Tab should show previously discovered prospect contacts."""
        resp = client.get(f"/v2/partials/vendors/{vendor_with_domain.id}/tab/find_contacts")
        assert resp.status_code == 200
        assert "Jane Smith" in resp.text
        assert "Procurement Manager" in resp.text

    def test_find_contacts_tab_empty_state(self, client: TestClient, vendor_with_domain: VendorCard):
        """Tab should show empty state when no prospects exist."""
        resp = client.get(f"/v2/partials/vendors/{vendor_with_domain.id}/tab/find_contacts")
        assert resp.status_code == 200
        assert "No AI-discovered contacts yet" in resp.text


# ── AI Search Trigger ─────────────────────────────────────────────────


class TestFindContactsSearch:
    """Tests for the AI contact search POST endpoint."""

    @patch("app.config.settings")
    def test_search_disabled_when_ai_off(self, mock_settings, client: TestClient, vendor_with_domain: VendorCard):
        """Should return disabled message when AI features are off."""
        mock_settings.ai_features_enabled = "off"
        resp = client.post(
            f"/v2/partials/vendors/{vendor_with_domain.id}/ai/find-contacts",
            data={"title_keywords": "buyer"},
        )
        assert resp.status_code == 200
        assert "AI features are currently disabled" in resp.text

    def test_search_returns_finding_poller(self, client: TestClient, vendor_with_domain: VendorCard, monkeypatch):
        """The AI search is now async: the POST returns the "Finding contacts…" poller
        immediately (never awaiting the >15s web search inline).

        The persistence / dedup / error behaviours of the background runner and the poller's
        terminal swap are covered in test_vendor_find_contacts_async.py.
        """
        from app.routers.htmx import vendors as ven
        from app.services.vendor_contact_runs import vendor_contact_runs

        vendor_contact_runs.clear(vendor_with_domain.id)
        # Neutralise the background runner (TestClient runs background tasks synchronously
        # after the response) so this test asserts only the immediate response.
        monkeypatch.setattr(ven, "_run_vendor_find_contacts", AsyncMock())

        resp = client.post(
            f"/v2/partials/vendors/{vendor_with_domain.id}/ai/find-contacts",
            data={"title_keywords": "sales"},
        )
        assert resp.status_code == 200
        assert "Finding contacts" in resp.text
        assert f"/v2/partials/vendors/{vendor_with_domain.id}/ai/find-contacts-status" in resp.text

    def test_search_404_for_missing_vendor(self, client: TestClient):
        """Should return 404 for non-existent vendor."""
        resp = client.post(
            "/v2/partials/vendors/99999/ai/find-contacts",
            data={},
        )
        assert resp.status_code == 404


# ── Prospect CRUD ─────────────────────────────────────────────────────


class TestProspectSave:
    """Tests for saving a prospect contact."""

    def test_save_marks_is_saved(
        self,
        client: TestClient,
        db_session: Session,
        vendor_with_domain: VendorCard,
        prospect: ProspectContact,
    ):
        """Saving a prospect should set is_saved=True."""
        resp = client.post(f"/v2/partials/vendors/{vendor_with_domain.id}/ai/prospect/{prospect.id}/save")
        assert resp.status_code == 200
        assert "Saved" in resp.text
        db_session.refresh(prospect)
        assert prospect.is_saved is True

    def test_save_404_for_missing_prospect(self, client: TestClient, vendor_with_domain: VendorCard):
        resp = client.post(f"/v2/partials/vendors/{vendor_with_domain.id}/ai/prospect/99999/save")
        assert resp.status_code == 404


class TestProspectPromote:
    """Tests for promoting a prospect to a VendorContact."""

    def test_promote_creates_vendor_contact(
        self,
        client: TestClient,
        db_session: Session,
        vendor_with_domain: VendorCard,
        prospect: ProspectContact,
    ):
        """Promoting should create a VendorContact record."""
        resp = client.post(f"/v2/partials/vendors/{vendor_with_domain.id}/ai/prospect/{prospect.id}/promote")
        assert resp.status_code == 200
        assert "Promoted" in resp.text

        vc = (
            db_session.query(VendorContact)
            .filter_by(vendor_card_id=vendor_with_domain.id, email="jane.smith@digikey.com")
            .first()
        )
        assert vc is not None
        assert vc.full_name == "Jane Smith"

        db_session.refresh(prospect)
        assert prospect.promoted_to_type == "vendor_contact"
        assert prospect.promoted_to_id == vc.id

    def test_promote_deduplicates_existing_contact(
        self,
        client: TestClient,
        db_session: Session,
        vendor_with_domain: VendorCard,
        prospect: ProspectContact,
    ):
        """If a VendorContact with same email exists, should update not duplicate."""
        existing = VendorContact(
            vendor_card_id=vendor_with_domain.id,
            email="jane.smith@digikey.com",
            source="manual",
        )
        db_session.add(existing)
        db_session.commit()

        client.post(f"/v2/partials/vendors/{vendor_with_domain.id}/ai/prospect/{prospect.id}/promote")

        count = (
            db_session.query(VendorContact)
            .filter_by(vendor_card_id=vendor_with_domain.id, email="jane.smith@digikey.com")
            .count()
        )
        assert count == 1

        db_session.refresh(existing)
        assert existing.full_name == "Jane Smith"

    def test_promote_404_for_missing_prospect(self, client: TestClient, vendor_with_domain: VendorCard):
        resp = client.post(f"/v2/partials/vendors/{vendor_with_domain.id}/ai/prospect/99999/promote")
        assert resp.status_code == 404


class TestProspectDelete:
    """Tests for deleting a prospect contact."""

    def test_delete_removes_prospect(
        self,
        client: TestClient,
        db_session: Session,
        vendor_with_domain: VendorCard,
        prospect: ProspectContact,
    ):
        """Deleting should remove the ProspectContact from DB."""
        resp = client.delete(f"/v2/partials/vendors/{vendor_with_domain.id}/ai/prospect/{prospect.id}")
        assert resp.status_code == 200
        assert resp.text.strip() == ""

        remaining = db_session.query(ProspectContact).filter_by(id=prospect.id).first()
        assert remaining is None

    def test_delete_404_for_missing_prospect(self, client: TestClient, vendor_with_domain: VendorCard):
        resp = client.delete(f"/v2/partials/vendors/{vendor_with_domain.id}/ai/prospect/99999")
        assert resp.status_code == 404
