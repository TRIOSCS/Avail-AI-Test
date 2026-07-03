"""routers/prepayment_confirm.py — PUBLIC tokenized "confirm wire sent" page.

Purpose: Lets the (non-Avail) accounting/AP team who actually execute the wire mark an
APPROVED prepayment PAID straight from the "OK TO WIRE" approval email — with NO Avail
login. The single-use ``pay_token`` carried in the emailed URL IS the authorization; there
is no session, so these routes are deliberately auth-less, CSRF-exempt (the path is added
to ``CSRF_EXEMPT_URLS`` in app/main.py), and rate-limited (``@limiter.limit``). The token is
minted on approve and cleared on paid/void, so a spent link is inert (resolves to nothing →
404). Idempotent: a paid/void prepayment renders a read-only status page and NEVER re-marks
or re-fires the paid fan-out.

Routes (both PUBLIC):
  - GET  /p/confirm/{token} — the confirm form (approved) or a read-only status page.
  - POST /p/confirm/{token} — records the wire (approved → paid) then a thank-you page.

Called by: app.main (router registration); the "Confirm wire sent" link that
           notify_prepayment_approved embeds in the approval email + Teams card.
Depends on: app.database (get_db), app.models.quality_plan (Prepayment),
            app.services.prepayment_service (mark_prepayment_paid), app.constants
            (PrepaymentStatus), app.rate_limit (limiter), app.template_env
            (template_response).
"""

from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session
from starlette.responses import HTMLResponse

from ..constants import PrepaymentStatus
from ..database import get_db
from ..models.quality_plan import Prepayment
from ..rate_limit import limiter
from ..services.prepayment_service import mark_prepayment_paid
from ..template_env import template_response

router = APIRouter(tags=["prepayment-confirm"])

_TEMPLATE = "htmx/partials/prepayments/confirm_page.html"


def _beneficiary(prepayment: Prepayment) -> str:
    """Human payee name — the notifications module's legal-name helper if importable,
    else the snapshot vendor_name."""
    try:
        from ..services.prepayment_notifications import _beneficiary as _bn

        return _bn(prepayment)
    except Exception:
        return prepayment.vendor_name or "—"


def _amount_display(prepayment: Prepayment) -> str:
    """Amount to 2 decimals honoring the prepayment currency (e.g. ``USD
    20,002.38``)."""
    amount = prepayment.total_incl_fees if prepayment.total_incl_fees is not None else Decimal("0")
    return f"{prepayment.currency or 'USD'} {amount:,.2f}"


def _po_number(prepayment: Prepayment) -> str:
    line = prepayment.buy_plan_line
    return (line.po_number if line is not None and line.po_number else None) or "—"


def _so_number(prepayment: Prepayment) -> str:
    plan = prepayment.buy_plan
    return (plan.sales_order_number if plan is not None and plan.sales_order_number else None) or "—"


def _readonly_mode(prepayment: Prepayment) -> str:
    """The read-only page mode for a non-approved prepayment found by token."""
    if prepayment.status == PrepaymentStatus.PAID.value:
        return "paid"
    if prepayment.status == PrepaymentStatus.VOID.value:
        return "voided"
    # A token only exists on an approved/paid/void prepayment; anything else is defensive.
    return "inactive"


def _render(request: Request, prepayment: Prepayment | None, mode: str, *, status_code: int = 200):
    """Render the standalone public confirm page in *mode* (form / recorded / paid /
    voided / inactive / not_found)."""
    ctx = {
        "request": request,
        "pp": prepayment,
        "mode": mode,
        "beneficiary": _beneficiary(prepayment) if prepayment is not None else None,
        "amount_display": _amount_display(prepayment) if prepayment is not None else None,
        "po_number": _po_number(prepayment) if prepayment is not None else None,
        "so_number": _so_number(prepayment) if prepayment is not None else None,
    }
    return template_response(_TEMPLATE, ctx, status_code=status_code)


@router.get("/p/confirm/{token}", response_class=HTMLResponse)
@limiter.limit("10/minute")
async def confirm_get(request: Request, token: str, db: Session = Depends(get_db)):
    """Show the confirm form for an approved prepayment, or a read-only status/404 page.

    The token IS the authorization (public route). A cleared/unknown token resolves to
    nothing → the 404 "expired link" page.
    """
    prepayment = db.query(Prepayment).filter_by(pay_token=token).one_or_none()
    if prepayment is None:
        return _render(request, None, "not_found", status_code=404)
    if prepayment.status != PrepaymentStatus.APPROVED.value:
        return _render(request, prepayment, _readonly_mode(prepayment))
    return _render(request, prepayment, "form")


@router.post("/p/confirm/{token}", response_class=HTMLResponse)
@limiter.limit("10/minute")
async def confirm_post(
    request: Request,
    token: str,
    wire_reference: str = Form(""),
    confirmer: str = Form(""),
    db: Session = Depends(get_db),
):
    """Record the wire: transition an approved prepayment to paid, then the thank-you page.

    Idempotent + token-scoped: acts ONLY when the token still resolves to an ``approved``
    prepayment. A paid/void prepayment (or a status flip between the lookup and the
    transition, caught as ValueError) renders the read-only page and never re-marks or
    re-fires the paid fan-out. ``paid_via`` is stamped ``accounting_email`` (this path);
    ``paid_amount`` defaults to the full ``total_incl_fees``.
    """
    prepayment = db.query(Prepayment).filter_by(pay_token=token).one_or_none()
    if prepayment is None:
        return _render(request, None, "not_found", status_code=404)
    if prepayment.status != PrepaymentStatus.APPROVED.value:
        return _render(request, prepayment, _readonly_mode(prepayment))

    try:
        mark_prepayment_paid(
            db,
            prepayment,
            wire_reference=(wire_reference or "").strip() or None,
            paid_amount=prepayment.total_incl_fees,
            paid_via="accounting_email",
            paid_by_label=(confirmer or "").strip() or "Accounting",
        )
    except ValueError:
        # Concurrent double-submit: status already flipped out of approved — idempotent,
        # no re-mark, no re-fire. Show the read-only page for the now-current status.
        db.refresh(prepayment)
        return _render(request, prepayment, _readonly_mode(prepayment))

    return _render(request, prepayment, "recorded")
