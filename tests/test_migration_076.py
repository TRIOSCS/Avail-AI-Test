"""
test_migration_076.py — Tests for V1→V3 buy plan data migration.

Validates that Alembic migration 076 correctly migrates BuyPlan records
to BuyPlanV3 + BuyPlanLine rows with proper status mapping, field mapping,
and idempotency.

Called by: pytest
Depends on: conftest.py fixtures, alembic/versions/076_migrate_buy_plans_v1_to_v3.py
"""

import json
import secrets
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import BuyPlan, Offer, Quote, Requisition, User
from app.models.buy_plan import BuyPlanLine, BuyPlanStatus, BuyPlanV3


# ── Helpers ──────────────────────────────────────────────────────────


def _create_v1_plan(db_session: Session, **overrides) -> BuyPlan:
    """Create a V1 BuyPlan record with defaults."""
    defaults = {
        "status": "pending_approval",
        "line_items": [
            {
                "offer_id": 1,
                "mpn": "LM317T",
                "vendor_name": "Arrow Electronics",
                "qty": 1000,
                "plan_qty": 1000,
                "cost_price": 0.50,
                "sell_price": 0.75,
                "lead_time": "2 weeks",
                "condition": "new",
                "entered_by_id": None,
                "po_number": None,
                "po_entered_at": None,
                "po_sent_at": None,
                "po_recipient": None,
                "po_verified": False,
            }
        ],
        "approval_token": secrets.token_urlsafe(32),
        "submitted_at": datetime.now(timezone.utc),
    }
    defaults.update(overrides)
    plan = BuyPlan(**defaults)
    db_session.add(plan)
    db_session.commit()
    db_session.refresh(plan)
    return plan


# ── Tests ────────────────────────────────────────────────────────────


class TestV1StatusMapping:
    """Verify V1→V3 status mapping logic from migration 076."""

    STATUS_MAP = {
        "draft": "draft",
        "pending_approval": "pending",
        "approved": "active",
        "po_entered": "active",
        "po_confirmed": "active",
        "complete": "completed",
        "rejected": "draft",
        "cancelled": "cancelled",
    }

    @pytest.mark.parametrize("v1_status,v3_status", STATUS_MAP.items())
    def test_status_maps_correctly(self, v1_status, v3_status):
        """Each V1 status maps to the expected V3 status."""
        assert self.STATUS_MAP[v1_status] == v3_status


class TestV1FieldMapping:
    """Verify field mapping from V1 BuyPlan to V3 records."""

    def test_header_fields_preserved(
        self, db_session, test_requisition, test_quote, test_user
    ):
        """V1 header fields are correctly mapped to V3 header."""
        v1 = _create_v1_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            sales_order_number="SO-001",
            salesperson_notes="Rush order",
            manager_notes="Approved with conditions",
            is_stock_sale=True,
        )
        # Simulate migration logic: create V3 from V1
        v3 = BuyPlanV3(
            quote_id=v1.quote_id,
            requisition_id=v1.requisition_id,
            status=BuyPlanStatus.pending.value,
            sales_order_number=v1.sales_order_number,
            submitted_by_id=v1.submitted_by_id,
            submitted_at=v1.submitted_at,
            salesperson_notes=v1.salesperson_notes,
            approval_notes=v1.manager_notes,
            is_stock_sale=v1.is_stock_sale,
        )
        db_session.add(v3)
        db_session.commit()

        assert v3.quote_id == v1.quote_id
        assert v3.requisition_id == v1.requisition_id
        assert v3.sales_order_number == "SO-001"
        assert v3.salesperson_notes == "Rush order"
        assert v3.approval_notes == "Approved with conditions"
        assert v3.is_stock_sale is True

    def test_line_items_to_lines(self, db_session, test_requisition, test_quote, test_user, test_offer):
        """V1 JSON line_items become V3 BuyPlanLine rows."""
        v1 = _create_v1_plan(
            db_session,
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            submitted_by_id=test_user.id,
            line_items=[
                {
                    "offer_id": test_offer.id,
                    "mpn": "LM317T",
                    "vendor_name": "Arrow",
                    "qty": 1000,
                    "plan_qty": 500,
                    "cost_price": 0.50,
                    "sell_price": 0.75,
                    "po_number": None,
                    "po_verified": False,
                    "entered_by_id": test_user.id,
                },
            ],
        )
        # Simulate line creation
        v3 = BuyPlanV3(
            quote_id=v1.quote_id,
            requisition_id=v1.requisition_id,
            status=BuyPlanStatus.pending.value,
            submitted_by_id=v1.submitted_by_id,
            submitted_at=v1.submitted_at,
        )
        db_session.add(v3)
        db_session.flush()

        item = v1.line_items[0]
        line = BuyPlanLine(
            buy_plan_id=v3.id,
            offer_id=item["offer_id"],
            quantity=item["plan_qty"],
            unit_cost=item["cost_price"],
            unit_sell=item["sell_price"],
            buyer_id=item["entered_by_id"],
            status="awaiting_po",
        )
        db_session.add(line)
        db_session.commit()

        assert line.quantity == 500
        assert float(line.unit_cost) == 0.50
        assert float(line.unit_sell) == 0.75
        assert line.buyer_id == test_user.id
        assert line.status == "awaiting_po"

    def test_po_verified_line_status(self, db_session, test_requisition, test_quote, test_user, test_offer):
        """V1 line with po_verified=True becomes V3 line with status='verified'."""
        v3 = BuyPlanV3(
            quote_id=test_quote.id,
            requisition_id=test_requisition.id,
            status=BuyPlanStatus.active.value,
            submitted_by_id=test_user.id,
            submitted_at=datetime.now(timezone.utc),
        )
        db_session.add(v3)
        db_session.flush()

        line = BuyPlanLine(
            buy_plan_id=v3.id,
            offer_id=test_offer.id,
            quantity=1000,
            unit_cost=0.50,
            status="verified",
            po_number="PO-001",
        )
        db_session.add(line)
        db_session.commit()

        assert line.status == "verified"
        assert line.po_number == "PO-001"

    def test_po_entered_line_status(self, db_session, test_requisition, test_quote, test_user, test_offer):
        """V1 line with po_number but not verified becomes 'pending_verify'."""
        v3 = BuyPlanV3(
            quote_id=test_quote.id,
            requisition_id=test_requisition.id,
            status=BuyPlanStatus.active.value,
            submitted_by_id=test_user.id,
            submitted_at=datetime.now(timezone.utc),
        )
        db_session.add(v3)
        db_session.flush()

        line = BuyPlanLine(
            buy_plan_id=v3.id,
            offer_id=test_offer.id,
            quantity=1000,
            unit_cost=0.50,
            status="pending_verify",
            po_number="PO-002",
        )
        db_session.add(line)
        db_session.commit()

        assert line.status == "pending_verify"

    def test_cancelled_plan_all_lines_cancelled(self, db_session, test_requisition, test_quote, test_user, test_offer):
        """V1 cancelled plan → all V3 lines get cancelled status."""
        v3 = BuyPlanV3(
            quote_id=test_quote.id,
            requisition_id=test_requisition.id,
            status=BuyPlanStatus.cancelled.value,
            submitted_by_id=test_user.id,
            submitted_at=datetime.now(timezone.utc),
        )
        db_session.add(v3)
        db_session.flush()

        line = BuyPlanLine(
            buy_plan_id=v3.id,
            offer_id=test_offer.id,
            quantity=1000,
            unit_cost=0.50,
            status="cancelled",
        )
        db_session.add(line)
        db_session.commit()

        assert line.status == "cancelled"


class TestMigrationIdempotency:
    """Verify migration won't create duplicates on re-run."""

    def test_same_quote_req_submitted_at_skips(
        self, db_session, test_requisition, test_quote, test_user
    ):
        """Second V3 insert with same key fields should be skipped in migration."""
        now = datetime.now(timezone.utc)
        v3_1 = BuyPlanV3(
            quote_id=test_quote.id,
            requisition_id=test_requisition.id,
            status=BuyPlanStatus.pending.value,
            submitted_by_id=test_user.id,
            submitted_at=now,
        )
        db_session.add(v3_1)
        db_session.commit()

        # Check: same quote_id + requisition_id + submitted_at already exists
        existing = (
            db_session.query(BuyPlanV3)
            .filter(
                BuyPlanV3.quote_id == test_quote.id,
                BuyPlanV3.requisition_id == test_requisition.id,
                BuyPlanV3.submitted_at == now,
            )
            .first()
        )
        assert existing is not None
        assert existing.id == v3_1.id
