"""buyplan_hub.py — shared buy-plan display/query helpers.

Purpose: The surviving helpers of the retired Buy Plan Deal Hub read models
         (spec §11.1; docs/APPROVALS_PARITY_CHECKLIST.md): the customer-name
         derivation, the wait-clock formatter, the headline-MPN pick, and the
         org-wide pending-verify line query — all reused by the Approvals
         Workspace's PO queue. The hub's own read models (my_queue, deals_board,
         completed_archive, supervise_overview, open_avg_margin, the line queues)
         retired with their surfaces.

Called by: services/approvals/po_queue.py (build_po_queue_view)
Depends on: models.buy_plan (BuyPlan, BuyPlanLine), models.crm/quotes/sourcing
            (customer-name chains), constants (BuyPlanLineStatus)
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session, joinedload

from ..constants import BuyPlanLineStatus
from ..models.buy_plan import BuyPlan, BuyPlanLine
from ..models.crm import CustomerSite
from ..models.quotes import Quote
from ..models.sourcing import Requisition


def _customer_name(plan: BuyPlan) -> str | None:
    """Derive a display customer name from quote or requisition.

    Quote path (preferred): plan.quote → customer_site → company.name.
    Requisition fallback (SO-origin plans with no quote): req.customer_name,
    then req.customer_site → company.name.
    Returns ``None`` when neither source has a customer name.
    """
    # Typed locals: the relationship chains below are legacy untyped reads.
    name: str | None
    if plan.quote and plan.quote.customer_site and plan.quote.customer_site.company:
        name = plan.quote.customer_site.company.name
        return name
    req = plan.requisition
    if req:
        if req.customer_name:
            name = req.customer_name
            return name
        if req.customer_site and req.customer_site.company:
            name = req.customer_site.company.name
            return name
    return None


def _age_hours(since: datetime | None) -> float:
    """Whole-and-fractional hours from *since* until now (UTC), floored at 0.

    Naive datetimes are treated as UTC (defensive — UTCDateTime returns aware values, but
    raw back-dated test rows can be naive). ``None`` → 0.0.
    """
    if since is None:
        return 0.0
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - since).total_seconds() / 3600.0)


def _line_mpn(line: BuyPlanLine) -> str | None:
    """Headline MPN for a line row: the offer's MPN, falling back to the requirement's.

    Both relationships are eager-loaded by the ``_LINE_PLAN_LOADS`` chain (no N+1); the
    requirement fallback covers RESOURCING-pool lines whose offer fell down.
    """
    # Typed locals: both relationship chains are legacy untyped reads.
    mpn: str | None
    if line.offer and line.offer.mpn:
        mpn = line.offer.mpn
        return mpn
    if line.requirement and line.requirement.primary_mpn:
        mpn = line.requirement.primary_mpn
        return mpn
    return None


# Eager-load chain for line queries: a line row needs its parent plan's customer (quote
# OR requisition path), the offer (MPN/vendor), the buyer and the requirement.
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


def _query_po_pending_verify(db: Session) -> list[BuyPlanLine]:
    """PENDING_VERIFY lines awaiting PO verification (org-wide), oldest first."""
    return (
        db.query(BuyPlanLine)
        .filter(BuyPlanLine.status == BuyPlanLineStatus.PENDING_VERIFY)
        .options(*_LINE_PLAN_LOADS)
        .order_by(BuyPlanLine.created_at.asc())
        .all()
    )
