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

from sqlalchemy import func as sqlfunc, and_, or_
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
    Company,
    CustomerSite,
    SiteContact,
    ActivityLog,
    Requisition,
    ProactiveOffer,
)

log = logging.getLogger("avail.performance")

# ── Constants ──────────────────────────────────────────────────────────
VENDOR_WINDOW_DAYS = 90
COLD_START_THRESHOLD = 5

# Vendor composite weights (sum = 1.0)
# Only metrics computable from AVAIL data (no ERP required)
W_RESPONSE_RATE = 0.25
W_QUOTE_CONVERSION = 0.25
W_PO_CONVERSION = 0.25
W_REVIEW_RATING = 0.25

# Buyer point multipliers
PTS_LOGGED = 1
PTS_QUOTED = 3
PTS_BUYPLAN = 5
PTS_PO_CONFIRMED = 8
PTS_STOCK_LIST = 2

GRACE_DAYS = 7


# ── Vendor Scorecard ──────────────────────────────────────────────────


def compute_vendor_scorecard(
    db: Session,
    vendor_card_id: int,
    window_days: int = VENDOR_WINDOW_DAYS,
    *,
    quoted_offer_ids: set[int] | None = None,
    po_offer_ids: set[int] | None = None,
) -> dict:
    """Compute all 6 metrics for a single vendor over the rolling window.

    When called from compute_all_vendor_scorecards(), pre-loaded lookup sets
    are passed in so we avoid re-querying all quotes/buy-plans per vendor.
    When called individually (single vendor endpoint), the sets are loaded
    on demand from the database.
    """
    vc = db.get(VendorCard, vendor_card_id)
    if not vc:
        return {}

    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    # ── 1. Response rate ──
    # Match by normalized name (case-insensitive) and also by display name
    norm = (vc.normalized_name or "").lower()
    disp = vc.display_name or ""
    rfqs_sent = (
        db.query(sqlfunc.count(Contact.id))
        .filter(
            or_(
                sqlfunc.lower(sqlfunc.trim(Contact.vendor_name)) == norm,
                Contact.vendor_name == disp,
            ),
            Contact.contact_type == "email",
            Contact.created_at >= cutoff,
        )
        .scalar()
        or 0
    )

    # Match responses by email domain (vendor_name stores person name, not company)
    rfqs_answered = 0
    domains = [vc.domain.lower()] if vc.domain else []
    for alias in (vc.domain_aliases or []):
        if alias:
            domains.append(alias.lower())
    if domains:
        domain_filters = [VendorResponse.vendor_email.ilike(f"%@{d}") for d in domains]
        rfqs_answered = (
            db.query(sqlfunc.count(VendorResponse.id))
            .filter(
                or_(*domain_filters),
                VendorResponse.status != "noise",
                VendorResponse.received_at >= cutoff,
            )
            .scalar()
            or 0
        )

    response_rate = (rfqs_answered / rfqs_sent) if rfqs_sent > 0 else None

    # ── Offers in window (used for quote and PO conversion) ──
    offers_in_window = (
        db.query(Offer)
        .filter(
            Offer.vendor_card_id == vendor_card_id,
            Offer.created_at >= cutoff,
            Offer.unit_price.isnot(None),
        )
        .all()
    )

    # ── 2. Quote conversion rate ──
    # What % of this vendor's offers made it into quotes?
    total_offers = len(offers_in_window)
    offers_in_quotes = 0
    if total_offers > 0:
        # Build lookup on demand if not pre-loaded (single-vendor call)
        if quoted_offer_ids is None:
            quoted_offer_ids = set()
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
                    if oid:
                        quoted_offer_ids.add(oid)

        for o in offers_in_window:
            if o.id in quoted_offer_ids:
                offers_in_quotes += 1
    quote_conversion = (offers_in_quotes / total_offers) if total_offers > 0 else None

    # ── 8. PO conversion rate ──
    # What % of this vendor's offers made it to PO confirmed/complete?
    offers_to_po = 0
    if total_offers > 0:
        # Build lookup on demand if not pre-loaded (single-vendor call)
        if po_offer_ids is None:
            po_offer_ids = set()
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
                    if oid:
                        po_offer_ids.add(oid)

        for o in offers_in_window:
            if o.id in po_offer_ids:
                offers_to_po += 1
    po_conversion = (offers_to_po / total_offers) if total_offers > 0 else None

    # ── 5. Average buyer review rating ──
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

    is_sufficient = interaction_count >= COLD_START_THRESHOLD

    # ── Composite score ──
    composite = (
        _compute_composite(
            response_rate,
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
        "quote_conversion": quote_conversion,
        "po_conversion": po_conversion,
        "avg_review_rating": avg_review_rating,
        "composite_score": composite,
        "interaction_count": interaction_count,
        "is_sufficient_data": is_sufficient,
        "rfqs_sent": rfqs_sent,
        "rfqs_answered": rfqs_answered,
    }


def _compute_composite(
    response_rate,
    quote_conversion=None,
    po_conversion=None,
    avg_review_rating=None,
) -> float:
    """Weighted average of available metrics."""
    metrics = []
    weights = []

    if response_rate is not None:
        metrics.append(min(1.0, response_rate))
        weights.append(W_RESPONSE_RATE)
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
    updated = 0
    skipped = 0

    # ── Preload quote and buy-plan offer-id lookups ONCE ──
    quoted_offer_ids: set[int] = set()
    all_quotes = (
        db.query(Quote)
        .filter(Quote.status.in_(["sent", "won", "lost"]))
        .all()
    )
    for q in all_quotes:
        for item in q.line_items or []:
            oid = item.get("offer_id")
            if oid:
                quoted_offer_ids.add(oid)

    po_offer_ids: set[int] = set()
    po_plans = (
        db.query(BuyPlan)
        .filter(BuyPlan.status.in_(["po_entered", "po_confirmed", "complete"]))
        .all()
    )
    for bp in po_plans:
        for item in bp.line_items or []:
            oid = item.get("offer_id")
            if oid:
                po_offer_ids.add(oid)

    # Process vendor IDs in chunks of 500 to avoid loading all at once
    CHUNK_SIZE = 500
    total_vendors = db.query(VendorCard.id).count()

    for chunk_offset in range(0, total_vendors, CHUNK_SIZE):
        vendor_ids_chunk = [
            r[0]
            for r in db.query(VendorCard.id)
            .order_by(VendorCard.id)
            .offset(chunk_offset)
            .limit(CHUNK_SIZE)
            .all()
        ]

        for vid in vendor_ids_chunk:
            # Use a savepoint so a single vendor failure doesn't roll back
            # previously computed scorecards in this transaction.
            savepoint = db.begin_nested()
            try:
                result = compute_vendor_scorecard(
                    db,
                    vid,
                    quoted_offer_ids=quoted_offer_ids,
                    po_offer_ids=po_offer_ids,
                )
                if not result:
                    savepoint.rollback()
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
                snap.quote_conversion = result["quote_conversion"]
                snap.po_conversion = result["po_conversion"]
                snap.avg_review_rating = result["avg_review_rating"]
                snap.composite_score = result["composite_score"]
                snap.interaction_count = result["interaction_count"]
                snap.is_sufficient_data = result["is_sufficient_data"]
                snap.rfqs_sent = result["rfqs_sent"]
                snap.rfqs_answered = result["rfqs_answered"]

                if result["is_sufficient_data"]:
                    updated += 1
                else:
                    skipped += 1

                savepoint.commit()

            except Exception as e:
                log.error(f"Vendor scorecard error for {vid}: {e}")
                savepoint.rollback()
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
    buyers = db.query(User).filter(User.role.in_(["buyer", "trader"])).all()

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
        result.append({
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
        })
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


# ── Salesperson Scorecard ────────────────────────────────────────────


def get_salesperson_scorecard(db: Session, month: date) -> dict:
    """Compute 12 activity metrics for all active users (monthly + YTD)."""
    month_start = month.replace(day=1)
    if month_start.month == 12:
        month_end = month_start.replace(year=month_start.year + 1, month=1)
    else:
        month_end = month_start.replace(month=month_start.month + 1)

    ytd_start = date(month_start.year, 1, 1)

    month_start_dt = datetime(
        month_start.year, month_start.month, month_start.day, tzinfo=timezone.utc
    )
    month_end_dt = datetime(
        month_end.year, month_end.month, month_end.day, tzinfo=timezone.utc
    )
    ytd_start_dt = datetime(
        ytd_start.year, ytd_start.month, ytd_start.day, tzinfo=timezone.utc
    )

    users = db.query(User).filter(User.is_active == True).all()  # noqa: E712

    user_ids = [u.id for u in users]

    # Batch compute all metrics in 2 rounds instead of N×2×12 queries
    monthly_batch = _salesperson_metrics_batch(db, user_ids, month_start_dt, month_end_dt)
    ytd_batch = _salesperson_metrics_batch(db, user_ids, ytd_start_dt, month_end_dt)

    entries = []
    for user in users:
        entries.append({
            "user_id": user.id,
            "user_name": user.name or user.email,
            "monthly": monthly_batch.get(user.id, {}),
            "ytd": ytd_batch.get(user.id, {}),
        })

    # Default sort: won_revenue descending
    entries.sort(key=lambda e: float(e["monthly"]["won_revenue"] or 0), reverse=True)

    return {
        "month": month_start.isoformat(),
        "year": month_start.year,
        "entries": entries,
    }


def _salesperson_metrics_batch(
    db: Session, user_ids: list[int], start_dt: datetime, end_dt: datetime
) -> dict[int, dict]:
    """Compute all 12 metrics for multiple users over a date range in batch.

    Returns {user_id: metrics_dict} — replaces N×12 individual queries with 6 batch queries.
    """
    from sqlalchemy import case

    # Initialize results for all users
    results = {uid: {
        "new_accounts": 0, "new_contacts": 0, "calls_made": 0, "emails_sent": 0,
        "requisitions_entered": 0, "quotes_sent": 0, "orders_won": 0,
        "won_revenue": 0.0, "proactive_sent": 0, "proactive_converted": 0,
        "proactive_revenue": 0.0, "boms_uploaded": 0,
    } for uid in user_ids}

    if not user_ids:
        return results

    # Batch 1: Company new accounts
    for uid, cnt in (
        db.query(Company.account_owner_id, sqlfunc.count(Company.id))
        .filter(Company.account_owner_id.in_(user_ids), Company.created_at >= start_dt, Company.created_at < end_dt)
        .group_by(Company.account_owner_id)
        .all()
    ):
        results[uid]["new_accounts"] = cnt

    # Batch 2: New contacts via CustomerSite
    for uid, cnt in (
        db.query(CustomerSite.owner_id, sqlfunc.count(SiteContact.id))
        .join(CustomerSite, CustomerSite.id == SiteContact.customer_site_id)
        .filter(CustomerSite.owner_id.in_(user_ids), SiteContact.created_at >= start_dt, SiteContact.created_at < end_dt)
        .group_by(CustomerSite.owner_id)
        .all()
    ):
        results[uid]["new_contacts"] = cnt

    # Batch 3: ActivityLog — calls
    for uid, cnt in (
        db.query(ActivityLog.user_id, sqlfunc.count(ActivityLog.id))
        .filter(ActivityLog.user_id.in_(user_ids), ActivityLog.activity_type == "call_outbound",
                ActivityLog.created_at >= start_dt, ActivityLog.created_at < end_dt)
        .group_by(ActivityLog.user_id)
        .all()
    ):
        results[uid]["calls_made"] = cnt

    # Batch 4: Contacts — emails sent
    for uid, cnt in (
        db.query(Contact.user_id, sqlfunc.count(Contact.id))
        .filter(Contact.user_id.in_(user_ids), Contact.contact_type == "email",
                Contact.created_at >= start_dt, Contact.created_at < end_dt)
        .group_by(Contact.user_id)
        .all()
    ):
        results[uid]["emails_sent"] = cnt

    # Batch 5: Requisitions entered
    for uid, cnt in (
        db.query(Requisition.created_by, sqlfunc.count(Requisition.id))
        .filter(Requisition.created_by.in_(user_ids),
                Requisition.created_at >= start_dt, Requisition.created_at < end_dt)
        .group_by(Requisition.created_by)
        .all()
    ):
        results[uid]["requisitions_entered"] = cnt

    # Batch 6: Quotes — sent, won, won_revenue (single query with conditional aggregation)
    for uid, q_sent, q_won, q_rev in (
        db.query(
            Quote.created_by_id,
            sqlfunc.count(case((and_(Quote.sent_at.isnot(None), Quote.sent_at >= start_dt, Quote.sent_at < end_dt), 1))),
            sqlfunc.count(case((and_(Quote.result == "won", Quote.result_at >= start_dt, Quote.result_at < end_dt), 1))),
            sqlfunc.coalesce(sqlfunc.sum(case((and_(Quote.result == "won", Quote.result_at >= start_dt, Quote.result_at < end_dt), Quote.won_revenue))), 0),
        )
        .filter(Quote.created_by_id.in_(user_ids))
        .group_by(Quote.created_by_id)
        .all()
    ):
        results[uid]["quotes_sent"] = q_sent
        results[uid]["orders_won"] = q_won
        results[uid]["won_revenue"] = float(q_rev)

    # Batch 7: ProactiveOffer — sent, converted, revenue (single query)
    for uid, p_sent, p_conv, p_rev in (
        db.query(
            ProactiveOffer.salesperson_id,
            sqlfunc.count(case((and_(ProactiveOffer.sent_at >= start_dt, ProactiveOffer.sent_at < end_dt), 1))),
            sqlfunc.count(case((and_(ProactiveOffer.status == "converted", ProactiveOffer.converted_at >= start_dt, ProactiveOffer.converted_at < end_dt), 1))),
            sqlfunc.coalesce(sqlfunc.sum(case((and_(ProactiveOffer.status == "converted", ProactiveOffer.converted_at >= start_dt, ProactiveOffer.converted_at < end_dt), ProactiveOffer.total_sell))), 0),
        )
        .filter(ProactiveOffer.salesperson_id.in_(user_ids))
        .group_by(ProactiveOffer.salesperson_id)
        .all()
    ):
        results[uid]["proactive_sent"] = p_sent
        results[uid]["proactive_converted"] = p_conv
        results[uid]["proactive_revenue"] = float(p_rev)

    # Batch 8: BOMs uploaded
    for uid, cnt in (
        db.query(StockListHash.user_id, sqlfunc.count(StockListHash.id))
        .filter(StockListHash.user_id.in_(user_ids),
                StockListHash.first_seen_at >= start_dt, StockListHash.first_seen_at < end_dt)
        .group_by(StockListHash.user_id)
        .all()
    ):
        results[uid]["boms_uploaded"] = cnt

    return results


def _salesperson_metrics(
    db: Session, user_id: int, start_dt: datetime, end_dt: datetime
) -> dict:
    """Compute all 12 metrics for a single user over a date range.

    Delegates to batch function for consistency.
    """
    batch = _salesperson_metrics_batch(db, [user_id], start_dt, end_dt)
    return batch.get(user_id, {
        "new_accounts": 0, "new_contacts": 0, "calls_made": 0, "emails_sent": 0,
        "requisitions_entered": 0, "quotes_sent": 0, "orders_won": 0,
        "won_revenue": 0.0, "proactive_sent": 0, "proactive_converted": 0,
        "proactive_revenue": 0.0, "boms_uploaded": 0,
    })
