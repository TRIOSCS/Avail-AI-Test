"""buyplan_hub.py — Buy Plan Deal Hub read models.

Purpose: Role-aware read models for the Buy Plan Deal Hub page.
         Provides the buyer's per-line PO queue (lines the buyer must cut a PO for)
         across all their active deals, plus the stage-grouped deal board for sales
         and manager views, plus the manager supervise overview (metric strip + triage).

Called by: routers/htmx_views.py (buy plan hub partials)
Depends on: models.buy_plan (BuyPlan, BuyPlanLine), models.auth (User),
            constants (BuyPlanStatus, BuyPlanLineStatus, SOVerificationStatus)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from ..config import settings
from ..constants import BuyPlanLineStatus, BuyPlanStatus, SOVerificationStatus
from ..models.buy_plan import BuyPlan, BuyPlanLine


def _customer_name(plan: BuyPlan) -> str | None:
    """Derive a display customer name: plan.quote → customer_site → company.name.

    Falls back to the site's ``site_name`` when the company is missing, and to
    ``None`` when there is no quote/site. Mirrors buy_plans_list_partial in
    routers/htmx_views.py.
    """
    if plan.quote and plan.quote.customer_site:
        site = plan.quote.customer_site
        co = site.company if hasattr(site, "company") else None
        return co.name if co else getattr(site, "site_name", None)
    return None


def buyer_line_queue(db: Session, user: object) -> list[dict]:
    """Return one dict per actionable buy-plan line assigned to ``user``.

    "Actionable" means:
    - ``BuyPlanLine.buyer_id == user.id``
    - ``BuyPlanLine.status == AWAITING_PO``
    - parent ``BuyPlan.status == ACTIVE``

    Rows are sorted kicked-back first (``po_rejection_note is not None``), then
    by ``plan.created_at`` ascending so the oldest deal surfaces first.

    Each dict contains:
        line_id, plan_id, customer_name, mpn, description, vendor_name,
        vendor_contact_email, quantity, unit_cost, status, kicked_back,
        po_rejection_note, plan_created_at
    """
    lines = (
        db.query(BuyPlanLine)
        .join(BuyPlan, BuyPlanLine.buy_plan_id == BuyPlan.id)
        .filter(
            BuyPlanLine.buyer_id == user.id,
            BuyPlanLine.status == BuyPlanLineStatus.AWAITING_PO,
            BuyPlan.status == BuyPlanStatus.ACTIVE,
        )
        .options(
            joinedload(BuyPlanLine.buy_plan).joinedload(BuyPlan.quote),
            joinedload(BuyPlanLine.requirement),
            joinedload(BuyPlanLine.offer),
        )
        .all()
    )

    # Sort: kicked-back rows first, then oldest plan first
    lines.sort(key=lambda ln: (ln.po_rejection_note is None, ln.buy_plan.created_at))

    rows = []
    for ln in lines:
        plan = ln.buy_plan
        customer_name = _customer_name(plan)

        req = ln.requirement
        offer = ln.offer

        rows.append(
            {
                "line_id": ln.id,
                "plan_id": plan.id,
                "customer_name": customer_name,
                "mpn": req.primary_mpn if req else None,
                "description": req.description if req else None,
                "vendor_name": offer.vendor_name if offer else None,
                # Offer has no direct contact-email column; None until enriched
                "vendor_contact_email": None,
                "quantity": ln.quantity,
                "unit_cost": ln.unit_cost,
                "status": ln.status,
                "kicked_back": ln.po_rejection_note is not None,
                "po_rejection_note": ln.po_rejection_note,
                "plan_created_at": plan.created_at,
            }
        )

    return rows


def team_line_queue(db: Session, user: object) -> list[dict]:
    """Return one dict per open buy-plan line assigned to OTHER buyers.

    Read-only awareness model for the buyer "My Orders" lens: it lets a buyer
    see what their teammates are working on. The requesting buyer's own lines are
    excluded (they live in :func:`buyer_line_queue`).

    A line qualifies when:
    - parent ``BuyPlan.status == ACTIVE``
    - ``BuyPlanLine.status`` is ``AWAITING_PO`` or ``PENDING_VERIFY``
    - ``BuyPlanLine.buyer_id IS NOT NULL`` and ``buyer_id != user.id``

    Rows are ordered by ``buyer_name`` then ``plan.created_at`` ascending so the
    template can group consecutive rows under each buyer.

    Each dict contains:
        line_id, plan_id, customer_name, mpn, vendor_name, quantity, status,
        kicked_back, buyer_name, plan_created_at
    """
    lines = (
        db.query(BuyPlanLine)
        .join(BuyPlan, BuyPlanLine.buy_plan_id == BuyPlan.id)
        .filter(
            BuyPlan.status == BuyPlanStatus.ACTIVE,
            BuyPlanLine.status.in_((BuyPlanLineStatus.AWAITING_PO, BuyPlanLineStatus.PENDING_VERIFY)),
            BuyPlanLine.buyer_id.isnot(None),
            BuyPlanLine.buyer_id != user.id,
        )
        .options(
            joinedload(BuyPlanLine.buy_plan).joinedload(BuyPlan.quote),
            joinedload(BuyPlanLine.requirement),
            joinedload(BuyPlanLine.offer),
            joinedload(BuyPlanLine.buyer),
        )
        .all()
    )

    rows = []
    for ln in lines:
        plan = ln.buy_plan
        req = ln.requirement
        offer = ln.offer
        buyer = ln.buyer

        rows.append(
            {
                "line_id": ln.id,
                "plan_id": plan.id,
                "customer_name": _customer_name(plan),
                "mpn": req.primary_mpn if req else None,
                "vendor_name": offer.vendor_name if offer else None,
                "quantity": ln.quantity,
                "status": ln.status,
                "kicked_back": ln.po_rejection_note is not None,
                "buyer_name": (buyer.name or buyer.email) if buyer else None,
                "plan_created_at": plan.created_at,
            }
        )

    # Group consecutive rows by buyer, oldest plan first within each buyer.
    rows.sort(key=lambda r: (r["buyer_name"] or "", r["plan_created_at"]))
    return rows


# ── Column mapping ────────────────────────────────────────────────────

_STATUS_TO_COLUMN: dict[str, str] = {
    BuyPlanStatus.DRAFT: "draft",
    BuyPlanStatus.PENDING: "pending",
    BuyPlanStatus.ACTIVE: "active",
    BuyPlanStatus.HALTED: "active",
    BuyPlanStatus.COMPLETED: "done",
    # CANCELLED → omitted (no entry)
}

_STAGE_LABELS: dict[str, str] = {
    BuyPlanStatus.DRAFT: "Draft",
    BuyPlanStatus.PENDING: "Pending Approval",
    BuyPlanStatus.ACTIVE: "Active",
    BuyPlanStatus.HALTED: "Halted",
    BuyPlanStatus.COMPLETED: "Completed",
}


def _compute_blocker(plan: BuyPlan) -> str:
    """Return the single highest-priority hold-up string for a plan.

    Priority order (first match wins):
    1. PENDING → "awaiting approval"
    2. ACTIVE + so_status PENDING → "SO needs verification"
    3. ACTIVE + N AWAITING_PO lines → "N POs to cut"
    4. ACTIVE + N PENDING_VERIFY lines → "N POs verifying"
    5. ACTIVE + all non-cancelled lines VERIFIED + so_status APPROVED → "ready to fulfill"
    6. DRAFT + so_status REJECTED or approval_notes suggesting rejection → "rejected — resubmit"
    7. DRAFT fresh → "ready to submit"
    8. HALTED → "halted"
    """
    status = plan.status
    so_status = plan.so_status

    if status == BuyPlanStatus.PENDING:
        return "awaiting approval"

    if status == BuyPlanStatus.HALTED:
        return "halted"

    if status == BuyPlanStatus.ACTIVE:
        if so_status == SOVerificationStatus.PENDING:
            return "SO needs verification"

        active_lines = [ln for ln in plan.lines if ln.status != BuyPlanLineStatus.CANCELLED]
        awaiting = sum(1 for ln in active_lines if ln.status == BuyPlanLineStatus.AWAITING_PO)
        verifying = sum(1 for ln in active_lines if ln.status == BuyPlanLineStatus.PENDING_VERIFY)

        if awaiting:
            return f"{awaiting} POs to cut"
        if verifying:
            return f"{verifying} POs verifying"
        # All non-cancelled lines are VERIFIED (or ISSUE, treated as done here)
        if so_status == SOVerificationStatus.APPROVED:
            return "ready to fulfill"
        return "SO needs verification"

    if status == BuyPlanStatus.DRAFT:
        rejected = so_status == SOVerificationStatus.REJECTED or bool(
            plan.approval_notes and "reject" in plan.approval_notes.lower()
        )
        if rejected:
            return "rejected — resubmit"
        return "ready to submit"

    # COMPLETED / CANCELLED — no active blocker
    return ""


def deals_board(db: Session, user: object, *, scope: str) -> dict[str, list[dict]]:
    """Return the stage-grouped deal board for sales or manager views.

    Parameters
    ----------
    db:
        Active SQLAlchemy session.
    user:
        The requesting user (``user.id`` used for scope filtering and
        ``needs_my_action`` computation).
    scope:
        ``"mine"``  — only plans where ``submitted_by_id == user.id``.
        ``"all"``   — all plans regardless of owner.

    Returns
    -------
    dict with keys ``"draft"``, ``"pending"``, ``"active"``, ``"done"``.
    Each value is a list of deal dicts ordered newest-first (by ``created_at``
    descending).  CANCELLED plans are omitted entirely.

    Each deal dict contains:
        plan_id, customer_name, value, margin_pct, stage_label, blocker,
        po_progress (cut: int, total: int), needs_my_action: bool, is_stock_sale
    """
    query = (
        db.query(BuyPlan)
        .filter(BuyPlan.status != BuyPlanStatus.CANCELLED)
        .options(
            # Eager-load quote + lines; customer_site/company lazy-loaded within session
            joinedload(BuyPlan.quote),
            joinedload(BuyPlan.lines),
        )
        .order_by(BuyPlan.created_at.desc())
    )

    if scope == "mine":
        query = query.filter(BuyPlan.submitted_by_id == user.id)

    plans = query.all()

    board: dict[str, list[dict]] = {"draft": [], "pending": [], "active": [], "done": []}

    for plan in plans:
        column = _STATUS_TO_COLUMN.get(plan.status)
        if column is None:
            # Safety net — CANCELLED already filtered above; unknown status skipped
            continue

        customer_name = _customer_name(plan)

        # po_progress: (verified_count, total_non_cancelled_count)
        active_lines = [ln for ln in plan.lines if ln.status != BuyPlanLineStatus.CANCELLED]
        verified_count = sum(1 for ln in active_lines if ln.status == BuyPlanLineStatus.VERIFIED)
        po_progress = (verified_count, len(active_lines))

        # needs_my_action: sales owner must act when plan is DRAFT
        needs_my_action = plan.status == BuyPlanStatus.DRAFT and plan.submitted_by_id == user.id

        board[column].append(
            {
                "plan_id": plan.id,
                "customer_name": customer_name,
                "value": plan.total_cost,
                "margin_pct": plan.total_margin_pct,
                "stage_label": _STAGE_LABELS.get(plan.status, plan.status),
                "blocker": _compute_blocker(plan),
                "po_progress": po_progress,
                "needs_my_action": needs_my_action,
                "is_stock_sale": plan.is_stock_sale,
            }
        )

    return board


# ── Supervise overview ────────────────────────────────────────────────

#: Non-terminal plan statuses that contribute to the "open value" strip metric.
_OPEN_STATUSES: frozenset[str] = frozenset(
    {
        BuyPlanStatus.DRAFT,
        BuyPlanStatus.PENDING,
        BuyPlanStatus.ACTIVE,
        BuyPlanStatus.HALTED,
    }
)


def supervise_overview(db: Session) -> dict:
    """Return the manager metric strip and needs-attention triage for the supervise
    lens.

    Strip metrics
    -------------
    open_value       : sum of total_cost over non-terminal plans (DRAFT/PENDING/ACTIVE/HALTED).
                       NULL total_cost rows contribute 0.
    avg_margin       : average of total_margin_pct over those same plans.
                       Plans with NULL total_margin_pct are excluded from the average;
                       returns 0.0 when no data.
    approval_count   : plans with status==PENDING and approved_by_id IS NULL.
    halted_count     : plans with status==HALTED.
    overdue_po_count : AWAITING_PO lines on ACTIVE plans where the plan has
                       approved_at set AND coalesce(last_nudge_at, approved_at)
                       is older than the buyer nudge SLA
                       (``settings.buyplan_nudge_buyer_hours``).
                       Mirrors the predicate used by the buyer-nudge job in
                       inventory_jobs.py. SLA is read at call time so test
                       overrides take effect.
    flagged_count    : lines with status==ISSUE.

    Triage lists
    ------------
    approvals        : list of plan dicts for plans awaiting approval.
    so_pending       : list of plan dicts for ACTIVE plans with so_status PENDING.
    halted           : list of plan dicts for halted plans.
    overdue_pos      : list of line dicts for overdue AWAITING_PO lines.
    po_pending_verify: list of line dicts for PENDING_VERIFY lines.
    flagged          : list of line dicts for ISSUE lines.

    Plan dicts contain: plan_id, customer_name, value, submitted_by_name.
    Line dicts contain: line_id, plan_id, mpn, vendor_name, buyer_name,
                        plus issue_type for flagged items.
    """
    nudge_threshold = datetime.now(timezone.utc) - timedelta(hours=settings.buyplan_nudge_buyer_hours)

    # ── Strip aggregates (single query) ──────────────────────────────
    agg = (
        db.query(
            func.coalesce(func.sum(BuyPlan.total_cost), 0).label("open_value"),
            func.coalesce(func.avg(BuyPlan.total_margin_pct), 0).label("avg_margin"),
        )
        .filter(BuyPlan.status.in_(_OPEN_STATUSES))
        .one()
    )
    open_value = float(agg.open_value)
    avg_margin = float(agg.avg_margin)

    # ── Approval queue ───────────────────────────────────────────────
    approval_plans = (
        db.query(BuyPlan)
        .filter(
            BuyPlan.status == BuyPlanStatus.PENDING,
            BuyPlan.approved_by_id.is_(None),
        )
        .options(joinedload(BuyPlan.quote), joinedload(BuyPlan.submitted_by))
        .order_by(BuyPlan.created_at.asc())
        .all()
    )

    # ── ACTIVE plans needing SO verification ─────────────────────────
    so_pending_plans = (
        db.query(BuyPlan)
        .filter(
            BuyPlan.status == BuyPlanStatus.ACTIVE,
            BuyPlan.so_status == SOVerificationStatus.PENDING,
        )
        .options(joinedload(BuyPlan.quote), joinedload(BuyPlan.submitted_by))
        .order_by(BuyPlan.created_at.asc())
        .all()
    )

    # ── Halted plans ─────────────────────────────────────────────────
    halted_plans = (
        db.query(BuyPlan)
        .filter(BuyPlan.status == BuyPlanStatus.HALTED)
        .options(joinedload(BuyPlan.quote), joinedload(BuyPlan.submitted_by))
        .order_by(BuyPlan.created_at.asc())
        .all()
    )

    # ── Overdue AWAITING_PO lines on ACTIVE plans ────────────────────
    # Mirrors the buyer-nudge predicate in inventory_jobs.py:
    #   plan must be ACTIVE + approved_at set (approval is the start clock);
    #   coalesce(last_nudge_at, approved_at) < threshold (line has not been
    #   recently nudged, falling back to the plan approval timestamp).
    overdue_lines = (
        db.query(BuyPlanLine)
        .join(BuyPlan, BuyPlanLine.buy_plan_id == BuyPlan.id)
        .filter(
            BuyPlanLine.status == BuyPlanLineStatus.AWAITING_PO,
            BuyPlan.status == BuyPlanStatus.ACTIVE,
            BuyPlan.approved_at.isnot(None),
            func.coalesce(BuyPlanLine.last_nudge_at, BuyPlan.approved_at) < nudge_threshold,
        )
        .options(
            joinedload(BuyPlanLine.buy_plan).joinedload(BuyPlan.quote),
            joinedload(BuyPlanLine.offer),
            joinedload(BuyPlanLine.buyer),
        )
        .order_by(BuyPlanLine.created_at.asc())
        .all()
    )

    # ── PENDING_VERIFY lines awaiting ops PO verification ────────────
    po_pending_verify_lines = (
        db.query(BuyPlanLine)
        .filter(BuyPlanLine.status == BuyPlanLineStatus.PENDING_VERIFY)
        .options(
            joinedload(BuyPlanLine.buy_plan).joinedload(BuyPlan.quote),
            joinedload(BuyPlanLine.offer),
            joinedload(BuyPlanLine.buyer),
        )
        .order_by(BuyPlanLine.created_at.asc())
        .all()
    )

    # ── Flagged (ISSUE) lines ────────────────────────────────────────
    flagged_lines = (
        db.query(BuyPlanLine)
        .filter(BuyPlanLine.status == BuyPlanLineStatus.ISSUE)
        .options(
            joinedload(BuyPlanLine.buy_plan).joinedload(BuyPlan.quote),
            joinedload(BuyPlanLine.offer),
            joinedload(BuyPlanLine.buyer),
        )
        .order_by(BuyPlanLine.created_at.asc())
        .all()
    )

    # ── Helpers ──────────────────────────────────────────────────────
    def _submitted_by_name(plan: BuyPlan) -> str | None:
        sub = plan.submitted_by
        if sub:
            return sub.name or sub.email
        return None

    def _plan_dict(plan: BuyPlan) -> dict:
        return {
            "plan_id": plan.id,
            "customer_name": _customer_name(plan),
            "value": plan.total_cost,
            "submitted_by_name": _submitted_by_name(plan),
        }

    def _line_dict(ln: BuyPlanLine, *, include_issue_type: bool = False) -> dict:
        offer = ln.offer
        buyer = ln.buyer
        row = {
            "line_id": ln.id,
            "plan_id": ln.buy_plan_id,
            "mpn": offer.mpn if offer else None,
            "vendor_name": offer.vendor_name if offer else None,
            "buyer_name": (buyer.name or buyer.email) if buyer else None,
        }
        if include_issue_type:
            row["issue_type"] = ln.issue_type
        return row

    return {
        "strip": {
            "open_value": open_value,
            "avg_margin": avg_margin,
            "approval_count": len(approval_plans),
            "so_pending_count": len(so_pending_plans),
            "halted_count": len(halted_plans),
            "overdue_po_count": len(overdue_lines),
            "po_pending_verify_count": len(po_pending_verify_lines),
            "flagged_count": len(flagged_lines),
        },
        "triage": {
            "approvals": [_plan_dict(p) for p in approval_plans],
            "so_pending": [_plan_dict(p) for p in so_pending_plans],
            "halted": [_plan_dict(p) for p in halted_plans],
            "overdue_pos": [_line_dict(ln) for ln in overdue_lines],
            "po_pending_verify": [_line_dict(ln) for ln in po_pending_verify_lines],
            "flagged": [_line_dict(ln, include_issue_type=True) for ln in flagged_lines],
        },
    }
