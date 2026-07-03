"""create_prepayment links the specific line, validates it, and blocks a 2nd pending.

Called by: pytest
Depends on: app.services.prepayment_service (create_prepayment),
            tests.test_po_line_signoff (_make_user/_make_plan/_make_line fixtures).
"""

from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.services.prepayment_service import create_prepayment

# reuse the plan/line/user fixtures pattern from tests/test_po_line_signoff.py
from tests.test_po_line_signoff import _make_line, _make_plan, _make_user


def _prepay_approver(db: Session) -> object:
    """A user eligible to approve PREPAYMENT requests (so routing succeeds).

    _make_user exposes no can_approve_prepayments kwarg, so set the attribute directly.
    is_active defaults True on the model, satisfying the routing eligibility filter.
    """
    u = _make_user(db)
    u.can_approve_prepayments = True
    db.flush()
    return u


def test_create_prepayment_sets_line(db_session: Session) -> None:
    u = _prepay_approver(db_session)
    plan = _make_plan(db_session, u)
    line = _make_line(db_session, plan)  # PENDING_VERIFY, po_number set
    db_session.commit()
    pp, req = create_prepayment(
        db_session,
        buy_plan_id=plan.id,
        buy_plan_line_id=line.id,
        vendor_card_id=None,
        payment_method="wire",
        total_incl_fees=Decimal("20002.38"),
        test_report_sent=False,
        buyer_remarks="x",
        created_by=u,
    )
    assert pp.buy_plan_line_id == line.id


def test_create_prepayment_rejects_line_not_on_plan(db_session: Session) -> None:
    u = _prepay_approver(db_session)
    plan = _make_plan(db_session, u)
    other = _make_plan(db_session, u)
    stray = _make_line(db_session, other)
    db_session.commit()
    with pytest.raises(ValueError):
        create_prepayment(
            db_session,
            buy_plan_id=plan.id,
            buy_plan_line_id=stray.id,
            vendor_card_id=None,
            payment_method="wire",
            total_incl_fees=Decimal("1"),
            test_report_sent=False,
            buyer_remarks=None,
            created_by=u,
        )


def test_create_prepayment_blocks_second_pending_on_same_line(db_session: Session) -> None:
    u = _prepay_approver(db_session)
    plan = _make_plan(db_session, u)
    line = _make_line(db_session, plan)
    db_session.commit()
    create_prepayment(
        db_session,
        buy_plan_id=plan.id,
        buy_plan_line_id=line.id,
        vendor_card_id=None,
        payment_method="wire",
        total_incl_fees=Decimal("5"),
        test_report_sent=False,
        buyer_remarks=None,
        created_by=u,
    )
    with pytest.raises(ValueError):
        create_prepayment(
            db_session,
            buy_plan_id=plan.id,
            buy_plan_line_id=line.id,
            vendor_card_id=None,
            payment_method="wire",
            total_incl_fees=Decimal("5"),
            test_report_sent=False,
            buyer_remarks=None,
            created_by=u,
        )
