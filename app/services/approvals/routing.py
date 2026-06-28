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
  purchase_order → User.can_approve_pos (no amount limit)

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

    if gate == ApprovalGateType.BUY_PLAN:
        candidates = (
            db.query(User)
            .filter(
                User.is_active.is_(True),
                User.can_approve_buy_plans.is_(True),
            )
            .all()
        )
        # No amount check for buy_plan gate
        eligible = candidates

    elif gate == ApprovalGateType.PREPAYMENT:
        candidates = (
            db.query(User)
            .filter(
                User.is_active.is_(True),
                User.can_approve_prepayments.is_(True),
            )
            .all()
        )
        # Filter in Python to handle NULL limit (unlimited) vs capped limit
        amount = request.amount
        eligible = [
            u
            for u in candidates
            if u.prepayment_approval_limit is None or (amount is not None and amount <= u.prepayment_approval_limit)
        ]

    elif gate == ApprovalGateType.QP_SALES:
        # QP Sales section: route to every active user holding can_approve_qp_sales.
        # No amount check (the SO gate approves the section, not a spend).
        eligible = (
            db.query(User)
            .filter(
                User.is_active.is_(True),
                User.can_approve_qp_sales.is_(True),
            )
            .all()
        )

    elif gate == ApprovalGateType.PURCHASE_ORDER:
        # QP Purchasing section: route to every active user holding can_approve_pos.
        # No amount check (the PO gate approves the section, not a spend).
        eligible = (
            db.query(User)
            .filter(
                User.is_active.is_(True),
                User.can_approve_pos.is_(True),
            )
            .all()
        )

    else:
        raise NoEligibleApproverError(f"No routing rule defined for gate={gate!r}")

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
