"""tests/test_quote_seeding.py — B4: shared sell-price seeding + zero-margin preflight.

Covers:
  - ``seed_sell_price`` (app.services.quote_builder_service): last-quoted price wins when
    pricing history exists for the MPN; else cost x DEFAULT_MARKUP_PCT, with margin_pct
    computed the same way ``margin_guardrail``/``quote_export_context`` already do.
  - The three quote-line creation call sites now seed through that SAME helper (no more
    sell=cost or hardcoded sell=0): offer-select create-quote (offers/crud.py), add-offer-
    to-quote and add-offers-to-draft-quote (htmx/quotes.py).
  - ``quote_preflight``'s new zero/negative-margin "pricing" warning.

Called by: pytest
Depends on: conftest.py fixtures (client, db_session, test_user, test_requisition,
    test_customer_site, test_offer, test_quote), app.services.quote_builder_service,
    app.services.quote_preflight.
"""

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Quote, QuoteLine, Requisition, User
from app.services.quote_builder_service import DEFAULT_MARKUP_PCT, seed_sell_price
from app.services.quote_preflight import quote_preflight


def _prior_sent_quote(db: Session, req: Requisition, user: User, mpn: str, sell_price: float) -> Quote:
    """A previously-sent quote for *mpn* — the pricing history seed_sell_price's last-
    quoted branch reads.

    Mirrors ``preload_last_quoted_prices``'s own tests (test_quote_builder_service.py::
    TestBuildQuoteTabData::test_sell_seed_prefers_last_quoted_price) — history is read from
    ``Quote.line_items`` JSON, not the QuoteLine ORM rows.
    """
    q = Quote(
        requisition_id=req.id,
        quote_number=f"Q-PRIOR-{mpn}",
        status="sent",
        line_items=[{"mpn": mpn, "sell_price": sell_price, "margin_pct": 50.0}],
        subtotal=sell_price,
        created_by_id=user.id,
        created_at=datetime.now(UTC),
    )
    db.add(q)
    db.commit()
    return q


def _draft_quote(db: Session, req: Requisition, site, user: User, quote_number: str) -> Quote:
    q = Quote(
        requisition_id=req.id,
        customer_site_id=site.id,
        quote_number=quote_number,
        status="draft",
        line_items=[],
        created_by_id=user.id,
        created_at=datetime.now(UTC),
    )
    db.add(q)
    db.commit()
    db.refresh(q)
    return q


# ── seed_sell_price — the shared helper ─────────────────────────────────────


class TestSeedSellPrice:
    def test_last_quoted_price_wins_when_history_exists(
        self, db_session: Session, test_requisition: Requisition, test_user: User
    ):
        _prior_sent_quote(db_session, test_requisition, test_user, "LM317T", 0.99)
        sell, margin_pct = seed_sell_price(db_session, "LM317T", 0.50)
        assert sell == pytest.approx(0.99)
        assert margin_pct == pytest.approx((0.99 - 0.50) / 0.99 * 100, rel=1e-3)

    def test_no_history_falls_back_to_cost_times_markup(self, db_session: Session):
        sell, margin_pct = seed_sell_price(db_session, "NOPART-XYZ", 0.50)
        expected_sell = round(0.50 * (1 + DEFAULT_MARKUP_PCT / 100.0), 4)
        assert sell == pytest.approx(expected_sell)
        assert margin_pct == pytest.approx((expected_sell - 0.50) / expected_sell * 100, rel=1e-3)

    def test_no_history_and_no_cost_returns_none_none(self, db_session: Session):
        assert seed_sell_price(db_session, "NOPART-XYZ", None) == (None, None)

    def test_preloaded_last_quoted_dict_is_honored_and_case_insensitive(self, db_session: Session):
        preloaded = {"LM317T": {"sell_price": 1.50}}
        sell, margin_pct = seed_sell_price(db_session, "lm317t", 1.00, last_quoted=preloaded)
        assert sell == pytest.approx(1.50)
        assert margin_pct == pytest.approx((1.50 - 1.00) / 1.50 * 100, rel=1e-3)


# ── The three quote-line creation sites ──────────────────────────────────────


class TestCreateQuoteFromOffersSeedsPrice:
    """offers/crud.py::create_quote_from_offers (offer-select "Create Quote from Selected")."""

    def test_seeds_last_quoted_price_when_history_exists(
        self, client: TestClient, db_session: Session, test_requisition: Requisition, test_user: User, test_offer
    ):
        _prior_sent_quote(db_session, test_requisition, test_user, test_offer.mpn, 0.99)
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/create-quote",
            data={"offer_ids": str(test_offer.id)},
        )
        assert resp.status_code == 200
        line = db_session.query(QuoteLine).filter(QuoteLine.offer_id == test_offer.id).first()
        assert line is not None
        assert float(line.sell_price) == pytest.approx(0.99)
        assert float(line.cost_price) == pytest.approx(0.50)

    def test_no_history_seeds_cost_times_markup(
        self, client: TestClient, db_session: Session, test_requisition: Requisition, test_offer
    ):
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/create-quote",
            data={"offer_ids": str(test_offer.id)},
        )
        assert resp.status_code == 200
        line = db_session.query(QuoteLine).filter(QuoteLine.offer_id == test_offer.id).first()
        expected_sell = round(0.50 * (1 + DEFAULT_MARKUP_PCT / 100.0), 4)
        assert float(line.sell_price) == pytest.approx(expected_sell)
        assert line.margin_pct is not None


class TestAddOfferToQuoteSeedsPrice:
    """htmx/quotes.py::add_offer_to_quote — previously hardcoded sell_price=0."""

    def test_no_history_seeds_cost_times_markup(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_customer_site,
        test_user: User,
        test_offer,
    ):
        quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user, "TEST-Q-SEED-ADD-OFFER")
        resp = client.post(
            f"/v2/partials/quotes/{quote.id}/add-offer/{test_offer.id}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        line = db_session.query(QuoteLine).filter(QuoteLine.quote_id == quote.id).first()
        expected_sell = round(0.50 * (1 + DEFAULT_MARKUP_PCT / 100.0), 4)
        assert float(line.sell_price) == pytest.approx(expected_sell)
        assert float(line.sell_price) != 0  # was hardcoded 0 before B4

    def test_seeds_last_quoted_price_when_history_exists(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_customer_site,
        test_user: User,
        test_offer,
    ):
        _prior_sent_quote(db_session, test_requisition, test_user, test_offer.mpn, 0.99)
        quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user, "TEST-Q-SEED-ADD-OFFER-HIST")
        resp = client.post(
            f"/v2/partials/quotes/{quote.id}/add-offer/{test_offer.id}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        line = db_session.query(QuoteLine).filter(QuoteLine.quote_id == quote.id).first()
        assert float(line.sell_price) == pytest.approx(0.99)


class TestAddOffersToDraftQuoteSeedsPrice:
    """htmx/quotes.py::add_offers_to_draft_quote — previously sell_price = cost_price."""

    def test_no_history_seeds_cost_times_markup(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_customer_site,
        test_user: User,
        test_offer,
    ):
        quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user, "TEST-Q-SEED-ADD-DRAFT")
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/add-offers-to-quote",
            content=json.dumps({"offer_ids": [test_offer.id], "quote_id": quote.id}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        line = db_session.query(QuoteLine).filter(QuoteLine.quote_id == quote.id).first()
        expected_sell = round(0.50 * (1 + DEFAULT_MARKUP_PCT / 100.0), 4)
        assert float(line.sell_price) == pytest.approx(expected_sell)
        assert float(line.sell_price) != float(line.cost_price)  # was sell == cost before B4

    def test_seeds_last_quoted_price_when_history_exists(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_customer_site,
        test_user: User,
        test_offer,
    ):
        _prior_sent_quote(db_session, test_requisition, test_user, test_offer.mpn, 0.99)
        quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user, "TEST-Q-SEED-ADD-DRAFT-HIST")
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/add-offers-to-quote",
            content=json.dumps({"offer_ids": [test_offer.id], "quote_id": quote.id}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        line = db_session.query(QuoteLine).filter(QuoteLine.quote_id == quote.id).first()
        assert float(line.sell_price) == pytest.approx(0.99)


# ── quote_preflight: zero/negative-margin pricing check ──────────────────────


class TestPricingPreflightCheck:
    def test_zero_sell_line_flagged(self, db_session: Session, test_quote: Quote):
        db_session.add(QuoteLine(quote_id=test_quote.id, mpn="LM317T", qty=1, cost_price=1.0, sell_price=0))
        db_session.commit()
        warnings = quote_preflight(db_session, test_quote)
        assert any(w.code == "pricing" for w in warnings)

    def test_unset_sell_line_flagged(self, db_session: Session, test_quote: Quote):
        db_session.add(QuoteLine(quote_id=test_quote.id, mpn="LM317T", qty=1, cost_price=1.0))
        db_session.commit()
        warnings = quote_preflight(db_session, test_quote)
        assert any(w.code == "pricing" for w in warnings)

    def test_sell_at_or_below_cost_flagged(self, db_session: Session, test_quote: Quote):
        db_session.add(QuoteLine(quote_id=test_quote.id, mpn="LM317T", qty=1, cost_price=2.0, sell_price=1.5))
        db_session.commit()
        warnings = quote_preflight(db_session, test_quote)
        pricing = [w for w in warnings if w.code == "pricing"]
        assert len(pricing) == 1
        assert "1 line" in pricing[0].message

    def test_healthy_line_not_flagged(self, db_session: Session, test_quote: Quote):
        db_session.add(QuoteLine(quote_id=test_quote.id, mpn="LM317T", qty=1, cost_price=1.0, sell_price=2.0))
        db_session.commit()
        warnings = quote_preflight(db_session, test_quote)
        assert not any(w.code == "pricing" for w in warnings)

    def test_no_lines_not_flagged(self, db_session: Session, test_quote: Quote):
        warnings = quote_preflight(db_session, test_quote)
        assert not any(w.code == "pricing" for w in warnings)

    def test_multiple_bad_lines_counted(self, db_session: Session, test_quote: Quote):
        db_session.add(QuoteLine(quote_id=test_quote.id, mpn="LM317T", qty=1, cost_price=1.0, sell_price=0))
        db_session.add(QuoteLine(quote_id=test_quote.id, mpn="NE555P", qty=1, cost_price=2.0, sell_price=1.0))
        db_session.add(QuoteLine(quote_id=test_quote.id, mpn="TL072CP", qty=1, cost_price=1.0, sell_price=2.0))
        db_session.commit()
        warnings = quote_preflight(db_session, test_quote)
        pricing = [w for w in warnings if w.code == "pricing"]
        assert len(pricing) == 1
        assert "2 line" in pricing[0].message
