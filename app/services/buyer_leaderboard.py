"""Buyer Leaderboard — Multiplier scoring with 7-day grace period and stock list dedup.

Called by: scheduler.py (monthly), routers/performance.py (on-demand)
Depends on: models, database
"""

import hashlib
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ..models import (
    BuyerLeaderboardSnapshot,
    BuyPlan,
    Offer,
    Quote,
    StockListHash,
    User,
)

# Buyer point multipliers
PTS_LOGGED = 1
PTS_QUOTED = 3
PTS_BUYPLAN = 5
PTS_PO_CONFIRMED = 8
PTS_STOCK_LIST = 2

GRACE_DAYS = 7


def compute_buyer_leaderboard(db: Session, month: date) -> dict:
    """Compute buyer leaderboard for a given month."""
    # Normalize to first of month
    month_start = month.replace(day=1)
    if month_start.month == 12:
        month_end = month_start.replace(year=month_start.year + 1, month=1)
    else:
        month_end = month_start.replace(month=month_start.month + 1)

    month_start_dt = datetime(month_start.year, month_start.month, month_start.day, tzinfo=timezone.utc)
    month_end_dt = datetime(month_end.year, month_end.month, month_end.day, tzinfo=timezone.utc)

    # Grace period: last 7 days of previous month
    grace_start_dt = month_start_dt - timedelta(days=GRACE_DAYS)

    # Get all buyers
    buyers = db.query(User).filter(User.role.in_(["buyer", "trader"])).all()

    # Collect all offer_ids that appear in quotes and buy plans (for status checks)
    quoted_offer_ids = set()
    for (items,) in db.query(Quote.line_items).filter(Quote.status.in_(["sent", "won", "lost"])).limit(10000).all():
        for item in items or []:
            oid = item.get("offer_id")
            if oid:
                quoted_offer_ids.add(oid)

    # Buy plans: line_items JSON contains offer_id
    buyplan_offer_ids = set()
    po_confirmed_offer_ids = set()
    for bp_status, items in db.query(BuyPlan.status, BuyPlan.line_items).limit(10000).all():
        for item in items or []:
            oid = item.get("offer_id")
            if oid:
                buyplan_offer_ids.add(oid)
                if bp_status in ("po_confirmed", "complete"):
                    po_confirmed_offer_ids.add(oid)

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

        snap.offers_logged = entry["offers_logged"]
        snap.offers_quoted = entry["offers_quoted"]
        snap.offers_in_buyplan = entry["offers_in_buyplan"]
        snap.offers_po_confirmed = entry["offers_po_confirmed"]
        snap.stock_lists_uploaded = entry["stock_lists_uploaded"]
        snap.points_offers = entry["points_offers"]
        snap.points_quoted = entry["points_quoted"]
        snap.points_buyplan = entry["points_buyplan"]
        snap.points_po = entry["points_po"]
        snap.points_stock = entry["points_stock"]
        snap.total_points = entry["total_points"]
        snap.rank = entry["rank"]
        snap.updated_at = datetime.now(timezone.utc)

    db.commit()
    return {"month": month_start.isoformat(), "entries": len(entries)}


def get_buyer_leaderboard(db: Session, month: date) -> list[dict]:
    """Return leaderboard for a given month with YTD totals."""
    month_start = month.replace(day=1)
    rows = (
        db.query(BuyerLeaderboardSnapshot, User.name)
        .join(
            User,
            User.id == BuyerLeaderboardSnapshot.user_id,
        )
        .filter(
            BuyerLeaderboardSnapshot.month == month_start,
        )
        .order_by(BuyerLeaderboardSnapshot.rank)
        .all()
    )

    # Compute YTD totals: sum all snapshots from Jan through selected month
    ytd_start = date(month_start.year, 1, 1)
    ytd_rows = (
        db.query(
            BuyerLeaderboardSnapshot.user_id,
            sqlfunc.sum(BuyerLeaderboardSnapshot.offers_logged).label("ytd_offers_logged"),
            sqlfunc.sum(BuyerLeaderboardSnapshot.offers_quoted).label("ytd_offers_quoted"),
            sqlfunc.sum(BuyerLeaderboardSnapshot.offers_in_buyplan).label("ytd_offers_in_buyplan"),
            sqlfunc.sum(BuyerLeaderboardSnapshot.offers_po_confirmed).label("ytd_offers_po_confirmed"),
            sqlfunc.sum(BuyerLeaderboardSnapshot.stock_lists_uploaded).label("ytd_stock_lists"),
            sqlfunc.sum(BuyerLeaderboardSnapshot.total_points).label("ytd_total_points"),
        )
        .filter(
            BuyerLeaderboardSnapshot.month >= ytd_start,
            BuyerLeaderboardSnapshot.month <= month_start,
        )
        .group_by(BuyerLeaderboardSnapshot.user_id)
        .all()
    )
    ytd_map = {
        r.user_id: {
            "ytd_offers_logged": r.ytd_offers_logged or 0,
            "ytd_offers_quoted": r.ytd_offers_quoted or 0,
            "ytd_offers_in_buyplan": r.ytd_offers_in_buyplan or 0,
            "ytd_offers_po_confirmed": r.ytd_offers_po_confirmed or 0,
            "ytd_stock_lists": r.ytd_stock_lists or 0,
            "ytd_total_points": r.ytd_total_points or 0,
        }
        for r in ytd_rows
    }

    result = []
    for snap, user_name in rows:
        ytd = ytd_map.get(snap.user_id, {})
        result.append(
            {
                "user_id": snap.user_id,
                "user_name": user_name,
                "rank": snap.rank,
                "offers_logged": snap.offers_logged,
                "offers_quoted": snap.offers_quoted,
                "offers_in_buyplan": snap.offers_in_buyplan,
                "offers_po_confirmed": snap.offers_po_confirmed,
                "stock_lists_uploaded": snap.stock_lists_uploaded or 0,
                "points_offers": snap.points_offers,
                "points_quoted": snap.points_quoted,
                "points_buyplan": snap.points_buyplan,
                "points_po": snap.points_po,
                "points_stock": snap.points_stock or 0,
                "total_points": snap.total_points,
                "ytd_offers_logged": ytd.get("ytd_offers_logged", 0),
                "ytd_offers_po_confirmed": ytd.get("ytd_offers_po_confirmed", 0),
                "ytd_total_points": ytd.get("ytd_total_points", 0),
            }
        )
    return result


def get_buyer_leaderboard_months(db: Session) -> list[str]:
    """Return available months for leaderboard."""
    rows = (
        db.query(BuyerLeaderboardSnapshot.month)
        .distinct()
        .order_by(BuyerLeaderboardSnapshot.month.desc())
        .limit(12)
        .all()
    )
    return [r[0].isoformat() for r in rows]


# ── Stock List Dedup ──────────────────────────────────────────────────


def compute_stock_list_hash(rows: list[dict]) -> str:
    """Normalize and hash stock list content. Sort MPNs, lowercase, SHA-256."""
    mpns = sorted(
        set(
            str(r.get("mpn") or r.get("part_number") or "").strip().upper()
            for r in rows
            if r.get("mpn") or r.get("part_number")
        )
    )
    raw = "|".join(mpns)
    return hashlib.sha256(raw.encode()).hexdigest()


def check_and_record_stock_list(
    db: Session,
    user_id: int,
    content_hash: str,
    vendor_card_id: int | None,
    file_name: str,
    row_count: int,
) -> dict:
    """Check if a stock list is a duplicate and record it."""
    existing = (
        db.query(StockListHash)
        .filter(
            StockListHash.user_id == user_id,
            StockListHash.content_hash == content_hash,
        )
        .first()
    )

    if existing:
        existing.upload_count += 1
        existing.last_seen_at = datetime.now(timezone.utc)
        db.commit()
        return {
            "is_duplicate": True,
            "first_seen_at": existing.first_seen_at.isoformat(),
            "upload_count": existing.upload_count,
        }

    slh = StockListHash(
        user_id=user_id,
        content_hash=content_hash,
        vendor_card_id=vendor_card_id,
        file_name=file_name,
        row_count=row_count,
    )
    db.add(slh)
    db.commit()
    return {
        "is_duplicate": False,
        "first_seen_at": slh.first_seen_at.isoformat(),
        "upload_count": 1,
    }
