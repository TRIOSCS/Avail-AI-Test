"""routing.py — RoutingService: eligibility filtering + step/recipient creation.

Purpose: Given an ApprovalRequest, queries active Users for the request's gate_type
         using per-gate per-user toggles on the User model (can_approve_buy_plans,
         can_approve_prepayments + prepayment_approval_limit). Creates one
         ApprovalStep (rule=ANY) and one ApprovalStepRecipient (status=PENDING) per
         eligible approver.

         Raises NoEligibleApproverError when no active, amount-eligible user exists
         for the gate — caller must handle this (e.g. block the action or alert admins).

Gate → column map:
  buy_plan       → User.can_approve_buy_plans (no amount limit)
  prepayment     → User.can_approve_prepayments + optional prepayment_approval_limit
  qp_sales       → User.can_approve_qp_sales (no amount limit)
  qp_purchasing  → User.can_approve_qp_purchasing (no amount limit)
  purchase_order → User.can_approve_purchase_orders + optional purchase_order_approval_limit

Called by: app.services.approvals (re-exported), ApprovalService (Task 4+)
Depends on: app.models.approvals, app.models.auth, app.constants
"""

from sqlalchemy.orm import Session

from ...constants import ApprovalGateType, ApprovalRecipientStatus, ApprovalStepRule
from ...models.approvals import (
    ApprovalRequest,
    ApprovalStep,
    ApprovalStepRecipient,
)
from ...models.auth import User


class NoEligibleApproverError(Exception):
    """Raised when no active, amount-eligible approver exists for the gate."""


def _eligible_approvers(db: Session, gate: str, amount) -> list[User]:
    """Active users eligible to approve *gate* for *amount* (per-gate per-user toggles).

    The single source of truth for approver eligibility, shared by ``route_request`` (which
    creates the recipients) and ``has_eligible_approver`` (which only asks whether any exist).
    Amount-limited gates (prepayment, purchase_order) filter their NULL-means-unlimited limit
    in Python.
    """
    if gate == ApprovalGateType.BUY_PLAN:
        return db.query(User).filter(User.is_active.is_(True), User.can_approve_buy_plans.is_(True)).all()
    if gate == ApprovalGateType.PREPAYMENT:
        candidates = db.query(User).filter(User.is_active.is_(True), User.can_approve_prepayments.is_(True)).all()
        return [
            u
            for u in candidates
            if u.prepayment_approval_limit is None or (amount is not None and amount <= u.prepayment_approval_limit)
        ]
    if gate == ApprovalGateType.QP_SALES:
        return db.query(User).filter(User.is_active.is_(True), User.can_approve_qp_sales.is_(True)).all()
    if gate == ApprovalGateType.QP_PURCHASING:
        return db.query(User).filter(User.is_active.is_(True), User.can_approve_qp_purchasing.is_(True)).all()
    if gate == ApprovalGateType.PURCHASE_ORDER:
        candidates = db.query(User).filter(User.is_active.is_(True), User.can_approve_purchase_orders.is_(True)).all()
        return [
            u
            for u in candidates
            if u.purchase_order_approval_limit is None
            or (amount is not None and amount <= u.purchase_order_approval_limit)
        ]
    raise NoEligibleApproverError(f"No routing rule defined for gate={gate!r}")


def has_eligible_approver(db: Session, gate: ApprovalGateType, amount=None) -> bool:
    """True if at least one active user can approve *gate* for *amount*.

    Read-only counterpart to ``route_request``'s eligibility. Used to detect (and surface in
    the UI) a plan that will silently stall because no approver is configured for its open
    gate — otherwise ``NoEligibleApproverError`` is only logged and the plan sits invisibly.
    """
    try:
        return bool(_eligible_approvers(db, gate, amount))
    except NoEligibleApproverError:
        return False


def route_request(db: Session, request: ApprovalRequest) -> ApprovalStep:
    """Create one ApprovalStep + one ApprovalStepRecipient per eligible approver.

    Eligibility is determined by per-gate per-user toggles on the User model:

    - buy_plan gate: User.can_approve_buy_plans is True (no amount check).
    - prepayment gate: User.can_approve_prepayments is True AND
      (prepayment_approval_limit IS NULL OR request.amount <= limit).

    Args:
        db: SQLAlchemy session (sync, 2.0 style).
        request: Flushed ApprovalRequest row (must have .id, .gate_type, .amount).

    Returns:
        The newly created ApprovalStep (with .recipients already populated in-session).

    Raises:
        NoEligibleApproverError: If zero eligible users exist for the gate.
    """
    gate = request.gate_type
    eligible = _eligible_approvers(db, gate, request.amount)
    if not eligible:
        raise NoEligibleApproverError(f"No eligible approver for gate={gate!r} amount={request.amount}")

    step = ApprovalStep(
        request_id=request.id,
        seq=1,
        rule=ApprovalStepRule.ANY,
    )
    db.add(step)
    db.flush()  # assign step.id before creating recipients

    for user in eligible:
        recipient = ApprovalStepRecipient(
            step_id=step.id,
            user_id=user.id,
            status=ApprovalRecipientStatus.PENDING,
        )
        db.add(recipient)

    db.flush()
    return step
