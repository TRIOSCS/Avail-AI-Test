"""routing.py — RoutingService: eligibility filtering + step/recipient creation.

Purpose: Given an ApprovalRequest, reads active ApprovalGateConfig rows for the
         request's gate_type, filters by amount eligibility (max_amount IS NULL OR
         request.amount <= max_amount), creates one ApprovalStep (rule=ANY) and one
         ApprovalStepRecipient (status=PENDING) per eligible approver.

         Raises NoEligibleApproverError when no active, amount-eligible config exists
         for the gate — caller must handle this (e.g. block the action or alert admins).

Called by: app.services.approvals (re-exported), future ApprovalService (Task 4+)
Depends on: app.models.approvals, app.constants
"""

from sqlalchemy.orm import Session

from ...constants import ApprovalRecipientStatus, ApprovalStepRule
from ...models.approvals import (
    ApprovalGateConfig,
    ApprovalRequest,
    ApprovalStep,
    ApprovalStepRecipient,
)


class NoEligibleApproverError(Exception):
    """Raised when no active, amount-eligible approver exists for the gate."""


def route_request(db: Session, request: ApprovalRequest) -> ApprovalStep:
    """Create one ApprovalStep + one ApprovalStepRecipient per eligible approver.

    Eligibility criteria (both must hold):
    - config.active is True
    - config.max_amount IS NULL  OR  request.amount <= config.max_amount

    Args:
        db: SQLAlchemy session (sync, 2.0 style).
        request: Flushed ApprovalRequest row (must have .id, .gate_type, .amount).

    Returns:
        The newly created ApprovalStep (with .recipients already populated in-session).

    Raises:
        NoEligibleApproverError: If zero eligible configs exist for the gate.
    """
    configs: list[ApprovalGateConfig] = (
        db.query(ApprovalGateConfig)
        .filter(
            ApprovalGateConfig.gate_type == request.gate_type,
            ApprovalGateConfig.active.is_(True),
        )
        .all()
    )

    # Filter in Python to avoid db-dialect differences for Numeric comparison with NULL
    eligible = [cfg for cfg in configs if cfg.max_amount is None or request.amount <= cfg.max_amount]

    if not eligible:
        raise NoEligibleApproverError(f"No eligible approver for gate={request.gate_type!r} amount={request.amount}")

    step = ApprovalStep(
        request_id=request.id,
        seq=1,
        rule=ApprovalStepRule.ANY,
    )
    db.add(step)
    db.flush()  # Assign step.id before creating recipients

    for cfg in eligible:
        recipient = ApprovalStepRecipient(
            step_id=step.id,
            user_id=cfg.approver_user_id,
            status=ApprovalRecipientStatus.PENDING,
        )
        db.add(recipient)

    db.flush()
    return step
