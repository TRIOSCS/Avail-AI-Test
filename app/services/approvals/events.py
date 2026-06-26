"""events.py — ApprovalEventService: append-only audit trail + reassign/cancel.

Purpose: Provides three public functions for the Approval Engine:
  - record(db, request, actor, event_type, metadata=None)
      Appends an ApprovalEvent row (immutable audit) and a summary ActivityLog row.
  - reassign(db, request_id, from_user, to_user, actor)
      Moves from_user's PENDING recipient to REASSIGNED (sets reassigned_to_id),
      adds a new PENDING recipient for to_user, records a 'reassigned' event.
  - cancel(db, request_id, actor)
      Raises PermissionError if the actor is neither the requester/owner nor a
      manager/admin; raises ValueError if the request is not in REQUESTED status;
      else sets status CANCELLED and records a 'cancelled' event.

Called by: app.services.approvals.service (decide, reassign, cancel entrypoints),
           app.routers.approvals (future cancel/reassign endpoints).
Depends on: app.models.approvals, app.services.activity_service.log_activity,
            app.dependencies.is_manager_or_admin,
            app.constants (ActivityType, ApprovalRequestStatus, ApprovalRecipientStatus).
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...constants import (
    ActivityType,
    ApprovalRecipientStatus,
    ApprovalRequestStatus,
)
from ...models.approvals import (
    ApprovalEvent,
    ApprovalRequest,
    ApprovalStep,
    ApprovalStepRecipient,
)
from ..activity_service import log_activity

# Map from event_type string → ActivityType member used in the ActivityLog summary.
_EVENT_TO_ACTIVITY_TYPE: dict[str, ActivityType] = {
    "submitted": ActivityType.APPROVAL_REQUESTED,
    "approved": ActivityType.APPROVAL_APPROVED,
    "rejected": ActivityType.APPROVAL_REJECTED,
    "reassigned": ActivityType.APPROVAL_DELEGATED,
    "cancelled": ActivityType.APPROVAL_CANCELLED,
}

# ActivityType to use for any event_type not in the map above.
_DEFAULT_ACTIVITY_TYPE = ActivityType.APPROVAL_REQUESTED


def record(
    db: Session,
    request: ApprovalRequest,
    actor: Any,
    event_type: str,
    metadata: dict | None = None,
) -> ApprovalEvent:
    """Append one ApprovalEvent row and one ActivityLog summary row.

    Args:
        db: SQLAlchemy session (sync, 2.0 style).
        request: The ApprovalRequest being audited.
        actor: The User performing the action (may be None for system events).
        event_type: Short label, e.g. 'submitted', 'approved', 'rejected',
            'reassigned', 'cancelled'.
        metadata: Optional extra structured context stored in payload.

    Returns:
        The flushed ApprovalEvent row.
    """
    event = ApprovalEvent(
        request_id=request.id,
        actor_id=actor.id if actor is not None else None,
        event_type=event_type,
        payload=metadata,
    )
    db.add(event)

    activity_type = _EVENT_TO_ACTIVITY_TYPE.get(event_type, _DEFAULT_ACTIVITY_TYPE)
    log_activity(
        db,
        activity_type=activity_type,
        user_id=actor.id if actor is not None else None,
        description=f"Approval request #{request.id} — {event_type}",
    )

    db.flush()
    return event


def reassign(
    db: Session,
    request_id: int,
    from_user: Any,
    to_user: Any,
    actor: Any,
) -> ApprovalStepRecipient:
    """Reassign a PENDING recipient slot from from_user to to_user.

    Sets from_user's recipient status to REASSIGNED and records reassigned_to_id.
    Adds a new PENDING ApprovalStepRecipient for to_user on the same step.
    Records a 'reassigned' ApprovalEvent + ActivityLog.

    Args:
        db: SQLAlchemy session (sync, 2.0 style).
        request_id: PK of the ApprovalRequest.
        from_user: The User whose PENDING slot is being handed off.
        to_user: The User who will receive the new PENDING slot.
        actor: The User performing the reassignment (for audit).

    Returns:
        The new PENDING ApprovalStepRecipient for to_user.

    Raises:
        ValueError: If from_user has no PENDING recipient row on the request.
    """
    from_recipient = db.execute(
        select(ApprovalStepRecipient)
        .join(ApprovalStep, ApprovalStepRecipient.step_id == ApprovalStep.id)
        .where(
            ApprovalStep.request_id == request_id,
            ApprovalStepRecipient.user_id == from_user.id,
            ApprovalStepRecipient.status == ApprovalRecipientStatus.PENDING,
        )
    ).scalar_one_or_none()

    if from_recipient is None:
        raise ValueError(f"User {from_user.id} has no pending recipient row on request {request_id}")

    # Mark the original slot as reassigned.
    from_recipient.status = ApprovalRecipientStatus.REASSIGNED
    from_recipient.reassigned_to_id = to_user.id

    # Add the new recipient on the same step.
    new_recipient = ApprovalStepRecipient(
        step_id=from_recipient.step_id,
        user_id=to_user.id,
        status=ApprovalRecipientStatus.PENDING,
    )
    db.add(new_recipient)
    db.flush()

    # Row-locked read (matches decide()): serializes against a concurrent decide()
    # that could move the request to a terminal status, which would otherwise leave
    # this new PENDING recipient orphaned on an already-resolved request. SQLite
    # ignores FOR UPDATE; the status guard below is what enforces correctness there.
    request = db.execute(
        select(ApprovalRequest).where(ApprovalRequest.id == request_id).with_for_update()
    ).scalar_one_or_none()
    if request is None:
        raise ValueError(f"ApprovalRequest {request_id} not found")
    if request.status != ApprovalRequestStatus.REQUESTED:
        raise ValueError(f"ApprovalRequest {request_id} is not open (status={request.status!r})")

    record(
        db,
        request,
        actor,
        "reassigned",
        metadata={"from_user_id": from_user.id, "to_user_id": to_user.id},
    )
    return new_recipient


def cancel(
    db: Session,
    request_id: int,
    actor: Any,
) -> ApprovalRequest:
    """Cancel an open ApprovalRequest.

    Args:
        db: SQLAlchemy session (sync, 2.0 style).
        request_id: PK of the ApprovalRequest to cancel.
        actor: The User performing the cancellation (for audit).

    Returns:
        The updated ApprovalRequest with status CANCELLED.

    Raises:
        PermissionError: If *actor* is neither the requester nor the owner of the
            request, and is not a manager/admin. (Authz/ownership — IDOR guard.)
        ValueError: If the request is not found, or not in REQUESTED status
            (already decided/cancelled).
    """
    from ...dependencies import is_manager_or_admin

    # Row-locked read (matches decide()): serializes against a concurrent decide()
    # so a cancel can't race a committed decision into a contradictory status. SQLite
    # ignores FOR UPDATE; the status guard below is what enforces correctness there.
    request = db.execute(
        select(ApprovalRequest).where(ApprovalRequest.id == request_id).with_for_update()
    ).scalar_one_or_none()
    if request is None:
        raise ValueError(f"ApprovalRequest {request_id} not found")

    # Ownership/authz: only the requester, the owner, or a manager/admin may cancel
    # someone's open approval request (prevents the cross-user cancel IDOR).
    actor_id = actor.id if actor is not None else None
    is_requester_or_owner = actor_id is not None and actor_id in (request.requested_by_id, request.owner_id)
    if not (is_requester_or_owner or (actor is not None and is_manager_or_admin(actor))):
        raise PermissionError(f"User {actor_id} may not cancel approval request {request_id}")

    if request.status != ApprovalRequestStatus.REQUESTED:
        raise ValueError(f"ApprovalRequest {request_id} is not open (status={request.status!r})")

    request.status = ApprovalRequestStatus.CANCELLED
    record(db, request, actor, "cancelled")
    return request
