"""service.py — ApprovalService: create_request + decide (first-responder-wins).

Purpose: The orchestration core of the Approval Engine.

         create_request persists an ApprovalRequest for a subject entity (a Prepayment
         or a QualityPlan), wires the matching subject FK, then routes it to eligible
         approvers via route_request (Task 3).

         decide resolves a request. It takes a row lock on the request
         (SELECT … FOR UPDATE — enforced on PostgreSQL, a no-op on SQLite) and guards on
         status == REQUESTED, so a concurrent or replayed second decision is rejected
         (idempotent / first-responder-wins) even where the lock is a no-op. The acting
         user must hold a PENDING recipient row on the request (else PermissionError). On
         resolution it records the recipient decision, closes the request
         (APPROVED / REJECTED), writes one audit ApprovalEvent, and enqueues a single
         "decided" ApprovalOutbox row for the notification worker.

Called by: routers/approvals.py (Task 5+), buy-plan / prepayment flows that gate on
           approval.
Depends on: app.models.approvals, app.models.quality_plan (Prepayment),
            app.services.approvals.routing.route_request, app.constants.

Note: the inline ApprovalEvent writer here is intentionally minimal; Task 5 replaces it
      with ApprovalEventService.record (YAGNI for this task).
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...constants import ApprovalRecipientStatus, ApprovalRequestStatus
from ...models.approvals import (
    ApprovalOutbox,
    ApprovalRequest,
    ApprovalStep,
    ApprovalStepRecipient,
)
from ...models.quality_plan import Prepayment, QualityPlan
from .events import record as _record_event
from .routing import route_request

# action → terminal request status / per-recipient status / audit event_type
_APPROVE = "approve"
_REJECT = "reject"
_VALID_ACTIONS = (_APPROVE, _REJECT)


def create_request(
    db: Session,
    *,
    gate_type: str,
    amount: Decimal | None,
    subject: Prepayment | QualityPlan,
    requested_by: Any,
    owner: Any,
) -> ApprovalRequest:
    """Persist an ApprovalRequest for *subject* and route it to eligible approvers.

    Args:
        db: SQLAlchemy session (sync, 2.0 style).
        gate_type: An ApprovalGateType value (the gate this request belongs to).
        amount: Spend amount used for threshold routing (may be None for non-spend gates).
        subject: The entity being approved — a Prepayment or a QualityPlan. The matching
            subject_*_id FK is set from its id.
        requested_by: The User who triggered the request.
        owner: The User who owns the originating entity (notified on resolution).

    Returns:
        The flushed ApprovalRequest, already routed (its steps/recipients exist in-session).

    Raises:
        TypeError: If *subject* is neither a Prepayment nor a QualityPlan.
        NoEligibleApproverError: Propagated from route_request when no approver is eligible.
    """
    request = ApprovalRequest(
        gate_type=gate_type,
        amount=amount,
        status=ApprovalRequestStatus.REQUESTED,
        requested_by_id=requested_by.id if requested_by is not None else None,
        owner_id=owner.id if owner is not None else None,
    )

    if isinstance(subject, Prepayment):
        request.subject_prepayment_id = subject.id
    elif isinstance(subject, QualityPlan):
        request.subject_quality_plan_id = subject.id
    else:
        raise TypeError(f"subject must be a Prepayment or QualityPlan, got {type(subject).__name__}")

    db.add(request)
    db.flush()  # Assign request.id before routing

    route_request(db, request)
    return request


def decide(
    db: Session,
    request_id: int,
    user: Any,
    action: str,
    comment: str | None = None,
) -> ApprovalRequest:
    """Resolve an ApprovalRequest as *user* (first-responder-wins, idempotent).

    Args:
        db: SQLAlchemy session (sync, 2.0 style).
        request_id: PK of the ApprovalRequest to decide.
        user: The acting User — must hold a PENDING recipient row on the request.
        action: "approve" or "reject".
        comment: Decision note. Required (non-blank) for "reject".

    Returns:
        The resolved ApprovalRequest (status APPROVED or REJECTED).

    Raises:
        ValueError: Unknown action, missing request, request not in REQUESTED state
            (already decided / cancelled / expired), or a blank reject comment.
        PermissionError: *user* has no PENDING recipient row on this request.
    """
    if action not in _VALID_ACTIONS:
        raise ValueError(f"action must be one of {_VALID_ACTIONS}, got {action!r}")

    if action == _REJECT and not (comment or "").strip():
        raise ValueError("reject requires a non-blank comment")

    # Row-locked read. The lock serializes concurrent deciders on PostgreSQL; SQLite
    # ignores FOR UPDATE, so the status guard below is what enforces idempotency there.
    request = db.execute(
        select(ApprovalRequest).where(ApprovalRequest.id == request_id).with_for_update()
    ).scalar_one_or_none()

    if request is None:
        raise ValueError(f"ApprovalRequest {request_id} not found")

    # Idempotency / first-responder-wins: only an open request can be decided. A second
    # (concurrent or replayed) decision finds a terminal status and is rejected here.
    if request.status != ApprovalRequestStatus.REQUESTED:
        raise ValueError(f"ApprovalRequest {request_id} is not open (status={request.status})")

    # The acting user must hold a PENDING recipient row on one of this request's steps.
    recipient = db.execute(
        select(ApprovalStepRecipient)
        .join(ApprovalStep, ApprovalStepRecipient.step_id == ApprovalStep.id)
        .where(
            ApprovalStep.request_id == request_id,
            ApprovalStepRecipient.user_id == user.id,
            ApprovalStepRecipient.status == ApprovalRecipientStatus.PENDING,
        )
    ).scalar_one_or_none()

    if recipient is None:
        raise PermissionError(f"User {user.id} is not a pending recipient of request {request_id}")

    now = datetime.now(timezone.utc)
    approved = action == _APPROVE

    # Record this recipient's decision.
    recipient.status = ApprovalRecipientStatus.APPROVED if approved else ApprovalRecipientStatus.REJECTED
    recipient.decided_at = now
    recipient.decision_note = comment

    # Close the request.
    request.status = ApprovalRequestStatus.APPROVED if approved else ApprovalRequestStatus.REJECTED
    request.resolved_at = now
    request.resolution_note = comment

    # Audit trail — one ApprovalEvent + one ActivityLog via ApprovalEventService.
    event_type = "approved" if approved else "rejected"
    _record_event(db, request, user, event_type, metadata={"comment": comment} if comment else None)

    # Enqueue exactly one notification for the request owner (fall back to requester,
    # then the decider) — recipient_user_id is NOT NULL.
    notify_user_id = request.owner_id or request.requested_by_id or user.id
    db.add(
        ApprovalOutbox(
            request_id=request.id,
            recipient_user_id=notify_user_id,
            channel="in_app",
            payload={"event_type": "decided", "decision": event_type},
        )
    )

    db.flush()
    return request
