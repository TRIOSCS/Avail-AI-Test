"""tests/test_remediation_p2b.py — QC 2026-08-10 P2 (dead-end states, wave 2).

D2: a halted buy plan, on resume, restores its EXACT pre-halt state. Before, halt
overwrote status (-> HALTED) and so_status (-> REJECTED) with no record, and
resume forced ACTIVE while leaving so_status REJECTED — so the plan could never
complete (check_completion needs so_status=APPROVED), and a PENDING-halted plan
came back ACTIVE with its approval never granted.

Called by: pytest autodiscovery
Depends on: conftest fixtures + tests.test_prepayment builders.
"""

from datetime import UTC, datetime

from tests.conftest import engine  # noqa: F401
from tests.test_prepayment import _make_buy_plan


def _manager(db):
    from app.models import User

    u = User(email="mgr.d2@trioscs.com", name="Mgr", role="manager", azure_id="az-d2mgr", is_active=True)
    db.add(u)
    db.flush()
    return u


def _plan(db, requester, *, status, so_status):
    from app.constants import SOVerificationStatus  # noqa: F401

    bp = _make_buy_plan(db, requester)
    bp.status = status
    bp.so_status = so_status
    db.flush()
    return bp


def test_resume_restores_active_plan_so_status(db_session, test_user):
    from app.constants import BuyPlanStatus, SOVerificationStatus
    from app.services.buyplan_workflow.buyplan_approval import halt_plan, resume_plan

    mgr = _manager(db_session)
    bp = _plan(db_session, test_user, status=BuyPlanStatus.ACTIVE.value, so_status=SOVerificationStatus.APPROVED.value)
    db_session.commit()

    halt_plan(bp.id, mgr, db_session, reason="supplier issue")
    db_session.commit()
    db_session.refresh(bp)
    assert bp.status == BuyPlanStatus.HALTED.value
    assert bp.so_status == SOVerificationStatus.REJECTED.value
    assert bp.status_before_halt == BuyPlanStatus.ACTIVE.value
    assert bp.so_status_before_halt == SOVerificationStatus.APPROVED.value

    resume_plan(bp.id, mgr, db_session)
    db_session.commit()
    db_session.refresh(bp)
    # so_status restored to APPROVED so the plan can complete again (was stuck REJECTED).
    assert bp.status == BuyPlanStatus.ACTIVE.value
    assert bp.so_status == SOVerificationStatus.APPROVED.value
    assert bp.status_before_halt is None and bp.so_status_before_halt is None


def test_resume_returns_pending_plan_to_pending(db_session, test_user):
    from app.constants import BuyPlanStatus, SOVerificationStatus
    from app.services.buyplan_workflow.buyplan_approval import halt_plan, resume_plan

    mgr = _manager(db_session)
    bp = _plan(db_session, test_user, status=BuyPlanStatus.PENDING.value, so_status=SOVerificationStatus.PENDING.value)
    db_session.commit()

    halt_plan(bp.id, mgr, db_session, reason="paused pre-approval")
    db_session.commit()

    resume_plan(bp.id, mgr, db_session)
    db_session.commit()
    db_session.refresh(bp)
    # The bonus wedge: a PENDING-halted plan must NOT resurface ACTIVE (approval unowed).
    assert bp.status == BuyPlanStatus.PENDING.value
    assert bp.so_status == SOVerificationStatus.PENDING.value


def test_resumed_active_plan_can_complete(db_session, test_user):
    """The whole point: after resume, a fully-verified plan completes (the halt→resume
    wedge was the only thing stopping it)."""
    from app.constants import BuyPlanLineStatus, BuyPlanStatus, SOVerificationStatus
    from app.models.buy_plan import BuyPlanLine
    from app.services.buyplan_workflow.buyplan_approval import halt_plan, resume_plan

    mgr = _manager(db_session)
    bp = _plan(db_session, test_user, status=BuyPlanStatus.ACTIVE.value, so_status=SOVerificationStatus.APPROVED.value)
    line = BuyPlanLine(
        buy_plan_id=bp.id,
        status=BuyPlanLineStatus.VERIFIED.value,
        unit_cost=10.0,
        quantity=5,
        po_number="PO-D2",
        po_confirmed_at=datetime.now(UTC),
    )
    db_session.add(line)
    db_session.commit()

    halt_plan(bp.id, mgr, db_session, reason="brief hold")
    db_session.commit()
    resume_plan(bp.id, mgr, db_session)
    db_session.commit()
    db_session.refresh(bp)
    # resume_plan runs check_completion; an all-verified + so-APPROVED plan finishes.
    assert bp.status == BuyPlanStatus.COMPLETED.value


# ── D1 · manual completion for zero-line Testing/Comps plans ─────────────


def _lite_plan(db, requester, order_type):
    from app.constants import BuyPlanStatus, SOVerificationStatus

    bp = _make_buy_plan(db, requester)
    bp.status = BuyPlanStatus.ACTIVE.value
    bp.so_status = SOVerificationStatus.APPROVED.value
    bp.order_type = order_type
    db.flush()
    return bp


def test_creator_can_complete_testing_service_plan(db_session, test_user):
    from app.constants import BuyPlanStatus, SalesOrderType
    from app.services.buyplan_workflow import complete_lite_plan

    bp = _lite_plan(db_session, test_user, SalesOrderType.TESTING_SERVICE.value)
    db_session.commit()
    complete_lite_plan(bp.id, test_user, db_session)  # test_user is the requisition creator
    db_session.commit()
    db_session.refresh(bp)
    assert bp.status == BuyPlanStatus.COMPLETED.value  # no longer stranded ACTIVE


def test_non_creator_non_manager_cannot_complete(db_session, test_user):
    import pytest

    from app.constants import SalesOrderType
    from app.models import User
    from app.services.buyplan_workflow import complete_lite_plan

    bp = _lite_plan(db_session, test_user, SalesOrderType.COMPS.value)
    other = User(email="rando@trioscs.com", name="Rando", role="sales", azure_id="az-rando", is_active=True)
    db_session.add(other)
    db_session.commit()
    with pytest.raises(PermissionError, match="creator or a manager"):
        complete_lite_plan(bp.id, other, db_session)


def test_cannot_manually_complete_a_plan_with_lines(db_session, test_user):
    import pytest

    from app.constants import BuyPlanLineStatus, SalesOrderType
    from app.models.buy_plan import BuyPlanLine
    from app.services.buyplan_workflow import complete_lite_plan

    bp = _lite_plan(db_session, test_user, SalesOrderType.TESTING_SERVICE.value)
    db_session.add(
        BuyPlanLine(buy_plan_id=bp.id, status=BuyPlanLineStatus.AWAITING_PO.value, unit_cost=1.0, quantity=1)
    )
    db_session.commit()
    with pytest.raises(ValueError, match="completes automatically"):
        complete_lite_plan(bp.id, test_user, db_session)
