"""test_prepayment_dangling_cancel.py — teardown sweep: a plan that dies voids its
pending prepayment (wire) approvals (simulation finding #2, Task 9 extended).

THE RISK: an open (REQUESTED) PREPAYMENT ApprovalRequest is never cancelled when its
PO's plan dies, so a manager could approve a wire for a cancelled / halted / completed
deal, or a re-sourced PO (vendor changed underneath). ``_cancel_open_prepayment_requests_
for_plan`` sweeps those REQUESTED rows to CANCELLED from cancel / halt / complete /
re-source. Idempotent: an already-APPROVED request (about to be wired) is left alone — a
claw-back of an approved wire needs the follow-up VOID lifecycle state.

Called by: pytest
Depends on: app.services.buyplan_workflow (cancel_buy_plan, halt_plan, check_completion,
            resource_line, _cancel_open_prepayment_requests_for_plan),
            app.services.prepayment_service (create_prepayment),
            tests.test_po_line_signoff (_make_user/_make_plan/_make_line),
            tests.test_prepayment_service_line (_prepay_approver).
"""

from decimal import Decimal

from sqlalchemy.orm import Session

from app.constants import (
    ApprovalRequestStatus,
    BuyPlanLineStatus,
    BuyPlanStatus,
    LineResourceReason,
)
from app.models.approvals import ApprovalRequest
from app.services.buyplan_workflow import (
    _cancel_open_prepayment_requests_for_plan,
    cancel_buy_plan,
    check_completion,
    halt_plan,
    resource_line,
)
from app.services.prepayment_service import create_prepayment

# reuse the plan/line/user fixtures from the sibling prepayment + PO-line suites
from tests.test_po_line_signoff import _make_line, _make_plan
from tests.test_prepayment_service_line import _prepay_approver


def _prepay(
    db: Session,
    user,
    plan,
    line,
    *,
    amount: str = "5000.00",
) -> tuple[object, ApprovalRequest]:
    """Create a Prepayment + its routed (REQUESTED) PREPAYMENT ApprovalRequest."""
    pp, req = create_prepayment(
        db,
        buy_plan_id=plan.id,
        buy_plan_line_id=line.id,
        vendor_card_id=None,
        payment_method="wire",
        total_incl_fees=Decimal(amount),
        test_report_sent=False,
        buyer_remarks=None,
        created_by=user,
    )
    assert req.status == ApprovalRequestStatus.REQUESTED
    return pp, req


def test_resource_line_voids_pending_prepayment(db_session: Session) -> None:
    """Re-sourcing a line (PO/vendor changed) cancels the plan's pending prepayment."""
    u = _prepay_approver(db_session)
    plan = _make_plan(db_session, u, status=BuyPlanStatus.ACTIVE.value)
    line = _make_line(db_session, plan)  # PENDING_VERIFY, has PO → resourceable + prepayable
    db_session.commit()
    _pp, req = _prepay(db_session, u, plan, line)

    resource_line(plan.id, line.id, LineResourceReason.DEFECTIVE.value, "bad", u, db_session)

    db_session.refresh(req)
    assert req.status == ApprovalRequestStatus.CANCELLED
    assert req.resolved_at is not None
    assert req.resolution_note == "buy plan line re-sourced — prepayment voided"


def test_resource_line_covers_all_resourced_lines(db_session: Session) -> None:
    """A multi-line re-source (also_line_ids) voids EVERY line's pending prepayment."""
    u = _prepay_approver(db_session)
    plan = _make_plan(db_session, u, status=BuyPlanStatus.ACTIVE.value)
    line_a = _make_line(db_session, plan, po_number="PO-A")
    line_b = _make_line(db_session, plan, po_number="PO-B")
    db_session.commit()
    _pp_a, req_a = _prepay(db_session, u, plan, line_a)
    _pp_b, req_b = _prepay(db_session, u, plan, line_b)

    resource_line(
        plan.id, line_a.id, LineResourceReason.WRONG_PART.value, None, u, db_session, also_line_ids=[line_b.id]
    )

    db_session.refresh(req_a)
    db_session.refresh(req_b)
    assert req_a.status == ApprovalRequestStatus.CANCELLED
    assert req_b.status == ApprovalRequestStatus.CANCELLED


def test_cancel_buy_plan_voids_pending_prepayment(db_session: Session) -> None:
    u = _prepay_approver(db_session)
    plan = _make_plan(db_session, u, status=BuyPlanStatus.ACTIVE.value)
    line = _make_line(db_session, plan)
    db_session.commit()
    _pp, req = _prepay(db_session, u, plan, line)

    cancel_buy_plan(plan.id, u, db_session, reason="customer pulled out")

    db_session.refresh(req)
    assert req.status == ApprovalRequestStatus.CANCELLED
    assert req.resolution_note == "buy plan cancelled — prepayment voided"


def test_halt_plan_voids_pending_prepayment(db_session: Session) -> None:
    u = _prepay_approver(db_session)  # role=admin → may halt
    plan = _make_plan(db_session, u, status=BuyPlanStatus.ACTIVE.value)
    line = _make_line(db_session, plan)
    db_session.commit()
    _pp, req = _prepay(db_session, u, plan, line)

    halt_plan(plan.id, u, db_session, reason="on hold")

    db_session.refresh(req)
    assert req.status == ApprovalRequestStatus.CANCELLED
    assert req.resolution_note == "buy plan halted — prepayment voided"


def test_complete_plan_voids_pending_prepayment(db_session: Session) -> None:
    """A completed deal must not leave a pending wire request behind."""
    u = _prepay_approver(db_session)
    plan = _make_plan(db_session, u, status=BuyPlanStatus.ACTIVE.value)
    # VERIFIED line: terminal for completion AND prepayable (has PO, VERIFIED accepted).
    line = _make_line(db_session, plan, status=BuyPlanLineStatus.VERIFIED.value)
    db_session.commit()
    _pp, req = _prepay(db_session, u, plan, line)

    check_completion(plan.id, db_session)

    db_session.refresh(plan)
    db_session.refresh(req)
    assert plan.status == BuyPlanStatus.COMPLETED.value
    assert req.status == ApprovalRequestStatus.CANCELLED
    assert req.resolution_note == "buy plan completed — pending prepayment voided"


def test_approved_prepayment_not_touched_on_teardown(db_session: Session) -> None:
    """Only REQUESTED requests are swept — an already-APPROVED wire is left alone.

    (Clawing back an approved wire needs the follow-up VOID lifecycle state; the sweep
    must not silently flip an approved authorisation to cancelled.)
    """
    u = _prepay_approver(db_session)
    plan = _make_plan(db_session, u, status=BuyPlanStatus.ACTIVE.value)
    line = _make_line(db_session, plan)
    db_session.commit()
    _pp, req = _prepay(db_session, u, plan, line)

    # Manager approves the wire, THEN the plan is torn down.
    req.status = ApprovalRequestStatus.APPROVED
    db_session.flush()

    cancel_buy_plan(plan.id, u, db_session, reason="cancelled after approval")

    db_session.refresh(req)
    assert req.status == ApprovalRequestStatus.APPROVED  # untouched


def test_helper_is_idempotent_and_counts(db_session: Session) -> None:
    """Direct helper call returns the swept count; a second call is a no-op (0)."""
    u = _prepay_approver(db_session)
    plan = _make_plan(db_session, u, status=BuyPlanStatus.ACTIVE.value)
    line = _make_line(db_session, plan)
    db_session.commit()
    _pp, _req = _prepay(db_session, u, plan, line)

    first = _cancel_open_prepayment_requests_for_plan(plan.id, db_session, "swept")
    second = _cancel_open_prepayment_requests_for_plan(plan.id, db_session, "swept")
    assert first == 1
    assert second == 0
