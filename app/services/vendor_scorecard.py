"""Vendor Scorecard — 6 metrics over 90-day rolling window with cold-start protection.

Called by: scheduler.py (daily), routers/performance.py (on-demand)
Depends on: models, database
"""

from datetime import date, datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import and_, or_
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ..models import (
    BuyPlan,
    BuyPlanLine,
    Contact,
    Offer,
    Quote,
    VendorCard,
    VendorMetricsSnapshot,
    VendorResponse,
    VendorReview,
)
from ..utils.sql_helpers import escape_like

# ── Constants ──────────────────────────────────────────────────────────
VENDOR_WINDOW_DAYS = 90
COLD_START_THRESHOLD = 5

# Vendor composite weights (sum = 1.0)
W_RESPONSE_RATE = 0.25
W_QUOTE_CONVERSION = 0.25
W_PO_CONVERSION = 0.25
W_REVIEW_RATING = 0.25


def compute_vendor_scorecard(
    db: Session,
    vendor_card_id: int,
    window_days: int = VENDOR_WINDOW_DAYS,
    *,
    quoted_offer_ids: set[int] | None = None,
    po_offer_ids: set[int] | None = None,
) -> dict:
    """Compute all 6 metrics for a single vendor over the rolling window.

    When called from compute_all_vendor_scorecards(), pre-loaded lookup sets are passed
    in so we avoid re-querying all quotes/buy-plans per vendor. When called individually
    (single vendor endpoint), the sets are loaded on demand from the database.
    """
    vc = db.get(VendorCard, vendor_card_id)
    if not vc:
        return {}

    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    # ── 1. Response rate ──
    norm = (vc.normalized_name or "").lower()
    disp = vc.display_name or ""
    rfqs_sent = (
        db.query(sqlfunc.count(Contact.id))
        .filter(
            or_(
                Contact.vendor_name_normalized == norm,
                Contact.vendor_name == disp,
            ),
            Contact.contact_type == "email",
            Contact.created_at >= cutoff,
        )
        .scalar()
        or 0
    )

    rfqs_answered = 0
    domains = [vc.domain.lower()] if vc.domain else []
    for alias in vc.domain_aliases or []:
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
    total_offers = len(offers_in_window)
    offers_in_quotes = 0
    if total_offers > 0:
        if quoted_offer_ids is None:
            quoted_offer_ids = set()
            for (items,) in (
                db.query(Quote.line_items).filter(Quote.status.in_(["sent", "won", "lost"])).limit(10000).all()
            ):
                for item in items or []:
                    oid = item.get("offer_id")
                    if oid:
                        quoted_offer_ids.add(oid)

        for o in offers_in_window:
            if o.id in quoted_offer_ids:
                offers_in_quotes += 1
    quote_conversion = (offers_in_quotes / total_offers) if total_offers > 0 else None

    # ── 8. PO conversion rate ──
    offers_to_po = 0
    if total_offers > 0:
        if po_offer_ids is None:
            po_offer_ids = set()
            for (offer_id,) in (
                db.query(BuyPlanLine.offer_id)
                .join(BuyPlan, BuyPlanLine.buy_plan_id == BuyPlan.id)
                .filter(BuyPlan.status.in_(["completed"]))
                .filter(BuyPlanLine.offer_id.isnot(None))
                .limit(10000)
                .all()
            ):
                po_offer_ids.add(offer_id)

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
    avg_review_rating = float(avg_rating_row) / 5.0 if avg_rating_row else None  # Normalize to 0-1

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
    for (items,) in db.query(Quote.line_items).filter(Quote.status.in_(["sent", "won", "lost"])).limit(10000).all():
        for item in items or []:
            oid = item.get("offer_id")
            if oid:
                quoted_offer_ids.add(oid)

    po_offer_ids: set[int] = set()
    for (offer_id,) in (
        db.query(BuyPlanLine.offer_id)
        .join(BuyPlan, BuyPlanLine.buy_plan_id == BuyPlan.id)
        .filter(BuyPlan.status.in_(["completed"]))
        .filter(BuyPlanLine.offer_id.isnot(None))
        .limit(10000)
        .all()
    ):
        po_offer_ids.add(offer_id)

    # Process vendor IDs in chunks of 500 to avoid loading all at once
    CHUNK_SIZE = 500
    total_vendors = db.query(VendorCard.id).count()

    for chunk_offset in range(0, total_vendors, CHUNK_SIZE):
        vendor_ids_chunk = [
            r[0] for r in db.query(VendorCard.id).order_by(VendorCard.id).offset(chunk_offset).limit(CHUNK_SIZE).all()
        ]

        for vid in vendor_ids_chunk:
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
                logger.error(f"Vendor scorecard error for {vid}: {e}")
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
    from sqlalchemy import asc as sql_asc
    from sqlalchemy import desc as sql_desc

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
        q = q.filter(VendorCard.display_name.ilike(f"%{escape_like(search)}%"))

    # Count before pagination
    total = q.count()

    # Sort — whitelist allowed columns to prevent attribute probing
    _ALLOWED_SORT_COLS = {
        "composite_score",
        "response_rate",
        "quote_conversion",
        "po_conversion",
        "avg_review_rating",
        "interaction_count",
        "snapshot_date",
    }
    if sort_by not in _ALLOWED_SORT_COLS:
        sort_by = "composite_score"
    sort_col = getattr(VendorMetricsSnapshot, sort_by)
    order = order if order in ("asc", "desc") else "desc"
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
