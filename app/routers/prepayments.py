"""routers/prepayments.py — Prepayment creation (JSON API + HTMX request entry point).

Purpose: Exposes the prepayment request surface:
  - POST /v2/prepayments — thin JSON route (returns the new approval-request id).
  - GET  /v2/partials/prepayments/new?line_id=... — the HTMX request modal, prefilled
    from the specific cut PO line.
  - POST /v2/partials/prepayments — the HTMX form create (form-encoded) → success toast.
All three validate the body/form, delegate business logic to
prepayment_service.create_prepayment, and (for HTMX) surface honest toasts.

Called by: app.main (router registration); the request modal / trigger button.
Depends on: app.services.prepayment_service, app.services.buyplan_workflow (_line_amount),
            app.dependencies (require_user, get_buyplan_for_user), app.database (get_db),
            app.models.buy_plan (BuyPlanLine), app.constants (PaymentMethod),
            app.template_env (template_response).
"""

import json
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session
from starlette.responses import HTMLResponse

from ..constants import PaymentMethod
from ..database import get_db
from ..dependencies import get_buyplan_for_user, require_user
from ..models.buy_plan import BuyPlanLine
from ..services.approvals.routing import NoEligibleApproverError
from ..services.buyplan_workflow import _line_amount
from ..services.prepayment_service import create_prepayment
from ..template_env import template_response

router = APIRouter(tags=["prepayments"])

# Payment-method options offered in the request modal (value = PaymentMethod enum value).
_PAYMENT_METHOD_CHOICES: list[tuple[str, str]] = [
    (PaymentMethod.WIRE.value, "Wire"),
    (PaymentMethod.CC.value, "Credit Card"),
    (PaymentMethod.PAYPAL.value, "PayPal"),
]

# Form checkbox / string truthy values ("on" from an HTML checkbox; "true"/"1" from JS).
_TRUTHY = {"true", "on", "1", "yes"}


class PrepaymentCreate(BaseModel):
    """Request body for POST /v2/prepayments.

    ``buy_plan_line_id`` is optional at the schema boundary so the ownership gate in
    ``create_prepayment`` (get_buyplan_for_user) still runs FIRST and a restricted
    non-owner gets a 404 rather than a 422 — line validation follows the ownership check.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    buy_plan_id: int
    buy_plan_line_id: int | None = None
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
            buy_plan_line_id=body.buy_plan_line_id,
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
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "prepayment_id": prepayment.id,
        "approval_request_id": request.id,
    }


# ── HTMX request entry point ────────────────────────────────────────────────


def _prepayment_toast(response: HTMLResponse, message: str, kind: str = "success") -> None:
    """Attach a showToast HX-Trigger so the Alpine $store.toast surfaces feedback."""
    response.headers["HX-Trigger"] = json.dumps({"showToast": {"message": message, "type": kind}})


def _prepayment_error_toast(message: str) -> HTMLResponse:
    """Honest error feedback for the request modal, which has no surface to re-render.

    Mirrors prospecting._prospect_error_toast: HTMX suppresses non-2xx swaps and the
    JSON HTTPException handler carries no showToast, so a raw 4xx would leave zero
    feedback. Return a 200 that swaps nothing (HX-Reswap: none) but fires an error
    showToast.
    """
    resp = HTMLResponse("", headers={"HX-Reswap": "none"})
    _prepayment_toast(resp, message, "error")
    return resp


@router.get("/v2/partials/prepayments/new", response_class=HTMLResponse)
def prepayment_request_modal(
    request: Request,
    line_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_user),
):
    """Render the prepayment request modal, prefilled from the specific cut PO line.

    The ownership gate (get_buyplan_for_user → 404 for a restricted non-owner) is
    applied here too so the modal can't be opened against a plan the actor can't access.
    """
    line = db.get(BuyPlanLine, line_id)
    if line is None:
        raise HTTPException(status_code=404, detail="PO line not found")
    plan = get_buyplan_for_user(db, current_user, line.buy_plan_id)

    vendor_name = None
    if line.offer is not None and line.offer.vendor_card is not None:
        vendor_name = line.offer.vendor_card.display_name

    ctx = {
        "request": request,
        "user": current_user,
        "line": line,
        "plan": plan,
        "vendor_name": vendor_name,
        "amount": _line_amount(line),
        "payment_methods": _PAYMENT_METHOD_CHOICES,
    }
    return template_response("htmx/partials/prepayments/request_modal.html", ctx)


@router.post("/v2/partials/prepayments", response_class=HTMLResponse)
def prepayment_request_create(
    request: Request,
    buy_plan_id: int = Form(...),
    buy_plan_line_id: int = Form(...),
    payment_method: str | None = Form(None),
    total_incl_fees: str = Form(...),
    test_report_sent: str | None = Form(None),
    buyer_remarks: str | None = Form(None),
    db: Session = Depends(get_db),
    current_user=Depends(require_user),
):
    """HTMX create: parse the request form, spawn the prepayment + approval, toast success.

    On a service ValueError (line not on plan / no cut PO / duplicate pending) or
    NoEligibleApproverError, roll back and return an error toast so the modal surfaces the
    reason instead of a silent no-op.
    """
    try:
        amount = Decimal(total_incl_fees)
    except (InvalidOperation, TypeError):
        return _prepayment_error_toast("Enter a valid prepayment amount.")

    report_sent = str(test_report_sent or "").strip().lower() in _TRUTHY

    try:
        create_prepayment(
            db,
            buy_plan_id=buy_plan_id,
            buy_plan_line_id=buy_plan_line_id,
            vendor_card_id=None,
            payment_method=payment_method or None,
            total_incl_fees=amount,
            test_report_sent=report_sent,
            buyer_remarks=(buyer_remarks or None),
            created_by=current_user,
        )
        db.commit()
    except NoEligibleApproverError as exc:
        db.rollback()
        return _prepayment_error_toast(str(exc))
    except ValueError as exc:
        db.rollback()
        return _prepayment_error_toast(str(exc))

    # NOTE: notification wired in Task 6 — fire run_prepayment_notify_bg(
    #       notify_prepayment_requested, prepayment.id) here after the successful commit.

    resp = HTMLResponse("", headers={"HX-Reswap": "none"})
    _prepayment_toast(resp, "Prepayment request submitted for approval.", "success")
    return resp
