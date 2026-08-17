"""test_buyplan_money_decimal.py — plan financials are Decimal-exact (QC money-math-
float).

Pins the two failure modes of the old binary-float math:
1. Cent drift: sums like 3 × $0.10 must be exactly $0.30 (float gives 0.30000000000000004
   pre-round, and larger plans accumulate real cent errors).
2. The at-limit routing edge: a line amount exactly equal to an approver's limit must
   compare as equal (float 16.1 * 3 = 48.30000000000000426... > Decimal("48.30") would
   wrongly exclude an at-limit approver).

Called by: pytest
Depends on: buyplan_workflow.money, buyplan_workflow.buyplan_approval, buyplan_po
"""

from decimal import Decimal

from app.services.buyplan_workflow.buyplan_po import _line_amount
from app.services.buyplan_workflow.money import pct, to_money


class _Line:
    def __init__(self, unit_cost, unit_sell, quantity):
        self.unit_cost = unit_cost
        self.unit_sell = unit_sell
        self.quantity = quantity


class _Plan:
    total_cost = None
    total_revenue = None
    total_margin_pct = None

    def __init__(self, lines):
        self.lines = lines


class TestRecalculateFinancials:
    def test_cent_exact_sum(self):
        from app.services.buyplan_workflow.buyplan_approval import _recalculate_financials

        plan = _Plan([_Line(Decimal("0.10"), Decimal("0.30"), 3)])
        _recalculate_financials(plan)
        assert plan.total_cost == Decimal("0.30")
        assert plan.total_revenue == Decimal("0.90")
        assert plan.total_margin_pct == Decimal("66.67")

    def test_float_written_lines_coerced_exactly(self):
        """Legacy writers may leave in-session floats on lines — totals stay exact."""
        from app.services.buyplan_workflow.buyplan_approval import _recalculate_financials

        plan = _Plan([_Line(0.10, 0.30, 3), _Line(Decimal("16.10"), None, 3)])
        _recalculate_financials(plan)
        assert plan.total_cost == Decimal("48.60")  # 0.30 + 48.30, no binary noise
        assert plan.total_revenue == Decimal("0.90")

    def test_zero_priced_lines_report_zero_not_none(self):
        from app.services.buyplan_workflow.buyplan_approval import _recalculate_financials

        plan = _Plan([_Line(Decimal("0"), Decimal("0"), 5)])
        _recalculate_financials(plan)
        assert plan.total_cost == Decimal("0.00")
        assert plan.total_revenue == Decimal("0.00")
        assert plan.total_margin_pct is None  # revenue 0 → margin undefined


class TestLineAmountAtLimit:
    def test_line_amount_equals_limit_exactly(self):
        """16.10 × 3 must equal a $48.30 approval limit — the float version yields
        48.30000000000000426 and wrongly excludes the at-limit approver."""
        line = _Line(Decimal("16.10"), None, 3)
        amount = _line_amount(line)
        assert amount == Decimal("48.30")
        assert amount <= Decimal("48.30")  # the routing comparison


class TestMoneyHelpers:
    def test_to_money_exact_from_float(self):
        assert to_money(0.1) == Decimal("0.1")
        assert to_money(None) is None
        assert to_money(Decimal("5.55")) == Decimal("5.55")

    def test_pct_quantizes_half_up(self):
        assert pct(Decimal("1"), Decimal("3")) == Decimal("33.33")
        assert pct(Decimal("0.005"), Decimal("1")) == Decimal("0.50")
