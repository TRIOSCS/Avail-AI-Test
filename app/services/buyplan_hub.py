"""buyplan_hub.py — Buy Plan Deal Hub read models.

Purpose: Role-aware read models for the Buy Plan Deal Hub page.
         Provides the buyer's per-line PO queue (lines the buyer must cut a PO for)
         across all their active deals, plus the stage-grouped deal board for sales
         and manager views.

Called by: routers/htmx_views.py (buy plan hub partials)
Depends on: models.buy_plan (BuyPlan, BuyPlanLine), models.auth (User),
            constants (BuyPlanStatus, BuyPlanLineStatus, SOVerificationStatus)
"""

from __future__ import annotations

from sqlalchemy.orm import Session, joinedload

from ..constants import BuyPlanLineStatus, BuyPlanStatus, SOVerificationStatus
from ..models.buy_plan import BuyPlan, BuyPlanLine


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

        # Derive customer_name: plan.quote → customer_site → company.name, with site_name fallback
        # Mirrors buy_plans_list_partial in routers/htmx_views.py
        customer_name = None
        if plan.quote and plan.quote.customer_site:
            site = plan.quote.customer_site
            co = site.company if hasattr(site, "company") else None
            customer_name = co.name if co else getattr(site, "site_name", None)

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

        # Derive customer_name: plan.quote → customer_site → company.name
        customer_name = None
        if plan.quote and plan.quote.customer_site:
            site = plan.quote.customer_site
            co = site.company if hasattr(site, "company") else None
            customer_name = co.name if co else getattr(site, "site_name", None)

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
