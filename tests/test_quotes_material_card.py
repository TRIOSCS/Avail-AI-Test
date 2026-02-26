"""Tests for Phase 5: Quotes Integration with Material Cards.

Verifies:
1. quote_to_dict enriches line items with MaterialCard description/category
2. Pricing history matches by material_card_id
3. Quote creation copies material_card_id from offers
4. Quote creation resolves material_card for manual line items
5. Pricing history legacy fallback (MPN string match for items without card_id)
6. Pricing history multi-item skip logic
7. Error resilience (enrichment/resolve failures don't break endpoints)
8. _preload_last_quoted_prices card_id keying
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import (
    MaterialCard,
    Offer,
    Quote,
)
from app.routers.crm import _preload_last_quoted_prices, quote_to_dict

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def material_card(db_session: Session) -> MaterialCard:
    mc = MaterialCard(
        normalized_mpn="lm317t",
        display_mpn="LM317T",
        manufacturer="Texas Instruments",
        description="Adjustable Voltage Regulator, 1.5A",
        category="Semiconductors",
    )
    db_session.add(mc)
    db_session.commit()
    return mc


@pytest.fixture()
def material_card_2(db_session: Session) -> MaterialCard:
    mc = MaterialCard(
        normalized_mpn="ne555p",
        display_mpn="NE555P",
        manufacturer="Texas Instruments",
        description="Precision Timer IC",
        category="Semiconductors",
    )
    db_session.add(mc)
    db_session.commit()
    return mc


def _mock_quote(line_items=None, **overrides):
    """Build a mock Quote for unit tests (no DB)."""
    q = MagicMock()
    q.id = overrides.get("id", 1)
    q.requisition_id = overrides.get("requisition_id", 10)
    q.customer_site_id = overrides.get("customer_site_id", 5)
    q.quote_number = overrides.get("quote_number", "Q-2026-0099")
    q.revision = overrides.get("revision", 1)
    q.line_items = line_items or []
    q.subtotal = overrides.get("subtotal", 100.0)
    q.total_cost = overrides.get("total_cost", 80.0)
    q.total_margin_pct = overrides.get("total_margin_pct", 20.0)
    q.payment_terms = overrides.get("payment_terms", "Net 30")
    q.shipping_terms = overrides.get("shipping_terms", "FOB")
    q.validity_days = overrides.get("validity_days", 30)
    q.notes = overrides.get("notes", None)
    q.status = overrides.get("status", "draft")
    q.sent_at = overrides.get("sent_at", None)
    q.result = overrides.get("result", None)
    q.result_reason = overrides.get("result_reason", None)
    q.result_notes = overrides.get("result_notes", None)
    q.result_at = overrides.get("result_at", None)
    q.won_revenue = overrides.get("won_revenue", None)
    q.created_at = overrides.get("created_at", datetime(2026, 2, 1, tzinfo=timezone.utc))
    q.updated_at = overrides.get("updated_at", datetime(2026, 2, 1, tzinfo=timezone.utc))
    created_by = MagicMock()
    created_by.name = "Mike"
    q.created_by = created_by
    site = MagicMock()
    site.site_name = "HQ"
    site.contact_name = "John"
    site.contact_email = "john@acme.com"
    site.site_contacts = []
    company = MagicMock()
    company.name = "Acme Corp"
    company.domain = "acme.com"
    site.company = company
    q.customer_site = site
    return q


# ── quote_to_dict enrichment ─────────────────────────────────────────


def test_quote_to_dict_no_db_returns_raw_items():
    """Without db param, line_items are returned as-is."""
    items = [{"mpn": "LM317T", "material_card_id": 99}]
    q = _mock_quote(line_items=items)
    d = quote_to_dict(q)
    assert d["line_items"] == items
    assert "description" not in d["line_items"][0]


def test_quote_to_dict_with_db_enriches_items(db_session, material_card):
    """With db param, line_items get description/category from MaterialCard."""
    items = [
        {"mpn": "LM317T", "material_card_id": material_card.id, "qty": 100},
    ]
    q = _mock_quote(line_items=items)
    d = quote_to_dict(q, db=db_session)
    assert len(d["line_items"]) == 1
    assert d["line_items"][0]["description"] == "Adjustable Voltage Regulator, 1.5A"
    assert d["line_items"][0]["category"] == "Semiconductors"
    assert d["line_items"][0]["mpn"] == "LM317T"
    assert d["line_items"][0]["qty"] == 100


def test_quote_to_dict_enriches_multiple_items(db_session, material_card, material_card_2):
    """Multiple line items each get their own card's data."""
    items = [
        {"mpn": "LM317T", "material_card_id": material_card.id},
        {"mpn": "NE555P", "material_card_id": material_card_2.id},
    ]
    q = _mock_quote(line_items=items)
    d = quote_to_dict(q, db=db_session)
    assert d["line_items"][0]["description"] == "Adjustable Voltage Regulator, 1.5A"
    assert d["line_items"][1]["description"] == "Precision Timer IC"


def test_quote_to_dict_preserves_existing_description(db_session, material_card):
    """If line item already has a description, don't overwrite it (setdefault)."""
    items = [
        {
            "mpn": "LM317T",
            "material_card_id": material_card.id,
            "description": "Custom override desc",
        },
    ]
    q = _mock_quote(line_items=items)
    d = quote_to_dict(q, db=db_session)
    assert d["line_items"][0]["description"] == "Custom override desc"


def test_quote_to_dict_item_without_card_id(db_session, material_card):
    """Line items without material_card_id are returned unchanged."""
    items = [
        {"mpn": "LM317T", "material_card_id": material_card.id},
        {"mpn": "UNKNOWN-PART", "qty": 50},
    ]
    q = _mock_quote(line_items=items)
    d = quote_to_dict(q, db=db_session)
    assert d["line_items"][0]["description"] == "Adjustable Voltage Regulator, 1.5A"
    assert "description" not in d["line_items"][1]


def test_quote_to_dict_empty_line_items(db_session):
    """Empty line_items with db param doesn't crash."""
    q = _mock_quote(line_items=[])
    d = quote_to_dict(q, db=db_session)
    assert d["line_items"] == []


def test_quote_to_dict_none_line_items(db_session):
    """None line_items with db param doesn't crash."""
    q = _mock_quote(line_items=None)
    d = quote_to_dict(q, db=db_session)
    assert d["line_items"] == []


# ── Pricing history endpoint ─────────────────────────────────────────


class TestPricingHistory:
    def test_pricing_history_returns_material_card_id(
        self, client, db_session, test_requisition, test_customer_site, test_user, material_card
    ):
        """Pricing history response includes material_card_id."""
        q = Quote(
            requisition_id=test_requisition.id,
            customer_site_id=test_customer_site.id,
            quote_number="Q-2026-PH01",
            status="sent",
            sent_at=datetime.now(timezone.utc),
            line_items=[{"mpn": "LM317T", "material_card_id": material_card.id, "sell_price": 2.50, "qty": 100}],
            subtotal=250.0,
            created_by_id=test_user.id,
        )
        db_session.add(q)
        db_session.commit()

        resp = client.get("/api/pricing-history/LM317T")
        assert resp.status_code == 200
        data = resp.json()
        assert data["material_card_id"] == material_card.id
        assert len(data["history"]) >= 1
        assert data["history"][0]["sell_price"] == 2.50

    def test_pricing_history_matches_by_card_id(
        self, client, db_session, test_requisition, test_customer_site, test_user, material_card
    ):
        """Pricing history matches line items via material_card_id even with variant MPN strings."""
        q = Quote(
            requisition_id=test_requisition.id,
            customer_site_id=test_customer_site.id,
            quote_number="Q-2026-PH02",
            status="sent",
            sent_at=datetime.now(timezone.utc),
            line_items=[
                {"mpn": "LM317T/NOPB", "material_card_id": material_card.id, "sell_price": 3.00, "qty": 50},
            ],
            subtotal=150.0,
            created_by_id=test_user.id,
        )
        db_session.add(q)
        db_session.commit()

        # Search by the canonical MPN — should still find the variant via card_id
        resp = client.get("/api/pricing-history/LM317T")
        assert resp.status_code == 200
        data = resp.json()
        assert data["material_card_id"] == material_card.id
        assert len(data["history"]) >= 1

    def test_pricing_history_no_card(self, client, db_session):
        """Unknown MPN returns empty history with null material_card_id."""
        resp = client.get("/api/pricing-history/ZZZZZZZ")
        assert resp.status_code == 200
        data = resp.json()
        assert data["material_card_id"] is None
        assert data["history"] == []


# ── Quote creation with material_card_id ─────────────────────────────


class TestQuoteCreationMaterialCard:
    def test_create_quote_from_offers_copies_card_id(
        self, client, db_session, test_requisition, test_customer_site, test_user, material_card
    ):
        """When creating a quote from offers, material_card_id is copied to line items."""
        test_requisition.customer_site_id = test_customer_site.id
        offer = Offer(
            requisition_id=test_requisition.id,
            vendor_name="Arrow Electronics",
            vendor_name_normalized="arrow electronics",
            mpn="LM317T",
            material_card_id=material_card.id,
            qty_available=500,
            unit_price=1.20,
            entered_by_id=test_user.id,
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.commit()

        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/quote",
            json={"offer_ids": [offer.id]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["line_items"]) == 1
        assert data["line_items"][0]["material_card_id"] == material_card.id

    def test_create_quote_resolves_card_for_manual_items(
        self, client, db_session, test_requisition, test_customer_site, test_user, material_card
    ):
        """Manual line items without material_card_id get resolved via MPN."""
        test_requisition.customer_site_id = test_customer_site.id
        db_session.commit()

        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/quote",
            json={
                "line_items": [
                    {"mpn": "LM317T", "qty": 100, "sell_price": 2.00, "cost_price": 1.00},
                ],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["line_items"]) == 1
        assert data["line_items"][0]["material_card_id"] == material_card.id

    def test_create_quote_auto_creates_card_for_new_mpn(
        self, client, db_session, test_requisition, test_customer_site, test_user
    ):
        """If no MaterialCard exists for the MPN, one is auto-created."""
        test_requisition.customer_site_id = test_customer_site.id
        db_session.commit()

        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/quote",
            json={
                "line_items": [
                    {"mpn": "NEWPART-XYZ-123", "qty": 10, "sell_price": 5.00, "cost_price": 3.00},
                ],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["line_items"][0]["material_card_id"] is not None

        # Verify card was created in DB
        card = db_session.query(MaterialCard).filter(
            MaterialCard.normalized_mpn == "newpartxyz123"
        ).first()
        assert card is not None
        assert card.display_mpn == "NEWPART-XYZ-123"


# ── Pricing history: legacy fallback + multi-item ────────────────────


class TestPricingHistoryEdgeCases:
    def test_pricing_history_legacy_mpn_string_fallback(
        self, client, db_session, test_requisition, test_customer_site, test_user
    ):
        """Legacy quotes without material_card_id still match via MPN string."""
        q = Quote(
            requisition_id=test_requisition.id,
            customer_site_id=test_customer_site.id,
            quote_number="Q-2026-LEG01",
            status="sent",
            sent_at=datetime.now(timezone.utc),
            line_items=[{"mpn": "LM317T", "sell_price": 1.75, "qty": 200}],
            subtotal=350.0,
            created_by_id=test_user.id,
        )
        db_session.add(q)
        db_session.commit()

        resp = client.get("/api/pricing-history/LM317T")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["history"]) >= 1
        assert data["history"][0]["sell_price"] == 1.75

    def test_pricing_history_multi_item_picks_correct_match(
        self, client, db_session, test_requisition, test_customer_site, test_user, material_card
    ):
        """In a multi-item quote, only the matching item is returned."""
        q = Quote(
            requisition_id=test_requisition.id,
            customer_site_id=test_customer_site.id,
            quote_number="Q-2026-MULTI",
            status="sent",
            sent_at=datetime.now(timezone.utc),
            line_items=[
                {"mpn": "UNRELATED-PART", "sell_price": 99.99, "qty": 10},
                {"mpn": "LM317T", "material_card_id": material_card.id, "sell_price": 3.25, "qty": 50},
            ],
            subtotal=200.0,
            created_by_id=test_user.id,
        )
        db_session.add(q)
        db_session.commit()

        resp = client.get("/api/pricing-history/LM317T")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["history"]) >= 1
        assert data["history"][0]["sell_price"] == 3.25
        assert data["history"][0]["qty"] == 50

    def test_pricing_history_aggregation_with_card_matching(
        self, client, db_session, test_requisition, test_customer_site, test_user, material_card
    ):
        """avg_price and price_range computed correctly with card-based matching."""
        for i, price in enumerate([2.00, 4.00]):
            q = Quote(
                requisition_id=test_requisition.id,
                customer_site_id=test_customer_site.id,
                quote_number=f"Q-2026-AGG{i}",
                status="sent",
                sent_at=datetime.now(timezone.utc),
                line_items=[{"mpn": "LM317T", "material_card_id": material_card.id, "sell_price": price, "qty": 100}],
                subtotal=price * 100,
                created_by_id=test_user.id,
            )
            db_session.add(q)
        db_session.commit()

        resp = client.get("/api/pricing-history/LM317T")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["history"]) == 2
        assert data["avg_price"] == 3.0
        assert data["price_range"] == [2.0, 4.0]


# ── quote_to_dict edge cases ────────────────────────────────────────


def test_quote_to_dict_db_but_no_card_ids(db_session):
    """When db is passed but no items have material_card_id, items are unchanged."""
    items = [
        {"mpn": "PARTX", "qty": 10},
        {"mpn": "PARTY", "qty": 20},
    ]
    q = _mock_quote(line_items=items)
    d = quote_to_dict(q, db=db_session)
    assert len(d["line_items"]) == 2
    assert d["line_items"][0]["mpn"] == "PARTX"
    assert "description" not in d["line_items"][0]


def test_quote_to_dict_enrichment_failure_returns_raw_items(db_session, material_card):
    """If MaterialCard query raises, raw items are returned instead of 500."""
    items = [{"mpn": "LM317T", "material_card_id": material_card.id}]
    q = _mock_quote(line_items=items)
    broken_db = MagicMock()
    broken_db.query.side_effect = Exception("DB connection lost")
    d = quote_to_dict(q, db=broken_db)
    assert len(d["line_items"]) == 1
    assert d["line_items"][0]["mpn"] == "LM317T"


# ── Error resilience ────────────────────────────────────────────────


class TestQuoteCreationErrorResilience:
    def test_create_quote_survives_resolve_failure(
        self, client, db_session, test_requisition, test_customer_site, test_user
    ):
        """Quote creation succeeds even if resolve_material_card throws."""
        test_requisition.customer_site_id = test_customer_site.id
        db_session.commit()

        with patch(
            "app.search_service.resolve_material_card",
            side_effect=Exception("DB error"),
        ):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/quote",
                json={
                    "line_items": [
                        {"mpn": "FAILPART-123", "qty": 5, "sell_price": 10.00, "cost_price": 7.00},
                    ],
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["line_items"]) == 1
        assert data["line_items"][0]["mpn"] == "FAILPART-123"
        # material_card_id should be absent/None since resolve failed
        assert data["line_items"][0].get("material_card_id") is None


# ── _preload_last_quoted_prices card_id keying ──────────────────────


def test_preload_includes_card_id_keys():
    """_preload_last_quoted_prices indexes by both MPN string and card:id."""
    q = MagicMock()
    q.line_items = [
        {"mpn": "LM317T", "material_card_id": 42, "sell_price": 2.50, "margin_pct": 15.0},
        {"mpn": "NE555P", "sell_price": 1.00, "margin_pct": 10.0},
    ]
    q.quote_number = "Q-2026-0099"
    q.sent_at = datetime(2026, 2, 1, tzinfo=timezone.utc)
    q.created_at = datetime(2026, 1, 28, tzinfo=timezone.utc)
    q.result = "won"
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [q]

    result = _preload_last_quoted_prices(db)
    # MPN key
    assert "LM317T" in result
    assert result["LM317T"]["sell_price"] == 2.50
    # card: key
    assert "card:42" in result
    assert result["card:42"]["sell_price"] == 2.50
    # NE555P has no card_id — only MPN key
    assert "NE555P" in result
    assert "card:None" not in result


def test_preload_card_id_dedup():
    """First occurrence of a card_id wins (most recent quote)."""
    q1 = MagicMock()
    q1.line_items = [{"mpn": "LM317T/NOPB", "material_card_id": 42, "sell_price": 3.00, "margin_pct": 20.0}]
    q1.quote_number = "Q-2026-0010"
    q1.sent_at = datetime(2026, 2, 10, tzinfo=timezone.utc)
    q1.created_at = datetime(2026, 2, 9, tzinfo=timezone.utc)
    q1.result = "sent"

    q2 = MagicMock()
    q2.line_items = [{"mpn": "LM317T", "material_card_id": 42, "sell_price": 2.00, "margin_pct": 10.0}]
    q2.quote_number = "Q-2026-0005"
    q2.sent_at = datetime(2026, 1, 15, tzinfo=timezone.utc)
    q2.created_at = datetime(2026, 1, 14, tzinfo=timezone.utc)
    q2.result = "won"

    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [q1, q2]

    result = _preload_last_quoted_prices(db)
    # First quote's price should win for card key
    assert result["card:42"]["sell_price"] == 3.00
