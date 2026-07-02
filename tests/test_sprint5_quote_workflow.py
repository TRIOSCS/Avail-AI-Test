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


# ── Reopen button (clarity: closed quotes were un-reopenable from the detail UI) ──────


class TestQuoteReopenButton:
    def test_lost_quote_detail_shows_reopen(self, client: TestClient, db_session: Session, test_quote: Quote):
        test_quote.status = "lost"
        db_session.commit()
        resp = client.get(f"/v2/partials/quotes/{test_quote.id}", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert "Reopen" in resp.text
        assert f"/v2/partials/quotes/{test_quote.id}/reopen" in resp.text

    def test_draft_quote_detail_has_no_reopen(self, client: TestClient, draft_quote: Quote):
        resp = client.get(f"/v2/partials/quotes/{draft_quote.id}", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert "Reopen" not in resp.text


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


# ── Delete-draft button (detail UI wiring for the DELETE endpoint) ────


class TestDeleteDraftButton:
    def test_draft_detail_shows_delete_button(self, client: TestClient, draft_quote: Quote):
        """Draft detail renders a Delete-draft button wired to the DELETE endpoint."""
        resp = client.get(f"/v2/partials/quotes/{draft_quote.id}", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert "Delete draft" in resp.text
        assert f'hx-delete="/v2/partials/quotes/{draft_quote.id}"' in resp.text
        # Confirmation dialog guards the destructive action.
        assert "hx-confirm=" in resp.text

    def test_non_draft_detail_has_no_delete_button(self, client: TestClient, db_session: Session, test_quote: Quote):
        """Sent/won/lost quotes must NOT expose the Delete-draft button (endpoint
        400s)."""
        test_quote.status = "sent"
        db_session.commit()
        resp = client.get(f"/v2/partials/quotes/{test_quote.id}", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert "Delete draft" not in resp.text
        assert f'hx-delete="/v2/partials/quotes/{test_quote.id}"' not in resp.text

    def test_delete_success_redirects_to_requisitions(self, client: TestClient, draft_quote: Quote):
        """The button relies on HX-Redirect → /v2/requisitions on a successful
        delete."""
        resp = client.delete(f"/v2/partials/quotes/{draft_quote.id}", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert resp.headers.get("HX-Redirect") == "/v2/requisitions"


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


class TestReviseClonesLines:
    def test_revise_clones_quote_lines(self, client: TestClient, db_session: Session, test_quote: Quote, test_user):
        """P0/P1 (OQ-04): revising a quote must clone the parent's QuoteLine rows —
        quote_detail_partial, the sent email, the PDF, and Build-Buy-Plan all read
        QuoteLine, not line_items JSON.

        Without the clone the revision showed an empty line table and couldn't build a
        buy plan.
        """
        from app.models import QuoteLine

        # Give the parent quote a real line.
        db_session.add(
            QuoteLine(
                quote_id=test_quote.id,
                mpn="LM317T",
                manufacturer="TI",
                qty=100,
                cost_price=0.40,
                sell_price=0.55,
                margin_pct=27.3,
            )
        )
        db_session.commit()

        resp = client.post(f"/v2/partials/quotes/{test_quote.id}/revise", headers={"HX-Request": "true"})
        assert resp.status_code == 200

        # The new revision must have its own cloned QuoteLine row.
        rev = (
            db_session.query(Quote)
            .filter(Quote.requisition_id == test_quote.requisition_id, Quote.id != test_quote.id)
            .order_by(Quote.id.desc())
            .first()
        )
        assert rev is not None
        rev_lines = db_session.query(QuoteLine).filter(QuoteLine.quote_id == rev.id).all()
        assert len(rev_lines) == 1
        assert rev_lines[0].mpn == "LM317T"
        assert float(rev_lines[0].sell_price) == 0.55
        # The revision detail must render the line (not "No line items yet").
        assert "LM317T" in resp.text


class TestPricingHistoryUrl:
    def test_pricing_history_route_resolves(self, client: TestClient):
        """OQ-05: the quote-detail pricing-history panel must hit a real route. The
        template pointed at /v2/partials/quotes/pricing-history/{mpn} (404); the route
        is /v2/partials/pricing-history/{mpn}."""
        resp = client.get("/v2/partials/pricing-history/LM317T")
        assert resp.status_code == 200
        # The dead URL must 404 (proves the template no longer uses it).
        assert client.get("/v2/partials/quotes/pricing-history/LM317T").status_code == 404
