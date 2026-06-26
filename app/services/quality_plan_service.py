"""quality_plan_service.py — Business logic for creating and submitting QualityPlan
records.

Purpose:
  - create_qp: Persist a QualityPlan header in DRAFT status.
  - validate_complete: Return a list of human-readable error strings for any
    Phase-1 required fields that are blank/null. Empty list == ready to submit.
  - submit: Transition a complete QP to IN_REVIEW and write one ActivityLog event.
    Raises IncompleteQPError (carrying the missing-field list) if the QP is not yet
    complete.

Phase-1 required fields: created_by_id (owner), order_type, buy_plan_id.

Called by: app.routers.quality_plans (Task 9+).
Depends on: app.models.quality_plan (QualityPlan),
            app.services.activity_service (log_activity),
            app.constants (QualityPlanStatus, QPOrderType, ActivityType).
"""

from typing import Any

from loguru import logger
from sqlalchemy.orm import Session

from ..constants import ActivityType, QualityPlanStatus
from ..models.quality_plan import QualityPlan
from ..services.activity_service import log_activity


class IncompleteQPError(Exception):
    """Raised by submit() when validate_complete() returns a non-empty list.

    Attributes:
        missing_fields: Human-readable list of field-level error messages.
    """

    def __init__(self, missing_fields: list[str]) -> None:
        self.missing_fields = missing_fields
        super().__init__(f"Quality plan is incomplete: {missing_fields}")


def create_qp(
    db: Session,
    *,
    owner_id: int,
    buy_plan_id: int | None = None,
) -> QualityPlan:
    """Persist a new QualityPlan header in DRAFT status.

    Args:
        db: SQLAlchemy session (sync, 2.0 style).
        owner_id: FK to users.id — the user responsible for completing the plan.
        buy_plan_id: FK to buy_plans_v3.id. May be None at creation and set later
            before submit.

    Returns:
        The flushed QualityPlan ORM object (not yet committed).
    """
    qp = QualityPlan(
        created_by_id=owner_id,
        buy_plan_id=buy_plan_id,
        status=QualityPlanStatus.DRAFT,
    )
    db.add(qp)
    db.flush()
    logger.debug("Created QualityPlan id={} owner={}", qp.id, owner_id)
    return qp


def validate_complete(qp: QualityPlan) -> list[str]:
    """Return a list of human-readable error strings for missing required fields.

    Phase-1 required fields:
      - created_by_id (owner)
      - order_type
      - buy_plan_id

    An empty list means the QP is ready to submit.

    Args:
        qp: QualityPlan ORM instance (does not touch the database).

    Returns:
        List of field-level error strings; empty if the QP is complete.
    """
    errors: list[str] = []
    if not qp.created_by_id:
        errors.append("owner is required")
    if not qp.order_type:
        errors.append("order_type is required")
    if not qp.buy_plan_id:
        errors.append("buy_plan_id is required")
    return errors


def submit(
    db: Session,
    qp_id: int,
    user: Any,
) -> QualityPlan:
    """Transition a QualityPlan from DRAFT to IN_REVIEW.

    Validates all required Phase-1 fields via validate_complete() first. On success
    sets status to IN_REVIEW and writes one ActivityLog row (activity_type=APPROVAL_REQUESTED,
    buy_plan_id linked when present).

    Args:
        db: SQLAlchemy session (sync, 2.0 style).
        qp_id: PK of the QualityPlan to submit.
        user: The authenticated User performing the submission.

    Returns:
        The updated QualityPlan with status IN_REVIEW.

    Raises:
        ValueError: If the QualityPlan is not found.
        IncompleteQPError: If validate_complete() returns a non-empty error list,
            carrying the list as missing_fields.
    """
    qp = db.get(QualityPlan, qp_id)
    if qp is None:
        raise ValueError(f"QualityPlan {qp_id} not found")

    errors = validate_complete(qp)
    if errors:
        raise IncompleteQPError(errors)

    qp.status = QualityPlanStatus.IN_REVIEW

    log_activity(
        db,
        activity_type=ActivityType.APPROVAL_REQUESTED,
        user_id=user.id if user is not None else None,
        buy_plan_id=qp.buy_plan_id,
        description=f"Quality plan #{qp.id} submitted for review",
    )

    db.flush()
    logger.info("QualityPlan id={} submitted by user={}", qp.id, user.id if user else None)
    return qp
