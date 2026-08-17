"""money.py — Decimal money helpers for the buy-plan workflow (QC money-math-float).

Plan financials are Decimal end-to-end: the columns are Numeric and the approval
limits they are compared against are Numeric — binary-float round-trips were where
cent drift and the at-limit routing edge came from. ``to_money`` is the single
coercion point for number-like inputs still arriving from legacy callers/tests
(str() first, so a float like 0.1 coerces to Decimal("0.1"), not its binary noise).

Called by: buyplan_approval, buyplan_lines (margin/total math)
Depends on: stdlib decimal only
"""

from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def to_money(value) -> Decimal | None:
    """None-safe coercion to Decimal (exact via str() for float/int/str inputs)."""
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def pct(numerator: Decimal, denominator: Decimal) -> Decimal:
    """(numerator / denominator) * 100, quantized to 2 places, HALF_UP."""
    return (numerator / denominator * 100).quantize(CENT, rounding=ROUND_HALF_UP)
