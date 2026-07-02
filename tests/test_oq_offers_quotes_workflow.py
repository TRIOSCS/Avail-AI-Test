"""test_oq_offers_quotes_workflow.py — P2 offers/quotes workflow-break fixes.

Covers the production-polish audit findings:
  - OQ-06: the offers-tab select-all now checks/unchecks the row offer_ids checkboxes
    that "Create Quote from Selected" reads via hx-include (else it 400s).
  - OQ-12: every quote-line mutation (add / edit / delete / add-offer / apply-markup)
    recomputes the quote header totals so quote.subtotal (emailed by quote_send) no
    longer drifts from the visible lines.

Called by: pytest
Depends on: tests/conftest.py fixtures (client, db_session, test_requisition,
test_customer_site, test_user, test_offer), app.routers.htmx.quotes / .offers.
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Offer, Quote, QuoteLine, Requisition, User


@pytest.fixture()
def draft_quote(
    db_session: Session,
    test_requisition: Requisition,
    test_customer_site,
    test_user: User,
) -> Quote:
    """A fresh draft quote (no totals yet) for line-mutation tests."""
    q = Quote(
        requisition_id=test_requisition.id,
        customer_site_id=test_customer_site.id,
        quote_number="TEST-Q-OQ12-001",
        status="draft",
        line_items=[],
        created_by_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(q)
    db_session.commit()
    db_session.refresh(q)
    return q


def _add_line(db: Session, quote: Quote, **kw) -> QuoteLine:
    line = QuoteLine(
        quote_id=quote.id,
        mpn=kw.get("mpn", "LM317T"),
        qty=kw.get("qty", 1),
        cost_price=kw.get("cost_price", 0.0),
        sell_price=kw.get("sell_price", 0.0),
        margin_pct=kw.get("margin_pct", 0.0),
    )
    db.add(line)
    db.commit()
    db.refresh(line)
    return line


# ── OQ-06: select-all drives the offer_ids checkboxes hx-include reads ──────────


class TestSelectAllChecksRowCheckboxes:
    def test_select_all_toggles_offer_id_checkboxes(self, client: TestClient, test_offer: Offer):
        """The header select-all @change must set .checked on the row offer_ids inputs
        so hx-include="[name='offer_ids']" actually sends them (OQ-06)."""
        resp = client.get(
            f"/v2/partials/requisitions/{test_offer.requisition_id}/tab/offers",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        # The select-all handler now scopes to the offers form and mirrors $el.checked.
        assert "input[name=offer_ids]" in resp.text
        assert "c.checked = $el.checked" in resp.text

    def test_create_quote_succeeds_with_offer_ids(self, client: TestClient, db_session: Session, test_offer: Offer):
        """With offer_ids posted (what a fixed select-all produces) create-quote builds
        a draft quote rather than 400ing."""
        resp = client.post(
            f"/v2/partials/requisitions/{test_offer.requisition_id}/create-quote",
            data={"offer_ids": str(test_offer.id)},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        quote = (
            db_session.query(Quote)
            .filter(Quote.requisition_id == test_offer.requisition_id)
            .order_by(Quote.id.desc())
            .first()
        )
        assert quote is not None
        assert quote.status == "draft"


# ── OQ-12: line mutations keep the header totals in sync ────────────────────────


class TestLineMutationsRecalcTotals:
    def test_add_line_recalcs(self, client: TestClient, db_session: Session, draft_quote: Quote):
        resp = client.post(
            f"/v2/partials/quotes/{draft_quote.id}/lines",
            data={"mpn": "LM317T", "qty": "10", "cost_price": "1.00", "sell_price": "2.00"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        db_session.refresh(draft_quote)
        assert draft_quote.subtotal == pytest.approx(20.0)  # 10 * 2.00
        assert draft_quote.total_cost == pytest.approx(10.0)  # 10 * 1.00
        assert draft_quote.total_margin_pct == pytest.approx(50.0)

    def test_update_line_recalcs(self, client: TestClient, db_session: Session, draft_quote: Quote):
        line = _add_line(db_session, draft_quote, qty=5, cost_price=1.0, sell_price=2.0)
        resp = client.put(
            f"/v2/partials/quotes/{draft_quote.id}/lines/{line.id}",
            data={"qty": "10", "cost_price": "1.00", "sell_price": "3.00"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        db_session.refresh(draft_quote)
        assert draft_quote.subtotal == pytest.approx(30.0)  # 10 * 3.00
        assert draft_quote.total_cost == pytest.approx(10.0)

    def test_delete_line_recalcs(self, client: TestClient, db_session: Session, draft_quote: Quote):
        keep = _add_line(db_session, draft_quote, mpn="KEEP", qty=5, cost_price=1.0, sell_price=2.0)
        drop = _add_line(db_session, draft_quote, mpn="DROP", qty=2, cost_price=1.0, sell_price=4.0)
        # Seed a stale header to prove the delete refreshes it.
        draft_quote.subtotal = 999.0
        db_session.commit()
        resp = client.delete(
            f"/v2/partials/quotes/{draft_quote.id}/lines/{drop.id}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        db_session.refresh(draft_quote)
        assert draft_quote.subtotal == pytest.approx(10.0)  # only KEEP: 5 * 2.00
        assert db_session.get(QuoteLine, drop.id) is None
        assert db_session.get(QuoteLine, keep.id) is not None

    def test_apply_markup_recalcs(self, client: TestClient, db_session: Session, draft_quote: Quote):
        _add_line(db_session, draft_quote, qty=10, cost_price=1.0, sell_price=1.0)
        resp = client.post(
            f"/v2/partials/quotes/{draft_quote.id}/apply-markup",
            data={"markup_pct": "25"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        db_session.refresh(draft_quote)
        # sell = cost * 1.25 = 1.25 per unit, qty 10 => subtotal 12.5
        assert draft_quote.subtotal == pytest.approx(12.5)
        assert draft_quote.total_cost == pytest.approx(10.0)

    def test_add_offer_recalcs(self, client: TestClient, db_session: Session, draft_quote: Quote, test_offer: Offer):
        resp = client.post(
            f"/v2/partials/quotes/{draft_quote.id}/add-offer/{test_offer.id}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        db_session.refresh(draft_quote)
        # add-offer seeds sell_price=0 (buyer prices later); cost = 0.50 * 1000 = 500.
        assert draft_quote.total_cost == pytest.approx(500.0)
