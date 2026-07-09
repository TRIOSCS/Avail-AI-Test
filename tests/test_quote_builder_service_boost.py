"""tests/test_quote_builder_service_boost.py — Coverage boost for quote_builder_service.

Covers: apply_smart_defaults, build_excel_export, and save_quote_from_builder
revision path (existing old quote).

Called by: pytest
Depends on: conftest fixtures, unittest.mock
"""

import os

os.environ["TESTING"] = "1"

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.models import Offer, Quote, Requisition, User
from app.services.quote_builder_service import (
    apply_smart_defaults,
    build_excel_export,
    get_builder_data,
    save_quote_from_builder,
)
from tests.conftest import engine

_ = engine


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_payload(quote_id=None, lines=None, payment_terms=None, shipping_terms=None):
    line = SimpleNamespace(
        mpn="LM317T",
        manufacturer="TI",
        qty=100,
        cost_price=0.5,
        sell_price=0.75,
        margin_pct=33.0,
        lead_time="4 weeks",
        date_code="2024",
        condition="new",
        packaging="reel",
        moq=100,
        offer_id=None,
        material_card_id=None,
        notes=None,
    )
    return SimpleNamespace(
        quote_id=quote_id,
        lines=lines if lines is not None else [line],
        payment_terms=payment_terms,
        shipping_terms=shipping_terms,
        validity_days=30,
        notes=None,
    )


@pytest.fixture()
def req_and_item(db_session: Session, test_user: User, test_customer_site):
    req = Requisition(
        name="QB-BOOST",
        customer_name="Boost Co",
        status="open",
        # save_quote_from_builder now runs the customer-consistency gate; the router
        # already requires a linked customer site before quoting, so the fixture matches
        # that real precondition.
        customer_site_id=test_customer_site.id,
        created_by=test_user.id,
        created_at=datetime.now(UTC),
    )
    db_session.add(req)
    db_session.flush()

    from app.models import Requirement

    item = Requirement(
        requisition_id=req.id,
        primary_mpn="LM317T",
        target_qty=100,
        created_at=datetime.now(UTC),
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(req)
    db_session.refresh(item)
    return req, item


# ── apply_smart_defaults ──────────────────────────────────────────────────────


class TestApplySmartDefaults:
    def test_zero_offers_sets_no_offers_status(self):
        lines = [{"offers": [], "status": "unknown", "selected_offer_id": None, "sell_price": None}]
        apply_smart_defaults(lines)
        assert lines[0]["status"] == "no_offers"
        assert lines[0]["selected_offer_id"] is None

    def test_single_offer_auto_selects(self):
        lines = [
            {
                "offers": [{"id": 42, "unit_price": 1.25}],
                "status": "unknown",
                "selected_offer_id": None,
                "sell_price": None,
                "sell_price_manual": False,
            }
        ]
        apply_smart_defaults(lines)
        assert lines[0]["status"] == "decided"
        assert lines[0]["selected_offer_id"] == 42
        assert lines[0]["sell_price"] == 1.25
        assert lines[0]["sell_price_manual"] is False

    def test_multiple_offers_needs_review(self):
        lines = [
            {
                "offers": [{"id": 1, "unit_price": 0.50}, {"id": 2, "unit_price": 0.60}],
                "status": "unknown",
                "selected_offer_id": None,
                "sell_price": None,
            }
        ]
        apply_smart_defaults(lines)
        assert lines[0]["status"] == "needs_review"
        assert lines[0]["selected_offer_id"] is None

    def test_multiple_lines_each_applied_independently(self):
        lines = [
            {"offers": [], "status": "x", "selected_offer_id": None, "sell_price": None},
            {
                "offers": [{"id": 10, "unit_price": 2.00}],
                "status": "x",
                "selected_offer_id": None,
                "sell_price": None,
                "sell_price_manual": True,
            },
        ]
        apply_smart_defaults(lines)
        assert lines[0]["status"] == "no_offers"
        assert lines[1]["status"] == "decided"


# ── build_excel_export ────────────────────────────────────────────────────────


class TestBuildExcelExport:
    @pytest.mark.parametrize(
        ("items", "quote_number", "customer", "check_nonempty"),
        [
            (
                [
                    {
                        "mpn": "LM317T",
                        "manufacturer": "TI",
                        "qty": 100,
                        "sell_price": 0.75,
                        "lead_time": "4 weeks",
                        "date_code": "2024",
                        "condition": "new",
                        "packaging": "reel",
                        "moq": 100,
                        "vendor_name": "Arrow",
                    }
                ],
                "Q-2026-0001",
                "ACME Corp",
                True,
            ),
            ([], "Q-EMPTY", "Nobody", True),
            (
                [
                    {
                        "mpn": "ABC123",
                        "manufacturer": "Mfr1",
                        "qty": 50,
                        "sell_price": 1.0,
                        "lead_time": None,
                        "date_code": None,
                        "condition": None,
                        "packaging": None,
                        "moq": None,
                        "vendor_name": "V1",
                    },
                    {
                        "mpn": "XYZ789",
                        "manufacturer": "Mfr2",
                        "qty": 0,
                        "sell_price": 0,
                        "lead_time": None,
                        "date_code": None,
                        "condition": None,
                        "packaging": None,
                        "moq": None,
                        "vendor_name": "V2",
                    },
                ],
                "Q-MULTI",
                "MultiCo",
                False,
            ),
            (
                [
                    {
                        "mpn": "P1",
                        "manufacturer": None,
                        "qty": None,
                        "sell_price": None,
                        "lead_time": None,
                        "date_code": None,
                        "condition": None,
                        "packaging": None,
                        "moq": None,
                        "vendor_name": None,
                    }
                ],
                "Q-X",
                "X",
                False,
            ),
        ],
        ids=["single_line", "empty_line_items", "multiple_line_items", "none_qty_and_price"],
    )
    def test_returns_bytes(self, items, quote_number, customer, check_nonempty):
        result = build_excel_export(items, quote_number, customer)
        assert isinstance(result, bytes)
        if check_nonempty:
            assert len(result) > 0


# ── save_quote_from_builder — revision path ───────────────────────────────────


class TestSaveQuoteRevision:
    def test_existing_old_quote_creates_revision(self, db_session: Session, req_and_item, test_user: User):
        """Lines 253-257: payload.quote_id points to existing quote → revision bumped."""
        req, item = req_and_item

        old_quote = Quote(
            requisition_id=req.id,
            quote_number="Q-2026-0001",
            revision=1,
            line_items=[],
            subtotal=500.0,
            total_cost=250.0,
            total_margin_pct=50.0,
            created_by_id=test_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(old_quote)
        db_session.commit()
        db_session.refresh(old_quote)
        old_id = old_quote.id

        with patch("app.services.requisition_state.transition", side_effect=ValueError):
            with patch("app.services.knowledge_service.capture_quote_fact"):
                result = save_quote_from_builder(db_session, req.id, _make_payload(quote_id=old_id), test_user)

        assert result["ok"] is True
        assert result["revision"] == 2
        # Old quote should be renamed to R1
        db_session.expire(old_quote)
        refreshed = db_session.get(Quote, old_id)
        assert refreshed.quote_number == "Q-2026-0001-R1"

    def test_revision_number_increments_from_existing(self, db_session: Session, req_and_item, test_user: User):
        """When old quote already has revision=3, new quote gets revision 4."""
        req, item = req_and_item

        old_quote = Quote(
            requisition_id=req.id,
            quote_number="Q-2026-0005",
            revision=3,
            line_items=[],
            subtotal=100.0,
            total_cost=50.0,
            total_margin_pct=50.0,
            created_by_id=test_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(old_quote)
        db_session.commit()
        db_session.refresh(old_quote)

        with patch("app.services.requisition_state.transition", side_effect=ValueError):
            with patch("app.services.knowledge_service.capture_quote_fact"):
                result = save_quote_from_builder(db_session, req.id, _make_payload(quote_id=old_quote.id), test_user)

        assert result["revision"] == 4

    def test_payment_and_shipping_terms_from_payload(self, db_session: Session, req_and_item, test_user: User):
        """payment_terms and shipping_terms from payload override site defaults."""
        req, item = req_and_item

        with patch("app.services.requisition_state.transition", side_effect=ValueError):
            with patch("app.services.knowledge_service.capture_quote_fact"):
                result = save_quote_from_builder(
                    db_session,
                    req.id,
                    _make_payload(payment_terms="NET60", shipping_terms="FOB"),
                    test_user,
                )

        assert result["ok"] is True
        quote = db_session.get(Quote, result["quote_id"])
        assert quote.payment_terms == "NET60"
        assert quote.shipping_terms == "FOB"


# ── get_builder_data — active offer path ─────────────────────────────────────


class TestGetBuilderDataActiveOffer:
    def test_active_offer_included_in_offers_data(self, db_session: Session, req_and_item):
        """Line 38 — active offer is included, inactive is excluded."""
        req, item = req_and_item

        active = Offer(
            requisition_id=req.id,
            requirement_id=item.id,
            vendor_name="Arrow",
            mpn="LM317T",
            normalized_mpn="LM317T",
            status="active",
            unit_price=0.55,
            qty_available=500,
            created_at=datetime.now(UTC),
        )
        inactive = Offer(
            requisition_id=req.id,
            requirement_id=item.id,
            vendor_name="Avnet",
            mpn="LM317T",
            normalized_mpn="LM317T",
            status="inactive",
            unit_price=0.60,
            qty_available=200,
            created_at=datetime.now(UTC),
        )
        db_session.add_all([active, inactive])
        db_session.commit()

        result = get_builder_data(req.id, db_session)
        assert len(result) == 1
        assert result[0]["offer_count"] == 1
        assert result[0]["offers"][0]["vendor_name"] == "Arrow"

    def test_pricing_history_populated_when_available(self, db_session: Session, req_and_item):
        """Lines 90-103 — _preload_last_quoted_prices returns data for the MPN."""
        req, item = req_and_item

        mock_prices = {
            "LM317T": {
                "sell_price": 0.80,
                "quote_number": "Q-2025-001",
                "date": "2025-01-10",
                "cost_price": 0.40,
                "margin_pct": 50.0,
                "result": "won",
            }
        }

        with patch("app.routers.crm._helpers._preload_last_quoted_prices", return_value=mock_prices):
            result = get_builder_data(req.id, db_session)

        ph = result[0]["pricing_history"]
        assert ph is not None
        assert ph["avg_price"] == 0.80
        assert ph["recent"][0]["quote_number"] == "Q-2025-001"
