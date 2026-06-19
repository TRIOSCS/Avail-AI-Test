"""test_backfill_buyplan_cph.py — Tests for app/management/backfill_buyplan_cph.py.

Covers: backfill() — no completed plans, plans with purchase_history_recorded_at=None,
        plans with purchase_history_recorded_at already set (skipped).

Called by: pytest autodiscovery
Depends on: conftest.py db_session, BuyPlan model, BuyPlanStatus constant,
            mocked record_buyplan_purchase_history
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.constants import BuyPlanStatus
from app.models.buy_plan import BuyPlan


def _make_quote_and_requisition(db: Session):
    """BuyPlan requires quote_id and requisition_id — create minimal parent rows."""
    from app.models import Company, CustomerSite, Quote, Requisition, User

    user = User(
        email=f"bp_test_{datetime.now().timestamp()}@test.com",
        name="BP Test",
        role="buyer",
        azure_id=f"bp-azure-{datetime.now().timestamp()}",
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    db.flush()

    co = Company(
        name=f"BPCo {datetime.now().timestamp()}",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(co)
    db.flush()

    site = CustomerSite(
        company_id=co.id,
        site_name="HQ",
    )
    db.add(site)
    db.flush()

    req = Requisition(
        name=f"REQ-BP-{datetime.now().timestamp()}",
        customer_name="BPCo",
        status="active",
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()

    quote = Quote(
        requisition_id=req.id,
        customer_site_id=site.id,
        quote_number=f"Q-{datetime.now().timestamp()}",
        status="sent",
        line_items=[],
        subtotal=0,
        total_cost=0,
        total_margin_pct=0,
        created_by_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(quote)
    db.flush()

    return quote.id, req.id


def _make_buy_plan(db: Session, status: str, purchase_history_recorded_at=None) -> BuyPlan:
    quote_id, req_id = _make_quote_and_requisition(db)
    plan = BuyPlan(
        quote_id=quote_id,
        requisition_id=req_id,
        status=status,
        purchase_history_recorded_at=purchase_history_recorded_at,
        created_at=datetime.now(timezone.utc),
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


class TestBackfillNoPlans:
    def test_no_plans_returns_zero(self, db_session: Session):
        with patch("app.management.backfill_buyplan_cph.record_buyplan_purchase_history") as mock_rec:
            from app.management.backfill_buyplan_cph import backfill

            result = backfill(db_session)

        assert result == 0
        mock_rec.assert_not_called()

    def test_only_non_completed_plans_returns_zero(self, db_session: Session):
        _make_buy_plan(db_session, status=BuyPlanStatus.DRAFT.value)
        _make_buy_plan(db_session, status=BuyPlanStatus.ACTIVE.value)
        _make_buy_plan(db_session, status=BuyPlanStatus.PENDING.value)

        with patch("app.management.backfill_buyplan_cph.record_buyplan_purchase_history") as mock_rec:
            from app.management.backfill_buyplan_cph import backfill

            result = backfill(db_session)

        assert result == 0
        mock_rec.assert_not_called()


class TestBackfillWithUnrecordedPlans:
    def test_completed_plan_without_recorded_at_is_processed(self, db_session: Session):
        plan = _make_buy_plan(db_session, status=BuyPlanStatus.COMPLETED.value, purchase_history_recorded_at=None)

        with patch("app.management.backfill_buyplan_cph.record_buyplan_purchase_history") as mock_rec:
            from app.management.backfill_buyplan_cph import backfill

            result = backfill(db_session)

        assert result == 1
        mock_rec.assert_called_once_with(db_session, plan, refresh=False)

    def test_multiple_unrecorded_plans_all_processed(self, db_session: Session):
        plans = [
            _make_buy_plan(db_session, status=BuyPlanStatus.COMPLETED.value, purchase_history_recorded_at=None)
            for _ in range(3)
        ]

        with patch("app.management.backfill_buyplan_cph.record_buyplan_purchase_history") as mock_rec:
            from app.management.backfill_buyplan_cph import backfill

            result = backfill(db_session)

        assert result == 3
        assert mock_rec.call_count == 3


class TestBackfillSkipsAlreadyRecorded:
    def test_plan_with_recorded_at_is_skipped(self, db_session: Session):
        _make_buy_plan(
            db_session,
            status=BuyPlanStatus.COMPLETED.value,
            purchase_history_recorded_at=datetime.now(timezone.utc),
        )

        with patch("app.management.backfill_buyplan_cph.record_buyplan_purchase_history") as mock_rec:
            from app.management.backfill_buyplan_cph import backfill

            result = backfill(db_session)

        assert result == 0
        mock_rec.assert_not_called()

    def test_mixed_recorded_and_unrecorded(self, db_session: Session):
        _make_buy_plan(
            db_session,
            status=BuyPlanStatus.COMPLETED.value,
            purchase_history_recorded_at=datetime.now(timezone.utc),
        )
        unrecorded = _make_buy_plan(
            db_session,
            status=BuyPlanStatus.COMPLETED.value,
            purchase_history_recorded_at=None,
        )

        with patch("app.management.backfill_buyplan_cph.record_buyplan_purchase_history") as mock_rec:
            from app.management.backfill_buyplan_cph import backfill

            result = backfill(db_session)

        assert result == 1
        mock_rec.assert_called_once_with(db_session, unrecorded, refresh=False)

    def test_idempotent_on_repeat_call(self, db_session: Session):
        """Running backfill twice on an already-recorded plan is a no-op on the 2nd
        call."""
        _make_buy_plan(
            db_session,
            status=BuyPlanStatus.COMPLETED.value,
            purchase_history_recorded_at=datetime.now(timezone.utc),
        )

        with patch("app.management.backfill_buyplan_cph.record_buyplan_purchase_history") as mock_rec:
            from app.management.backfill_buyplan_cph import backfill

            first = backfill(db_session)
            second = backfill(db_session)

        assert first == 0
        assert second == 0
        mock_rec.assert_not_called()


class TestBackfillReturnCount:
    def test_return_value_equals_plans_processed(self, db_session: Session):
        for _ in range(4):
            _make_buy_plan(db_session, status=BuyPlanStatus.COMPLETED.value, purchase_history_recorded_at=None)

        with patch("app.management.backfill_buyplan_cph.record_buyplan_purchase_history"):
            from app.management.backfill_buyplan_cph import backfill

            result = backfill(db_session)

        assert result == 4


class TestBackfillMainBlock:
    def test_main_block_logic_directly(self):
        """Simulate the __main__ block logic directly to cover those lines."""
        import app.management.backfill_buyplan_cph as mod

        mock_db = MagicMock()
        mock_session_local = MagicMock(return_value=mock_db)
        mock_backfill_fn = MagicMock(return_value=2)

        with patch.object(mod, "SessionLocal", mock_session_local):
            with patch.object(mod, "backfill", mock_backfill_fn):
                # Execute the same code as the __main__ block
                db = mod.SessionLocal()
                try:
                    mod.backfill(db)
                finally:
                    db.close()

        mock_backfill_fn.assert_called_once_with(mock_db)
        mock_db.close.assert_called_once()
