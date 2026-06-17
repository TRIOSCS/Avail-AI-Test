"""Tests for the buy-plan → customer_part_history auto-feed.

Called by: pytest. Depends on: buyplan_workflow, purchase_history_service.
"""

from app.models.buy_plan import BuyPlan


def test_buyplan_has_recorded_at_column():
    bp = BuyPlan(quote_id=1, requisition_id=1)
    assert bp.purchase_history_recorded_at is None
