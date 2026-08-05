"""quality_plan_service.py — Business logic for creating and validating QualityPlan
records.

Purpose:
  - create_qp: Persist a QualityPlan header in DRAFT status.
  - validate_complete: Return a list of human-readable error strings for any
    Phase-1 required fields that are blank/null. Empty list == ready to submit.
  - validate_section / _validate_sales_section / _validate_purchasing_section: the
    per-section completeness check backing the router's inline section-error display.

W3.7 dropped the Mark-Reviewed ceremony (toggle_section_reviewed and its review-right
checks): section locking now lives in the ONE matrix, qp_workspace.can_edit_qp_section.

Phase-1 required fields: created_by_id (owner), order_type, buy_plan_id.

Called by: app.routers.quality_plans.
Depends on: app.models.quality_plan (QualityPlan),
            app.constants (QualityPlanStatus, ApprovalGateType).
"""

from loguru import logger
from sqlalchemy.orm import Session

from ..constants import QualityPlanStatus
from ..models.quality_plan import QualityPlan


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
