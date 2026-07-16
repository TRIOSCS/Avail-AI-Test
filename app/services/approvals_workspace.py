"""approvals_workspace.py — read-side view models for the four-tab Approvals Workspace.

Purpose: compose the EXISTING read models (buy_plan_tracking_rows, the per-gate engine
         queues, build_po_queue_view, the buyplan_hub line queries) into the workspace's
         four tab bodies (Sales Orders / Buy Plans / Purchase Orders / Prepayments) plus
         the per-tab "waiting on YOU" badge counts. Presentation composition only — no
         decisions, no writes, no engine logic (specs/approvals-workspace.md §5; the
         approvals engine is untouched). services/approvals/* is deliberately imported,
         never modified.

Called by: routers/htmx/approvals_hub.py (workspace shell + ws tab dispatch).
Depends on: services/approvals/{queue,po_queue}, services/buyplan_hub,
            services/buyplan_workflow (_line_amount), app.dependencies
            (can_verify_po_line, is_manager_or_admin), app.constants enums.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..constants import (
    ApprovalGateType,
    ApprovalRequestStatus,
    BuyPlanStatus,
    UserRole,
)
from ..models.approvals import ApprovalRequest
from ..models.auth import User
from ..models.buy_plan import BuyPlan, BuyPlanLine
from .approvals.po_queue import POQueueView, build_po_queue_view
from .approvals.queue import (
    RowVM,
    _actionable_request_ids,
    buy_plan_tracking_rows,
    pending_rows_for_gate,
    resolved_rows_for_gate,
)

# The four workspace tabs (dash-cased URL segments), in display order.
WORKSPACE_TABS = ("sales-orders", "buy-plans", "purchase-orders", "prepayments")
WORKSPACE_TAB_LABELS = {
    "sales-orders": "Sales Orders",
    "buy-plans": "Buy Plans",
    "purchase-orders": "Purchase Orders",
    "prepayments": "Prepayments",
}
DEFAULT_WORKSPACE_TAB = "sales-orders"

# Old 3-tab console keys → their workspace home, so pushed/bookmarked ?tab= URLs from
# the retired hub land on the right tab instead of 404ing.
LEGACY_TAB_MAP = {
    "buy-plan": "buy-plans",
    "po-approval": "purchase-orders",
    "prepayment": "prepayments",
}

# Legacy export tab key per workspace tab (the CSV export routes keep their gate keys).
EXPORT_TAB_MAP = {
    "sales-orders": "buy-plan",
    "buy-plans": "buy-plan",
    "purchase-orders": "po-approval",
    "prepayments": "prepayment",
}

# PO-cutter roles (mirrors routers/htmx/buy_plans._PO_CUTTER_ROLES; duplicated here
# because a service must not import from routers).
_PO_CUTTER_ROLES = frozenset({UserRole.BUYER, UserRole.MANAGER, UserRole.ADMIN})

# Statuses hidden by the left list's live-work default filter.
_DONE_STATUSES = frozenset({BuyPlanStatus.COMPLETED.value, BuyPlanStatus.CANCELLED.value})


def resolve_workspace_tab(tab: str) -> str:
    """Normalize a ?tab= value to a workspace tab key (legacy keys map to their
    home)."""
    if tab in WORKSPACE_TABS:
        return tab
    return LEGACY_TAB_MAP.get(tab, DEFAULT_WORKSPACE_TAB)


# ── Row view-models (plain fields only — no ORM access in Jinja) ─────────────


@dataclass
class WsPlanRow:
    """One buy plan / sales order for the Sales Orders + Buy Plans left lists."""

    plan_id: int
    status: str
    customer_name: str | None
    so_number: str | None
    amount: object  # Decimal | None — rendered via '{:,.0f}'.format
    revenue: object
    gross_profit: object
    margin_pct: object
    part_count: int
    can_decide: bool
    age_hours: float
    is_live: bool

    @property
    def search_blob(self) -> str:
        """Lower-cased haystack for the client-side search filter."""
        parts = [self.customer_name or "", self.so_number or "", f"plan {self.plan_id}"]
        return " ".join(p for p in parts if p).lower()


@dataclass
class WsLineRow:
    """One buy-plan line for the Purchase Orders left list (buyer/pool sections)."""

    line_id: int
    plan_id: int
    status: str
    customer_name: str | None
    primary_mpn: str | None
    vendor_name: str | None
    amount: float
    po_number: str | None
    age_hours: float

    @property
    def search_blob(self) -> str:
        parts = [
            self.customer_name or "",
            self.primary_mpn or "",
            self.vendor_name or "",
            self.po_number or "",
        ]
        return " ".join(p for p in parts if p).lower()


@dataclass
class WsPoTab:
    """Everything the Purchase Orders tab body renders."""

    queue: POQueueView
    my_lines: list[WsLineRow] = field(default_factory=list)
    pool: list[WsLineRow] = field(default_factory=list)


# ── Tab builders ─────────────────────────────────────────────────────────────


def _plan_age_reference(db: Session, plan_ids: list[int]) -> dict[int, datetime | None]:
    """Batch (one query) the aging anchor per plan: submitted_at when set (decision
    queues age from submission), else created_at."""
    if not plan_ids:
        return {}
    rows = db.execute(
        select(BuyPlan.id, func.coalesce(BuyPlan.submitted_at, BuyPlan.created_at)).where(BuyPlan.id.in_(plan_ids))
    ).all()
    return {pid: ts for pid, ts in rows}


def plan_rows(db: Session, user: User, *, scope: str = "all") -> list[WsPlanRow]:
    """Sales Orders / Buy Plans left-list rows: the tracking read model enriched with an
    age chip anchor, sorted decision-queue-first (oldest pending decision on top).

    Composes ``buy_plan_tracking_rows`` untouched; the extra aging query is batched.
    """
    from .buyplan_hub import _age_hours

    base = buy_plan_tracking_rows(db, user, scope=scope)
    ages = _plan_age_reference(db, [r.plan_id for r in base])
    rows = [
        WsPlanRow(
            plan_id=r.plan_id,
            status=r.status,
            customer_name=r.customer_name,
            so_number=r.so_number,
            amount=r.amount,
            revenue=r.revenue,
            gross_profit=r.gross_profit,
            margin_pct=r.margin_pct,
            part_count=r.part_count,
            can_decide=r.can_decide,
            age_hours=_age_hours(ages.get(r.plan_id)),
            is_live=r.status not in _DONE_STATUSES,
        )
        for r in base
    ]
    # Decision queue first, OLDEST first (the spec's "decision queues oldest-first");
    # tracking rows after, newest first.
    rows.sort(key=lambda r: (not r.can_decide, -r.age_hours if r.can_decide else -r.plan_id))
    return rows


def _line_row(line: BuyPlanLine) -> WsLineRow:
    from .buyplan_hub import _age_hours, _customer_name, _line_mpn
    from .buyplan_workflow import _line_amount

    plan = line.buy_plan
    return WsLineRow(
        line_id=line.id,
        plan_id=line.buy_plan_id,
        status=line.status,
        customer_name=_customer_name(plan) if plan else None,
        primary_mpn=_line_mpn(line),
        vendor_name=line.offer.vendor_name if line.offer else None,
        amount=_line_amount(line),
        po_number=line.po_number,
        age_hours=_age_hours(line.po_confirmed_at or line.created_at),
    )


def po_tab(db: Session, user: User, *, scope: str = "all") -> WsPoTab:
    """Purchase Orders tab: the org-wide approval queue (oldest first) plus the
    viewer's own cut-PO worklist and the open re-sourcing pool (PO cutters only)."""
    from .buyplan_hub import _query_buyer_awaiting_po_lines, _query_resourcing_pool

    view = build_po_queue_view(db, user, scope=scope)
    my_lines: list[WsLineRow] = []
    pool: list[WsLineRow] = []
    if user.role in _PO_CUTTER_ROLES:
        my_lines = [_line_row(ln) for ln in _query_buyer_awaiting_po_lines(db, buyer_id=user.id)]
        pool = [_line_row(ln) for ln in _query_resourcing_pool(db)]
    return WsPoTab(queue=view, my_lines=my_lines, pool=pool)


def prepayment_rows(
    db: Session, user: User, *, scope: str = "all"
) -> tuple[list[RowVM], list[RowVM], dict[int, float]]:
    """Prepayments tab rows: (pending, recently-resolved, pending-row ages in hours)."""
    from .buyplan_hub import _age_hours

    pending = pending_rows_for_gate(db, user, ApprovalGateType.PREPAYMENT, scope=scope)
    resolved = resolved_rows_for_gate(db, ApprovalGateType.PREPAYMENT, scope=scope, user=user)
    ages = {row.id: _age_hours(row.created_at) for row in pending}
    return pending, resolved, ages


# ── Badges: items waiting on the VIEWER (not the org) ────────────────────────


def waiting_counts(db: Session, user: User) -> dict[str, int]:
    """Per-tab "waiting on you" badge counts.

    - sales-orders: plan approvals the viewer can decide + their own DRAFT (incl.
      returned) plans awaiting submit.
    - buy-plans: plan approvals the viewer can decide (same single approval, spec §7 —
      reachable from both tabs).
    - purchase-orders: PENDING_VERIFY lines within the viewer's approval limit + (for
      PO cutters) their own awaiting-PO lines and the open re-sourcing pool.
    - prepayments: prepayment requests the viewer can decide.
    """
    from ..dependencies import can_verify_po_line
    from .buyplan_hub import (
        _query_buyer_awaiting_po_lines,
        _query_po_pending_verify,
        _query_resourcing_pool,
    )

    gate_counts: dict[str, int] = {}
    actionable = _actionable_request_ids(db, user)
    if actionable:
        rows = db.execute(
            select(ApprovalRequest.gate_type, func.count(ApprovalRequest.id))
            .where(
                ApprovalRequest.id.in_(actionable),
                ApprovalRequest.status == ApprovalRequestStatus.REQUESTED,
            )
            .group_by(ApprovalRequest.gate_type)
        ).all()
        gate_counts = {gate: int(cnt) for gate, cnt in rows}

    plan_approvals = gate_counts.get(ApprovalGateType.BUY_PLAN, 0)

    own_drafts = int(
        db.execute(
            select(func.count(BuyPlan.id)).where(
                BuyPlan.status == BuyPlanStatus.DRAFT,
                BuyPlan.submitted_by_id == user.id,
            )
        ).scalar_one()
    )

    po_count = sum(1 for line in _query_po_pending_verify(db) if can_verify_po_line(user, line))
    if user.role in _PO_CUTTER_ROLES:
        po_count += len(_query_buyer_awaiting_po_lines(db, buyer_id=user.id))
        po_count += len(_query_resourcing_pool(db))

    return {
        "sales-orders": plan_approvals + own_drafts,
        "buy-plans": plan_approvals,
        "purchase-orders": po_count,
        "prepayments": gate_counts.get(ApprovalGateType.PREPAYMENT, 0),
    }


def role_ctx(user: User) -> dict[str, bool]:
    """Role-detection booleans the workspace templates key on (no generic role global —
    matches the codebase convention of passing explicit booleans)."""
    from ..dependencies import is_manager_or_admin

    return {
        "is_manager_admin": is_manager_or_admin(user),
        "can_cut_po": user.role in _PO_CUTTER_ROLES,
        "is_sales_tier": user.role in (UserRole.SALES, UserRole.TRADER),
    }
