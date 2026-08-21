"""Shared scoring helpers — date-range utilities, offer-tier lookup loaders, and the
buyer point values used by the performance scoring services.

Provides the common building blocks used by avail_score_service,
multiplier_score_service, and buyer_leaderboard so month-range logic, the
quoted/buyplan/PO offer-tier derivation, and the buyer point values live in ONE
place — a change applied here reaches every leaderboard at once.

Called by: avail_score_service.py, multiplier_score_service.py, buyer_leaderboard.py
Depends on: datetime (stdlib), app.constants (BuyPlanStatus),
            app.models (BuyPlan, BuyPlanLine, Quote)
"""

from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from ..constants import BuyPlanStatus
from ..models import BuyPlan, BuyPlanLine, Quote

# ── Buyer point values (shared by buyer_leaderboard + multiplier_score_service) ──
# Offer pipeline tiers (non-stacking — each offer earns only its highest tier) plus
# the stock-list bonus. One definition so the two buyer leaderboards never diverge.
PTS_OFFER_BASE = 1
PTS_OFFER_QUOTED = 3
PTS_OFFER_BUYPLAN = 5
PTS_OFFER_PO = 8
PTS_STOCK_LIST = 2

# Grace window: offers logged in the last GRACE_DAYS of the previous month still count
# for this month when they advanced (quoted / entered a buy plan).
GRACE_DAYS = 7


def month_range(month: date) -> tuple[datetime, datetime]:
    """Return (start_dt, end_dt) as UTC-aware datetimes for the given month.

    start_dt is midnight on the 1st of the month. end_dt is midnight on the 1st of the
    next month.
    """
    month_start = month.replace(day=1)
    if month_start.month == 12:
        month_end = month_start.replace(year=month_start.year + 1, month=1)
    else:
        month_end = month_start.replace(month=month_start.month + 1)
    midnight = datetime.min.time()
    start_dt = datetime.combine(month_start, midnight, tzinfo=UTC)
    end_dt = datetime.combine(month_end, midnight, tzinfo=UTC)
    return start_dt, end_dt


def load_quoted_offer_ids(db: Session) -> set[int]:
    """Return set of offer IDs that appear in any sent/won/lost quote line_items."""
    ids = set()
    for (items,) in db.query(Quote.line_items).filter(Quote.status.in_(["sent", "won", "lost"])).limit(10000).all():
        for item in items or []:
            oid = item.get("offer_id")
            if oid:
                ids.add(oid)
    return ids


def load_buyplan_offer_ids(db: Session) -> tuple[set[int], set[int]]:
    """Return (bp_offer_ids, po_confirmed_offer_ids) from buy plan lines."""
    bp_ids = set()
    po_ids = set()
    for bp_status, offer_id in (
        db.query(BuyPlan.status, BuyPlanLine.offer_id)
        .join(BuyPlanLine, BuyPlanLine.buy_plan_id == BuyPlan.id)
        .filter(BuyPlanLine.offer_id.isnot(None))
        .limit(10000)
        .all()
    ):
        bp_ids.add(offer_id)
        if bp_status in (BuyPlanStatus.COMPLETED.value,):
            po_ids.add(offer_id)
    return bp_ids, po_ids
