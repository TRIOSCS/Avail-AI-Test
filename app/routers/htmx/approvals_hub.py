"""routers/htmx/approvals_hub.py — Approvals Workspace shell + tab dispatch (HTMX +
Alpine).

/v2/approvals is the four-tab Approvals Workspace (specs/approvals-workspace.md):
Sales Orders / Buy Plans / Purchase Orders / Prepayments — one page replacing TRIO's
Teams approval forms, QP workbooks, and Planner boards. The shell renders four tab
pills with per-viewer "waiting on you" badges; each tab body is a split-view partial
(_ws_tab_*.html) built from services/approvals_workspace view models.

The pre-workspace 3-tab console bodies (buy-plan / po-approval / prepayment) remain
served by the same {tab} route until Phase 6 cutover: the buy_plans.py decide handlers
(verify-po / resource / approve / prepay-decide, origin=approvals_hub) still re-render
them via ``render_tab_body``, and legacy ?tab= deep links map to their workspace home.

Called by: app/main.py (router mount).
Depends on: app.dependencies, app.database, app.services.approvals.{queue,po_queue},
    app.services.approvals_workspace, ._shared (_base_ctx), app.template_env.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ...constants import AccessKey, ApprovalGateType
from ...database import get_db
from ...dependencies import require_access, require_user
from ...models import User
from ...services.approvals.po_queue import build_po_queue_view
from ...services.approvals.queue import (
    buy_plan_tracking_rows,
    pending_rows_for_gate,
    resolved_rows_for_gate,
)
from ...services.approvals_workspace import (
    EXPORT_TAB_MAP,
    WORKSPACE_TAB_LABELS,
    WORKSPACE_TABS,
    plan_rows,
    po_tab,
    prepayment_rows,
    resolve_workspace_tab,
    role_ctx,
    waiting_counts,
)
from ...template_env import template_response
from ...utils.csv_export import stream_csv
from ._shared import _base_ctx

router = APIRouter(tags=["htmx-views"])

# Legacy 3-tab console keys (kept until Phase 6 cutover — the decide handlers'
# origin=approvals_hub branch still re-renders these bodies).
_TABS = ("buy-plan", "po-approval", "prepayment")
DEFAULT_TAB = "buy-plan"


@router.get("/v2/partials/approvals", response_class=HTMLResponse)
async def approvals_hub_shell(
    request: Request,
    tab: str = "",
    user: User = Depends(require_access(AccessKey.BUY_PLANS)),
    db: Session = Depends(get_db),
):
    """Return the Approvals Workspace shell (4-pill tab switcher + a lazy body).

    The shell renders the Sales Orders / Buy Plans / Purchase Orders / Prepayments pills
    with per-viewer "waiting on you" badge counts + a lazy body that loads the active tab
    partial into ``#ws-body``. Row data is fetched by the body, not here. ``?tab=``
    threads a deep-link / pushed tab URL; legacy 3-tab keys map to their workspace home.
    """
    active_tab = resolve_workspace_tab(tab)
    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update(
        {
            "active_tab": active_tab,
            "tabs": WORKSPACE_TABS,
            "tab_labels": WORKSPACE_TAB_LABELS,
            "counts": waiting_counts(db, user),
        }
    )
    return template_response("htmx/partials/approvals/workspace.html", ctx)


@router.get("/v2/partials/approvals/{tab}", response_class=HTMLResponse)
async def approvals_hub_tab(
    request: Request,
    tab: str,
    scope: str = "all",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render one Approvals tab body.

    Workspace keys (sales-orders / buy-plans / purchase-orders / prepayments) render the
    split-view ``_ws_tab_*`` bodies into ``#ws-body``; legacy 3-tab keys still render the
    old console bodies into ``#ap-hub-body`` (the decide handlers' origin=approvals_hub
    branch depends on them until Phase 6). Any other value 404s. ``scope`` is the
    All / Mine toggle (default ``all``). The two-segment prepay-decide POST
    (buy_plans.py) does not collide with this one-segment GET converter.
    """
    if tab in WORKSPACE_TABS:
        return render_ws_tab_body(request, user, db, tab, scope)
    if tab not in _TABS:
        raise HTTPException(404, "Unknown approvals tab")
    return render_tab_body(request, user, db, tab, scope)


def render_ws_tab_body(request: Request, user: User, db: Session, tab: str, scope: str = "all") -> HTMLResponse:
    """Build + render one workspace tab body (shared by the tab GET now and, from Phase
    2 on, the decide handlers' origin=workspace re-render branches)."""
    scope = "mine" if scope == "mine" else "all"
    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update(role_ctx(user))
    ctx.update({"scope": scope, "tab": tab, "export_tab": EXPORT_TAB_MAP[tab], "user": user})

    if tab in ("sales-orders", "buy-plans"):
        ctx["rows"] = plan_rows(db, user, scope=scope)
        template = (
            "htmx/partials/approvals/_ws_tab_sales_orders.html"
            if tab == "sales-orders"
            else "htmx/partials/approvals/_ws_tab_buy_plans.html"
        )
        return template_response(template, ctx)

    if tab == "purchase-orders":
        from ...services.prepayment_service import prepayment_state_for_lines

        po = po_tab(db, user, scope=scope)
        ctx.update(
            {
                "po": po,
                "prepay_state": prepayment_state_for_lines(db, [row.line.id for row in po.queue.pending]),
            }
        )
        return template_response("htmx/partials/approvals/_ws_tab_purchase_orders.html", ctx)

    # prepayments
    pending, resolved, ages = prepayment_rows(db, user, scope=scope)
    ctx.update({"pending_rows": pending, "resolved_rows": resolved, "prepay_ages": ages})
    return template_response("htmx/partials/approvals/_ws_tab_prepayments.html", ctx)


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
