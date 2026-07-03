"""test_prepayment_dangling_cancel.py — teardown sweep: a plan that dies stands down its
in-flight prepayment (wire) authorisations (simulation finding #2 + Task 7 void-on-
teardown).

THE RISK: an in-flight PREPAYMENT authorisation is never stood down when its PO's plan dies,
so a wire could go out for a cancelled / halted / completed deal, or a re-sourced PO (vendor
changed underneath). ``_cancel_open_prepayment_requests_for_plan`` closes it in two parts from
cancel / halt / complete / re-source: (1) REQUESTED requests → CANCELLED; (2) APPROVED-but-
unwired ``Prepayment``s → ``void`` (pay_token cleared) + a DO-NOT-WIRE stand-down. A ``paid``
prepayment is NEVER touched — the wire already went out (no auto claw-back).

Called by: pytest
Depends on: app.services.buyplan_workflow (cancel_buy_plan, halt_plan, check_completion,
            resource_line, _cancel_open_prepayment_requests_for_plan),
            app.services.prepayment_service (create_prepayment),
            tests.test_po_line_signoff (_make_user/_make_plan/_make_line),
            tests.test_prepayment_service_line (_prepay_approver).
"""

import uuid
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy.orm import Session

from app.constants import (
    ApprovalGateType,
    ApprovalRequestStatus,
    ApprovalSubjectType,
    BuyPlanLineStatus,
    BuyPlanStatus,
    LineResourceReason,
    PrepaymentStatus,
)
from app.models.approvals import ApprovalRequest
from app.models.quality_plan import Prepayment
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


def test_resource_line_leaves_sibling_line_prepayment(db_session: Session) -> None:
    """Re-sourcing line A must NOT cancel a legitimate REQUESTED prepayment on sibling
    line B of the SAME plan.

    resource_line is line-scoped: only the resourced line's wire
    is stale (its PO/vendor changed); B's authorised-in-flight wire stays intact.
    """
    u = _prepay_approver(db_session)
    plan = _make_plan(db_session, u, status=BuyPlanStatus.ACTIVE.value)
    line_a = _make_line(db_session, plan, po_number="PO-A")
    line_b = _make_line(db_session, plan, po_number="PO-B")
    db_session.commit()
    _pp_a, req_a = _prepay(db_session, u, plan, line_a)
    _pp_b, req_b = _prepay(db_session, u, plan, line_b)

    resource_line(plan.id, line_a.id, LineResourceReason.DEFECTIVE.value, "bad", u, db_session)

    db_session.refresh(req_a)
    db_session.refresh(req_b)
    assert req_a.status == ApprovalRequestStatus.CANCELLED  # line A's wire voided
    assert req_b.status == ApprovalRequestStatus.REQUESTED  # sibling B untouched


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


# ── Task 7: void an APPROVED-but-unwired prepayment on teardown + stand-down ──


def _approved_prepay(db: Session, user, plan, line, *, amount: str = "5000.00") -> Prepayment:
    """An APPROVED prepayment (a live pay_token, an approved PREPAYMENT ApprovalRequest)
    on *line* — the about-to-be-wired state a plan teardown must stand down."""
    pp = Prepayment(
        buy_plan_id=plan.id,
        buy_plan_line_id=line.id,
        total_incl_fees=Decimal(amount),
        currency="USD",
        payment_method="wire",
        created_by_id=user.id,
        status=PrepaymentStatus.APPROVED.value,
        pay_token=f"tok-{uuid.uuid4().hex}",
    )
    db.add(pp)
    db.flush()
    db.add(
        ApprovalRequest(
            gate_type=ApprovalGateType.PREPAYMENT,
            status=ApprovalRequestStatus.APPROVED,
            subject_type=ApprovalSubjectType.PREPAYMENT,
            subject_id=pp.id,
            requested_by_id=user.id,
            owner_id=user.id,
        )
    )
    db.commit()
    return pp


def test_cancel_voids_approved_prepayment_and_stands_down(db_session: Session) -> None:
    """Cancelling a plan voids its APPROVED (unwired) prepayment, clears the pay_token,
    and dispatches the DO-NOT-WIRE stand-down."""
    u = _prepay_approver(db_session)
    plan = _make_plan(db_session, u, status=BuyPlanStatus.ACTIVE.value)
    line = _make_line(db_session, plan)
    db_session.commit()
    pp = _approved_prepay(db_session, u, plan, line)

    with patch("app.services.prepayment_notifications.run_prepayment_notify_bg") as bg:
        cancel_buy_plan(plan.id, u, db_session, reason="customer pulled out")

    db_session.refresh(pp)
    assert pp.status == PrepaymentStatus.VOID.value
    assert pp.pay_token is None
    assert pp.voided_at is not None
    assert pp.void_reason == "buy plan cancelled — prepayment voided"
    # The stand-down (notify_prepayment_voided) was dispatched for this prepayment.
    assert any(
        c.args and c.args[0].__name__ == "notify_prepayment_voided" and c.args[1] == pp.id for c in bg.call_args_list
    )


def test_halt_voids_approved_prepayment(db_session: Session) -> None:
    u = _prepay_approver(db_session)  # role=admin → may halt
    plan = _make_plan(db_session, u, status=BuyPlanStatus.ACTIVE.value)
    line = _make_line(db_session, plan)
    db_session.commit()
    pp = _approved_prepay(db_session, u, plan, line)

    with patch("app.services.prepayment_notifications.run_prepayment_notify_bg"):
        halt_plan(plan.id, u, db_session, reason="on hold")

    db_session.refresh(pp)
    assert pp.status == PrepaymentStatus.VOID.value
    assert pp.pay_token is None


def test_complete_voids_approved_prepayment(db_session: Session) -> None:
    u = _prepay_approver(db_session)
    plan = _make_plan(db_session, u, status=BuyPlanStatus.ACTIVE.value)
    line = _make_line(db_session, plan, status=BuyPlanLineStatus.VERIFIED.value)
    db_session.commit()
    pp = _approved_prepay(db_session, u, plan, line)

    with patch("app.services.prepayment_notifications.run_prepayment_notify_bg"):
        check_completion(plan.id, db_session)

    db_session.refresh(plan)
    db_session.refresh(pp)
    assert plan.status == BuyPlanStatus.COMPLETED.value
    assert pp.status == PrepaymentStatus.VOID.value
    assert pp.pay_token is None


def test_resource_voids_approved_prepayment_line_scoped(db_session: Session) -> None:
    """Re-sourcing a line voids that line's APPROVED prepayment but not a sibling's."""
    u = _prepay_approver(db_session)
    plan = _make_plan(db_session, u, status=BuyPlanStatus.ACTIVE.value)
    line_a = _make_line(db_session, plan, po_number="PO-A")
    line_b = _make_line(db_session, plan, po_number="PO-B")
    db_session.commit()
    pp_a = _approved_prepay(db_session, u, plan, line_a)
    pp_b = _approved_prepay(db_session, u, plan, line_b)

    with patch("app.services.prepayment_notifications.run_prepayment_notify_bg"):
        resource_line(plan.id, line_a.id, LineResourceReason.DEFECTIVE.value, "bad", u, db_session)

    db_session.refresh(pp_a)
    db_session.refresh(pp_b)
    assert pp_a.status == PrepaymentStatus.VOID.value  # line A's approved wire stood down
    assert pp_b.status == PrepaymentStatus.APPROVED.value  # sibling untouched
    assert pp_b.pay_token is not None


def test_paid_prepayment_is_never_voided_on_teardown(db_session: Session) -> None:
    """A PAID prepayment (the wire already went out) must survive a plan teardown
    untouched — it is never auto-voided (no claw-back on a real wire)."""
    u = _prepay_approver(db_session)
    plan = _make_plan(db_session, u, status=BuyPlanStatus.ACTIVE.value)
    line = _make_line(db_session, plan)
    db_session.commit()
    pp = Prepayment(
        buy_plan_id=plan.id,
        buy_plan_line_id=line.id,
        total_incl_fees=Decimal("5000.00"),
        currency="USD",
        created_by_id=u.id,
        status=PrepaymentStatus.PAID.value,
        wire_reference="WIRE-DONE",
        pay_token=None,
    )
    db_session.add(pp)
    db_session.commit()

    with patch("app.services.prepayment_notifications.run_prepayment_notify_bg") as bg:
        cancel_buy_plan(plan.id, u, db_session, reason="cancelled after wire")

    db_session.refresh(pp)
    assert pp.status == PrepaymentStatus.PAID.value  # untouched
    assert pp.wire_reference == "WIRE-DONE"
    # No stand-down was dispatched for a paid prepayment.
    assert not any(c.args and c.args[0].__name__ == "notify_prepayment_voided" for c in bg.call_args_list)
