"""test_prepayment_mark_paid.py — the mark_prepayment_paid service transition (Task 3).

Verifies app.services.prepayment_service.mark_prepayment_paid:
  - marking an APPROVED prepayment sets status=paid + the paid fields and clears pay_token;
  - marking a non-APPROVED prepayment raises ValueError (only approved → paid).
The paid fan-out (run_prepayment_notify_bg) is suppressed under TESTING, so no notify work
runs here.

Called by: pytest
Depends on: app.services.prepayment_service (mark_prepayment_paid),
            tests.test_po_line_signoff (_make_plan/_make_user builders), conftest (db_session).
"""

from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.constants import PrepaymentStatus
from app.models.quality_plan import Prepayment
from app.services.prepayment_service import mark_prepayment_paid

# reuse the plan/user builders the sibling prepayment tests rely on
from tests.test_po_line_signoff import _make_plan, _make_user


def _prepay(db: Session, *, status: str, pay_token: str | None) -> Prepayment:
    """A minimal Prepayment in *status* on a fresh plan (buy_plan_id is NOT NULL)."""
    u = _make_user(db)
    plan = _make_plan(db, u)
    pp = Prepayment(
        buy_plan_id=plan.id,
        total_incl_fees=Decimal("20002.38"),
        currency="USD",
        created_by_id=u.id,
        status=status,
        pay_token=pay_token,
    )
    db.add(pp)
    db.commit()
    return pp


@pytest.fixture()
def approved_prepay(db_session: Session) -> Prepayment:
    return _prepay(db_session, status=PrepaymentStatus.APPROVED.value, pay_token="tok-abc123")


@pytest.fixture()
def requested_prepay(db_session: Session) -> Prepayment:
    return _prepay(db_session, status=PrepaymentStatus.REQUESTED.value, pay_token=None)


def test_mark_paid_sets_fields_and_clears_token(db_session: Session, approved_prepay: Prepayment):
    pp = approved_prepay  # status=approved, pay_token set
    mark_prepayment_paid(
        db_session,
        pp,
        wire_reference="WIRE-1",
        paid_amount=Decimal("20002.38"),
        paid_via="in_app",
        paid_by_id=pp.created_by_id,
        paid_by_label="MK",
    )
    assert pp.status == PrepaymentStatus.PAID.value
    assert pp.wire_reference == "WIRE-1"
    assert pp.paid_amount == Decimal("20002.38")
    assert pp.paid_via == "in_app"
    assert pp.paid_by_label == "MK"
    assert pp.paid_at is not None
    assert pp.pay_token is None


def test_mark_paid_requires_approved(db_session: Session, requested_prepay: Prepayment):
    with pytest.raises(ValueError):
        mark_prepayment_paid(
            db_session,
            requested_prepay,
            wire_reference="x",
            paid_amount=Decimal("1"),
            paid_via="in_app",
        )
