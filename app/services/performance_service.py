"""
Performance Tracking — Vendor Scorecards and Buyer Leaderboard.

Vendor Scorecard: 6 metrics over 90-day rolling window with cold-start protection.
Buyer Leaderboard: Multiplier scoring with 7-day grace period and stock list dedup.

Called by: scheduler.py (daily), routers/performance.py (on-demand)
Depends on: models, database
"""

import hashlib
import logging
from datetime import datetime, date, timezone, timedelta

from sqlalchemy import func as sqlfunc, and_
from sqlalchemy.orm import Session

from ..models import (
    User,
    VendorCard,
    VendorReview,
    Contact,
    VendorResponse,
    Offer,
    Quote,
    BuyPlan,
    VendorMetricsSnapshot,
    BuyerLeaderboardSnapshot,
    StockListHash,
)

log = logging.getLogger("avail.performance")

# ── Constants ──────────────────────────────────────────────────────────
VENDOR_WINDOW_DAYS = 90
COLD_START_THRESHOLD = 5

# Vendor composite weights (sum = 1.0)
W_RESPONSE_RATE = 0.15
W_QUOTE_ACCURACY = 0.10
W_ON_TIME = 0.10
W_CANCELLATION = 0.10
W_RMA = 0.05
W_LEAD_TIME = 0.05
W_QUOTE_CONVERSION = 0.15
W_PO_CONVERSION = 0.15
W_REVIEW_RATING = 0.15

# Buyer point multipliers
PTS_LOGGED = 1
PTS_QUOTED = 3
PTS_BUYPLAN = 5
PTS_PO_CONFIRMED = 8
PTS_STOCK_LIST = 2

GRACE_DAYS = 7


# ── Vendor Scorecard ──────────────────────────────────────────────────


def compute_vendor_scorecard(
    db: Session, vendor_card_id: int, window_days: int = VENDOR_WINDOW_DAYS
) -> dict:
    """Compute all 6 metrics for a single vendor over the rolling window."""
    vc = db.get(VendorCard, vendor_card_id)
    if not vc:
        return {}

    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    # ── 1. Response rate ──
    rfqs_sent = (
        db.query(sqlfunc.count(Contact.id))
        .filter(
            Contact.vendor_name == vc.display_name,
            Contact.contact_type == "email",
            Contact.created_at >= cutoff,
        )
        .scalar()
        or 0
    )

    rfqs_answered = (
        db.query(sqlfunc.count(VendorResponse.id))
        .filter(
            VendorResponse.vendor_name == vc.display_name,
            VendorResponse.status != "noise",
            VendorResponse.received_at >= cutoff,
        )
        .scalar()
        or 0
    )

    response_rate = (rfqs_answered / rfqs_sent) if rfqs_sent > 0 else None

    # ── 2. Quote accuracy ──
    # Compare offer prices to buy plan cost prices for same offer_id
    offers_in_window = (
        db.query(Offer)
        .filter(
            Offer.vendor_card_id == vendor_card_id,
            Offer.created_at >= cutoff,
            Offer.unit_price.isnot(None),
        )
        .all()
    )

    accuracy_diffs = []
    for offer in offers_in_window:
        # Find buy plans that reference this offer in line_items
        buy_plans = (
            db.query(BuyPlan)
            .filter(
                BuyPlan.status.in_(["po_entered", "po_confirmed", "complete"]),
            )
            .all()
        )
        for bp in buy_plans:
            for item in bp.line_items or []:
                if item.get("offer_id") == offer.id and item.get("cost_price"):
                    bp_price = float(item["cost_price"])
                    offer_price = float(offer.unit_price)
                    if bp_price > 0:
                        diff = abs(offer_price - bp_price) / bp_price
                        accuracy_diffs.append(diff)

    quote_accuracy = (
        (1 - (sum(accuracy_diffs) / len(accuracy_diffs))) if accuracy_diffs else None
    )
    if quote_accuracy is not None:
        quote_accuracy = max(0.0, min(1.0, quote_accuracy))

    # ── 3. On-time delivery ──
    # BuyPlans with po_confirmed — check if confirmation was within lead time
    on_time_count = 0
    total_delivery = 0
    for bp in (
        db.query(BuyPlan)
        .filter(
            BuyPlan.status.in_(["po_confirmed", "complete"]),
            BuyPlan.approved_at >= cutoff,
            BuyPlan.approved_at.isnot(None),
        )
        .all()
    ):
        for item in bp.line_items or []:
            if item.get("vendor_name", "").lower() == (vc.display_name or "").lower():
                total_delivery += 1
                # Use lead_time field to estimate expected delivery
                lead_str = item.get("lead_time", "")
                try:
                    lead_days = int(
                        "".join(c for c in str(lead_str) if c.isdigit()) or "0"
                    )
                except (ValueError, TypeError):
                    lead_days = 0
                if lead_days > 0 and bp.approved_at:
                    # If PO was confirmed, check the timeline
                    # We don't have actual delivery date, so use po_confirmed timing
                    if item.get("po_verified"):
                        on_time_count += 1

    on_time_delivery = (on_time_count / total_delivery) if total_delivery > 0 else None

    # ── 4. Cancellation rate (from Acctivate) ──
    cancellation_rate = vc.cancellation_rate

    # ── 5. RMA rate (from Acctivate) ──
    rma_rate = vc.rma_rate

    # ── 6. Lead time accuracy ──
    lead_time_accuracy = None
    if total_delivery > 0 and on_time_delivery is not None:
        # Approximate from on-time delivery data
        lead_time_accuracy = on_time_delivery

    # ── 7. Quote conversion rate ──
    # What % of this vendor's offers made it into quotes?
    total_offers = len(offers_in_window)
    offers_in_quotes = 0
    if total_offers > 0:
        offer_id_set = {o.id for o in offers_in_window}
        all_quotes = (
            db.query(Quote)
            .filter(
                Quote.status.in_(["sent", "won", "lost"]),
            )
            .all()
        )
        for q in all_quotes:
            for item in q.line_items or []:
                oid = item.get("offer_id")
                if oid in offer_id_set:
                    offers_in_quotes += 1
                    offer_id_set.discard(oid)
    quote_conversion = (offers_in_quotes / total_offers) if total_offers > 0 else None

    # ── 8. PO conversion rate ──
    # What % of this vendor's offers made it to PO confirmed/complete?
    offers_to_po = 0
    if total_offers > 0:
        offer_id_set2 = {o.id for o in offers_in_window}
        po_plans = (
            db.query(BuyPlan)
            .filter(
                BuyPlan.status.in_(["po_entered", "po_confirmed", "complete"]),
            )
            .all()
        )
        for bp in po_plans:
            for item in bp.line_items or []:
                oid = item.get("offer_id")
                if oid in offer_id_set2:
                    offers_to_po += 1
                    offer_id_set2.discard(oid)
    po_conversion = (offers_to_po / total_offers) if total_offers > 0 else None

    # ── 9. Average buyer review rating ──
    avg_rating_row = (
        db.query(sqlfunc.avg(VendorReview.rating))
        .filter(
            VendorReview.vendor_card_id == vendor_card_id,
        )
        .scalar()
    )
    avg_review_rating = (
        float(avg_rating_row) / 5.0 if avg_rating_row else None
    )  # Normalize to 0-1

    # ── Interaction count & cold-start ──
    interaction_count = rfqs_sent + total_offers

    # Count POs in window
    pos_in_window = total_delivery

    is_sufficient = interaction_count >= COLD_START_THRESHOLD

    # ── Composite score ──
    composite = (
        _compute_composite(
            response_rate,
            quote_accuracy,
            on_time_delivery,
            cancellation_rate,
            rma_rate,
            lead_time_accuracy,
            quote_conversion,
            po_conversion,
            avg_review_rating,
        )
        if is_sufficient
        else None
    )

    return {
        "vendor_card_id": vendor_card_id,
        "response_rate": response_rate,
        "quote_accuracy": quote_accuracy,
        "on_time_delivery": on_time_delivery,
        "cancellation_rate": cancellation_rate,
        "rma_rate": rma_rate,
        "lead_time_accuracy": lead_time_accuracy,
        "quote_conversion": quote_conversion,
        "po_conversion": po_conversion,
        "avg_review_rating": avg_review_rating,
        "composite_score": composite,
        "interaction_count": interaction_count,
        "is_sufficient_data": is_sufficient,
        "rfqs_sent": rfqs_sent,
        "rfqs_answered": rfqs_answered,
        "pos_in_window": pos_in_window,
    }


def _compute_composite(
    response_rate,
    quote_accuracy,
    on_time,
    cancellation_rate,
    rma_rate,
    lead_time,
    quote_conversion=None,
    po_conversion=None,
    avg_review_rating=None,
) -> float:
    """Weighted average of available metrics. Cancellation & RMA are inverted (lower=better)."""
    metrics = []
    weights = []

    if response_rate is not None:
        metrics.append(min(1.0, response_rate))
        weights.append(W_RESPONSE_RATE)
    if quote_accuracy is not None:
        metrics.append(quote_accuracy)
        weights.append(W_QUOTE_ACCURACY)
    if on_time is not None:
        metrics.append(on_time)
        weights.append(W_ON_TIME)
    if cancellation_rate is not None:
        metrics.append(
            max(0.0, 1.0 - cancellation_rate)
        )  # Invert: lower cancel = higher score
        weights.append(W_CANCELLATION)
    if rma_rate is not None:
        metrics.append(max(0.0, 1.0 - rma_rate))  # Invert: lower RMA = higher score
        weights.append(W_RMA)
    if lead_time is not None:
        metrics.append(lead_time)
        weights.append(W_LEAD_TIME)
    if quote_conversion is not None:
        metrics.append(min(1.0, quote_conversion))
        weights.append(W_QUOTE_CONVERSION)
    if po_conversion is not None:
        metrics.append(min(1.0, po_conversion))
        weights.append(W_PO_CONVERSION)
    if avg_review_rating is not None:
        metrics.append(min(1.0, avg_review_rating))
        weights.append(W_REVIEW_RATING)

    if not weights:
        return None

    total_w = sum(weights)
    return round(sum(m * w for m, w in zip(metrics, weights)) / total_w, 4)


def compute_all_vendor_scorecards(db: Session) -> dict:
    """Batch compute and snapshot all vendor scorecards."""
    today = date.today()
    vendor_ids = [r[0] for r in db.query(VendorCard.id).all()]
    updated = 0
    skipped = 0

    for vid in vendor_ids:
        try:
            result = compute_vendor_scorecard(db, vid)
            if not result:
                continue

            # Upsert snapshot
            existing = (
                db.query(VendorMetricsSnapshot)
                .filter(
                    VendorMetricsSnapshot.vendor_card_id == vid,
                    VendorMetricsSnapshot.snapshot_date == today,
                )
                .first()
            )

            if existing:
                snap = existing
            else:
                snap = VendorMetricsSnapshot(vendor_card_id=vid, snapshot_date=today)
                db.add(snap)

            snap.response_rate = result["response_rate"]
            snap.quote_accuracy = result["quote_accuracy"]
            snap.on_time_delivery = result["on_time_delivery"]
            snap.cancellation_rate = result["cancellation_rate"]
            snap.rma_rate = result["rma_rate"]
            snap.lead_time_accuracy = result["lead_time_accuracy"]
            snap.quote_conversion = result["quote_conversion"]
            snap.po_conversion = result["po_conversion"]
            snap.avg_review_rating = result["avg_review_rating"]
            snap.composite_score = result["composite_score"]
            snap.interaction_count = result["interaction_count"]
            snap.is_sufficient_data = result["is_sufficient_data"]
            snap.rfqs_sent = result["rfqs_sent"]
            snap.rfqs_answered = result["rfqs_answered"]
            snap.pos_in_window = result["pos_in_window"]

            if result["is_sufficient_data"]:
                updated += 1
            else:
                skipped += 1

        except Exception as e:
            log.error(f"Vendor scorecard error for {vid}: {e}")
            db.rollback()
            continue

    db.commit()
    return {"updated": updated, "skipped_cold_start": skipped}


def get_vendor_scorecard_list(
    db: Session,
    sort_by: str = "composite_score",
    order: str = "desc",
    limit: int = 50,
    offset: int = 0,
    search: str | None = None,
) -> dict:
    """Return paginated vendor scorecards with latest snapshot."""
    from sqlalchemy import desc as sql_desc, asc as sql_asc

    # Subquery: latest snapshot date per vendor
    latest_sq = (
        db.query(
            VendorMetricsSnapshot.vendor_card_id,
            sqlfunc.max(VendorMetricsSnapshot.snapshot_date).label("max_date"),
        )
        .group_by(VendorMetricsSnapshot.vendor_card_id)
        .subquery()
    )

    q = (
        db.query(VendorMetricsSnapshot, VendorCard.display_name)
        .join(
            latest_sq,
            and_(
                VendorMetricsSnapshot.vendor_card_id == latest_sq.c.vendor_card_id,
                VendorMetricsSnapshot.snapshot_date == latest_sq.c.max_date,
            ),
        )
        .join(VendorCard, VendorCard.id == VendorMetricsSnapshot.vendor_card_id)
    )

    if search:
        q = q.filter(VendorCard.display_name.ilike(f"%{search}%"))

    # Count before pagination
    total = q.count()

    # Sort
    sort_col = getattr(
        VendorMetricsSnapshot, sort_by, VendorMetricsSnapshot.composite_score
    )
    if order == "asc":
        q = q.order_by(sql_asc(sort_col).nulls_last())
    else:
        q = q.order_by(sql_desc(sort_col).nulls_last())

    rows = q.offset(offset).limit(limit).all()

    items = []
    for snap, vendor_name in rows:
        items.append(
            {
                "vendor_card_id": snap.vendor_card_id,
                "vendor_name": vendor_name,
                "snapshot_date": snap.snapshot_date.isoformat(),
                "response_rate": snap.response_rate,
                "quote_accuracy": snap.quote_accuracy,
                "on_time_delivery": snap.on_time_delivery,
                "cancellation_rate": snap.cancellation_rate,
                "rma_rate": snap.rma_rate,
                "lead_time_accuracy": snap.lead_time_accuracy,
                "quote_conversion": snap.quote_conversion,
                "po_conversion": snap.po_conversion,
                "avg_review_rating": snap.avg_review_rating,
                "composite_score": snap.composite_score,
                "interaction_count": snap.interaction_count,
                "is_sufficient_data": snap.is_sufficient_data,
            }
        )

    return {"items": items, "total": total}


def get_vendor_scorecard_detail(db: Session, vendor_card_id: int) -> dict:
    """Return latest snapshot + 3-month trend for a vendor."""
    vc = db.get(VendorCard, vendor_card_id)
    if not vc:
        return {}

    cutoff = date.today() - timedelta(days=90)
    snapshots = (
        db.query(VendorMetricsSnapshot)
        .filter(
            VendorMetricsSnapshot.vendor_card_id == vendor_card_id,
            VendorMetricsSnapshot.snapshot_date >= cutoff,
        )
        .order_by(VendorMetricsSnapshot.snapshot_date.desc())
        .limit(90)
        .all()
    )

    if not snapshots:
        return {
            "vendor_card_id": vendor_card_id,
            "vendor_name": vc.display_name,
            "latest": None,
            "trend": [],
        }

    latest = snapshots[0]
    trend = [
        {
            "date": s.snapshot_date.isoformat(),
            "composite_score": s.composite_score,
            "response_rate": s.response_rate,
        }
        for s in reversed(snapshots)
    ]

    return {
        "vendor_card_id": vendor_card_id,
        "vendor_name": vc.display_name,
        "latest": {
            "snapshot_date": latest.snapshot_date.isoformat(),
            "response_rate": latest.response_rate,
            "quote_accuracy": latest.quote_accuracy,
            "on_time_delivery": latest.on_time_delivery,
            "cancellation_rate": latest.cancellation_rate,
            "rma_rate": latest.rma_rate,
            "lead_time_accuracy": latest.lead_time_accuracy,
            "quote_conversion": latest.quote_conversion,
            "po_conversion": latest.po_conversion,
            "avg_review_rating": latest.avg_review_rating,
            "composite_score": latest.composite_score,
            "interaction_count": latest.interaction_count,
            "is_sufficient_data": latest.is_sufficient_data,
            "rfqs_sent": latest.rfqs_sent,
            "rfqs_answered": latest.rfqs_answered,
            "pos_in_window": latest.pos_in_window,
        },
        "trend": trend,
    }


# ── Buyer Leaderboard ─────────────────────────────────────────────────


def compute_buyer_leaderboard(db: Session, month: date) -> dict:
    """Compute buyer leaderboard for a given month."""
    # Normalize to first of month
    month_start = month.replace(day=1)
    if month_start.month == 12:
        month_end = month_start.replace(year=month_start.year + 1, month=1)
    else:
        month_end = month_start.replace(month=month_start.month + 1)

    month_start_dt = datetime(
        month_start.year, month_start.month, month_start.day, tzinfo=timezone.utc
    )
    month_end_dt = datetime(
        month_end.year, month_end.month, month_end.day, tzinfo=timezone.utc
    )

    # Grace period: last 7 days of previous month
    grace_start_dt = month_start_dt - timedelta(days=GRACE_DAYS)

    # Get all buyers
    buyers = db.query(User).filter(User.role == "buyer").all()

    # Collect all offer_ids that appear in quotes and buy plans (for status checks)
    # Quotes: line_items JSON contains offer_id
    all_quotes = (
        db.query(Quote)
        .filter(
            Quote.status.in_(["sent", "won", "lost"]),
        )
        .all()
    )
    quoted_offer_ids = set()
    for q in all_quotes:
        for item in q.line_items or []:
            oid = item.get("offer_id")
            if oid:
                quoted_offer_ids.add(oid)

    # Buy plans: line_items JSON contains offer_id
    all_buyplans = db.query(BuyPlan).all()
    buyplan_offer_ids = set()
    po_confirmed_offer_ids = set()
    for bp in all_buyplans:
        for item in bp.line_items or []:
            oid = item.get("offer_id")
            if oid:
                buyplan_offer_ids.add(oid)
                if bp.status in ("po_confirmed", "complete"):
                    po_confirmed_offer_ids.add(oid)

    entries = []
    for buyer in buyers:
        # Offers logged in this month (plus grace period offers that advanced)
        month_offers = (
            db.query(Offer)
            .filter(
                Offer.entered_by_id == buyer.id,
                Offer.created_at >= month_start_dt,
                Offer.created_at < month_end_dt,
            )
            .all()
        )

        # Grace offers: created in last 7 days of prev month
        grace_offers = (
            db.query(Offer)
            .filter(
                Offer.entered_by_id == buyer.id,
                Offer.created_at >= grace_start_dt,
                Offer.created_at < month_start_dt,
            )
            .all()
        )

        # Grace offers only count if they advanced during this month
        grace_advanced = []
        for o in grace_offers:
            if o.id in quoted_offer_ids or o.id in buyplan_offer_ids:
                grace_advanced.append(o)

        all_offers = month_offers + grace_advanced
        offer_ids = {o.id for o in all_offers}

        logged = len(all_offers)
        quoted = sum(1 for oid in offer_ids if oid in quoted_offer_ids)
        in_buyplan = sum(1 for oid in offer_ids if oid in buyplan_offer_ids)
        po_confirmed = sum(1 for oid in offer_ids if oid in po_confirmed_offer_ids)

        # Stock lists uploaded in this month (unique, non-duplicate)
        stock_uploaded = (
            db.query(StockListHash)
            .filter(
                StockListHash.user_id == buyer.id,
                StockListHash.first_seen_at >= month_start_dt,
                StockListHash.first_seen_at < month_end_dt,
            )
            .count()
        )

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
    """Return leaderboard for a given month."""
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

    return [
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
        }
        for snap, user_name in rows
    ]


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
    content = "|".join(mpns)
    return hashlib.sha256(content.encode()).hexdigest()


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
