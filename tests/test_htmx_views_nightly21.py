"""tests/test_htmx_views_nightly21.py — Coverage for sourcing, pricing history, quote
edit.

Targets:
  - pricing_history (GET)
  - edit_quote_metadata (POST)
  - rfq_prepare_panel (GET)
  - sourcing_results_partial (GET, with filters)
  - lead_detail_partial (GET, not found)
  - v2_sourcing_page / v2_lead_detail_page (GET)

Called by: pytest autodiscovery
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

import os

os.environ["TESTING"] = "1"

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import QuoteStatus
from app.models import Requirement, Requisition, User
from app.models.quotes import Quote

# ── Helpers ───────────────────────────────────────────────────────────────


def _make_requirement(db: Session, req: Requisition, mpn: str = "LM741") -> Requirement:
    r = Requirement(
        requisition_id=req.id,
        primary_mpn=mpn,
        target_qty=50,
        created_at=datetime.now(UTC),
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _make_quote(db: Session, req: Requisition, user: User) -> Quote:
    q = Quote(
        requisition_id=req.id,
        quote_number=f"Q-{uuid.uuid4().hex[:8]}",
        status=QuoteStatus.DRAFT,
        created_by_id=user.id,
    )
    db.add(q)
    db.commit()
    db.refresh(q)
    return q


# ── Pricing History ───────────────────────────────────────────────────────


class TestPricingHistory:
    @pytest.mark.parametrize("mpn", ["NE555", "XYZ999"], ids=["no_data", "empty_mpn"])
    def test_pricing_history_ok(self, client: TestClient, mpn: str):
        resp = client.get(f"/v2/partials/pricing-history/{mpn}")
        assert resp.status_code == 200


# ── Edit Quote Metadata ───────────────────────────────────────────────────


class TestEditQuoteMetadata:
    def test_edit_payment_terms(
        self, client: TestClient, db_session: Session, test_requisition: Requisition, test_user: User
    ):
        quote = _make_quote(db_session, test_requisition, test_user)
        resp = client.post(
            f"/v2/partials/quotes/{quote.id}/edit",
            data={"payment_terms": "Net 30"},
        )
        assert resp.status_code == 200
        db_session.refresh(quote)
        assert quote.payment_terms == "Net 30"

    def test_edit_not_found(self, client: TestClient):
        resp = client.post("/v2/partials/quotes/99999/edit", data={"payment_terms": "Net 30"})
        assert resp.status_code == 404

    def test_edit_shipping_and_notes(
        self, client: TestClient, db_session: Session, test_requisition: Requisition, test_user: User
    ):
        quote = _make_quote(db_session, test_requisition, test_user)
        resp = client.post(
            f"/v2/partials/quotes/{quote.id}/edit",
            data={"shipping_terms": "FOB", "notes": "Urgent order"},
        )
        assert resp.status_code == 200


# ── Sourcing Results ──────────────────────────────────────────────────────


class TestSourcingResultsPartial:
    @pytest.mark.parametrize(
        "query",
        ["", "?confidence=green", "?sort=freshest", "?freshness=7d", "?corroborated=yes"],
        ids=["empty_results", "confidence_filter", "sort", "freshness_filter", "corroborated_filter"],
    )
    def test_sourcing_ok(self, client: TestClient, db_session: Session, test_requisition: Requisition, query: str):
        req_item = _make_requirement(db_session, test_requisition)
        resp = client.get(f"/v2/partials/sourcing/{req_item.id}{query}")
        assert resp.status_code == 200

    def test_sourcing_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/sourcing/99999")
        assert resp.status_code == 404


# ── Lead Detail ───────────────────────────────────────────────────────────


class TestLeadDetailPartial:
    def test_lead_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/sourcing/leads/99999")
        assert resp.status_code == 404


# ── Full-Page Sourcing Routes ─────────────────────────────────────────────


class TestSourcingFullPages:
    def test_v2_sourcing_page(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        req_item = _make_requirement(db_session, test_requisition)
        resp = client.get(f"/v2/sourcing/{req_item.id}")
        assert resp.status_code == 200

    def test_v2_lead_detail_page(self, client: TestClient):
        resp = client.get("/v2/sourcing/leads/1")
        assert resp.status_code == 200
