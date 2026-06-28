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

QP Phase C2a adds the per-section approval-gate submit/resolve helpers:
  - submit_section: open a QP_SALES / PURCHASE_ORDER ApprovalRequest for the QP
    (the QP is the subject; the gate_type discriminates the section). A missing approver
    surfaces NoSectionApproverError so the router can show an inline banner, not a 500.
  - _on_section_approved: the on-resolve hook the engine calls inside decide() (C2a logs
    an activity; C2b writes the section timestamp).

Called by: app.routers.quality_plans (Task 9+), app.services.approvals.service (decide,
           lazy import of _on_section_approved).
Depends on: app.models.quality_plan (QualityPlan),
            app.services.activity_service (log_activity),
            app.services.approvals.service (create_request),
            app.services.approvals.routing (NoEligibleApproverError),
            app.constants (QualityPlanStatus, QPOrderType, ActivityType, ApprovalGateType).
"""

from datetime import datetime, timezone
from typing import Any

from loguru import logger
from sqlalchemy.orm import Session

from ..constants import ActivityType, QualityPlanStatus
from ..models.quality_plan import QualityPlan
from ..services.activity_service import log_activity

# Human-readable section name per gate_type, used in activity descriptions / banners.
_SECTION_LABEL: dict[str, str] = {
    "qp_sales": "Sales",
    "purchase_order": "Purchasing",
}


class IncompleteQPError(Exception):
    """Raised by submit() when validate_complete() returns a non-empty list.

    Attributes:
        missing_fields: Human-readable list of field-level error messages.
    """

    def __init__(self, missing_fields: list[str]) -> None:
        self.missing_fields = missing_fields
        super().__init__(f"Quality plan is incomplete: {missing_fields}")


class NoSectionApproverError(Exception):
    """Raised by submit_section() when no eligible approver exists for the section gate.

    The router catches this and re-renders the QP detail with an inline "no approver
    configured" banner — never a 500. Carries the section label for the message.

    Attributes:
        section: Human-readable section name (e.g. "Sales", "Purchasing").
    """

    def __init__(self, section: str) -> None:
        self.section = section
        super().__init__(f"No approver configured for the {section} section")


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

    Phase-1 scope note (intentional, not a gap): submit() deliberately does NOT create
    an Approval Engine gate/ApprovalRequest here. It only transitions draft→in_review and
    writes the ActivityLog. The only active approval gate in Phase 1 is the Buy-Plan
    section, surfaced via the existing read-only buy-plan-approval bridge. The engine's
    QP/buy_plan gate is wired in Phase 1.5 — do not add gate-routing to submit() until then.

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


# Sales-section completeness: the SO# plus the QC-required fields a vendor needs to
# source against. (field, human-readable label) — a field is "missing" when its value is
# None or an empty/whitespace string. Booleans only require an explicit answer.
_SALES_REQUIRED: list[tuple[str, str]] = [
    ("sales_so_number", "Sales Order #"),
    ("sales_condition", "Condition"),
    ("sales_quantity", "Quantity"),
    ("sales_product_commodity", "Product Commodity"),
    ("sales_testing_required", "Testing Required"),
]

# Purchasing-section completeness: the PO# plus the QC-required fields.
_PURCHASING_REQUIRED: list[tuple[str, str]] = [
    ("purchasing_po_number", "Purchase Order #"),
    ("purchasing_condition", "Condition"),
    ("purchasing_product_commodity", "Product Commodity"),
    ("purchasing_testing_required", "Testing Required"),
]


def _missing_required(qp: QualityPlan, required: list[tuple[str, str]]) -> list[str]:
    """Return human-readable labels for any required field that is blank/None.

    A string field counts as present only when it has non-whitespace content; a Boolean
    counts as present once it is explicitly True or False (an unanswered Y/N is None).
    Integers count as present once set (including 0).
    """
    errors: list[str] = []
    for field, label in required:
        value = getattr(qp, field, None)
        if value is None or (isinstance(value, str) and not value.strip()):
            errors.append(f"{label} is required")
    return errors


def _validate_sales_section(qp: QualityPlan) -> list[str]:
    """Return completeness errors for the Sales section; empty list == submittable."""
    return _missing_required(qp, _SALES_REQUIRED)


def _validate_purchasing_section(qp: QualityPlan) -> list[str]:
    """Return completeness errors for the Purchasing section; empty list ==
    submittable."""
    return _missing_required(qp, _PURCHASING_REQUIRED)


def validate_section(qp: QualityPlan, gate_type: str) -> list[str]:
    """Dispatch to the per-section validator for the given gate_type.

    Returns the Sales validator's errors for the QP_SALES gate and the Purchasing
    validator's for the PURCHASE_ORDER gate; any other gate has no section fields to
    validate (empty list). The router uses this to render server-driven section_errors
    and to disable the submit button until the section is complete.
    """
    if str(gate_type) == "qp_sales":
        return _validate_sales_section(qp)
    if str(gate_type) == "purchase_order":
        return _validate_purchasing_section(qp)
    return []


def _on_section_approved(db: Session, qp_id: int, gate_type: str, approved: bool) -> None:
    """On-resolve hook for a QP section gate (QP_SALES / PURCHASE_ORDER).

    Called by the approval engine inside decide() (lazy import) when a section request
    resolves. On approval it stamps the QualityPlan's matching section-approved
    timestamp (sales_section_approved_at / purchasing_section_approved_at) and logs one
    ActivityLog row; on rejection it clears the stamp (a re-approval can re-set it) and
    logs the rejection. Same session as decide() — the caller flushes/commits.

    Args:
        db: SQLAlchemy session (sync, 2.0 style).
        qp_id: PK of the QualityPlan whose section resolved.
        gate_type: The section gate (ApprovalGateType.QP_SALES / .PURCHASE_ORDER).
        approved: True if the section was approved, False if rejected.

    Returns:
        None. A missing QP (concurrently deleted) is a no-op warning, not an error.
    """
    qp = db.get(QualityPlan, qp_id)
    if qp is None:
        logger.warning("_on_section_approved: QualityPlan id={} not found; skipping", qp_id)
        return

    # Stamp (or clear) the section's approved-at timestamp.
    stamp = datetime.now(timezone.utc) if approved else None
    if str(gate_type) == "qp_sales":
        qp.sales_section_approved_at = stamp
    elif str(gate_type) == "purchase_order":
        qp.purchasing_section_approved_at = stamp

    section = _SECTION_LABEL.get(str(gate_type), str(gate_type))
    verb = "approved" if approved else "rejected"
    log_activity(
        db,
        activity_type=ActivityType.APPROVAL_APPROVED if approved else ActivityType.APPROVAL_REJECTED,
        buy_plan_id=qp.buy_plan_id,
        description=f"Quality plan #{qp.id} {section} section {verb}",
    )
    db.flush()
    logger.info("QualityPlan id={} {} section {}", qp.id, section, verb)


def submit_section(db: Session, qp_id: int, gate_type: str, user: Any) -> Any:
    """Open a section approval request (QP_SALES / PURCHASE_ORDER) for the QP.

    The QualityPlan is the engine subject (subject_type=QUALITY_PLAN); the gate_type
    discriminates which section is being submitted. Routes to users holding the matching
    per-user approval toggle (can_approve_qp_sales / can_approve_pos). C2b enforces
    per-section field completeness first: a blank SO#/PO# or any missing QC-required
    field raises IncompleteQPError before any gate request is opened.

    Args:
        db: SQLAlchemy session (sync, 2.0 style).
        qp_id: PK of the QualityPlan to submit.
        gate_type: ApprovalGateType.QP_SALES or .PURCHASE_ORDER.
        user: The authenticated User submitting the section (requester + owner).

    Returns:
        The flushed ApprovalRequest (already routed to eligible approvers).

    Raises:
        ValueError: If the QualityPlan is not found.
        IncompleteQPError: If the section is missing a required field — carries the
            field-level error list. The router re-renders with those section_errors and
            no gate request is opened.
        NoSectionApproverError: If no eligible approver holds the section toggle — the
            router surfaces this as an inline banner (NOT a 500). The half-built request
            is removed by create_request, so no orphan engine state remains.
    """
    # Lazy import: approvals.service -> quality_plan_service (decide()'s lazy import of
    # _on_section_approved), so a top-level import here would be circular. The package
    # exposes no re-exports — import from the concrete submodules.
    from .approvals.routing import NoEligibleApproverError
    from .approvals.service import create_request

    qp = db.get(QualityPlan, qp_id)
    if qp is None:
        raise ValueError(f"QualityPlan {qp_id} not found")

    section_errors = validate_section(qp, gate_type)
    if section_errors:
        raise IncompleteQPError(section_errors)

    try:
        request = create_request(
            db,
            gate_type=gate_type,
            amount=None,
            subject=qp,
            requested_by=user,
            owner=user,
        )
    except NoEligibleApproverError as exc:
        section = _SECTION_LABEL.get(str(gate_type), str(gate_type))
        logger.warning("QualityPlan id={} {} section submit: no eligible approver", qp_id, section)
        raise NoSectionApproverError(section) from exc

    db.flush()
    logger.info("QualityPlan id={} {} section submitted by user={}", qp.id, gate_type, user.id if user else None)
    return request
