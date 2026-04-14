"""tests/test_quote_builder_service_nightly.py — Coverage for uncovered lines in quote_builder_service.

Targets lines: 27, 38, 90-107, 221, 259, 271-272, 321-329, 336-337
Called by: pytest
Depends on: conftest fixtures, unittest.mock
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.models import Offer, Requirement, Requisition, User
from app.services.quote_builder_service import get_builder_data, save_quote_from_builder


def _make_payload(quote_id=None, lines=None):
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
        payment_terms=None,
        shipping_terms=None,
        validity_days=30,
        notes=None,
    )


@pytest.fixture()
def req_with_item(db_session: Session, test_user: User):
    req = Requisition(
        name="QB-NIGHTLY",
        customer_name="Test Co",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()
    item = Requirement(
        requisition_id=req.id,
        primary_mpn="LM317T",
        target_qty=100,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(req)
    db_session.refresh(item)
    return req, item


class TestGetBuilderData:
    def test_with_requirement_ids_filter(self, db_session: Session, req_with_item):
        """Line 27 — requirement_ids filter is applied when provided."""
        req, item = req_with_item

        extra = Requirement(
            requisition_id=req.id,
            primary_mpn="LM7805",
            target_qty=50,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(extra)
        db_session.commit()

        result = get_builder_data(req.id, db_session, requirement_ids=[item.id])
        assert len(result) == 1
        assert result[0]["mpn"] == "LM317T"

    def test_requirement_with_no_active_offers(self, db_session: Session, req_with_item):
        """Line 38 — all offers are non-active → offers_data is empty."""
        req, item = req_with_item

        offer = Offer(
            requisition_id=req.id,
            requirement_id=item.id,
            vendor_name="Some Vendor",
            mpn="LM317T",
            normalized_mpn="LM317T",
            status="inactive",
            unit_price=0.50,
            qty_available=1000,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.commit()

        result = get_builder_data(req.id, db_session)
        assert result[0]["offer_count"] == 0
        assert result[0]["offers"] == []

    def test_pricing_history_exception_is_swallowed(self, db_session: Session, req_with_item):
        """Lines 90-107 — _preload_last_quoted_prices raises, pricing_history stays None."""
        req, item = req_with_item

        with patch(
            "app.routers.crm._helpers._preload_last_quoted_prices",
            side_effect=Exception("db error"),
        ):
            result = get_builder_data(req.id, db_session)

        assert result[0]["pricing_history"] is None


class TestSaveQuoteFromBuilder:
    def test_requisition_not_found_raises_value_error(self, db_session: Session, test_user: User):
        """Line 221 — missing requisition raises ValueError."""
        with pytest.raises(ValueError, match="Requisition not found"):
            save_quote_from_builder(db_session, 99999, _make_payload(), test_user)

    def test_quote_id_with_missing_old_quote_uses_next_number(
        self, db_session: Session, req_with_item, test_user: User
    ):
        """Line 259 — payload.quote_id points to non-existent quote → next_quote_number."""
        req, item = req_with_item

        # Patch at the source module where it's imported
        with patch("app.services.crm_service.next_quote_number", return_value="Q-9999"):
            with patch("app.services.requisition_state.transition", side_effect=ValueError):
                with patch("app.services.knowledge_service.capture_quote_fact"):
                    result = save_quote_from_builder(db_session, req.id, _make_payload(quote_id=99999), test_user)

        assert result["ok"] is True
        assert result["quote_number"] == "Q-9999"

    def test_state_transition_value_error_is_silently_swallowed(
        self, db_session: Session, req_with_item, test_user: User
    ):
        """Lines 271-272 — state transition ValueError → pass, quote still created."""
        req, item = req_with_item

        with patch("app.services.crm_service.next_quote_number", return_value="Q-0001"):
            with patch(
                "app.services.requisition_state.transition",
                side_effect=ValueError("already quoting"),
            ):
                with patch("app.services.knowledge_service.capture_quote_fact"):
                    result = save_quote_from_builder(db_session, req.id, _make_payload(), test_user)

        assert result["ok"] is True

    def test_on_quote_built_exception_is_swallowed(self, db_session: Session, req_with_item, test_user: User):
        """Lines 321-329 — on_quote_built raises → warning logged, continues."""
        req, item = req_with_item

        offer = Offer(
            requisition_id=req.id,
            requirement_id=item.id,
            vendor_name="Arrow",
            mpn="LM317T",
            normalized_mpn="LM317T",
            status="active",
            unit_price=0.50,
            qty_available=1000,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.commit()

        line = SimpleNamespace(
            mpn="LM317T",
            manufacturer="TI",
            qty=100,
            cost_price=0.5,
            sell_price=0.75,
            margin_pct=33.0,
            lead_time=None,
            date_code=None,
            condition="new",
            packaging=None,
            moq=None,
            offer_id=offer.id,
            material_card_id=None,
            notes=None,
        )
        payload = SimpleNamespace(
            quote_id=None,
            lines=[line],
            payment_terms=None,
            shipping_terms=None,
            validity_days=30,
            notes=None,
        )

        with patch("app.services.crm_service.next_quote_number", return_value="Q-0002"):
            with patch("app.services.requisition_state.transition", side_effect=ValueError):
                with patch(
                    "app.services.requirement_status.on_quote_built",
                    side_effect=Exception("hook failed"),
                ):
                    with patch("app.services.knowledge_service.capture_quote_fact"):
                        result = save_quote_from_builder(db_session, req.id, payload, test_user)

        assert result["ok"] is True

    def test_knowledge_capture_exception_is_swallowed(self, db_session: Session, req_with_item, test_user: User):
        """Lines 336-337 — knowledge capture raises → warning logged, returns ok."""
        req, item = req_with_item

        with patch("app.services.crm_service.next_quote_number", return_value="Q-0003"):
            with patch("app.services.requisition_state.transition", side_effect=ValueError):
                with patch(
                    "app.services.knowledge_service.capture_quote_fact",
                    side_effect=Exception("ledger down"),
                ):
                    result = save_quote_from_builder(db_session, req.id, _make_payload(), test_user)

        assert result["ok"] is True
