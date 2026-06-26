"""routers/prepayments.py — Thin JSON router for prepayment creation.

Purpose: Exposes POST /v2/prepayments. Validates the request body, delegates
         all business logic to prepayment_service.create_prepayment, and
         returns the resulting approval request id.

Called by: app.main (router registration).
Depends on: app.services.prepayment_service, app.dependencies (require_user),
            app.database (get_db).
"""

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_user
from ..services.approvals.routing import NoEligibleApproverError
from ..services.prepayment_service import create_prepayment

router = APIRouter(tags=["prepayments"])


class PrepaymentCreate(BaseModel):
    """Request body for POST /v2/prepayments."""

    model_config = ConfigDict(str_strip_whitespace=True)

    buy_plan_id: int
    vendor_card_id: int | None = None
    payment_method: str | None = None
    total_incl_fees: Decimal
    test_report_sent: bool = False
    buyer_remarks: str | None = None


@router.post("/v2/prepayments")
def post_prepayment(
    body: PrepaymentCreate,
    db: Session = Depends(get_db),
    current_user=Depends(require_user),
):
    """Create a prepayment and spawn its approval gate.

    Returns JSON with the prepayment_id and approval_request_id so the caller can poll
    or redirect to the approval workflow.
    """
    try:
        prepayment, request = create_prepayment(
            db,
            buy_plan_id=body.buy_plan_id,
            vendor_card_id=body.vendor_card_id,
            payment_method=body.payment_method,
            total_incl_fees=body.total_incl_fees,
            test_report_sent=body.test_report_sent,
            buyer_remarks=body.buyer_remarks,
            created_by=current_user,
        )
        db.commit()
    except NoEligibleApproverError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {
        "prepayment_id": prepayment.id,
        "approval_request_id": request.id,
    }
