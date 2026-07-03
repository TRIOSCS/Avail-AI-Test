"""Prepayment gains a nullable FK to the specific PO line it prepays (migration 178).

Called by: pytest
Depends on: app.models.quality_plan (Prepayment).
"""

from app.models.quality_plan import Prepayment


def test_prepayment_has_buy_plan_line_id_column():
    cols = Prepayment.__table__.columns
    assert "buy_plan_line_id" in cols
    fk = list(cols["buy_plan_line_id"].foreign_keys)[0]
    assert fk.column.table.name == "buy_plan_lines"
    assert cols["buy_plan_line_id"].nullable is True
