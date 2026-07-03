"""test_vendors_p2_workflow_breaks.py — regression tests for the vendors P2 cluster.

Covers three htmx workflow-break fixes:
  * vendors-reviews-target — add/delete-review target #vendor-reviews, which the
    reviews partial must actually render (else htmx:targetError aborts the swap).
  * vendors-setprimary-nesting — set-primary must return contact ROWS ONLY (tbody
    inner content), never the full contacts.html shell nested inside the <tbody>.
  * vendors-findcontacts-filter-dead — the Find Contacts button must hx-include the
    #find-contacts-form so the title-keywords filter is actually sent.

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_vendor_card, test_vendor_contact)
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import VendorCard, VendorContact

# ── vendors-reviews-target ───────────────────────────────────────────────────


class TestVendorReviewsTarget:
    def test_reviews_tab_renders_the_target_id(self, client: TestClient, test_vendor_card: VendorCard):
        """The reviews partial must render id="vendor-reviews" — the element the add and
        delete forms target.

        Without it htmx raises htmx:targetError and aborts.
        """
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/tab/reviews")
        assert resp.status_code == 200
        assert 'id="vendor-reviews"' in resp.text
        # The forms reference that exact target.
        assert 'hx-target="#vendor-reviews"' in resp.text

    def test_add_review_returns_self_replacing_partial(
        self, client: TestClient, db_session: Session, test_vendor_card: VendorCard
    ):
        """Add-review returns the full reviews.html; the target id must survive the swap
        and the swap must be outerHTML so the returned root replaces (not nests inside)
        the existing #vendor-reviews element."""
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/reviews",
            data={"rating": "5", "comment": "Excellent fulfilment"},
        )
        assert resp.status_code == 200
        assert 'id="vendor-reviews"' in resp.text  # target persists across swaps
        assert 'hx-swap="outerHTML"' in resp.text  # self-replace, no nesting
        assert "Excellent fulfilment" in resp.text

    def test_delete_review_targets_rendered_id(
        self, client: TestClient, db_session: Session, test_vendor_card: VendorCard, test_user
    ):
        """Deleting an own review returns the refreshed reviews partial with the target
        id present, so the swap lands instead of aborting."""
        from app.models import VendorReview

        review = VendorReview(
            vendor_card_id=test_vendor_card.id,
            user_id=test_user.id,
            rating=4,
            comment="Prompt replies",
        )
        db_session.add(review)
        db_session.commit()
        db_session.refresh(review)

        resp = client.delete(f"/v2/partials/vendors/{test_vendor_card.id}/reviews/{review.id}")
        assert resp.status_code == 200
        assert 'id="vendor-reviews"' in resp.text


# ── vendors-setprimary-nesting ───────────────────────────────────────────────


class TestVendorSetPrimaryNesting:
    def test_set_primary_returns_rows_only(
        self,
        client: TestClient,
        db_session: Session,
        test_vendor_card: VendorCard,
        test_vendor_contact: VendorContact,
    ):
        """Set-primary swaps innerHTML into <tbody id="contacts-table-body">, so the
        response must be table ROWS ONLY — never the full contacts.html shell (a
        <div>/<form>/<table> nested inside a <tbody> is malformed DOM)."""
        vc2 = VendorContact(
            vendor_card_id=test_vendor_card.id,
            email="second@vendor.com",
            source="manual",
            confidence=80,
            is_primary=True,
        )
        db_session.add(vc2)
        db_session.commit()

        resp = client.post(f"/v2/partials/vendors/{test_vendor_card.id}/contacts/{test_vendor_contact.id}/set-primary")
        assert resp.status_code == 200
        # Rows are present…
        assert "<tr" in resp.text
        assert test_vendor_contact.email in resp.text
        assert "second@vendor.com" in resp.text
        # …but the full-shell markup must NOT be — that was the nesting bug. (Per-row
        # inline-edit <form>s are expected; the shell's <table>/<tbody>/add-contact
        # header are the tell-tale nesting markers.)
        assert 'id="contacts-table-body"' not in resp.text
        assert "<table" not in resp.text
        assert "<thead" not in resp.text
        assert "+ Add Contact" not in resp.text

    def test_contacts_tab_still_renders_rows(
        self, client: TestClient, test_vendor_card: VendorCard, test_vendor_contact: VendorContact
    ):
        """Regression: the shared contact_rows partial still renders inside the full tab
        shell (tbody + rows both present)."""
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/tab/contacts")
        assert resp.status_code == 200
        assert 'id="contacts-table-body"' in resp.text
        assert test_vendor_contact.email in resp.text

    def test_contacts_tab_empty_state_preserved(self, client: TestClient, test_vendor_card: VendorCard):
        """Regression: the extracted empty state still renders for a zero-contact vendor."""
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/tab/contacts")
        assert resp.status_code == 200
        assert "No contacts found for this vendor" in resp.text
        assert 'id="contacts-table-body"' in resp.text


# ── vendors-findcontacts-filter-dead ─────────────────────────────────────────


@pytest.fixture()
def vendor_with_domain(db_session: Session) -> VendorCard:
    card = VendorCard(
        normalized_name="mouser",
        display_name="Mouser Electronics",
        domain="mouser.com",
        emails=["sales@mouser.com"],
        sighting_count=50,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.commit()
    db_session.refresh(card)
    return card


class TestFindContactsFilter:
    def test_button_includes_filter_form(self, client: TestClient, vendor_with_domain: VendorCard):
        """The Find Contacts button lives outside the filter <form>, so it must hx-
        include="#find-contacts-form" or the title-keywords input is never sent."""
        resp = client.get(f"/v2/partials/vendors/{vendor_with_domain.id}/tab/find_contacts")
        assert resp.status_code == 200
        assert 'id="find-contacts-form"' in resp.text
        assert 'hx-include="#find-contacts-form"' in resp.text
        # The keyword input lives inside that form.
        assert 'name="title_keywords"' in resp.text

    @patch("app.services.ai_service.enrich_contacts_websearch", new_callable=AsyncMock)
    def test_title_keywords_reach_search_service(self, mock_search, client: TestClient, vendor_with_domain: VendorCard):
        """Server side: once the form is included, its title_keywords value drives the
        AI search (proving the filter is honoured, not dropped)."""
        mock_search.return_value = []
        resp = client.post(
            f"/v2/partials/vendors/{vendor_with_domain.id}/ai/find-contacts",
            data={"title_keywords": "procurement, buyer"},
        )
        assert resp.status_code == 200
        # enrich_contacts_websearch(display_name, domain, keywords, limit=...)
        assert mock_search.await_count == 1
        assert mock_search.await_args.args[2] == "procurement, buyer"
