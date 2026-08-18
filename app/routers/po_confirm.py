"""routers/po_confirm.py — PUBLIC tokenized "confirm PO cut" page for buyers.

Purpose: the buy-plan approval email tells each buyer to cut their POs in the ERP; this
page lets them confirm it (PO# + est. ship + payment method) straight from that email
with NO AVAIL login — the buyer leg of the Deal Sheet relay, mirroring the accounting
pay-link (routers/prepayment_confirm.py). The signed token in the URL IS the
authorization (stateless — services/po_confirm_tokens.py), so the routes are
deliberately auth-less, CSRF-exempt (path added to CSRF_EXEMPT_URLS in app/main.py) and
rate-limited. Single-use by STATE: confirm_po acts only on an AWAITING_PO line of an
ACTIVE plan, so a spent or overtaken link renders a read-only status page and never
re-confirms. The confirm is recorded exactly as the in-app path records it (same
service, same notification fan-out), attributed to the token's buyer.

Routes (both PUBLIC):
  - GET  /po/confirm/{token} — the confirm form, or a read-only status page.
  - POST /po/confirm/{token} — records the PO then a thank-you page.

Called by: app.main (router registration); the per-line "Confirm PO" links that
           notify_approved embeds in the buyer email.
Depends on: app.database (get_db), app.models (BuyPlanLine, User), app.rate_limit
            (limiter), app.services.buyplan_workflow (confirm_po),
            app.services.po_confirm_tokens, app.template_env.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session, joinedload
from starlette.responses import HTMLResponse

from ..constants import PO_LINE_PAYMENT_METHODS, BuyPlanLineStatus, BuyPlanStatus
from ..database import get_db
from ..models import User
from ..models.buy_plan import BuyPlanLine
from ..rate_limit import limiter
from ..services.po_confirm_tokens import resolve_po_confirm_token
from ..template_env import template_response

router = APIRouter(tags=["po-confirm"])

_TEMPLATE = "htmx/partials/buy_plans/po_confirm_page.html"

_PAYMENT_CHOICES = [
    (m.value, m.value.upper() if len(m.value) <= 3 else m.value.title()) for m in PO_LINE_PAYMENT_METHODS
]


def _load(db: Session, token: str) -> tuple[BuyPlanLine | None, User | None]:
    """Resolve the token to its (line, buyer) — (None, None) for a bad/expired token."""
    resolved = resolve_po_confirm_token(token)
    if resolved is None:
        return None, None
    line_id, buyer_id = resolved
    line = db.get(
        BuyPlanLine,
        line_id,
        options=[
            joinedload(BuyPlanLine.offer),
            joinedload(BuyPlanLine.requirement),
            joinedload(BuyPlanLine.buy_plan),
        ],
    )
    buyer = db.get(User, buyer_id)
    return line, buyer


def _mode(line: BuyPlanLine) -> str:
    """Which panel to show for a resolvable line."""
    if line.status in (BuyPlanLineStatus.PENDING_VERIFY.value, BuyPlanLineStatus.VERIFIED.value):
        return "confirmed"
    plan = line.buy_plan
    if line.status == BuyPlanLineStatus.AWAITING_PO.value and plan and plan.status == BuyPlanStatus.ACTIVE.value:
        return "form"
    return "inactive"


def _render(request: Request, line: BuyPlanLine | None, mode: str, *, error: str = "", status_code: int = 200):
    plan = line.buy_plan if line is not None else None
    requirement = line.requirement if line is not None else None
    offer = line.offer if line is not None else None
    ctx = {
        "request": request,
        "line": line,
        "plan": plan,
        "mode": mode,
        "error": error,
        "mpn": (requirement.primary_mpn if requirement else None) or (offer.mpn if offer else None) or "—",
        "vendor": (offer.vendor_name if offer else None) or "—",
        "need_by": requirement.need_by_date if requirement is not None else None,
        "payment_methods": _PAYMENT_CHOICES,
    }
    return template_response(_TEMPLATE, ctx, status_code=status_code)


@router.get("/po/confirm/{token}", response_class=HTMLResponse)
@limiter.limit("10/minute")
async def po_confirm_get(request: Request, token: str, db: Session = Depends(get_db)):
    """The confirm form for an open line, or a read-only status/404 page."""
    line, buyer = _load(db, token)
    if line is None or buyer is None:
        return _render(request, None, "not_found", status_code=404)
    return _render(request, line, _mode(line))


@router.post("/po/confirm/{token}", response_class=HTMLResponse)
@limiter.limit("10/minute")
async def po_confirm_post(
    request: Request,
    token: str,
    po_number: str = Form(""),
    estimated_ship_date: str = Form(""),
    payment_method: str = Form(""),
    db: Session = Depends(get_db),
):
    """Record the PO exactly like the in-app confirm (same service, same fan-out),
    attributed to the token's buyer; then the thank-you page.

    Idempotent + state-scoped: acts only while the line is still AWAITING_PO on an
    ACTIVE plan — a concurrent confirm (caught as the service ValueError) renders the
    read-only page and never double-confirms.
    """
    line, buyer = _load(db, token)
    if line is None or buyer is None:
        return _render(request, None, "not_found", status_code=404)
    if _mode(line) != "form":
        return _render(request, line, _mode(line))

    from ..services.buyplan_workflow import confirm_po

    try:
        ship_date = datetime.fromisoformat(estimated_ship_date) if estimated_ship_date else datetime.now(UTC)
    except ValueError:
        ship_date = datetime.now(UTC)

    try:
        confirm_po(
            line.buy_plan_id,
            line.id,
            (po_number or "").strip(),
            ship_date,
            buyer,
            db,
            payment_method=(payment_method or "").strip() or None,
        )
        db.commit()
    except ValueError as e:
        db.rollback()
        db.refresh(line)
        mode = _mode(line)
        if mode != "form":  # overtaken concurrently — read-only, never re-confirm
            return _render(request, line, mode)
        return _render(request, line, "form", error=str(e), status_code=400)

    # Same fan-out the in-app confirm fires (manager verify nudge etc.).
    from ..services.buyplan_notifications import notify_po_confirmed, run_v3_notify_bg

    await run_v3_notify_bg(notify_po_confirmed, line.buy_plan_id, line_id=line.id)

    return _render(request, line, "recorded")
