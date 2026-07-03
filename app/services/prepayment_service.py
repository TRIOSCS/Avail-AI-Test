"""prepayment_service.py — Business logic for creating prepayment records.

Purpose: Persists a Prepayment row and immediately spawns a routed
         ApprovalRequest (gate_type=PREPAYMENT) via ApprovalService.create_request.
         The approval request is routed to all Users with can_approve_prepayments=True
         whose prepayment_approval_limit is NULL (unlimited) or high enough to cover
         the amount — i.e. eligible when total_incl_fees <= prepayment_approval_limit
         (matching the routing check request.amount <= limit). A limit *below* the
         amount makes that approver ineligible.

Called by: app.routers.prepayments (POST /v2/prepayments).
Depends on: app.models.quality_plan (Prepayment), app.models.buy_plan (BuyPlanLine),
            app.services.approvals.service (create_request),
            app.constants (ApprovalGateType, ApprovalRequestStatus, ApprovalSubjectType,
            BuyPlanLineStatus, PaymentMethod).
"""

from decimal import Decimal

from sqlalchemy.orm import Session

from ..constants import (
    ApprovalGateType,
    ApprovalRequestStatus,
    ApprovalSubjectType,
    BuyPlanLineStatus,
)
from ..dependencies import get_buyplan_for_user
from ..models.approvals import ApprovalRequest
from ..models.buy_plan import BuyPlanLine
from ..models.quality_plan import Prepayment
from ..services.approvals.service import create_request


def create_prepayment(
    db: Session,
    *,
    buy_plan_id: int,
    buy_plan_line_id: int,
    vendor_card_id: int | None,
    payment_method: str | None,
    total_incl_fees: Decimal,
    test_report_sent: bool,
    buyer_remarks: str | None,
    created_by,  # User ORM object
) -> tuple[Prepayment, ApprovalRequest]:
    """Persist a Prepayment and spawn a routed prepayment approval request.

    Args:
        db: SQLAlchemy session (sync, 2.0 style).
        buy_plan_id: FK to buy_plans_v3.id (required).
        buy_plan_line_id: FK to buy_plan_lines.id — the specific PO line being prepaid
            (required). Must belong to *buy_plan_id* and have a cut PO.
        vendor_card_id: FK to vendor_cards.id (optional).
        payment_method: PaymentMethod value (wire / cc / paypal) or None.
        total_incl_fees: Total payment amount including fees (used for limit routing).
        test_report_sent: Whether the vendor already returned the test report.
        buyer_remarks: Free-text notes from the buyer.
        created_by: The authenticated User triggering the prepayment.

    Returns:
        A (Prepayment, ApprovalRequest) tuple — both flushed, not yet committed.

    Raises:
        NoEligibleApproverError: Propagated from route_request when no eligible
            approver exists for the PREPAYMENT gate at this amount.
        HTTPException(404): If *created_by* may not access *buy_plan_id* (restricted
            roles not owning the parent requisition).
        ValueError: If *buy_plan_line_id* does not belong to *buy_plan_id*, the line has
            no cut PO (no po_number / not PENDING_VERIFY|VERIFIED), or a prepayment for
            the line is already awaiting approval (race-safe duplicate-pending guard).
    """
    # Ownership gate (service-layer so the router stays thin): a Prepayment + routed
    # ApprovalRequest must not be attachable to a buy plan the actor can't access.
    plan = get_buyplan_for_user(db, created_by, buy_plan_id)

    # Lock the line to serialize concurrent prepayment requests on the same PO (a no-op on
    # SQLite, enforced on PostgreSQL). The lock + the REQUESTED re-check below together are
    # the race-safe duplicate-pending guard.
    line = db.query(BuyPlanLine).filter(BuyPlanLine.id == buy_plan_line_id).with_for_update().one_or_none()
    if line is None or line.buy_plan_id != buy_plan_id:
        raise ValueError("Line does not belong to this buy plan.")
    if not line.po_number or line.status not in (
        BuyPlanLineStatus.PENDING_VERIFY.value,
        BuyPlanLineStatus.VERIFIED.value,
    ):
        raise ValueError("This PO is not ready for a prepayment request.")

    # One in-flight prepayment per PO: block a second REQUESTED prepayment on this line.
    # Enum members (no .value) match the ApprovalRequest comparison convention in
    # services/approvals/queue.py + service.py.
    existing = (
        db.query(ApprovalRequest.id)
        .join(Prepayment, Prepayment.id == ApprovalRequest.subject_id)
        .filter(
            ApprovalRequest.subject_type == ApprovalSubjectType.PREPAYMENT,
            ApprovalRequest.gate_type == ApprovalGateType.PREPAYMENT,
            ApprovalRequest.status == ApprovalRequestStatus.REQUESTED,
            Prepayment.buy_plan_line_id == buy_plan_line_id,
        )
        .first()
    )
    if existing:
        raise ValueError("A prepayment for this PO is already awaiting approval.")

    prepayment = Prepayment(
        buy_plan_id=plan.id,
        buy_plan_line_id=buy_plan_line_id,
        vendor_card_id=vendor_card_id,
        payment_method=payment_method,
        total_incl_fees=total_incl_fees,
        test_report_sent=test_report_sent,
        buyer_remarks=buyer_remarks,
        created_by_id=created_by.id if created_by is not None else None,
    )
    db.add(prepayment)
    db.flush()  # Assign prepayment.id before wiring as subject FK

    request = create_request(
        db,
        gate_type=ApprovalGateType.PREPAYMENT,
        amount=total_incl_fees,
        subject=prepayment,
        requested_by=created_by,
        owner=created_by,
        currency=prepayment.currency,
    )

    return prepayment, request
