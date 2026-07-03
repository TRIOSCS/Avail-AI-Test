"""Prepayment gains a status lifecycle + approved/paid/void columns (migration 179).

Called by: pytest
Depends on: app.constants (PrepaymentStatus), app.models.quality_plan (Prepayment).
"""

from app.constants import PrepaymentStatus
from app.models.quality_plan import Prepayment


def test_prepayment_status_enum():
    assert PrepaymentStatus.REQUESTED.value == "requested"
    assert {s.value for s in PrepaymentStatus} == {"requested", "approved", "paid", "void"}


def test_prepayment_lifecycle_columns_exist():
    cols = Prepayment.__table__.columns
    for name in (
        "status",
        "approved_by_id",
        "approved_at",
        "pay_token",
        "paid_at",
        "paid_by_id",
        "paid_by_label",
        "paid_via",
        "wire_reference",
        "paid_amount",
        "voided_at",
        "voided_by_id",
        "void_reason",
    ):
        assert name in cols, name
    assert cols["status"].default.arg == PrepaymentStatus.REQUESTED.value
