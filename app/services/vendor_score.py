"""Unified Vendor Score — Order Advancement Based.

Single source of truth for vendor scoring. Replaces engagement_scorer logic.

Score is based on how far a vendor's offers advance through the pipeline:
  - Offer entered: 1 pt
  - Used in sent/won/lost Quote: 3 pts
  - Awarded in BuyPlan (non-cancelled): 5 pts
  - PO Confirmed (po_entered/po_confirmed/complete): 8 pts

Only the highest stage reached per offer counts. The advancement score is
blended 80/20 with buyer review ratings to produce the final vendor_score.

Cold start: vendors with < 5 offers get vendor_score=None, is_new_vendor=True.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

MIN_OFFERS_FOR_SCORE = 5
ADVANCEMENT_WEIGHT = 0.80
REVIEW_WEIGHT = 0.20
MAX_STAGE_POINTS = 8

# BuyPlan statuses that count as PO confirmed
PO_CONFIRMED_STATUSES = {"po_entered", "po_confirmed", "complete"}
# BuyPlan statuses that count as awarded (any non-cancelled)
AWARDED_STATUSES = {
    "pending_approval", "approved", "po_entered", "po_confirmed", "complete",
}
# Quote statuses that count as "used in quote"
QUOTE_USED_STATUSES = {"sent", "won", "lost"}


def compute_vendor_score(
    offer_count: int,
    stage_points_sum: float,
    avg_rating: float | None,
) -> dict:
    """Pure calculation — no DB access.

    Returns:
        {
            "vendor_score": float|None (0-100),
            "advancement_score": float|None (0-100),
            "is_new_vendor": bool,
        }
    """
    if offer_count < MIN_OFFERS_FOR_SCORE:
        return {
            "vendor_score": None,
            "advancement_score": None,
            "is_new_vendor": True,
        }

    advancement_score = (stage_points_sum / (offer_count * MAX_STAGE_POINTS)) * 100
    advancement_score = min(100.0, max(0.0, advancement_score))

    review_factor = (avg_rating / 5.0) * 100 if avg_rating is not None else 50.0
    review_factor = min(100.0, max(0.0, review_factor))

    vendor_score = advancement_score * ADVANCEMENT_WEIGHT + review_factor * REVIEW_WEIGHT
    vendor_score = round(min(100.0, max(0.0, vendor_score)), 1)

    return {
        "vendor_score": vendor_score,
        "advancement_score": round(advancement_score, 1),
        "is_new_vendor": False,
    }


def compute_single_vendor_score(db: Session, vendor_card_id: int) -> dict:
    """Compute vendor score for one vendor. Returns same dict as compute_vendor_score."""
    from app.models import BuyPlan, Offer, Quote, VendorCard, VendorReview

    card = db.get(VendorCard, vendor_card_id)
    if not card:
        return {"vendor_score": None, "advancement_score": None, "is_new_vendor": True}

    # Get all offers for this vendor
    offers = (
        db.query(Offer.id)
        .filter(Offer.vendor_card_id == vendor_card_id)
        .all()
    )
    offer_ids = {o.id for o in offers}

    if not offer_ids:
        # Fallback: match by normalized name
        from app.vendor_utils import normalize_vendor_name
        norm = card.normalized_name
        offers = (
            db.query(Offer.id)
            .filter(func.lower(Offer.vendor_name) == norm)
            .all()
        )
        offer_ids = {o.id for o in offers}

    offer_count = len(offer_ids)

    if offer_count < MIN_OFFERS_FOR_SCORE:
        return {"vendor_score": None, "advancement_score": None, "is_new_vendor": True}

    # Build sets of offer_ids at each stage
    quote_offer_ids = _get_quote_offer_ids(db, offer_ids)
    awarded_offer_ids = _get_buyplan_offer_ids(db, offer_ids, AWARDED_STATUSES)
    po_confirmed_offer_ids = _get_buyplan_offer_ids(db, offer_ids, PO_CONFIRMED_STATUSES)

    stage_points_sum = _calc_stage_points(
        offer_ids, quote_offer_ids, awarded_offer_ids, po_confirmed_offer_ids
    )

    # Avg rating
    avg_rating = (
        db.query(func.avg(VendorReview.rating))
        .filter(VendorReview.vendor_card_id == vendor_card_id)
        .scalar()
    )
    if avg_rating is not None:
        avg_rating = float(avg_rating)

    return compute_vendor_score(offer_count, stage_points_sum, avg_rating)


async def compute_all_vendor_scores(db: Session) -> dict:
    """Batch recompute vendor scores for ALL VendorCards.

    Preloads quote/buyplan offer-id sets for efficiency.
    Returns: {"updated": int, "skipped": int}
    """
    from app.models import BuyPlan, Offer, Quote, VendorCard, VendorReview
    from app.vendor_utils import normalize_vendor_name

    now = datetime.now(timezone.utc)

    # ── Preload all offer_ids grouped by vendor_card_id ──
    offer_rows = (
        db.query(Offer.id, Offer.vendor_card_id, Offer.vendor_name)
        .all()
    )

    # Map vendor_card_id → set of offer_ids
    card_offer_ids: dict[int, set[int]] = {}
    # Map normalized_name → set of offer_ids (for vendors matched by name)
    name_offer_ids: dict[str, set[int]] = {}
    all_offer_ids: set[int] = set()

    for oid, vcid, vname in offer_rows:
        all_offer_ids.add(oid)
        if vcid:
            card_offer_ids.setdefault(vcid, set()).add(oid)
        if vname:
            norm = normalize_vendor_name(vname)
            name_offer_ids.setdefault(norm, set()).add(oid)

    # ── Preload quote line_items to find offer_ids used in quotes ──
    quotes = (
        db.query(Quote.line_items, Quote.status)
        .filter(Quote.status.in_(QUOTE_USED_STATUSES))
        .all()
    )
    quote_offer_id_set: set[int] = set()
    for line_items, _status in quotes:
        if line_items:
            for li in line_items:
                oid = li.get("offer_id")
                if oid:
                    quote_offer_id_set.add(oid)

    # ── Preload buyplan line_items ──
    buyplans = (
        db.query(BuyPlan.line_items, BuyPlan.status)
        .filter(BuyPlan.status != "cancelled")
        .all()
    )
    awarded_offer_id_set: set[int] = set()
    po_confirmed_offer_id_set: set[int] = set()
    for line_items, bp_status in buyplans:
        if line_items:
            for li in line_items:
                oid = li.get("offer_id")
                if oid:
                    if bp_status in AWARDED_STATUSES:
                        awarded_offer_id_set.add(oid)
                    if bp_status in PO_CONFIRMED_STATUSES:
                        po_confirmed_offer_id_set.add(oid)

    # ── Preload review averages ──
    review_rows = (
        db.query(
            VendorReview.vendor_card_id,
            func.avg(VendorReview.rating),
        )
        .group_by(VendorReview.vendor_card_id)
        .all()
    )
    review_avg_map = {cid: float(avg) for cid, avg in review_rows if avg is not None}

    # ── Iterate all VendorCards in batches ──
    total_count = db.query(func.count(VendorCard.id)).scalar() or 0
    updated = 0
    skipped = 0
    BATCH_SIZE = 1000

    for batch_offset in range(0, total_count, BATCH_SIZE):
        cards = (
            db.query(VendorCard)
            .order_by(VendorCard.id)
            .offset(batch_offset)
            .limit(BATCH_SIZE)
            .all()
        )

        for card in cards:
            # Gather offer_ids for this vendor
            oids = card_offer_ids.get(card.id, set())
            # Merge offers matched by name
            name_oids = name_offer_ids.get(card.normalized_name, set())
            oids = oids | name_oids

            offer_count = len(oids)

            # Calculate stage points
            stage_points_sum = _calc_stage_points(
                oids, quote_offer_id_set, awarded_offer_id_set, po_confirmed_offer_id_set
            )

            avg_rating = review_avg_map.get(card.id)

            result = compute_vendor_score(offer_count, stage_points_sum, avg_rating)

            card.vendor_score = result["vendor_score"]
            card.advancement_score = result["advancement_score"]
            card.is_new_vendor = result["is_new_vendor"]
            card.vendor_score_computed_at = now

            # Keep engagement_score in sync for backward compat
            if result["vendor_score"] is not None:
                card.engagement_score = result["vendor_score"]

            updated += 1

        try:
            db.flush()
        except Exception as e:
            log.error(f"Vendor scoring flush failed at offset {batch_offset}: {e}")

    try:
        db.commit()
        log.info(f"Vendor scoring: updated {updated} vendor cards, skipped {skipped}")
    except Exception as e:
        log.error(f"Vendor scoring commit failed: {e}")
        db.rollback()
        return {"updated": 0, "skipped": skipped}

    return {"updated": updated, "skipped": skipped}


def _get_quote_offer_ids(db: Session, offer_ids: set[int]) -> set[int]:
    """Get offer_ids that appear in sent/won/lost Quote line_items."""
    from app.models import Quote

    quotes = (
        db.query(Quote.line_items)
        .filter(Quote.status.in_(QUOTE_USED_STATUSES))
        .all()
    )
    found: set[int] = set()
    for (line_items,) in quotes:
        if line_items:
            for li in line_items:
                oid = li.get("offer_id")
                if oid and oid in offer_ids:
                    found.add(oid)
    return found


def _get_buyplan_offer_ids(
    db: Session, offer_ids: set[int], statuses: set[str]
) -> set[int]:
    """Get offer_ids that appear in BuyPlan line_items with given statuses."""
    from app.models import BuyPlan

    plans = (
        db.query(BuyPlan.line_items)
        .filter(BuyPlan.status.in_(statuses))
        .all()
    )
    found: set[int] = set()
    for (line_items,) in plans:
        if line_items:
            for li in line_items:
                oid = li.get("offer_id")
                if oid and oid in offer_ids:
                    found.add(oid)
    return found


def _calc_stage_points(
    offer_ids: set[int],
    quote_offer_ids: set[int],
    awarded_offer_ids: set[int],
    po_confirmed_offer_ids: set[int],
) -> float:
    """Calculate total stage points for a set of offers.

    Only the highest stage reached per offer counts.
    """
    total = 0.0
    for oid in offer_ids:
        if oid in po_confirmed_offer_ids:
            total += 8
        elif oid in awarded_offer_ids:
            total += 5
        elif oid in quote_offer_ids:
            total += 3
        else:
            total += 1
    return total
