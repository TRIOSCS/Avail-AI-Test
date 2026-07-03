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

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload, selectinload

from ..config import settings
from ..constants import (
    ApprovalGateType,
    BuyPlanLineStatus,
    BuyPlanStatus,
    SOVerificationStatus,
    UserRole,
)
from ..dependencies import can_approve_buy_plans
from ..models.approvals import ApprovalRequest
from ..models.buy_plan import BuyPlan, BuyPlanLine, VerificationGroupMember
from ..models.crm import CustomerSite
from ..models.quality_plan import Prepayment
from ..models.quotes import Quote
from ..models.sourcing import Requisition
from .approvals.queue import _actionable_request_ids
from .buyplan_naming import (
    CARD_KIND_BUY_PLAN,
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
        # Raw lifecycle status — the Pipeline card maps it to a 4-stage index for the
        # "who-has-the-ball" pip stepper (DRAFT→Build, PENDING→Approve,
        # ACTIVE→Purchase, COMPLETED→Done). stage_label keeps the legacy vocabulary.
        "status": plan.status,
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


def open_avg_margin(db: Session) -> float:
    """Average ``total_margin_pct`` across all open (non-terminal) plans; ``0.0`` if
    none.

    The single cheap aggregate behind the "avg margin" figure shown on BOTH the My Queue
    header and the Pipeline metric strip. Mirrors the avg_margin aggregation in
    :func:`supervise_overview` (same ``_OPEN_STATUSES`` set; NULL margins excluded by AVG,
    coalesced to 0) but as a standalone query so the two lighter surfaces don't pay for the
    full supervise overview build.
    """
    avg = (
        db.query(func.coalesce(func.avg(BuyPlan.total_margin_pct), 0))
        .filter(BuyPlan.status.in_(_OPEN_STATUSES))
        .scalar()
    )
    return float(avg or 0.0)


#: Risk-first priority tier for each *supervise-lens* action-queue row kind
#: (1 = highest risk, surfaced first). The supervise lens sorts its unified queue by
#: ``(priority, waiting_since)`` so the riskiest, oldest items lead. Distinct from the
#: role-aware ``_QUEUE_PRIORITY`` used by :func:`my_queue` (different kind vocabulary).
_SUPERVISE_QUEUE_PRIORITY: dict[str, int] = {
    "halted": 1,
    "flagged": 2,
    "overdue": 3,
    "approve": 4,
    "verify_po": 5,
}

#: Human-readable pill label for each supervise-lens action-queue row kind.
_SUPERVISE_QUEUE_LABEL: dict[str, str] = {
    "halted": "Halted",
    "flagged": "Flagged",
    "overdue": "Overdue PO",
    "approve": "Approve",
    "verify_po": "Verify PO",
}


# ── Shared SLA rule (single source of truth for the overdue-PO clock) ──────────────
# Both the SQL-side overdue filter (_query_overdue_lines, org-wide) and the Python-side
# per-row is_overdue flag (_line_overdue, used by my_queue's cut_po split) key off the
# SAME buyer-nudge SLA so the two surfaces never disagree on what "overdue" means.


def _nudge_cutoff() -> datetime:
    """Datetime before which an un-actioned AWAITING_PO line counts as overdue.

    Mirrors the buyer-nudge predicate in inventory_jobs.py: a line is overdue when its
    nudge clock (``coalesce(last_nudge_at, plan.approved_at)``) is older than
    ``settings.buyplan_nudge_buyer_hours``. Read at call time so test overrides apply.
    """
    return datetime.now(timezone.utc) - timedelta(hours=settings.buyplan_nudge_buyer_hours)


def _line_overdue(line: BuyPlanLine, cutoff: datetime) -> bool:
    """Python form of the overdue-PO rule, for a single eager-loaded line.

    True only for an AWAITING_PO line on an ACTIVE, approved plan whose nudge clock
    predates *cutoff*. Touches only the (already eager-loaded) parent plan — never a
    lazy relationship. Mirrors the SQL predicate in :func:`_query_overdue_lines`.
    """
    if line.status != BuyPlanLineStatus.AWAITING_PO:
        return False
    plan = line.buy_plan
    if plan is None or plan.status != BuyPlanStatus.ACTIVE or plan.approved_at is None:
        return False
    anchor = line.last_nudge_at or plan.approved_at
    return anchor < cutoff


# ── Source queries (single source of truth, shared by supervise_overview + my_queue) ──
# Each ``_query_*`` helper owns ONE source query. supervise_overview composes the org-wide
# set; my_queue composes the role-/owner-scoped set (passing buyer_id/owner_id). Keeping
# the queries here means a workflow change just stops emitting a kind from one builder.

# Eager-load chains reused below: a plan card needs the customer (quote OR requisition
# path), the AM (submitted_by) and its lines' requirements (headline MPN); a line card
# needs its parent plan's customer, the offer (MPN/vendor), the buyer and the requirement.
_PLAN_CUSTOMER_LOADS = (
    joinedload(BuyPlan.quote).joinedload(Quote.customer_site).joinedload(CustomerSite.company),
    joinedload(BuyPlan.requisition).joinedload(Requisition.customer_site).joinedload(CustomerSite.company),
)
_LINE_PLAN_LOADS = (
    joinedload(BuyPlanLine.buy_plan)
    .joinedload(BuyPlan.quote)
    .joinedload(Quote.customer_site)
    .joinedload(CustomerSite.company),
    joinedload(BuyPlanLine.buy_plan)
    .joinedload(BuyPlan.requisition)
    .joinedload(Requisition.customer_site)
    .joinedload(CustomerSite.company),
    joinedload(BuyPlanLine.offer),
    joinedload(BuyPlanLine.buyer),
    joinedload(BuyPlanLine.requirement),
)


def _query_approval_plans(db: Session) -> list[BuyPlan]:
    """PENDING plans awaiting a first approval decision (org-wide), oldest first."""
    return (
        db.query(BuyPlan)
        .filter(BuyPlan.status == BuyPlanStatus.PENDING, BuyPlan.approved_by_id.is_(None))
        .options(
            *_PLAN_CUSTOMER_LOADS,
            joinedload(BuyPlan.submitted_by),
            selectinload(BuyPlan.lines).selectinload(BuyPlanLine.requirement),
        )
        .order_by(BuyPlan.created_at.asc())
        .all()
    )


def _query_halted_plans(db: Session, *, owner_id: int | None = None) -> list[BuyPlan]:
    """HALTED plans, oldest first; ``owner_id`` scopes to a single AM (my_queue)."""
    q = db.query(BuyPlan).filter(BuyPlan.status == BuyPlanStatus.HALTED)
    if owner_id is not None:
        q = q.filter(BuyPlan.submitted_by_id == owner_id)
    return (
        q.options(
            *_PLAN_CUSTOMER_LOADS,
            joinedload(BuyPlan.submitted_by),
            selectinload(BuyPlan.lines).selectinload(BuyPlanLine.requirement),
        )
        .order_by(BuyPlan.created_at.asc())
        .all()
    )


def _query_overdue_lines(db: Session, *, buyer_id: int | None = None) -> list[BuyPlanLine]:
    """AWAITING_PO lines past the buyer-nudge SLA on ACTIVE/approved plans, oldest
    first.

    Org-wide for supervise; ``buyer_id`` scopes to one buyer for my_queue's cut_po_overdue.
    """
    cutoff = _nudge_cutoff()
    q = (
        db.query(BuyPlanLine)
        .join(BuyPlan, BuyPlanLine.buy_plan_id == BuyPlan.id)
        .filter(
            BuyPlanLine.status == BuyPlanLineStatus.AWAITING_PO,
            BuyPlan.status == BuyPlanStatus.ACTIVE,
            BuyPlan.approved_at.isnot(None),
            func.coalesce(BuyPlanLine.last_nudge_at, BuyPlan.approved_at) < cutoff,
        )
    )
    if buyer_id is not None:
        q = q.filter(BuyPlanLine.buyer_id == buyer_id)
    return q.options(*_LINE_PLAN_LOADS).order_by(BuyPlanLine.created_at.asc()).all()


def _query_po_pending_verify(db: Session) -> list[BuyPlanLine]:
    """PENDING_VERIFY lines awaiting PO verification (org-wide), oldest first."""
    return (
        db.query(BuyPlanLine)
        .filter(BuyPlanLine.status == BuyPlanLineStatus.PENDING_VERIFY)
        .options(*_LINE_PLAN_LOADS)
        .order_by(BuyPlanLine.created_at.asc())
        .all()
    )


def _query_flagged_lines(db: Session) -> list[BuyPlanLine]:
    """ISSUE (buyer-flagged) lines (org-wide), oldest first."""
    return (
        db.query(BuyPlanLine)
        .filter(BuyPlanLine.status == BuyPlanLineStatus.ISSUE)
        .options(*_LINE_PLAN_LOADS)
        .order_by(BuyPlanLine.created_at.asc())
        .all()
    )


def _query_resourcing_pool(db: Session) -> list[BuyPlanLine]:
    """Unclaimed RESOURCING-pool lines on ACTIVE plans (pool-wide), oldest first."""
    return (
        db.query(BuyPlanLine)
        .join(BuyPlan, BuyPlanLine.buy_plan_id == BuyPlan.id)
        .filter(
            BuyPlanLine.status == BuyPlanLineStatus.RESOURCING,
            BuyPlanLine.buyer_id.is_(None),
            BuyPlan.status == BuyPlanStatus.ACTIVE,
        )
        .options(*_LINE_PLAN_LOADS)
        .order_by(BuyPlanLine.created_at.asc())
        .all()
    )


def _query_owner_draft_plans(db: Session, *, owner_id: int) -> list[BuyPlan]:
    """DRAFT plans owned (submitted) by ``owner_id``, oldest first.

    Feeds both plan_draft (fresh) and plan_returned (sent back by an approver); the
    caller partitions them via :func:`_is_returned`.
    """
    return (
        db.query(BuyPlan)
        .filter(BuyPlan.status == BuyPlanStatus.DRAFT, BuyPlan.submitted_by_id == owner_id)
        .options(
            *_PLAN_CUSTOMER_LOADS,
            joinedload(BuyPlan.submitted_by),
            selectinload(BuyPlan.lines).selectinload(BuyPlanLine.requirement),
        )
        .order_by(BuyPlan.created_at.asc())
        .all()
    )


def _query_buyer_awaiting_po_lines(db: Session, *, buyer_id: int) -> list[BuyPlanLine]:
    """A buyer's AWAITING_PO lines on ACTIVE plans (overdue + not), oldest first.

    The caller splits these into cut_po_overdue / cut_po via :func:`_line_overdue` so the
    overdue clock is computed once, in the row builder, with no extra query.
    """
    return (
        db.query(BuyPlanLine)
        .join(BuyPlan, BuyPlanLine.buy_plan_id == BuyPlan.id)
        .filter(
            BuyPlanLine.status == BuyPlanLineStatus.AWAITING_PO,
            BuyPlan.status == BuyPlanStatus.ACTIVE,
            BuyPlanLine.buyer_id == buyer_id,
        )
        .options(*_LINE_PLAN_LOADS)
        .order_by(BuyPlanLine.created_at.asc())
        .all()
    )


def supervise_overview(db: Session) -> dict:
    """Return the manager metric strip and a unified action queue for the supervise
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

    Action queue
    ------------
    queue : one flat, risk-first-ordered list of uniform row dicts — every item
            that needs the supervisor, reshaped from the same five source queries
            (approvals, halted, overdue POs, PO-verify, flagged). Each row has the
            identical shape::

                kind, label, priority, plan_id, line_id, customer_name, so_number,
                mpn, vendor_name, owner_name, owner_role, value, margin_pct,
                waiting_since, issue_reason

            ``kind`` is one of ``halted``/``flagged``/``overdue``/``approve``/
            ``verify_po``. Plan kinds (approve/halted) carry
            ``owner_role="AM"`` (the Account Manager = ``submitted_by``) and a NULL
            ``line_id``/``mpn``/``vendor_name``; line kinds (overdue/verify_po/flagged)
            carry ``owner_role="Buyer"`` (``line.buyer``) plus the offer ``mpn`` /
            ``vendor_name``. ``value`` / ``margin_pct`` are always the *parent plan's*
            ``total_cost`` / ``total_margin_pct`` (uniform "deal value/margin"), and
            ``issue_reason`` is populated only on ``flagged`` rows. ``waiting_since``
            (the age + sort clock) is ``plan.created_at`` for approve/halted,
            ``coalesce(line.last_nudge_at, plan.approved_at)`` for overdue, and
            ``line.created_at`` for verify_po/flagged. The list is sorted by
            ``(priority, waiting_since)`` — risk-first, oldest-first within each tier.
    """
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

    # ── Source queries (shared single source of truth with my_queue) ──
    approval_plans = _query_approval_plans(db)
    halted_plans = _query_halted_plans(db)
    overdue_lines = _query_overdue_lines(db)
    po_pending_verify_lines = _query_po_pending_verify(db)
    flagged_lines = _query_flagged_lines(db)

    # ── Uniform row builders ─────────────────────────────────────────
    def _plan_row(plan: BuyPlan, *, kind: str, waiting_since: datetime) -> dict:
        """Map a plan-source row (approve/halted) into the uniform shape.

        Owner is the Account Manager (``submitted_by``); ``value`` / ``margin_pct`` are
        the plan's own deal totals. Line-only fields stay NULL.
        """
        return {
            "kind": kind,
            "label": _SUPERVISE_QUEUE_LABEL[kind],
            "priority": _SUPERVISE_QUEUE_PRIORITY[kind],
            "plan_id": plan.id,
            "line_id": None,
            "customer_name": _customer_name(plan),
            "so_number": plan.sales_order_number,
            "mpn": None,
            "vendor_name": None,
            "owner_name": _user_name(plan.submitted_by),
            "owner_role": "AM",
            "value": plan.total_cost,
            "margin_pct": plan.total_margin_pct,
            "waiting_since": waiting_since,
            "issue_reason": None,
        }

    def _line_row(ln: BuyPlanLine, *, kind: str, waiting_since: datetime) -> dict:
        """Map a line-source row (overdue/verify_po/flagged) into the uniform shape.

        Owner is the Buyer (``line.buyer``); ``value`` / ``margin_pct`` come from the
        *parent plan* so a line row shows the deal's value/margin uniformly.
        ``issue_reason`` is populated only for flagged lines.
        """
        offer = ln.offer
        plan = ln.buy_plan
        return {
            "kind": kind,
            "label": _SUPERVISE_QUEUE_LABEL[kind],
            "priority": _SUPERVISE_QUEUE_PRIORITY[kind],
            "plan_id": ln.buy_plan_id,
            "line_id": ln.id,
            "customer_name": _customer_name(plan) if plan else None,
            "so_number": plan.sales_order_number if plan else None,
            "mpn": offer.mpn if offer else None,
            "vendor_name": offer.vendor_name if offer else None,
            "owner_name": _user_name(ln.buyer),
            "owner_role": "Buyer",
            "value": plan.total_cost if plan else None,
            "margin_pct": plan.total_margin_pct if plan else None,
            "waiting_since": waiting_since,
            "issue_reason": _issue_reason(ln) if kind == "flagged" else None,
        }

    # ── Assemble the single uniform queue, then sort risk-first / oldest-first ──
    queue = (
        [_plan_row(p, kind="approve", waiting_since=p.created_at) for p in approval_plans]
        + [_plan_row(p, kind="halted", waiting_since=p.created_at) for p in halted_plans]
        # overdue uses the exact SLA clock the overdue predicate keys off of.
        + [
            _line_row(ln, kind="overdue", waiting_since=ln.last_nudge_at or ln.buy_plan.approved_at)
            for ln in overdue_lines
        ]
        + [_line_row(ln, kind="verify_po", waiting_since=ln.created_at) for ln in po_pending_verify_lines]
        + [_line_row(ln, kind="flagged", waiting_since=ln.created_at) for ln in flagged_lines]
    )
    # Ascending datetime ⇒ oldest-first within each priority tier.
    queue.sort(key=lambda r: (r["priority"], r["waiting_since"]))

    return {
        "strip": {
            "open_value": open_value,
            "avg_margin": avg_margin,
            "approval_count": len(approval_plans),
            "halted_count": len(halted_plans),
            "overdue_po_count": len(overdue_lines),
            "po_pending_verify_count": len(po_pending_verify_lines),
            "flagged_count": len(flagged_lines),
        },
        "queue": queue,
    }


# ── My Queue — one role-aware builder ─────────────────────────────────
# my_queue(db, user) is the single read model behind the "what needs YOU now" surface.
# It reuses the same _query_* helpers as supervise_overview (single source of truth) but
# gates which KINDS it emits by the viewer's rights/role/ownership, then sorts risk-first.
# Jinja consumes ONLY QueueRow — every ORM access stays inside this module.


@dataclass(frozen=True)
class QueueRow:
    """One actionable item for a single user, fully resolved (no ORM access in Jinja).

    ``value``/``tso`` are always the parent plan's deal value / sales-order number so a
    row reads uniformly regardless of kind. ``primary_mpn`` is the headline part (line MPN
    for line kinds, first non-cancelled line's MPN for plan kinds). ``age_hours`` is the
    wait clock used both for display and the oldest-first sort; ``is_overdue`` is True only
    for an AWAITING_PO line past its buyer-nudge SLA. ``extra`` carries kind-specific
    secondary-line fields (``margin_pct``, ``vendor_name``, ``owner_name``, ``owner_role``,
    and ``amount`` on prepay rows) the template renders below the identity line.
    """

    kind: str
    priority: int
    label: str
    plan_id: int | None
    line_id: int | None
    customer_name: str | None
    primary_mpn: str | None
    tso: str | None
    value: Decimal | None
    age_hours: float
    is_overdue: bool
    action_url: str | None
    action_label: str | None
    detail_href: str | None
    extra: dict = field(default_factory=dict)


#: Risk-first priority tier per my_queue kind (1 = highest risk, surfaced first). The
#: queue sorts by ``(priority, -age_hours)`` — risk-first, oldest-first within each tier.
_QUEUE_PRIORITY: dict[str, int] = {
    "halted": 1,
    "no_approver": 2,
    "plan_returned": 2,
    "flagged": 2,
    "plan_approve": 3,
    "prepay_approve": 3,
    "po_verify": 4,
    "claim": 5,
    "cut_po_overdue": 6,
    "cut_po": 7,
    "plan_draft": 9,
}

#: Short uppercase microlabel shown in the row's KIND column.
_QUEUE_LABEL: dict[str, str] = {
    "halted": "Halted",
    "no_approver": "No approver",
    "plan_returned": "Returned",
    "flagged": "Flagged",
    "plan_approve": "Approve",
    "prepay_approve": "Prepay",
    "po_verify": "Verify",
    "claim": "Claim",
    "cut_po_overdue": "Overdue",
    "cut_po": "Cut PO",
    "plan_draft": "Draft",
}

#: Primary action-button verb per kind (the right-rail CTA).
_QUEUE_ACTION_LABEL: dict[str, str] = {
    "halted": "Open",
    "no_approver": "Open",
    "plan_returned": "Resubmit",
    "flagged": "Open",
    "plan_approve": "Approve",
    "prepay_approve": "Approve",
    "po_verify": "Verify",
    "claim": "Claim",
    "cut_po_overdue": "Cut PO",
    "cut_po": "Cut PO",
    "plan_draft": "Submit",
}

#: Roles that may cut/claim POs (line-execution kinds).
_PO_CUTTER_ROLES: frozenset[str] = frozenset({UserRole.BUYER, UserRole.MANAGER, UserRole.ADMIN})


def _is_ops_member(db: Session, user: object) -> bool:
    """True when *user* is an active ops verification-group member.

    Queries VerificationGroupMember directly (the single source of truth) so this read
    model carries no dependency on the HTMX router layer.
    """
    return db.query(VerificationGroupMember).filter_by(user_id=user.id, is_active=True).first() is not None


def _is_returned(plan: BuyPlan) -> bool:
    """True when a DRAFT plan was sent back by an approver (vs a fresh, never-submitted
    draft).

    Mirrors the rejected branch of :func:`_compute_blocker`: an approval decision timestamp,
    a rejected SO, or rejection wording in the approval notes all mark a returned draft.
    """
    return (
        plan.approved_at is not None
        or plan.so_status == SOVerificationStatus.REJECTED
        or bool(plan.approval_notes and "reject" in plan.approval_notes.lower())
    )


def _age_hours(since: datetime | None) -> float:
    """Whole-and-fractional hours from *since* until now (UTC), floored at 0.

    Naive datetimes are treated as UTC (defensive — UTCDateTime returns aware values, but
    raw back-dated test rows can be naive). ``None`` → 0.0.
    """
    if since is None:
        return 0.0
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - since).total_seconds() / 3600.0)


def _line_mpn(line: BuyPlanLine) -> str | None:
    """Headline MPN for a line row: the offer's MPN, falling back to the requirement's.

    Both relationships are eager-loaded by the ``_LINE_PLAN_LOADS`` chain (no N+1); the
    requirement fallback covers RESOURCING-pool lines whose offer fell down.
    """
    if line.offer and line.offer.mpn:
        return line.offer.mpn
    if line.requirement and line.requirement.primary_mpn:
        return line.requirement.primary_mpn
    return None


def _action_url(kind: str, *, plan_id: int | None, line_id: int | None, request_id: int | None) -> str | None:
    """Build the primary-action endpoint for a row (``None`` for whole-row open
    kinds)."""
    base = f"/v2/partials/buy-plans/{plan_id}"
    if kind in ("plan_draft", "plan_returned"):
        return f"{base}/submit"
    if kind == "plan_approve":
        return f"{base}/approve"
    if kind == "prepay_approve":
        return f"/v2/approvals/requests/{request_id}/decision"
    if kind == "po_verify":
        return f"{base}/lines/{line_id}/verify-po"
    if kind == "claim":
        return f"{base}/lines/{line_id}/claim"
    if kind in ("cut_po", "cut_po_overdue"):
        return f"{base}/lines/{line_id}/confirm-po"
    # halted → whole row links to detail; no distinct action endpoint.
    return None


def _make_plan_row(plan: BuyPlan, *, kind: str, since: datetime | None) -> QueueRow:
    """Build a plan-level QueueRow. Owner is the Account Manager (``submitted_by``).

    Touches only eager-loaded relationships (customer, submitted_by, lines→requirement).
    """
    return QueueRow(
        kind=kind,
        priority=_QUEUE_PRIORITY[kind],
        label=_QUEUE_LABEL[kind],
        plan_id=plan.id,
        line_id=None,
        customer_name=_customer_name(plan),
        primary_mpn=_primary_mpn(plan),
        tso=plan.sales_order_number,
        value=plan.total_cost,
        age_hours=_age_hours(since),
        is_overdue=False,
        action_url=_action_url(kind, plan_id=plan.id, line_id=None, request_id=None),
        action_label=_QUEUE_ACTION_LABEL[kind],
        detail_href=f"/v2/partials/buy-plans/{plan.id}",
        extra={
            "margin_pct": plan.total_margin_pct,
            "owner_name": _user_name(plan.submitted_by),
            "owner_role": "AM",
        },
    )


def _make_line_row(line: BuyPlanLine, *, kind: str, since: datetime | None, cutoff: datetime) -> QueueRow:
    """Build a line-level QueueRow. Owner is the Buyer; value/margin come from the
    parent plan.

    Touches only eager-loaded relationships (buy_plan, offer, buyer, requirement).
    """
    plan = line.buy_plan
    extra: dict = {
        "margin_pct": plan.total_margin_pct if plan else None,
        "vendor_name": line.offer.vendor_name if line.offer else None,
        "owner_name": _user_name(line.buyer),
        "owner_role": "Buyer",
    }
    # flagged rows carry the buyer's issue reason for at-a-glance triage; cut-PO rows expose
    # whether they were kicked back (manager/ops rejected the PO) + the rejection note so the
    # My Queue surface can flag + explain the re-cut, mirroring the buyer Orders lens.
    if kind == "flagged":
        extra["issue_reason"] = _issue_reason(line)
    elif kind in ("cut_po", "cut_po_overdue"):
        extra["kicked_back"] = line.po_rejection_note is not None
        extra["po_rejection_note"] = line.po_rejection_note
    return QueueRow(
        kind=kind,
        priority=_QUEUE_PRIORITY[kind],
        label=_QUEUE_LABEL[kind],
        plan_id=line.buy_plan_id,
        line_id=line.id,
        customer_name=_customer_name(plan) if plan else None,
        primary_mpn=_line_mpn(line),
        tso=plan.sales_order_number if plan else None,
        value=plan.total_cost if plan else None,
        age_hours=_age_hours(since),
        is_overdue=_line_overdue(line, cutoff),
        action_url=_action_url(kind, plan_id=line.buy_plan_id, line_id=line.id, request_id=None),
        action_label=_QUEUE_ACTION_LABEL[kind],
        detail_href=f"/v2/partials/buy-plans/{line.buy_plan_id}",
        extra=extra,
    )


def _prepay_rows(db: Session, user: object) -> list[QueueRow]:
    """Prepayment-approval rows the *user* may decide.

    Reuses the approvals engine's ``_actionable_request_ids`` (REQUESTED requests where the
    user holds a PENDING recipient row), filtered to the PREPAYMENT gate — never duplicating
    that query. Subjects (with vendor + parent plan customer) are batch-loaded to avoid N+1.
    """
    actionable = _actionable_request_ids(db, user)
    if not actionable:
        return []

    reqs = list(
        db.execute(
            select(ApprovalRequest)
            .where(ApprovalRequest.id.in_(actionable), ApprovalRequest.gate_type == ApprovalGateType.PREPAYMENT)
            .order_by(ApprovalRequest.created_at.asc())
        ).scalars()
    )
    if not reqs:
        return []

    subject_ids = [ar.subject_id for ar in reqs if ar.subject_id]
    prepayments: dict[int, Prepayment] = {}
    if subject_ids:
        loaded = (
            db.execute(
                select(Prepayment)
                .options(
                    joinedload(Prepayment.vendor_card),
                    joinedload(Prepayment.buy_plan)
                    .joinedload(BuyPlan.quote)
                    .joinedload(Quote.customer_site)
                    .joinedload(CustomerSite.company),
                    joinedload(Prepayment.buy_plan)
                    .joinedload(BuyPlan.requisition)
                    .joinedload(Requisition.customer_site)
                    .joinedload(CustomerSite.company),
                )
                .where(Prepayment.id.in_(subject_ids))
            )
            .unique()
            .scalars()
        )
        prepayments = {pp.id: pp for pp in loaded}

    rows: list[QueueRow] = []
    for ar in reqs:
        pp = prepayments.get(ar.subject_id) if ar.subject_id else None
        plan = pp.buy_plan if pp else None
        plan_id = pp.buy_plan_id if pp else None
        rows.append(
            QueueRow(
                kind="prepay_approve",
                priority=_QUEUE_PRIORITY["prepay_approve"],
                label=_QUEUE_LABEL["prepay_approve"],
                plan_id=plan_id,
                line_id=None,
                customer_name=_customer_name(plan) if plan else None,
                primary_mpn=None,
                tso=plan.sales_order_number if plan else None,
                value=plan.total_cost if plan else None,
                age_hours=_age_hours(ar.created_at),
                is_overdue=False,
                action_url=f"/v2/approvals/requests/{ar.id}/decision",
                action_label=_QUEUE_ACTION_LABEL["prepay_approve"],
                detail_href=f"/v2/partials/buy-plans/{plan_id}" if plan_id else None,
                extra={
                    "margin_pct": plan.total_margin_pct if plan else None,
                    "vendor_name": (pp.vendor_card.display_name if pp and pp.vendor_card else None),
                    "amount": ar.amount,
                    "owner_role": "Approver",
                    # The engine request id so the My Queue inline action can build the
                    # decide URL (the QueueRow.action_url stays the JSON decision route).
                    "request_id": ar.id,
                },
            )
        )
    return rows


def _query_stuck_no_approver_plans(db: Session, *, owner_id: int | None = None) -> list[BuyPlan]:
    """Plans silently stalled because no approver is configured for their open gate.

    Emitted only in the rare, config-level case that no active user holds the approving
    right (otherwise the plan is merely awaiting a real approver). ``owner_id`` scopes to a
    single AM (my_queue); ``None`` returns every stuck plan (admins, so they can fix the
    config). BUY_PLAN eligibility is global — one check gates every PENDING plan — while
    PURCHASE_ORDER is amount-sensitive PER LINE (Phase 3): an ACTIVE plan is stuck when any
    of its PENDING_VERIFY lines has no approver eligible for that line's dollar amount.
    """
    from ..constants import ApprovalGateType
    from .approvals.routing import has_eligible_approver
    from .buyplan_workflow import _line_amount

    loads = (
        *_PLAN_CUSTOMER_LOADS,
        joinedload(BuyPlan.submitted_by),
        selectinload(BuyPlan.lines).selectinload(BuyPlanLine.requirement),
    )
    out: list[BuyPlan] = []

    if not has_eligible_approver(db, ApprovalGateType.BUY_PLAN):
        q = db.query(BuyPlan).filter(BuyPlan.status == BuyPlanStatus.PENDING)
        if owner_id is not None:
            q = q.filter(BuyPlan.submitted_by_id == owner_id)
        out += q.options(*loads).order_by(BuyPlan.created_at.asc()).all()

    # ACTIVE plans with a cut PO awaiting verification (EXISTS subquery — no row
    # multiplication), then the per-line amount-eligibility check in Python.
    q = db.query(BuyPlan).filter(
        BuyPlan.status == BuyPlanStatus.ACTIVE,
        BuyPlan.lines.any(BuyPlanLine.status == BuyPlanLineStatus.PENDING_VERIFY),
    )
    if owner_id is not None:
        q = q.filter(BuyPlan.submitted_by_id == owner_id)
    for plan in q.options(*loads).order_by(BuyPlan.created_at.asc()).all():
        if any(
            line.status == BuyPlanLineStatus.PENDING_VERIFY
            and not has_eligible_approver(db, ApprovalGateType.PURCHASE_ORDER, _line_amount(line))
            for line in plan.lines
        ):
            out.append(plan)

    return out


def my_queue(db: Session, user: object) -> list[QueueRow]:
    """Return the role-aware "what needs YOU now" queue for *user*, risk-first.

    Emits a uniform :class:`QueueRow` per actionable item, gated by the viewer's rights /
    role / ownership:

    - **halted** (P1): own halted plans; supervisors (manager/admin/ops) see all.
    - **plan_returned** (P2) / **plan_draft** (P9): DRAFT plans the user submitted, split
      by :func:`_is_returned`.
    - **plan_approve** (P3): all pending plans — buy-plan approvers (``can_approve_buy_plans``).
    - **prepay_approve** (P3): prepayment requests routed to the user (engine-actionable).
    - **po_verify** (P4): all pending-verify lines — PO approvers
      (``can_approve_purchase_orders`` — the same per-user right the verify-PO POST enforces).
    - **claim** (P5): the whole RESOURCING pool — PO-cutters.
    - **cut_po_overdue** (P6) / **cut_po** (P7): the user's own AWAITING_PO lines, split by
      the buyer-nudge SLA.

    Sorted by ``(priority, -age_hours)``: highest-risk tier first, oldest-first within it.
    """
    cutoff = _nudge_cutoff()
    is_approver = can_approve_buy_plans(user)
    is_ops = _is_ops_member(db, user)
    is_supervisor = user.role in (UserRole.MANAGER, UserRole.ADMIN) or is_ops
    # verify-PO is gated solely on the per-user right (Phase D moved it off ops membership);
    # `or is_ops` would re-introduce a dead 403 button for ops members lacking the right.
    is_po_approver = bool(getattr(user, "can_approve_purchase_orders", False))
    is_po_cutter = user.role in _PO_CUTTER_ROLES

    rows: list[QueueRow] = []

    # halted (P1): own plans always; supervisors see the whole risk surface.
    halted = _query_halted_plans(db) if is_supervisor else _query_halted_plans(db, owner_id=user.id)
    rows += [_make_plan_row(p, kind="halted", since=p.halted_at or p.created_at) for p in halted]

    # no_approver (P2): plans stalled because no approver is configured for the open gate.
    # Owner-scoped — a submitted plan leaves the AM's draft queue and no approver exists to
    # see it, so without this the owner has NO signal at all; admins see every stuck plan so
    # they can fix the config (grant someone the approving right).
    stuck_owner = None if user.role == UserRole.ADMIN else user.id
    rows += [
        _make_plan_row(p, kind="no_approver", since=p.submitted_at or p.created_at)
        for p in _query_stuck_no_approver_plans(db, owner_id=stuck_owner)
    ]

    # plan_returned (P2) + plan_draft (P9): the user's own DRAFT plans.
    for plan in _query_owner_draft_plans(db, owner_id=user.id):
        kind = "plan_returned" if _is_returned(plan) else "plan_draft"
        since = plan.approved_at if kind == "plan_returned" else plan.created_at
        rows.append(_make_plan_row(plan, kind=kind, since=since or plan.created_at))

    # flagged (P2): buyer-flagged (ISSUE) lines are a supervisor triage surface — the buyer
    # who raised them can't self-resolve, so they route to managers/admins/ops only (parity
    # with the supervise lens). Shares _query_flagged_lines with supervise_overview.
    if is_supervisor:
        rows += [
            _make_line_row(ln, kind="flagged", since=ln.created_at, cutoff=cutoff) for ln in _query_flagged_lines(db)
        ]

    # plan_approve (P3): buy-plan approvers see every pending plan.
    if is_approver:
        rows += [
            _make_plan_row(p, kind="plan_approve", since=p.submitted_at or p.created_at)
            for p in _query_approval_plans(db)
        ]

    # prepay_approve (P3): prepayment requests routed to this user.
    rows += _prepay_rows(db, user)

    # po_verify (P4): PO approvers (can_approve_purchase_orders) verify every pending-verify
    # line. Ops membership alone no longer grants this (Phase D) — see is_po_approver above.
    if is_po_approver:
        rows += [
            _make_line_row(ln, kind="po_verify", since=ln.po_confirmed_at or ln.created_at, cutoff=cutoff)
            for ln in _query_po_pending_verify(db)
        ]

    # claim (P5): any PO-cutter may claim a pooled line.
    if is_po_cutter:
        rows += [
            _make_line_row(ln, kind="claim", since=ln.updated_at or ln.created_at, cutoff=cutoff)
            for ln in _query_resourcing_pool(db)
        ]

    # cut_po_overdue (P6) + cut_po (P7): the user's own AWAITING_PO lines, SLA-split.
    if is_po_cutter:
        for ln in _query_buyer_awaiting_po_lines(db, buyer_id=user.id):
            overdue = _line_overdue(ln, cutoff)
            kind = "cut_po_overdue" if overdue else "cut_po"
            since = (ln.last_nudge_at or ln.buy_plan.approved_at or ln.created_at) if ln.buy_plan else ln.created_at
            rows.append(_make_line_row(ln, kind=kind, since=since, cutoff=cutoff))

    # Risk-first, then oldest-first (largest age) within each priority tier.
    rows.sort(key=lambda r: (r.priority, -r.age_hours))
    return rows
