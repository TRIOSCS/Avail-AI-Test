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
from ..models.crm import CustomerSite
from ..models.quotes import Quote
from ..models.sourcing import Requisition
from .buyplan_naming import (
    CARD_KIND_BUY_PLAN,
    CARD_KIND_PO,
    CARD_KIND_SALES_ORDER,
    build_card_title,
)


def _user_name(user: object | None) -> str | None:
    """Display name for a User (``name`` preferred, ``email`` fallback), or ``None``.

    The single owner-name derivation reused by every Deal Hub read model so the
    Account Manager (``submitted_by``) and the Buyer (``line.buyer``) format
    identically wherever they appear in a card title.
    """
    if user is None:
        return None
    name: str | None = getattr(user, "name", None) or getattr(user, "email", None)
    return name


def _issue_reason(line: BuyPlanLine) -> str:
    """Human-readable reason a buyer flagged this line, for at-a-glance triage.

    Buyers raise a line issue via ``buyplan_workflow.flag_line_issue`` which records
    an ``issue_type`` code (``sold_out`` / ``price_changed`` / ``lead_time_changed``
    / ``other``) plus an optional free-text ``issue_note``. The note is the most
    specific signal, so it wins; otherwise the type code is humanised
    (``lead_time_changed`` → ``Lead time changed``). Falls back to ``"Issue"`` when
    neither is set (legacy rows).
    """
    note = (line.issue_note or "").strip()
    if note:
        return note
    code = (line.issue_type or "").strip()
    if code:
        return code.replace("_", " ").capitalize()
    return "Issue"


def _customer_name(plan: BuyPlan) -> str | None:
    """Derive a display customer name from quote or requisition.

    Quote path (preferred): plan.quote → customer_site → company.name.
    Requisition fallback (SO-origin plans with no quote): req.customer_name,
    then req.customer_site → company.name.
    Returns ``None`` when neither source has a customer name.
    """
    if plan.quote and plan.quote.customer_site and plan.quote.customer_site.company:
        return plan.quote.customer_site.company.name
    req = plan.requisition
    if req:
        if req.customer_name:
            return req.customer_name
        if req.customer_site and req.customer_site.company:
            return req.customer_site.company.name
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
            joinedload(BuyPlanLine.buy_plan)
            .joinedload(BuyPlan.quote)
            .joinedload(Quote.customer_site)
            .joinedload(CustomerSite.company),
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
            joinedload(BuyPlanLine.buy_plan)
            .joinedload(BuyPlan.quote)
            .joinedload(Quote.customer_site)
            .joinedload(CustomerSite.company),
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


def resourcing_pool_queue(db: Session) -> list[dict]:
    """Every open-pool line awaiting a claim, pool-wide (not per-buyer).

    A line qualifies when it is ``RESOURCING``, ``buyer_id IS NULL``, and its parent
    plan is ``ACTIVE`` — i.e. a cut PO was cancelled (vendor fell down) and any buyer can
    backfill it. Each row carries the canceled-vendor + reason context (from the most
    recent POCancellation for that line) so the queue reads as a triage list. Oldest plan
    first.

    Each dict: line_id, plan_id, customer_name, mpn, description, quantity,
               canceled_vendor, reason_code, cancelled_at.
    """
    from ..models.po_cancellation import POCancellation

    lines = (
        db.query(BuyPlanLine)
        .join(BuyPlan, BuyPlanLine.buy_plan_id == BuyPlan.id)
        .filter(
            BuyPlanLine.status == BuyPlanLineStatus.RESOURCING,
            BuyPlanLine.buyer_id.is_(None),
            BuyPlan.status == BuyPlanStatus.ACTIVE,
        )
        .options(
            joinedload(BuyPlanLine.buy_plan)
            .joinedload(BuyPlan.quote)
            .joinedload(Quote.customer_site)
            .joinedload(CustomerSite.company),
            joinedload(BuyPlanLine.requirement),
        )
        .all()
    )
    if not lines:
        return []

    # Latest cancellation per line (the one that just sent it to the pool).
    cancels = (
        db.query(POCancellation)
        .filter(POCancellation.buy_plan_line_id.in_([ln.id for ln in lines]))
        .options(joinedload(POCancellation.vendor_card))
        .order_by(POCancellation.cancelled_at.desc())
        .all()
    )
    latest_cancel: dict[int, POCancellation] = {}
    for c in cancels:
        latest_cancel.setdefault(c.buy_plan_line_id, c)

    lines.sort(key=lambda ln: ln.buy_plan.created_at)

    rows = []
    for ln in lines:
        req = ln.requirement
        cancel = latest_cancel.get(ln.id)
        canceled_vendor = None
        if cancel:
            canceled_vendor = (
                cancel.vendor_card.display_name if cancel.vendor_card else None
            ) or cancel.vendor_name_normalized
        rows.append(
            {
                "line_id": ln.id,
                "plan_id": ln.buy_plan_id,
                "customer_name": _customer_name(ln.buy_plan),
                "mpn": req.primary_mpn if req else None,
                "description": req.description if req else None,
                "quantity": ln.quantity,
                "canceled_vendor": canceled_vendor,
                "reason_code": cancel.reason_code if cancel else None,
                "cancelled_at": cancel.cancelled_at if cancel else None,
            }
        )
    return rows


# ── Column mapping ────────────────────────────────────────────────────

_STATUS_TO_COLUMN: dict[str, str] = {
    BuyPlanStatus.DRAFT: "draft",
    BuyPlanStatus.PENDING: "pending",
    BuyPlanStatus.ACTIVE: "active",
    BuyPlanStatus.HALTED: "active",
    # COMPLETED → archive section (see completed_archive), not an active column
    # CANCELLED → omitted (no entry)
}

#: Default page size for the completed-transactions archive ("load older" chunk).
ARCHIVE_PAGE_SIZE = 20

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
        # A DRAFT that carries an approval decision timestamp was sent back by an approver
        # (approve_buy_plan stamps approved_at on reject) → distinguish it from a fresh draft.
        rejected = (
            plan.approved_at is not None
            or so_status == SOVerificationStatus.REJECTED
            or bool(plan.approval_notes and "reject" in plan.approval_notes.lower())
        )
        if rejected:
            return "rejected — resubmit"
        return "ready to submit"

    # COMPLETED / CANCELLED — no active blocker
    return ""


def _primary_mpn(plan: BuyPlan) -> str | None:
    """The plan's headline part number for at-a-glance scanning.

    Returns the first non-cancelled line's requirement MPN (lines are id-ordered),
    i.e. the part the deal leads with. ``None`` when no line carries a requirement
    MPN (e.g. a fresh plan with unlinked lines).
    """
    for ln in plan.lines:
        if ln.status == BuyPlanLineStatus.CANCELLED:
            continue
        req = ln.requirement
        if req and req.primary_mpn:
            return req.primary_mpn
    return None


def _our_po_numbers(plan: BuyPlan) -> list[str]:
    """Distinct purchase-order numbers we've cut on this plan's lines, in line order.

    These are *our* POs to vendors (``BuyPlanLine.po_number``) — distinct from
    ``BuyPlan.customer_po_number`` (the customer's PO to us). Cancelled lines are
    skipped. Order is preserved (id-ordered lines) and duplicates collapse, so a
    reviewer sees exactly which POs the deal involves.
    """
    seen: list[str] = []
    for ln in plan.lines:
        if ln.status == BuyPlanLineStatus.CANCELLED:
            continue
        po = (ln.po_number or "").strip()
        if po and po not in seen:
            seen.append(po)
    return seen


def _deal_card(plan: BuyPlan, user: object) -> dict:
    """Build the shared deal-card read dict for one plan.

    Used by both the active board (``deals_board``) and the completed archive
    (``completed_archive``) so a card looks identical wherever it renders. The
    ``completed_at`` field is meaningful only for archived (COMPLETED) plans.

    ``card_title`` is the canonical ``{SO#} - {Customer} - {Owner} - BP`` title
    (Owner = the Account Manager / sales owner) built by the shared
    :func:`buyplan_naming.build_card_title` helper. ``tso``, ``po_numbers`` and
    ``primary_mpn`` give the denser tile the sales-order #, the POs involved and
    the headline part without opening the deal.
    """
    # po_progress: (verified_count, total_non_cancelled_count)
    active_lines = [ln for ln in plan.lines if ln.status != BuyPlanLineStatus.CANCELLED]
    verified_count = sum(1 for ln in active_lines if ln.status == BuyPlanLineStatus.VERIFIED)

    # needs_my_action: sales owner must act when plan is DRAFT
    needs_my_action = plan.status == BuyPlanStatus.DRAFT and plan.submitted_by_id == user.id

    customer_name = _customer_name(plan)
    # Owner on a Buy-Plan card is the Account Manager (sales owner = submitted_by).
    owner_name = _user_name(plan.submitted_by)

    return {
        "plan_id": plan.id,
        "card_title": build_card_title(
            sales_order_number=plan.sales_order_number,
            customer_name=customer_name,
            owner_name=owner_name,
            kind=CARD_KIND_BUY_PLAN,
        ),
        "customer_name": customer_name,
        "owner_name": owner_name,
        "tso": plan.sales_order_number,
        "po_numbers": _our_po_numbers(plan),
        "primary_mpn": _primary_mpn(plan),
        "value": plan.total_cost,
        "margin_pct": plan.total_margin_pct,
        "stage_label": _STAGE_LABELS.get(plan.status, plan.status),
        "blocker": _compute_blocker(plan),
        "po_progress": (verified_count, len(active_lines)),
        "needs_my_action": needs_my_action,
        "is_stock_sale": plan.is_stock_sale,
        "completed_at": plan.completed_at,
    }


def deals_board(
    db: Session,
    user: object,
    *,
    scope: str,
    statuses: list[str] | None = None,
) -> dict[str, list[dict]]:
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
    statuses:
        When provided, filter to only plans whose status is in this list.
        When ``None`` (default), the original behaviour applies: CANCELLED and
        COMPLETED are excluded so only in-progress work appears.  Pass explicit
        status values (``BuyPlanStatus.ACTIVE.value``, etc.) to show a subset of
        the lifecycle — e.g. the Buy Plans tab passes ``[ACTIVE, HALTED]`` and the
        Sales Orders tab passes ``[DRAFT, PENDING]``.

    Returns
    -------
    dict with keys ``"draft"``, ``"pending"``, ``"active"``.
    Each value is a list of deal dicts ordered newest-first (by ``created_at``
    descending). COMPLETED plans are excluded by default — they live in the
    paginated archive (see ``completed_archive``); CANCELLED plans are omitted
    entirely by default.

    Each deal dict contains:
        plan_id, customer_name, value, margin_pct, stage_label, blocker,
        po_progress (cut: int, total: int), needs_my_action: bool, is_stock_sale,
        completed_at
    """
    status_filter = (
        BuyPlan.status.in_(statuses)
        if statuses is not None
        else BuyPlan.status.notin_((BuyPlanStatus.CANCELLED, BuyPlanStatus.COMPLETED))
    )
    query = (
        db.query(BuyPlan)
        .filter(status_filter)
        .options(
            # Eager-load quote → customer_site → company (eliminates N+1 per card)
            joinedload(BuyPlan.quote).joinedload(Quote.customer_site).joinedload(CustomerSite.company),
            # Eager-load requisition → customer_site → company for SO-origin cards (quote_id NULL)
            joinedload(BuyPlan.requisition).joinedload(Requisition.customer_site).joinedload(CustomerSite.company),
            joinedload(BuyPlan.submitted_by),
            # lines + their requirement feed _primary_mpn / _our_po_numbers on the card
            joinedload(BuyPlan.lines).joinedload(BuyPlanLine.requirement),
        )
        .order_by(BuyPlan.created_at.desc())
    )

    if scope == "mine":
        query = query.filter(BuyPlan.submitted_by_id == user.id)

    plans = query.all()

    board: dict[str, list[dict]] = {"draft": [], "pending": [], "active": []}

    for plan in plans:
        column = _STATUS_TO_COLUMN.get(plan.status)
        if column is None:
            # Safety net — CANCELLED/COMPLETED already filtered above; unknown status skipped
            continue
        board[column].append(_deal_card(plan, user))

    return board


def completed_archive(
    db: Session,
    user: object,
    *,
    scope: str,
    limit: int = ARCHIVE_PAGE_SIZE,
    offset: int = 0,
) -> dict:
    """Return one page of COMPLETED (archived) deals, newest-completed first.

    The active board (``deals_board``) shows only in-progress work; completed
    transactions move here so the daily 10–15 don't pile up. Ordered by
    ``completed_at`` descending (NULLS LAST, then ``created_at`` desc as a
    deterministic tiebreak for any legacy row whose ``completed_at`` was never
    stamped). Paginated via ``limit``/``offset`` for the "load older" lazy chunk.

    Parameters
    ----------
    scope:
        ``"mine"`` — only plans where ``submitted_by_id == user.id``.
        ``"all"``  — all completed plans regardless of owner (manager/ops view).
    limit, offset:
        Standard page window. ``limit`` is clamped to ``[1, 100]``.

    Returns
    -------
    dict with keys:
        ``deals``       — list of deal dicts (same shape as ``deals_board`` cards,
                          with ``completed_at`` populated),
        ``total``       — total COMPLETED plans in scope,
        ``limit``       — the clamped page size used,
        ``offset``      — the offset used,
        ``next_offset`` — offset for the next page, or ``None`` when exhausted.
    """
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))

    base = db.query(BuyPlan).filter(BuyPlan.status == BuyPlanStatus.COMPLETED)
    if scope == "mine":
        base = base.filter(BuyPlan.submitted_by_id == user.id)

    total = base.with_entities(func.count(BuyPlan.id)).scalar() or 0

    plans = (
        base.options(
            # Eager-load quote → customer_site → company (eliminates N+1 per card)
            joinedload(BuyPlan.quote).joinedload(Quote.customer_site).joinedload(CustomerSite.company),
            joinedload(BuyPlan.submitted_by),
            joinedload(BuyPlan.lines).joinedload(BuyPlanLine.requirement),
        )
        .order_by(BuyPlan.completed_at.desc().nullslast(), BuyPlan.created_at.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )

    deals = [_deal_card(plan, user) for plan in plans]
    next_offset = offset + limit if offset + limit < total else None

    return {
        "deals": deals,
        "total": total,
        "limit": limit,
        "offset": offset,
        "next_offset": next_offset,
    }


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
        .options(
            joinedload(BuyPlan.quote).joinedload(Quote.customer_site).joinedload(CustomerSite.company),
            joinedload(BuyPlan.submitted_by),
        )
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
        .options(
            joinedload(BuyPlan.quote).joinedload(Quote.customer_site).joinedload(CustomerSite.company),
            joinedload(BuyPlan.submitted_by),
        )
        .order_by(BuyPlan.created_at.asc())
        .all()
    )

    # ── Halted plans ─────────────────────────────────────────────────
    halted_plans = (
        db.query(BuyPlan)
        .filter(BuyPlan.status == BuyPlanStatus.HALTED)
        .options(
            joinedload(BuyPlan.quote).joinedload(Quote.customer_site).joinedload(CustomerSite.company),
            joinedload(BuyPlan.submitted_by),
        )
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
            joinedload(BuyPlanLine.buy_plan)
            .joinedload(BuyPlan.quote)
            .joinedload(Quote.customer_site)
            .joinedload(CustomerSite.company),
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
            joinedload(BuyPlanLine.buy_plan)
            .joinedload(BuyPlan.quote)
            .joinedload(Quote.customer_site)
            .joinedload(CustomerSite.company),
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
            joinedload(BuyPlanLine.buy_plan)
            .joinedload(BuyPlan.quote)
            .joinedload(Quote.customer_site)
            .joinedload(CustomerSite.company),
            joinedload(BuyPlanLine.offer),
            joinedload(BuyPlanLine.buyer),
        )
        .order_by(BuyPlanLine.created_at.asc())
        .all()
    )

    # ── Helpers ──────────────────────────────────────────────────────
    def _plan_dict(plan: BuyPlan, *, kind: str | None = None) -> dict:
        """Plan triage row.

        With ``kind`` set (e.g. "SO"), include the canonical
        ``{SO#} - {Customer} - {Owner} - {kind}`` title; Owner is the Account Manager.
        """
        customer_name = _customer_name(plan)
        owner_name = _user_name(plan.submitted_by)
        row = {
            "plan_id": plan.id,
            "customer_name": customer_name,
            "value": plan.total_cost,
            "submitted_by_name": owner_name,
        }
        if kind is not None:
            row["card_title"] = build_card_title(
                sales_order_number=plan.sales_order_number,
                customer_name=customer_name,
                owner_name=owner_name,
                kind=kind,
            )
        return row

    def _line_dict(ln: BuyPlanLine, *, include_issue_type: bool = False, kind: str | None = None) -> dict:
        offer = ln.offer
        buyer = ln.buyer
        plan = ln.buy_plan
        row = {
            "line_id": ln.id,
            "plan_id": ln.buy_plan_id,
            "mpn": offer.mpn if offer else None,
            "vendor_name": offer.vendor_name if offer else None,
            "buyer_name": _user_name(buyer),
        }
        if include_issue_type:
            # Human-readable issue reason: the buyer's free-text note when present,
            # else the issue-type label (underscores → spaces). Falls back to "issue".
            row["issue_type"] = ln.issue_type
            row["issue_reason"] = _issue_reason(ln)
        if kind is not None:
            # PO approval row: Owner is the Buyer (per-line procurement owner).
            row["card_title"] = build_card_title(
                sales_order_number=plan.sales_order_number if plan else None,
                customer_name=_customer_name(plan) if plan else None,
                owner_name=_user_name(buyer),
                kind=kind,
            )
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
            # SO-approval rows carry the "{SO#} - {Customer} - {AcctMgr} - SO" title.
            "so_pending": [_plan_dict(p, kind=CARD_KIND_SALES_ORDER) for p in so_pending_plans],
            "halted": [_plan_dict(p) for p in halted_plans],
            "overdue_pos": [_line_dict(ln) for ln in overdue_lines],
            # PO-approval rows carry the "{SO#} - {Customer} - {Buyer} - PO" title.
            "po_pending_verify": [_line_dict(ln, kind=CARD_KIND_PO) for ln in po_pending_verify_lines],
            "flagged": [_line_dict(ln, include_issue_type=True) for ln in flagged_lines],
        },
    }
