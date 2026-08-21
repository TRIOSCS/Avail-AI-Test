"""Buyer Leaderboard — Multiplier scoring with 7-day grace period and stock list dedup.

Called by: scheduler.py (monthly), routers/performance.py (on-demand)
Depends on: models, database, scoring_helpers (month_range, offer-tier loaders, shared
            buyer point values)
"""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ..constants import UserRole
from ..models import (
    BuyerLeaderboardSnapshot,
    Offer,
    StockListHash,
    User,
)
from .scoring_helpers import (
    GRACE_DAYS,
    PTS_OFFER_BASE,
    PTS_OFFER_BUYPLAN,
    PTS_OFFER_PO,
    PTS_OFFER_QUOTED,
    PTS_STOCK_LIST,
    load_buyplan_offer_ids,
    load_quoted_offer_ids,
    month_range,
)

# Buyer point multipliers — aliases over the ONE shared definition in scoring_helpers
# (also used by multiplier_score_service), kept under this module's historical names.
PTS_LOGGED = PTS_OFFER_BASE
PTS_QUOTED = PTS_OFFER_QUOTED
PTS_BUYPLAN = PTS_OFFER_BUYPLAN
PTS_PO_CONFIRMED = PTS_OFFER_PO


def compute_buyer_leaderboard(db: Session, month: date) -> dict:
    """Compute buyer leaderboard for a given month."""
    # Normalize to first of month
    month_start = month.replace(day=1)
    month_start_dt, month_end_dt = month_range(month)

    # Grace period: last 7 days of previous month
    grace_start_dt = month_start_dt - timedelta(days=GRACE_DAYS)

    # Get all buyers
    buyers = db.query(User).filter(User.role.in_([UserRole.BUYER, UserRole.TRADER])).all()

    # Collect all offer_ids that appear in quotes and buy plans (for status checks) —
    # shared loaders (scoring_helpers) also used by multiplier_score_service.
    quoted_offer_ids = load_quoted_offer_ids(db)
    buyplan_offer_ids, po_confirmed_offer_ids = load_buyplan_offer_ids(db)

    # Batch-fetch all offers and stock counts to avoid N+1 per buyer
    buyer_ids = [b.id for b in buyers]

    all_month_offers = (
        db.query(Offer)
        .filter(
            Offer.entered_by_id.in_(buyer_ids),
            Offer.created_at >= month_start_dt,
            Offer.created_at < month_end_dt,
        )
        .all()
    )
    month_offers_by_buyer: dict[int, list] = {}
    for o in all_month_offers:
        month_offers_by_buyer.setdefault(o.entered_by_id, []).append(o)

    all_grace_offers = (
        db.query(Offer)
        .filter(
            Offer.entered_by_id.in_(buyer_ids),
            Offer.created_at >= grace_start_dt,
            Offer.created_at < month_start_dt,
        )
        .all()
    )
    grace_offers_by_buyer: dict[int, list] = {}
    for o in all_grace_offers:
        grace_offers_by_buyer.setdefault(o.entered_by_id, []).append(o)

    stock_counts = dict(
        db.query(StockListHash.user_id, sqlfunc.count(StockListHash.id))
        .filter(
            StockListHash.user_id.in_(buyer_ids),
            StockListHash.first_seen_at >= month_start_dt,
            StockListHash.first_seen_at < month_end_dt,
        )
        .group_by(StockListHash.user_id)
        .all()
    )

    entries = []
    for buyer in buyers:
        month_offers = month_offers_by_buyer.get(buyer.id, [])
        grace_offers = grace_offers_by_buyer.get(buyer.id, [])

        # Grace offers only count if they advanced during this month
        grace_advanced = [o for o in grace_offers if o.id in quoted_offer_ids or o.id in buyplan_offer_ids]

        all_offers = month_offers + grace_advanced
        offer_ids = {o.id for o in all_offers}

        logged = len(all_offers)
        quoted = sum(1 for oid in offer_ids if oid in quoted_offer_ids)
        in_buyplan = sum(1 for oid in offer_ids if oid in buyplan_offer_ids)
        po_confirmed = sum(1 for oid in offer_ids if oid in po_confirmed_offer_ids)
        stock_uploaded = stock_counts.get(buyer.id, 0)

        pts_logged = logged * PTS_LOGGED
        pts_quoted = quoted * PTS_QUOTED
        pts_buyplan = in_buyplan * PTS_BUYPLAN
        pts_po = po_confirmed * PTS_PO_CONFIRMED
        pts_stock = stock_uploaded * PTS_STOCK_LIST
        total = pts_logged + pts_quoted + pts_buyplan + pts_po + pts_stock

        entries.append(
            {
                "user_id": buyer.id,
                "offers_logged": logged,
                "offers_quoted": quoted,
                "offers_in_buyplan": in_buyplan,
                "offers_po_confirmed": po_confirmed,
                "stock_lists_uploaded": stock_uploaded,
                "points_offers": pts_logged,
                "points_quoted": pts_quoted,
                "points_buyplan": pts_buyplan,
                "points_po": pts_po,
                "points_stock": pts_stock,
                "total_points": total,
            }
        )

    # Rank by total_points descending
    entries.sort(key=lambda e: e["total_points"], reverse=True)
    for i, entry in enumerate(entries):
        entry["rank"] = i + 1

    # Upsert snapshots
    for entry in entries:
        existing = (
            db.query(BuyerLeaderboardSnapshot)
            .filter(
                BuyerLeaderboardSnapshot.user_id == entry["user_id"],
                BuyerLeaderboardSnapshot.month == month_start,
            )
            .first()
        )

        if existing:
            snap = existing
        else:
            snap = BuyerLeaderboardSnapshot(user_id=entry["user_id"], month=month_start)
            db.add(snap)

        # Copy every entry metric onto the snapshot (entry keys mirror snapshot
        # attributes 1:1, except user_id which is the upsert key set above).
        for key, value in entry.items():
            if key != "user_id":
                setattr(snap, key, value)
        snap.updated_at = datetime.now(UTC)

    db.commit()
    return {"month": month_start.isoformat(), "entries": len(entries)}
