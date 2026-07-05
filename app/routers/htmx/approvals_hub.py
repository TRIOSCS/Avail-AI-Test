"""routers/htmx/approvals_hub.py — Approvals hub (3-tab decide surface, HTMX + Alpine).

The clean, org-wide "browse + decide + history, per gate type" console at /v2/approvals —
one tab per surviving approval gate:
  - Buy Plan   → BUY_PLAN engine gate   (services/approvals/queue helpers)
  - PO Approval→ per-line PENDING_VERIFY (services/approvals/po_queue — NOT engine-backed)
  - Prepayment → PREPAYMENT engine gate (services/approvals/queue helpers)

Distinct from the Buy Plans hub (routers/htmx/buy_plans.py), which owns the personal
My Queue / Pipeline surfaces at /v2/buy-plans. Origination ("New Buy Plan") is NOT here —
it is deal creation, not a decide action.

``render_tab_body`` is shared: the tab GET route and the buy_plans.py decide handlers
(verify-po / resource / approve / prepay-decide, origin=approvals_hub) both call it so a
one-click decision re-renders the refreshed tab in place.

Called by: app/main.py (router mount).
Depends on: app.dependencies, app.database, app.services.approvals.{queue,po_queue},
    ._shared (_base_ctx), app.template_env.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...constants import AccessKey, ApprovalGateType, BuyPlanLineStatus
from ...database import get_db
from ...dependencies import require_access, require_user
from ...models import BuyPlanLine, User
from ...services.approvals.po_queue import build_po_queue_view
from ...services.approvals.queue import (
    buy_plan_tracking_rows,
    pending_count_for_gate,
    pending_rows_for_gate,
    resolved_rows_for_gate,
)
from ...template_env import template_response
from ...utils.csv_export import stream_csv
from ._shared import _base_ctx

router = APIRouter(tags=["htmx-views"])

# tab key (dash-cased URL segment) → order. One per surviving gate type.
_TABS = ("buy-plan", "po-approval", "prepayment")
DEFAULT_TAB = "buy-plan"


def _po_pending_count(db: Session) -> int:
    """Org-wide count of PENDING_VERIFY lines (the PO Approval pill)."""
    return int(
        db.execute(
            select(func.count(BuyPlanLine.id)).where(BuyPlanLine.status == BuyPlanLineStatus.PENDING_VERIFY)
        ).scalar_one()
    )


@router.get("/v2/partials/approvals", response_class=HTMLResponse)
async def approvals_hub_shell(
    request: Request,
    tab: str = "",
    user: User = Depends(require_access(AccessKey.BUY_PLANS)),
    db: Session = Depends(get_db),
):
    """Return the Approvals hub shell (3-pill tab switcher + a lazy body).

    The shell renders the Buy Plan / PO Approval / Prepayment pills (with org-wide pending
    counts) + a lazy body that loads the active tab partial into ``#ap-hub-body``. Row data
    is fetched by the body, not here. ``?tab=`` threads a deep-link / pushed tab URL.
    """
    active_tab = tab if tab in _TABS else DEFAULT_TAB
    counts = {
        "buy-plan": pending_count_for_gate(db, ApprovalGateType.BUY_PLAN),
        "po-approval": _po_pending_count(db),
        "prepayment": pending_count_for_gate(db, ApprovalGateType.PREPAYMENT),
    }
    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update({"active_tab": active_tab, "counts": counts})
    return template_response("htmx/partials/approvals/approvals_hub.html", ctx)


@router.get("/v2/partials/approvals/{tab}", response_class=HTMLResponse)
async def approvals_hub_tab(
    request: Request,
    tab: str,
    scope: str = "all",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render one Approvals hub tab body into ``#ap-hub-body``.

    ``tab`` is one of buy-plan / po-approval / prepayment; any other value 404s. ``scope``
    is the SEE-ALL / SEE-MINE toggle (default ``all`` — the full org-wide queue). The
    two-segment prepay-decide POST (buy_plans.py) does not collide with this one-segment
    GET converter.
    """
    if tab not in _TABS:
        raise HTTPException(404, "Unknown approvals tab")
    return render_tab_body(request, user, db, tab, scope)


def _fmt_dt(dt: datetime | None) -> str:
    """Minute-precision timestamp for a CSV cell (empty string when missing)."""
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""


@router.get("/v2/partials/approvals/{tab}/export")
async def approvals_hub_export(
    tab: str,
    scope: str = "all",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Stream one Approvals hub list as a CSV download (attachment).

    Same auth (require_user) and SEE-ALL / SEE-MINE scope as the console tab body, reusing
    each tab's exact read model so the download can never drift from what the console shows:
      - ``buy-plan``    → the Buy Plans / Sales Orders tracking list (buy_plan_tracking_rows);
      - ``prepayment``  → the Prepayment "Recently resolved" audit feed (resolved_rows_for_gate);
      - ``po-approval`` → the PO Approval "Recently resolved" audit feed (build_po_queue_view
        .history — org-wide by construction, so ``scope`` is threaded but does not narrow it,
        matching the console's own PO history section).
    Any ``tab`` outside the three surviving gate tabs 404s.
    """
    if tab not in _TABS:
        raise HTTPException(404, "Unknown approvals tab")
    scope = "mine" if scope == "mine" else "all"

    if tab == "buy-plan":
        header = ["Plan ID", "Customer", "Sales Order", "Status", "Value"]
        rows = (
            [r.plan_id, r.customer_name, r.so_number, r.status, r.amount]
            for r in buy_plan_tracking_rows(db, user, scope=scope)
        )
        return stream_csv(f"approvals_buy_plans_{scope}.csv", header, rows)

    if tab == "prepayment":
        header = [
            "Prepayment ID",
            "Beneficiary",
            "Plan ID",
            "PO Number",
            "SO Number",
            "Amount",
            "Currency",
            "Request Status",
            "Payment Status",
            "Decided By",
            "Wire Reference",
            "Resolution Note",
            "Resolved Date",
        ]
        rows = (
            [
                r.subject_id,
                r.beneficiary or r.subject_label,
                r.plan_id,
                r.po_number,
                r.so_number,
                r.amount,
                r.currency,
                r.status,
                r.prepay_status,
                r.decided_by,
                r.wire_reference,
                r.resolution_note,
                _fmt_dt(r.resolved_at),
            ]
            for r in resolved_rows_for_gate(db, ApprovalGateType.PREPAYMENT, scope=scope, user=user)
        )
        return stream_csv(f"approvals_prepayments_resolved_{scope}.csv", header, rows)

    # po-approval — the org-wide Recently-resolved PO decision feed.
    header = ["Plan ID", "Outcome", "Description", "Actor", "Note", "Resolved Date"]
    rows = (
        [h.plan_id, h.kind, h.label, h.actor_name, h.note, _fmt_dt(h.when)]
        for h in build_po_queue_view(db, user, scope=scope).history
    )
    return stream_csv("approvals_po_resolved.csv", header, rows)


def render_tab_body(request: Request, user: User, db: Session, tab: str, scope: str = "all") -> HTMLResponse:
    """Build + render one Approvals hub tab body (shared by the tab GET + the decide
    handlers' origin=approvals_hub re-render branches).

    ``scope`` (``all`` | ``mine``) is threaded to every tab's read model AND into the tab's
    scope toggle + decide-form hidden field so a decision re-renders the SAME scope.
    """
    scope = "mine" if scope == "mine" else "all"
    ctx = _base_ctx(request, user, "buy-plans")
    ctx["scope"] = scope

    if tab == "buy-plan":
        ctx["rows"] = buy_plan_tracking_rows(db, user, scope=scope)
        return template_response("htmx/partials/approvals/_tab_buy_plan.html", ctx)

    if tab == "prepayment":
        from ...dependencies import is_manager_or_admin

        ctx.update(
            {
                "pending_rows": pending_rows_for_gate(db, user, ApprovalGateType.PREPAYMENT, scope=scope),
                "resolved_rows": resolved_rows_for_gate(db, ApprovalGateType.PREPAYMENT, scope=scope, user=user),
                "user": user,
                # Gate the payment-closure affordances: the "Undo paid" correction is
                # manager/admin only; "Mark paid" is also offered to the requester (owner).
                "is_manager_admin": is_manager_or_admin(user),
            }
        )
        return template_response("htmx/partials/approvals/_tab_prepayment.html", ctx)

    # po-approval — the per-line PENDING_VERIFY trio (not engine-backed).
    from ...services.prepayment_service import prepayment_state_for_lines
    from .buy_plans import _can_resource

    view = build_po_queue_view(db, user, scope=scope)
    ctx.update(
        {
            "view": view,
            "user": user,
            "can_resource": _can_resource(user),
            # Live prepayment state per line (badge #11 + button→pill #10), one batch query.
            "prepay_state": prepayment_state_for_lines(db, [row.line.id for row in view.pending]),
        }
    )
    return template_response("htmx/partials/approvals/_tab_po_approval.html", ctx)
