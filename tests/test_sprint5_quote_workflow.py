"""test_sprint5_quote_workflow.py — Tests for Sprint 5 quote workflow completion.

Verifies: Quote preview, delete draft, reopen, recent terms, pricing history,
edit quote metadata.

Called by: pytest
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Offer, Quote, Requisition, User


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def draft_quote(db_session: Session, test_requisition: Requisition, test_customer_site, test_user: User) -> Quote:
    """A draft quote for testing delete/edit."""
    q = Quote(
        requisition_id=test_requisition.id,
        customer_site_id=test_customer_site.id,
        quote_number="TEST-Q-DRAFT-001",
        status="draft",
        line_items=[],
        payment_terms="Net 30",
        shipping_terms="FOB Origin",
        created_by_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(q)
    db_session.commit()
    db_session.refresh(q)
    return q


# ── Quote Preview ────────────────────────────────────────────────────


class TestQuotePreview:
    def test_preview_renders(self, client: TestClient, test_quote: Quote):
        resp = client.post(
            f"/v2/partials/quotes/{test_quote.id}/preview",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Quote Preview" in resp.text
        assert test_quote.quote_number in resp.text

    def test_preview_nonexistent(self, client: TestClient):
        resp = client.post(
            "/v2/partials/quotes/99999/preview",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404


# ── Delete Quote ─────────────────────────────────────────────────────


class TestDeleteQuote:
    def test_delete_draft(self, client: TestClient, draft_quote: Quote, db_session: Session):
        qid = draft_quote.id
        resp = client.delete(
            f"/v2/partials/quotes/{qid}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert db_session.get(Quote, qid) is None

    def test_delete_non_draft_rejected(self, client: TestClient, test_quote: Quote):
        # test_quote has status="sent"
        resp = client.delete(
            f"/v2/partials/quotes/{test_quote.id}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400

    def test_delete_nonexistent(self, client: TestClient):
        resp = client.delete(
            "/v2/partials/quotes/99999",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404


# ── Reopen Quote ─────────────────────────────────────────────────────


class TestReopenQuote:
    def test_reopen_sent_quote(self, client: TestClient, test_quote: Quote, db_session: Session):
        # test_quote has status="sent"
        resp = client.post(
            f"/v2/partials/quotes/{test_quote.id}/reopen",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_quote)
        assert test_quote.status == "draft"

    def test_reopen_draft_rejected(self, client: TestClient, draft_quote: Quote):
        resp = client.post(
            f"/v2/partials/quotes/{draft_quote.id}/reopen",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400

    def test_reopen_nonexistent(self, client: TestClient):
        resp = client.post(
            "/v2/partials/quotes/99999/reopen",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404


# ── Recent Terms ─────────────────────────────────────────────────────


class TestRecentTerms:
    def test_returns_datalist(self, client: TestClient, draft_quote: Quote):
        resp = client.get(
            "/v2/partials/quotes/recent-terms",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "payment-terms" in resp.text
        assert "shipping-terms" in resp.text

    def test_includes_existing_terms(self, client: TestClient, draft_quote: Quote):
        resp = client.get(
            "/v2/partials/quotes/recent-terms",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Net 30" in resp.text
        assert "FOB Origin" in resp.text


# ── Pricing History ──────────────────────────────────────────────────


class TestPricingHistory:
    def test_pricing_history_with_data(self, client: TestClient, test_offer: Offer):
        resp = client.get(
            f"/v2/partials/pricing-history/{test_offer.mpn}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Pricing History" in resp.text

    def test_pricing_history_empty(self, client: TestClient):
        resp = client.get(
            "/v2/partials/pricing-history/NONEXISTENT-MPN-999",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "No pricing data" in resp.text


# ── Edit Quote Metadata ──────────────────────────────────────────────


class TestEditQuoteMetadata:
    def test_edit_terms(self, client: TestClient, draft_quote: Quote, db_session: Session):
        resp = client.post(
            f"/v2/partials/quotes/{draft_quote.id}/edit",
            data={"payment_terms": "Net 60", "shipping_terms": "DDP"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        db_session.refresh(draft_quote)
        assert draft_quote.payment_terms == "Net 60"
        assert draft_quote.shipping_terms == "DDP"

    def test_edit_notes(self, client: TestClient, draft_quote: Quote, db_session: Session):
        resp = client.post(
            f"/v2/partials/quotes/{draft_quote.id}/edit",
            data={"notes": "Customer needs by Friday"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        db_session.refresh(draft_quote)
        assert draft_quote.notes == "Customer needs by Friday"

    def test_edit_nonexistent(self, client: TestClient):
        resp = client.post(
            "/v2/partials/quotes/99999/edit",
            data={"notes": "Ghost"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404
