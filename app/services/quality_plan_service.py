"""quality_plan_service.py — Business logic for creating and reviewing QualityPlan
records.

Purpose:
  - create_qp: Persist a QualityPlan header in DRAFT status.
  - validate_complete: Return a list of human-readable error strings for any
    Phase-1 required fields that are blank/null. Empty list == ready to submit.
  - validate_section / _validate_sales_section / _validate_purchasing_section: the
    per-section completeness gate reused by the Mark-Reviewed toggle and the router's
    inline section-error display.
  - toggle_section_reviewed: the decision-C lightweight per-section fold — a buyer
    holding the section review right stamps a section reviewed (locking its form) or
    clears the stamp (re-opening it). No second approver, instant. Replaced the retired
    submit-for-approval gate (submit_section / _on_section_approved).

Phase-1 required fields: created_by_id (owner), order_type, buy_plan_id.

Called by: app.routers.quality_plans.
Depends on: app.models.quality_plan (QualityPlan),
            app.services.activity_service (log_activity),
            app.dependencies (can_review_qp_sales_section / can_review_qp_purchasing_section),
            app.constants (QualityPlanStatus, QPOrderType, ActivityType, ApprovalGateType).
"""

from datetime import datetime, timezone
from typing import Any

from loguru import logger
from sqlalchemy.orm import Session

from ..constants import ActivityType, QualityPlanStatus
from ..dependencies import can_review_qp_purchasing_section, can_review_qp_sales_section
from ..models.quality_plan import QualityPlan
from ..services.activity_service import log_activity

# Human-readable section name per gate_type, used in activity descriptions / banners.
_SECTION_LABEL: dict[str, str] = {
    "qp_sales": "Sales",
    "qp_purchasing": "Purchasing",
}


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


# Sales-section completeness: QC-required fields a vendor needs to source against.
# SO# is checked separately via the linked BuyPlan (see _validate_sales_section).
# (field, human-readable label) — a field is "missing" when its value is
# None or an empty/whitespace string. Booleans only require an explicit answer.
_SALES_REQUIRED: list[tuple[str, str]] = [
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
    """Return completeness errors for the Sales section; empty list == submittable.

    SO# is read from the linked BuyPlan (canonical since SP-2); all other required
    fields are still on the QP itself.
    """
    errors = _missing_required(qp, _SALES_REQUIRED)
    bp = qp.buy_plan
    if bp is None or not (bp.sales_order_number or "").strip():
        errors.append("Sales Order # is required")
    return errors


def _validate_purchasing_section(qp: QualityPlan) -> list[str]:
    """Return completeness errors for the Purchasing section; empty list ==
    submittable."""
    return _missing_required(qp, _PURCHASING_REQUIRED)


def validate_section(qp: QualityPlan, gate_type: str) -> list[str]:
    """Dispatch to the per-section validator for the given gate_type.

    Returns the Sales validator's errors for the QP_SALES gate and the Purchasing
    validator's for the QP_PURCHASING gate; any other gate has no section fields to
    validate (empty list). The router uses this to render server-driven section_errors
    and to disable the submit button until the section is complete.
    """
    if str(gate_type) == "qp_sales":
        return _validate_sales_section(qp)
    if str(gate_type) == "qp_purchasing":
        return _validate_purchasing_section(qp)
    return []


def _can_review_section(gate_type: str, user: Any) -> bool:
    """True if *user* holds the review right for the given QP section gate."""
    if gate_type == "qp_sales":
        return can_review_qp_sales_section(user)
    if gate_type == "qp_purchasing":
        return can_review_qp_purchasing_section(user)
    return False


def toggle_section_reviewed(db: Session, qp_id: int, gate_type: str, action: str, user: Any) -> QualityPlan:
    """Mark or unmark a QP section (Sales / Purchasing) as reviewed.

    The decision-C lightweight per-section fold that replaced the retired
    submit-for-approval gate: a buyer holding the section review right stamps the section
    reviewed — locking its edit form — or clears the stamp, re-opening it. No second
    approver, instant.

    action="mark": validate the section is complete (IncompleteQPError otherwise — the
        SAME completeness gate the old submit enforced), require the matching review
        right (PermissionError otherwise), then stamp {section}_section_reviewed_at=now()
        and {section}_section_reviewed_by_id=user.id.
    action="unmark": require the same review right, then clear both stamps (re-opens the
        section form).

    Both branches write one ActivityLog (QP_SECTION_REVIEWED) with a mark/unmark verb —
    replacing _on_section_approved's audit write.

    Args:
        db: SQLAlchemy session (sync, 2.0 style).
        qp_id: PK of the QualityPlan.
        gate_type: ApprovalGateType.QP_SALES or .QP_PURCHASING (discriminates the section).
        action: "mark" or "unmark".
        user: The authenticated User performing the toggle.

    Returns:
        The updated QualityPlan (flushed, not committed).

    Raises:
        ValueError: QP not found, or an unknown action / non-section gate_type.
        IncompleteQPError: (mark only) the section is missing a required field — carries
            the field-level error list so the router re-renders with inline errors.
        PermissionError: *user* lacks the section's review right.
    """
    if action not in ("mark", "unmark"):
        raise ValueError(f"action must be 'mark' or 'unmark', got {action!r}")

    gate = str(gate_type)
    section = _SECTION_LABEL.get(gate)
    if section is None:
        raise ValueError(f"gate_type must be a QP section gate, got {gate_type!r}")

    qp = db.get(QualityPlan, qp_id)
    if qp is None:
        raise ValueError(f"QualityPlan {qp_id} not found")

    # Both mark and unmark require the section's review right.
    if not _can_review_section(gate, user):
        raise PermissionError(f"User {getattr(user, 'id', None)} lacks the {section} section review right")

    if action == "mark":
        errors = validate_section(qp, gate_type)
        if errors:
            raise IncompleteQPError(errors)
        stamp = datetime.now(timezone.utc)
        if gate == "qp_sales":
            qp.sales_section_reviewed_at = stamp
            qp.sales_section_reviewed_by_id = user.id
        else:
            qp.purchasing_section_reviewed_at = stamp
            qp.purchasing_section_reviewed_by_id = user.id
        verb = "marked reviewed"
    else:
        if gate == "qp_sales":
            qp.sales_section_reviewed_at = None
            qp.sales_section_reviewed_by_id = None
        else:
            qp.purchasing_section_reviewed_at = None
            qp.purchasing_section_reviewed_by_id = None
        verb = "unmarked reviewed"

    log_activity(
        db,
        activity_type=ActivityType.QP_SECTION_REVIEWED,
        user_id=user.id if user is not None else None,
        buy_plan_id=qp.buy_plan_id,
        description=f"Quality plan #{qp.id} {section} section {verb}",
    )
    db.flush()
    logger.info("QualityPlan id={} {} section {} by user={}", qp.id, section, verb, getattr(user, "id", None))
    return qp
