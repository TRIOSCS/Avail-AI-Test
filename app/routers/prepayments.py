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

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session
from starlette.responses import HTMLResponse

from ..constants import PREPAYMENT_METHODS, PaymentMethod, PrepaymentStatus
from ..database import get_db
from ..dependencies import get_buyplan_for_user, is_manager_or_admin, require_user
from ..models.buy_plan import BuyPlanLine
from ..models.quality_plan import Prepayment
from ..services.approvals.routing import NoEligibleApproverError
from ..services.buyplan_workflow import _line_amount
from ..services.prepayment_service import create_prepayment, mark_prepayment_paid, unmark_prepayment_paid
from ..template_env import template_response
from .htmx._shared import set_toast, toast_error_response

router = APIRouter(tags=["prepayments"])

# Payment-method options offered in the request modal AND accepted by the create
# routes — derived from the frozen PREPAYMENT_METHODS list (wire / PayPal / CC / ACH;
# COD can never appear — paying on delivery is definitionally not a prepayment).
_METHOD_LABELS = {
    PaymentMethod.WIRE.value: "Wire",
    PaymentMethod.CC.value: "Credit Card",
    PaymentMethod.PAYPAL.value: "PayPal",
    PaymentMethod.ACH.value: "ACH",
}
_PAYMENT_METHOD_CHOICES: list[tuple[str, str]] = [
    (m.value, _METHOD_LABELS.get(m.value, m.value.title())) for m in PREPAYMENT_METHODS
]

# Form checkbox / string truthy values ("on" from an HTML checkbox; "true"/"1" from JS).
_TRUTHY = {"true", "on", "1", "yes"}


def _form_error_response(message: str) -> HTMLResponse:
    """The friendly 400 for a refused prepayment request (COD line, bad method, bad
    amount, duplicate pending, no eligible approver).

    A small inline partial (rendered into the modal's #pp-modal-error via the
    response-targets ext's hx-target-4xx) + a toast, with a REAL 400 status. Never
    ``toast_error_response`` here: that returns 200, and close_modal_on_success closes
    the modal on any 2xx — the buyer's input would be eaten.
    """
    resp = HTMLResponse(
        f'<div class="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-800">{message}</div>',
        status_code=400,
    )
    set_toast(resp, message, "error", merge=True)
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
    the create POST so it re-renders the RIGHT surface: ``approvals_workspace`` → the
    PO-line pane into #aw-pane (the Deal Sheet home); ``approvals_hub`` → the PO Approval
    tab body into #ap-hub-body (at the preserved SEE-ALL/MINE ``hub_scope``); anything
    else collapses to ``''`` → plan pane into #main-content.
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
        "origin": origin if origin in ("approvals_hub", "approvals_workspace") else "",
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
    NoEligibleApproverError, roll back and return the inline 400 partial (rendered into
    the modal's #pp-modal-error via hx-target-4xx) — a real 4xx keeps the modal open
    with input intact, where the old 200 error toast closed it.

    On success, re-render the caller's surface (threaded via ``origin``, mirroring the
    verify-po / resource routes): ``approvals_hub`` → the PO Approval tab body into
    #ap-hub-body (at ``hub_scope``); anything else → the refreshed plan detail into
    #main-content — so the new "Prepay requested" pill / "Prepayment pending" badge appears
    at once instead of leaving the buyer on a stale surface.
    """
    try:
        amount = Decimal(total_incl_fees)
    except (InvalidOperation, TypeError):
        return _form_error_response("Enter a valid prepayment amount.")

    # Method must be one of the frozen prepayment methods (COD is not in the list — a
    # forged/stale form can't smuggle it in).
    if payment_method and payment_method not in {m.value for m in PREPAYMENT_METHODS}:
        return _form_error_response("That payment method can't be prepaid — pick wire, PayPal, credit card, or ACH.")

    # COD guard (spec §8): a COD line has nothing to pay in advance. Enforced HERE, in
    # the router, BEFORE create_prepayment — prepayment_service.py stays untouched.
    guard_line = db.get(BuyPlanLine, buy_plan_line_id)
    if guard_line is not None and guard_line.payment_method == PaymentMethod.COD.value:
        return _form_error_response(
            "This PO is COD — payment happens on delivery, so there's nothing to pay in advance."
        )

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
        return _form_error_response(str(exc))
    except ValueError as exc:
        db.rollback()
        return _form_error_response(str(exc))

    # Notify accounting/AP (email + Teams) that a prepayment was requested — DO NOT PAY YET.
    # Fire-and-forget: the runner isolates every error so a failed notice never breaks the
    # request that just succeeded.
    from ..services.prepayment_notifications import notify_prepayment_requested, run_prepayment_notify_bg

    await run_prepayment_notify_bg(notify_prepayment_requested, prepayment.id)

    # Re-render the surface the request was raised from so the pill/badge update in place.
    if origin == "approvals_hub":
        from .htmx.approvals_hub import render_tab_body

        resp = render_tab_body(request, current_user, db, "po-approval", hub_scope)
    elif origin == "approvals_workspace":
        # The button's home since the Deal Sheet: the workspace PO-line pane (#aw-pane).
        from .htmx.approvals_hub import render_po_pane

        resp = render_po_pane(request, current_user, db, buy_plan_line_id)
        resp.headers["HX-Trigger"] = "awListRefresh"
    else:
        # Deal Sheet T3: the legacy detail page is retired — the plan's pane is the
        # one render surface.
        from .htmx.approvals_hub import render_plan_pane

        resp = render_plan_pane(request, current_user, db, buy_plan_id)
        resp.headers["HX-Trigger"] = "awListRefresh"

    set_toast(resp, "Prepayment request submitted for approval.", "success", merge=True)
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


def _lifecycle_rerender(
    request: Request, user, db: Session, pp: Prepayment, *, origin: str, scope: str
) -> HTMLResponse:
    """Post-action surface re-render for the mark-paid / unmark-paid / resend POSTs,
    mirroring prepayment_request_create's origin threading: ``approvals_workspace`` →
    the prepayment detail pane into #aw-pane + an awListRefresh nudge (the workspace
    list repaints itself); anything else → #ap-hub-body, the Prepayments hub-tab body
    (the unchanged default)."""
    if origin == "approvals_workspace":
        from .htmx.approvals_hub import render_prepayment_pane

        resp = render_prepayment_pane(request, user, db, pp.id)
        resp.headers["HX-Trigger"] = "awListRefresh"
        return resp
    return _render_prepayment_tab(request, user, db, scope)


@router.get("/v2/partials/prepayments/{prepayment_id}/mark-paid", response_class=HTMLResponse)
def prepayment_mark_paid_modal(
    request: Request,
    prepayment_id: int,
    scope: str = "all",
    origin: str = "",
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
        "origin": origin if origin == "approvals_workspace" else "",
    }
    return template_response("htmx/partials/prepayments/mark_paid_modal.html", ctx)


@router.post("/v2/partials/prepayments/{prepayment_id}/mark-paid", response_class=HTMLResponse)
async def prepayment_mark_paid(
    request: Request,
    prepayment_id: int,
    wire_reference: str | None = Form(None),
    paid_amount: str | None = Form(None),
    scope: str = Form("all"),
    origin: str = Form(""),
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
        return toast_error_response("Enter a valid paid amount.")

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
        return toast_error_response(str(exc))

    resp = _lifecycle_rerender(request, current_user, db, pp, origin=origin, scope=scope)
    set_toast(resp, "Prepayment marked paid.", "success", merge=True)
    return resp


@router.post("/v2/partials/prepayments/{prepayment_id}/unmark-paid", response_class=HTMLResponse)
async def prepayment_unmark_paid(
    request: Request,
    prepayment_id: int,
    scope: str = Form("all"),
    origin: str = Form(""),
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

    try:
        unmark_prepayment_paid(db, pp, current_user)
    except ValueError as exc:
        return toast_error_response(str(exc))

    resp = _lifecycle_rerender(request, current_user, db, pp, origin=origin, scope=scope)
    set_toast(resp, "Payment reversed — prepayment returned to approved.", "success", merge=True)
    return resp


@router.post("/v2/partials/prepayments/{prepayment_id}/resend-pay-link", response_class=HTMLResponse)
async def prepayment_resend_pay_link(
    request: Request,
    prepayment_id: int,
    scope: str = Form("all"),
    origin: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(require_user),
):
    """Re-send the OK-TO-WIRE accounting email for an APPROVED prepayment (the pay link
    got lost or accounting never saw it).

    Same access gate as mark-paid (manager/admin or the plan owner). The live single-use
    ``pay_token`` is NOT re-minted — the originally-emailed link keeps working; the notice
    simply embeds it again (notify_prepayment_approved reads the token live via
    _confirm_url). Approved-only, hard 400 otherwise: a requested prepayment is not yet
    authorized, and a paid/void one has a SPENT token — resending would email a link-less
    OK-TO-WIRE. Router-thin: no service function, only the existing fire-and-forget
    notifier runner.
    """
    pp = db.get(Prepayment, prepayment_id)
    if pp is None:
        raise HTTPException(status_code=404, detail="Prepayment not found")
    _require_mark_paid_access(db, current_user, pp)
    if pp.status != PrepaymentStatus.APPROVED.value:
        raise HTTPException(status_code=400, detail="Only an approved prepayment's pay link can be resent.")

    from ..services.prepayment_notifications import notify_prepayment_approved, run_prepayment_notify_bg

    await run_prepayment_notify_bg(notify_prepayment_approved, pp.id)

    resp = _lifecycle_rerender(request, current_user, db, pp, origin=origin, scope=scope)
    set_toast(resp, "OK-to-wire email resent to accounting.", "success", merge=True)
    return resp
