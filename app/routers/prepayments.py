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
import secrets
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session
from starlette.responses import HTMLResponse

from ..constants import ActivityType, PaymentMethod, PrepaymentStatus
from ..database import get_db
from ..dependencies import get_buyplan_for_user, is_manager_or_admin, require_user
from ..models import ActivityLog
from ..models.buy_plan import BuyPlanLine
from ..models.quality_plan import Prepayment
from ..services.approvals.routing import NoEligibleApproverError
from ..services.buyplan_workflow import _line_amount
from ..services.prepayment_service import create_prepayment, mark_prepayment_paid
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
    # Client-prefilled payee (fallback only — the authoritative payee is snapshotted
    # server-side in create_prepayment from the line's offer / vendor card).
    vendor_name: str | None = None
    currency: str = "USD"


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
            vendor_name=body.vendor_name,
            currency=body.currency,
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
    origin: str = "",
    hub_scope: str = "all",
    db: Session = Depends(get_db),
    current_user=Depends(require_user),
):
    """Render the prepayment request modal, prefilled from the specific cut PO line.

    The ownership gate (get_buyplan_for_user → 404 for a restricted non-owner) is
    applied here too so the modal can't be opened against a plan the actor can't access.

    ``origin``/``hub_scope`` (mirroring resource_form) thread the caller's surface through to
    the create POST so it re-renders the RIGHT surface: ``''`` → plan detail into
    #main-content; ``approvals_hub`` → the PO Approval tab body into #ap-hub-body (at the
    preserved SEE-ALL/MINE ``hub_scope``).
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
        "origin": "approvals_hub" if origin == "approvals_hub" else "",
        "hub_scope": "mine" if hub_scope == "mine" else "all",
    }
    return template_response("htmx/partials/prepayments/request_modal.html", ctx)


@router.post("/v2/partials/prepayments", response_class=HTMLResponse)
async def prepayment_request_create(
    request: Request,
    buy_plan_id: int = Form(...),
    buy_plan_line_id: int = Form(...),
    payment_method: str | None = Form(None),
    total_incl_fees: str = Form(...),
    test_report_sent: str | None = Form(None),
    buyer_remarks: str | None = Form(None),
    vendor_name: str | None = Form(None),
    currency: str = Form("USD"),
    origin: str = Form(""),
    hub_scope: str = Form("all"),
    db: Session = Depends(get_db),
    current_user=Depends(require_user),
):
    """HTMX create: parse the request form, spawn the prepayment + approval, toast success.

    On a service ValueError (line not on plan / no cut PO / duplicate pending) or
    NoEligibleApproverError, roll back and return an error toast so the modal surfaces the
    reason instead of a silent no-op.

    On success, re-render the caller's surface (threaded via ``origin``, mirroring the
    verify-po / resource routes): ``approvals_hub`` → the PO Approval tab body into
    #ap-hub-body (at ``hub_scope``); anything else → the refreshed plan detail into
    #main-content — so the new "Prepay requested" pill / "Prepayment pending" badge appears
    at once instead of leaving the buyer on a stale surface.
    """
    try:
        amount = Decimal(total_incl_fees)
    except (InvalidOperation, TypeError):
        return _prepayment_error_toast("Enter a valid prepayment amount.")

    report_sent = str(test_report_sent or "").strip().lower() in _TRUTHY

    try:
        prepayment, _req = create_prepayment(
            db,
            buy_plan_id=buy_plan_id,
            buy_plan_line_id=buy_plan_line_id,
            vendor_card_id=None,
            payment_method=payment_method or None,
            total_incl_fees=amount,
            test_report_sent=report_sent,
            buyer_remarks=(buyer_remarks or None),
            created_by=current_user,
            vendor_name=(vendor_name or None),
            currency=(currency or "USD"),
        )
        db.commit()
    except NoEligibleApproverError as exc:
        db.rollback()
        return _prepayment_error_toast(str(exc))
    except ValueError as exc:
        db.rollback()
        return _prepayment_error_toast(str(exc))

    # Notify accounting/AP (email + Teams) that a prepayment was requested — DO NOT PAY YET.
    # Fire-and-forget: the runner isolates every error so a failed notice never breaks the
    # request that just succeeded.
    from ..services.prepayment_notifications import notify_prepayment_requested, run_prepayment_notify_bg

    await run_prepayment_notify_bg(notify_prepayment_requested, prepayment.id)

    # Re-render the surface the request was raised from so the pill/badge update in place.
    if origin == "approvals_hub":
        from .htmx.approvals_hub import render_tab_body

        resp = render_tab_body(request, current_user, db, "po-approval", hub_scope)
    else:
        from .htmx.buy_plans import buy_plan_detail_partial

        resp = await buy_plan_detail_partial(request, buy_plan_id, current_user, db)

    _prepayment_toast(resp, "Prepayment request submitted for approval.", "success")
    return resp


# ── In-app mark-paid fallback + manager undo ────────────────────────────────
#
# The tokenized accounting-email link (routers/prepayment_confirm.py) is the primary
# confirm-paid path. These two routes are the in-app fallback + correction: a manager/admin
# (or the plan owner) records the wire from the Prepayment tab if the email is lost, and a
# manager/admin can reverse a mis-click. Both re-render the Prepayment tab body into
# #ap-hub-body so the row's badge/actions update in place.


def _require_mark_paid_access(db: Session, user, prepayment: Prepayment) -> None:
    """Gate the in-app mark-paid: a manager/admin may mark any; anyone else must own the
    plan (get_buyplan_for_user 404s a restricted role that doesn't own the requisition —
    the same ownership model create_prepayment enforces)."""
    if is_manager_or_admin(user):
        return
    get_buyplan_for_user(db, user, prepayment.buy_plan_id)


def _render_prepayment_tab(request: Request, user, db: Session, scope: str) -> HTMLResponse:
    """Re-render the Approvals-hub Prepayment tab body (the surface these actions live
    on)."""
    from .htmx.approvals_hub import render_tab_body

    return render_tab_body(request, user, db, "prepayment", scope)


@router.get("/v2/partials/prepayments/{prepayment_id}/mark-paid", response_class=HTMLResponse)
def prepayment_mark_paid_modal(
    request: Request,
    prepayment_id: int,
    scope: str = "all",
    db: Session = Depends(get_db),
    current_user=Depends(require_user),
):
    """Render the in-app "Mark paid" modal for an approved prepayment (house modal
    pattern).

    Same access gate as the POST so the modal can't be opened against a prepayment the actor
    may not settle. Only an ``approved`` prepayment can be marked paid.
    """
    pp = db.get(Prepayment, prepayment_id)
    if pp is None:
        raise HTTPException(status_code=404, detail="Prepayment not found")
    _require_mark_paid_access(db, current_user, pp)
    if pp.status != PrepaymentStatus.APPROVED.value:
        raise HTTPException(status_code=400, detail="Only an approved prepayment can be marked paid.")

    ctx = {
        "request": request,
        "user": current_user,
        "pp": pp,
        "amount": pp.total_incl_fees,
        "scope": "mine" if scope == "mine" else "all",
    }
    return template_response("htmx/partials/prepayments/mark_paid_modal.html", ctx)


@router.post("/v2/partials/prepayments/{prepayment_id}/mark-paid", response_class=HTMLResponse)
async def prepayment_mark_paid(
    request: Request,
    prepayment_id: int,
    wire_reference: str | None = Form(None),
    paid_amount: str | None = Form(None),
    scope: str = Form("all"),
    db: Session = Depends(get_db),
    current_user=Depends(require_user),
):
    """Record that the wire went out in-app (fallback for the tokenized email link).

    Gated to a manager/admin or the plan owner. ``paid_amount`` defaults to the prepayment's
    ``total_incl_fees``. On the service guard (non-approved) → an error toast (no swap).
    """
    pp = db.get(Prepayment, prepayment_id)
    if pp is None:
        raise HTTPException(status_code=404, detail="Prepayment not found")
    _require_mark_paid_access(db, current_user, pp)

    try:
        amount = Decimal(paid_amount) if paid_amount else pp.total_incl_fees
    except (InvalidOperation, TypeError):
        return _prepayment_error_toast("Enter a valid paid amount.")

    try:
        mark_prepayment_paid(
            db,
            pp,
            wire_reference=(wire_reference or "").strip(),
            paid_amount=amount,
            paid_via="in_app",
            paid_by_id=current_user.id,
            paid_by_label=current_user.name,
        )
    except ValueError as exc:
        return _prepayment_error_toast(str(exc))

    resp = _render_prepayment_tab(request, current_user, db, scope)
    _prepayment_toast(resp, "Prepayment marked paid.", "success")
    return resp


@router.post("/v2/partials/prepayments/{prepayment_id}/unmark-paid", response_class=HTMLResponse)
async def prepayment_unmark_paid(
    request: Request,
    prepayment_id: int,
    scope: str = Form("all"),
    db: Session = Depends(get_db),
    current_user=Depends(require_user),
):
    """Reverse a mis-clicked payment: revert ``paid`` → ``approved``, clear the paid fields,
    re-mint a fresh single-use ``pay_token``, and log the correction. Manager/admin only —
    reversing a recorded wire is an oversight action, not a plan-owner one.
    """
    if not is_manager_or_admin(current_user):
        raise HTTPException(status_code=403, detail="Manager or admin role required to reverse a payment.")
    pp = db.get(Prepayment, prepayment_id)
    if pp is None:
        raise HTTPException(status_code=404, detail="Prepayment not found")
    if pp.status != PrepaymentStatus.PAID.value:
        return _prepayment_error_toast("Only a paid prepayment can be reversed.")

    pp.status = PrepaymentStatus.APPROVED.value
    pp.paid_at = None
    pp.paid_by_id = None
    pp.paid_by_label = None
    pp.paid_via = None
    pp.wire_reference = None
    pp.paid_amount = None
    pp.pay_token = secrets.token_urlsafe(32)

    requisition_id = pp.buy_plan.requisition_id if pp.buy_plan is not None else None
    db.add(
        ActivityLog(
            user_id=current_user.id,
            activity_type=ActivityType.NOTE,
            channel="system",
            requisition_id=requisition_id,
            buy_plan_id=pp.buy_plan_id,
            subject="Prepayment payment reversed",
            notes=f"Prepayment #{pp.id} reverted paid → approved by {current_user.name or current_user.email}",
        )
    )
    db.commit()

    resp = _render_prepayment_tab(request, current_user, db, scope)
    _prepayment_toast(resp, "Payment reversed — prepayment returned to approved.", "success")
    return resp
