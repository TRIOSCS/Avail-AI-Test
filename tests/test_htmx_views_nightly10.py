"""tests/test_htmx_views_nightly10.py — Coverage for htmx_views.py shell routing and quotes routes.

Targets: shell page routing (buy-plans, excess, quotes, prospecting, etc.),
quote line CRUD, quote send/result/revise, buy plan building.

Called by: pytest autodiscovery
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

import os

os.environ["TESTING"] = "1"

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    CustomerSite,
    Offer,
    Quote,
    Requisition,
    User,
)
from app.models.quotes import QuoteLine

# ── Helpers ───────────────────────────────────────────────────────────────


def _draft_quote(db: Session, req: Requisition, site: CustomerSite, user: User, **kw) -> Quote:
    defaults = dict(
        requisition_id=req.id,
        customer_site_id=site.id,
        quote_number=f"DQ-{uuid.uuid4().hex[:8]}",
        status="draft",
        line_items=[],
        created_by_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    q = Quote(**defaults)
    db.add(q)
    db.commit()
    db.refresh(q)
    return q


def _sent_quote(db: Session, req: Requisition, site: CustomerSite, user: User, **kw) -> Quote:
    defaults = dict(
        requisition_id=req.id,
        customer_site_id=site.id,
        quote_number=f"SQ-{uuid.uuid4().hex[:8]}",
        status="sent",
        line_items=[],
        created_by_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    q = Quote(**defaults)
    db.add(q)
    db.commit()
    db.refresh(q)
    return q


def _won_quote(db: Session, req: Requisition, site: CustomerSite, user: User, **kw) -> Quote:
    defaults = dict(
        requisition_id=req.id,
        customer_site_id=site.id,
        quote_number=f"WQ-{uuid.uuid4().hex[:8]}",
        status="won",
        line_items=[],
        created_by_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    q = Quote(**defaults)
    db.add(q)
    db.commit()
    db.refresh(q)
    return q


def _quote_line(db: Session, quote: Quote, offer: Offer | None = None, **kw) -> QuoteLine:
    defaults = dict(
        quote_id=quote.id,
        offer_id=offer.id if offer else None,
        mpn="LM317T",
        manufacturer="TI",
        qty=100,
        cost_price=0.50,
        sell_price=0.75,
        margin_pct=33.33,
    )
    defaults.update(kw)
    line = QuoteLine(**defaults)
    db.add(line)
    db.commit()
    db.refresh(line)
    return line


# ── Section 1: Shell page routing ─────────────────────────────────────────


class TestShellPageRouting:
    """Covers v2_page handler for all untested path variants."""

    def test_buy_plans_list(self, client: TestClient):
        resp = client.get("/v2/buy-plans")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_buy_plans_detail(self, client: TestClient):
        resp = client.get("/v2/buy-plans/42")
        assert resp.status_code == 200

    def test_excess_list(self, client: TestClient):
        resp = client.get("/v2/excess")
        assert resp.status_code == 200

    def test_excess_detail(self, client: TestClient):
        resp = client.get("/v2/excess/7")
        assert resp.status_code == 200

    def test_quotes_list(self, client: TestClient):
        resp = client.get("/v2/quotes")
        assert resp.status_code == 200

    def test_quotes_detail(self, client: TestClient):
        resp = client.get("/v2/quotes/99")
        assert resp.status_code == 200

    def test_settings(self, client: TestClient):
        resp = client.get("/v2/settings")
        assert resp.status_code == 200

    def test_prospecting_list(self, client: TestClient):
        resp = client.get("/v2/prospecting")
        assert resp.status_code == 200

    def test_prospecting_detail(self, client: TestClient):
        resp = client.get("/v2/prospecting/3")
        assert resp.status_code == 200

    def test_proactive(self, client: TestClient):
        resp = client.get("/v2/proactive")
        assert resp.status_code == 200

    def test_materials_list(self, client: TestClient):
        resp = client.get("/v2/materials")
        assert resp.status_code == 200

    def test_materials_detail(self, client: TestClient):
        resp = client.get("/v2/materials/5")
        assert resp.status_code == 200

    def test_follow_ups(self, client: TestClient):
        resp = client.get("/v2/follow-ups")
        assert resp.status_code == 200

    def test_crm(self, client: TestClient):
        resp = client.get("/v2/crm")
        assert resp.status_code == 200

    def test_sightings(self, client: TestClient):
        resp = client.get("/v2/sightings")
        assert resp.status_code == 200

    def test_trouble_tickets_list(self, client: TestClient):
        resp = client.get("/v2/trouble-tickets")
        assert resp.status_code == 200

    def test_trouble_tickets_detail(self, client: TestClient):
        resp = client.get("/v2/trouble-tickets/12")
        assert resp.status_code == 200


# ── Section 2: Quote Line CRUD ────────────────────────────────────────────


class TestUpdateQuoteLine:
    """Covers PUT /v2/partials/quotes/{quote_id}/lines/{line_id}."""

    def test_update_mpn_and_qty(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_customer_site: CustomerSite,
        test_user: User,
    ):
        quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user)
        line = _quote_line(db_session, quote)
        resp = client.put(
            f"/v2/partials/quotes/{quote.id}/lines/{line.id}",
            data={"mpn": "NE555", "qty": "200"},
        )
        assert resp.status_code == 200
        db_session.refresh(line)
        assert line.mpn == "NE555"
        assert line.qty == 200

    def test_update_prices_recalculates_margin(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_customer_site: CustomerSite,
        test_user: User,
    ):
        quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user)
        line = _quote_line(db_session, quote)
        resp = client.put(
            f"/v2/partials/quotes/{quote.id}/lines/{line.id}",
            data={"cost_price": "1.00", "sell_price": "2.00"},
        )
        assert resp.status_code == 200
        db_session.refresh(line)
        assert float(line.sell_price) == 2.0
        assert float(line.margin_pct) == 50.0

    def test_update_invalid_qty_returns_400(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_customer_site: CustomerSite,
        test_user: User,
    ):
        quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user)
        line = _quote_line(db_session, quote)
        resp = client.put(
            f"/v2/partials/quotes/{quote.id}/lines/{line.id}",
            data={"qty": "not-a-number"},
        )
        assert resp.status_code == 400

    def test_update_invalid_cost_price_returns_400(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_customer_site: CustomerSite,
        test_user: User,
    ):
        quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user)
        line = _quote_line(db_session, quote)
        resp = client.put(
            f"/v2/partials/quotes/{quote.id}/lines/{line.id}",
            data={"cost_price": "bad"},
        )
        assert resp.status_code == 400

    def test_update_invalid_sell_price_returns_400(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_customer_site: CustomerSite,
        test_user: User,
    ):
        quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user)
        line = _quote_line(db_session, quote)
        resp = client.put(
            f"/v2/partials/quotes/{quote.id}/lines/{line.id}",
            data={"sell_price": "xyz"},
        )
        assert resp.status_code == 400

    def test_update_line_not_found(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_customer_site: CustomerSite,
        test_user: User,
    ):
        quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user)
        resp = client.put(
            f"/v2/partials/quotes/{quote.id}/lines/99999",
            data={"mpn": "ABC"},
        )
        assert resp.status_code == 404

    def test_update_line_wrong_quote_returns_404(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_customer_site: CustomerSite,
        test_user: User,
    ):
        q1 = _draft_quote(db_session, test_requisition, test_customer_site, test_user)
        q2 = _draft_quote(db_session, test_requisition, test_customer_site, test_user)
        line = _quote_line(db_session, q1)
        # Use q2.id but line belongs to q1 — should return 404
        resp = client.put(
            f"/v2/partials/quotes/{q2.id}/lines/{line.id}",
            data={"mpn": "ABC"},
        )
        assert resp.status_code == 404


class TestDeleteQuoteLine:
    """Covers DELETE /v2/partials/quotes/{quote_id}/lines/{line_id}."""

    def test_delete_line_success(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_customer_site: CustomerSite,
        test_user: User,
    ):
        quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user)
        line = _quote_line(db_session, quote)
        line_id = line.id
        resp = client.delete(f"/v2/partials/quotes/{quote.id}/lines/{line_id}")
        assert resp.status_code == 200
        assert resp.text == ""
        gone = db_session.get(QuoteLine, line_id)
        assert gone is None

    def test_delete_line_not_found(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_customer_site: CustomerSite,
        test_user: User,
    ):
        quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user)
        resp = client.delete(f"/v2/partials/quotes/{quote.id}/lines/99999")
        assert resp.status_code == 404

    def test_delete_line_wrong_quote(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_customer_site: CustomerSite,
        test_user: User,
    ):
        q1 = _draft_quote(db_session, test_requisition, test_customer_site, test_user)
        q2 = _draft_quote(db_session, test_requisition, test_customer_site, test_user)
        line = _quote_line(db_session, q1)
        resp = client.delete(f"/v2/partials/quotes/{q2.id}/lines/{line.id}")
        assert resp.status_code == 404


class TestAddQuoteLine:
    """Covers POST /v2/partials/quotes/{quote_id}/lines."""

    def test_add_line_success(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_customer_site: CustomerSite,
        test_user: User,
    ):
        quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user)
        resp = client.post(
            f"/v2/partials/quotes/{quote.id}/lines",
            data={"mpn": "LM7805", "manufacturer": "ST", "qty": "50", "cost_price": "0.25", "sell_price": "0.40"},
        )
        assert resp.status_code == 200
        lines = db_session.query(QuoteLine).filter_by(quote_id=quote.id).all()
        assert len(lines) == 1
        assert lines[0].mpn == "LM7805"

    def test_add_line_calculates_margin(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_customer_site: CustomerSite,
        test_user: User,
    ):
        quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user)
        resp = client.post(
            f"/v2/partials/quotes/{quote.id}/lines",
            data={"mpn": "NE555", "qty": "100", "cost_price": "1.0", "sell_price": "2.0"},
        )
        assert resp.status_code == 200
        line = db_session.query(QuoteLine).filter_by(quote_id=quote.id).first()
        assert line is not None
        assert float(line.margin_pct) == 50.0

    def test_add_line_quote_not_found(
        self,
        client: TestClient,
    ):
        resp = client.post(
            "/v2/partials/quotes/99999/lines",
            data={"mpn": "ABC", "qty": "1"},
        )
        assert resp.status_code == 404

    def test_add_line_zero_sell_price_no_margin(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_customer_site: CustomerSite,
        test_user: User,
    ):
        quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user)
        resp = client.post(
            f"/v2/partials/quotes/{quote.id}/lines",
            data={"mpn": "X1234", "qty": "10", "cost_price": "1.0", "sell_price": "0"},
        )
        assert resp.status_code == 200
        line = db_session.query(QuoteLine).filter_by(quote_id=quote.id).first()
        assert line is not None
        assert float(line.margin_pct) == 0.0


class TestAddOfferToQuote:
    """Covers POST /v2/partials/quotes/{quote_id}/add-offer/{offer_id}."""

    def test_add_offer_success(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_customer_site: CustomerSite,
        test_user: User,
        test_offer: Offer,
    ):
        quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user)
        resp = client.post(f"/v2/partials/quotes/{quote.id}/add-offer/{test_offer.id}")
        assert resp.status_code == 200
        lines = db_session.query(QuoteLine).filter_by(quote_id=quote.id).all()
        assert len(lines) == 1
        assert lines[0].offer_id == test_offer.id
        assert lines[0].mpn == test_offer.mpn

    def test_add_offer_quote_not_found(
        self,
        client: TestClient,
        test_offer: Offer,
    ):
        resp = client.post(f"/v2/partials/quotes/99999/add-offer/{test_offer.id}")
        assert resp.status_code == 404

    def test_add_offer_offer_not_found(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_customer_site: CustomerSite,
        test_user: User,
    ):
        quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user)
        resp = client.post(f"/v2/partials/quotes/{quote.id}/add-offer/99999")
        assert resp.status_code == 404


# ── Section 3: Quote status transitions ──────────────────────────────────


class TestSendQuoteHtmx:
    """Covers POST /v2/partials/quotes/{quote_id}/send."""

    def test_send_draft_quote(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_customer_site: CustomerSite,
        test_user: User,
    ):
        quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user)
        resp = client.post(f"/v2/partials/quotes/{quote.id}/send")
        assert resp.status_code == 200
        db_session.refresh(quote)
        assert quote.status == "sent"
        assert quote.sent_at is not None

    def test_send_quote_not_found(self, client: TestClient):
        resp = client.post("/v2/partials/quotes/99999/send")
        assert resp.status_code == 404

    def test_send_sent_quote_is_noop(
        self,
        client: TestClient,
        test_quote: Quote,
    ):
        # Same-to-same transitions are allowed (no-op). Sending an already-sent quote returns 200.
        resp = client.post(f"/v2/partials/quotes/{test_quote.id}/send")
        assert resp.status_code == 200


class TestQuoteResultHtmx:
    """Covers POST /v2/partials/quotes/{quote_id}/result."""

    def test_mark_quote_won(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_customer_site: CustomerSite,
        test_user: User,
        test_quote: Quote,
    ):
        # test_quote is "sent" — can transition to "won"
        resp = client.post(
            f"/v2/partials/quotes/{test_quote.id}/result",
            data={"result": "won", "result_reason": "Best price"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_quote)
        assert test_quote.status == "won"
        assert test_quote.result == "won"

    def test_mark_quote_lost(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_customer_site: CustomerSite,
        test_user: User,
        test_quote: Quote,
    ):
        resp = client.post(
            f"/v2/partials/quotes/{test_quote.id}/result",
            data={"result": "lost", "result_reason": "Too expensive"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_quote)
        assert test_quote.status == "lost"

    def test_result_invalid_value(
        self,
        client: TestClient,
        test_quote: Quote,
    ):
        resp = client.post(
            f"/v2/partials/quotes/{test_quote.id}/result",
            data={"result": "pending"},
        )
        assert resp.status_code == 400

    def test_result_quote_not_found(self, client: TestClient):
        resp = client.post(
            "/v2/partials/quotes/99999/result",
            data={"result": "won"},
        )
        assert resp.status_code == 404


class TestReviseQuoteHtmx:
    """Covers POST /v2/partials/quotes/{quote_id}/revise."""

    def test_revise_creates_new_quote(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_customer_site: CustomerSite,
        test_user: User,
    ):
        quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user)
        resp = client.post(f"/v2/partials/quotes/{quote.id}/revise")
        assert resp.status_code == 200
        # New revised quote should exist
        new_quotes = (
            db_session.query(Quote)
            .filter(Quote.requisition_id == test_requisition.id)
            .all()
        )
        assert len(new_quotes) == 2
        revisions = [q for q in new_quotes if q.id != quote.id]
        assert len(revisions) == 1
        assert revisions[0].status == "draft"
        assert "R" in revisions[0].quote_number

    def test_revise_not_found(self, client: TestClient):
        resp = client.post("/v2/partials/quotes/99999/revise")
        assert resp.status_code == 404


class TestApplyMarkupHtmx:
    """Covers POST /v2/partials/quotes/{quote_id}/apply-markup."""

    def test_apply_markup_updates_sell_price(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_customer_site: CustomerSite,
        test_user: User,
    ):
        quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user)
        line = _quote_line(db_session, quote, cost_price=1.0, sell_price=1.0, margin_pct=0.0)
        resp = client.post(
            f"/v2/partials/quotes/{quote.id}/apply-markup",
            data={"markup_pct": "25.0"},
        )
        assert resp.status_code == 200
        db_session.refresh(line)
        # 25% markup on $1.00 cost → $1.25 sell price
        assert float(line.sell_price) == pytest.approx(1.25, rel=1e-3)

    def test_apply_markup_default_25_pct(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_customer_site: CustomerSite,
        test_user: User,
    ):
        quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user)
        _quote_line(db_session, quote, cost_price=2.0, sell_price=2.0, margin_pct=0.0)
        # Omit markup_pct — should default to 25.0
        resp = client.post(f"/v2/partials/quotes/{quote.id}/apply-markup")
        assert resp.status_code == 200

    def test_apply_markup_no_lines(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_customer_site: CustomerSite,
        test_user: User,
    ):
        quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user)
        # No lines — should still return 200 with empty quote detail
        resp = client.post(
            f"/v2/partials/quotes/{quote.id}/apply-markup",
            data={"markup_pct": "20.0"},
        )
        assert resp.status_code == 200

    def test_apply_markup_skips_zero_cost_lines(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_customer_site: CustomerSite,
        test_user: User,
    ):
        quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user)
        line = _quote_line(db_session, quote, cost_price=0.0, sell_price=0.0, margin_pct=0.0)
        resp = client.post(
            f"/v2/partials/quotes/{quote.id}/apply-markup",
            data={"markup_pct": "30.0"},
        )
        assert resp.status_code == 200
        db_session.refresh(line)
        # sell_price should stay 0 since cost_price is 0
        assert float(line.sell_price) == 0.0


# ── Section 4: Add offers to draft quote ─────────────────────────────────


class TestAddOffersToDraftQuote:
    """Covers POST /v2/partials/requisitions/{req_id}/add-offers-to-quote."""

    def test_add_offers_success(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_customer_site: CustomerSite,
        test_user: User,
        test_offer: Offer,
    ):
        quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/add-offers-to-quote",
            content=json.dumps({"offer_ids": [test_offer.id], "quote_id": quote.id}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        lines = db_session.query(QuoteLine).filter_by(quote_id=quote.id).all()
        assert len(lines) == 1
        assert lines[0].offer_id == test_offer.id

    def test_add_offers_deduplicates(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_customer_site: CustomerSite,
        test_user: User,
        test_offer: Offer,
    ):
        quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user)
        # Add offer first time
        client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/add-offers-to-quote",
            content=json.dumps({"offer_ids": [test_offer.id], "quote_id": quote.id}),
            headers={"Content-Type": "application/json"},
        )
        # Add same offer again
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/add-offers-to-quote",
            content=json.dumps({"offer_ids": [test_offer.id], "quote_id": quote.id}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        lines = db_session.query(QuoteLine).filter_by(quote_id=quote.id).all()
        assert len(lines) == 1  # Not duplicated

    def test_add_offers_invalid_json(
        self,
        client: TestClient,
        test_requisition: Requisition,
    ):
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/add-offers-to-quote",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_add_offers_missing_offer_ids(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_customer_site: CustomerSite,
        test_user: User,
    ):
        quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/add-offers-to-quote",
            content=json.dumps({"offer_ids": [], "quote_id": quote.id}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_add_offers_missing_quote_id(
        self,
        client: TestClient,
        test_requisition: Requisition,
        test_offer: Offer,
    ):
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/add-offers-to-quote",
            content=json.dumps({"offer_ids": [test_offer.id], "quote_id": 0}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_add_offers_quote_not_found(
        self,
        client: TestClient,
        test_requisition: Requisition,
        test_offer: Offer,
    ):
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/add-offers-to-quote",
            content=json.dumps({"offer_ids": [test_offer.id], "quote_id": 99999}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 404

    def test_add_offers_non_draft_quote_rejected(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_customer_site: CustomerSite,
        test_user: User,
        test_offer: Offer,
        test_quote: Quote,
    ):
        # test_quote is "sent" — should reject adding offers
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/add-offers-to-quote",
            content=json.dumps({"offer_ids": [test_offer.id], "quote_id": test_quote.id}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_add_offers_invalid_offer_ids_type(
        self,
        client: TestClient,
        test_requisition: Requisition,
    ):
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/add-offers-to-quote",
            content=json.dumps({"offer_ids": ["not-int"], "quote_id": 1}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400


# ── Section 5: Build buy plan ─────────────────────────────────────────────


class TestBuildBuyPlanHtmx:
    """Covers POST /v2/partials/quotes/{quote_id}/build-buy-plan."""

    def test_build_buy_plan_quote_not_found(self, client: TestClient):
        resp = client.post("/v2/partials/quotes/99999/build-buy-plan")
        assert resp.status_code == 404

    def test_build_buy_plan_non_won_quote_rejected(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_customer_site: CustomerSite,
        test_user: User,
    ):
        quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user)
        resp = client.post(f"/v2/partials/quotes/{quote.id}/build-buy-plan")
        assert resp.status_code == 400

    def test_build_buy_plan_success(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_customer_site: CustomerSite,
        test_user: User,
    ):
        from app.models.buy_plan import BuyPlan

        quote = _won_quote(db_session, test_requisition, test_customer_site, test_user)
        mock_plan = BuyPlan(
            quote_id=quote.id,
            requisition_id=test_requisition.id,
            status="draft",
            submitted_by_id=test_user.id,
        )
        db_session.add(mock_plan)
        db_session.commit()
        db_session.refresh(mock_plan)

        # build_buy_plan is a lazy import inside the function — patch at source
        with patch("app.services.buyplan_builder.build_buy_plan", return_value=mock_plan):
            resp = client.post(f"/v2/partials/quotes/{quote.id}/build-buy-plan")
        assert resp.status_code == 200

    def test_build_buy_plan_builder_raises_value_error(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_customer_site: CustomerSite,
        test_user: User,
    ):
        quote = _won_quote(db_session, test_requisition, test_customer_site, test_user)
        # build_buy_plan is a lazy import inside the function — patch at source
        with patch("app.services.buyplan_builder.build_buy_plan", side_effect=ValueError("No offers")):
            resp = client.post(f"/v2/partials/quotes/{quote.id}/build-buy-plan")
        assert resp.status_code == 400
