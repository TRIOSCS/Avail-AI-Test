"""prepayment_service.py — Business logic for creating prepayment records.

Purpose: Persists a Prepayment row and immediately spawns a routed
         ApprovalRequest (gate_type=PREPAYMENT) via ApprovalService.create_request.
         The approval request is routed to all Users with can_approve_prepayments=True
         whose prepayment_approval_limit is NULL (unlimited) or high enough to cover
         the amount — i.e. eligible when total_incl_fees <= prepayment_approval_limit
         (matching the routing check request.amount <= limit). A limit *below* the
         amount makes that approver ineligible.

Called by: app.routers.prepayments (POST /v2/prepayments).
Depends on: app.models.quality_plan (Prepayment),
            app.services.approvals.service (create_request),
            app.constants (ApprovalGateType, PaymentMethod).
"""

from decimal import Decimal

from sqlalchemy.orm import Session

from ..constants import ApprovalGateType
from ..models.approvals import ApprovalRequest
from ..models.quality_plan import Prepayment
from ..services.approvals.service import create_request


def create_prepayment(
    db: Session,
    *,
    buy_plan_id: int,
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
    """
    prepayment = Prepayment(
        buy_plan_id=buy_plan_id,
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
    )

    return prepayment, request
