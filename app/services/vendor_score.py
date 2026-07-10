"""Unified Vendor Score — Order Advancement Based.

Single source of truth for vendor scoring. Replaces engagement_scorer logic.

Score is based on how far a vendor's offers advance through the pipeline:
  - Offer entered: 1 pt
  - Used in sent/won/lost Quote: 3 pts
  - Awarded in BuyPlan (pending/active/completed): 5 pts
  - PO Confirmed (completed plans): 8 pts

Only the highest stage reached per offer counts. The advancement score is
blended 80/20 with buyer review ratings to produce the final vendor_score.

Cold start: vendors with < 5 offers get vendor_score=None, is_new_vendor=True.
"""

from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.constants import BuyPlanStatus, QuoteStatus

MIN_OFFERS_FOR_SCORE = 5
ADVANCEMENT_WEIGHT = 0.80
REVIEW_WEIGHT = 0.20
MAX_STAGE_POINTS = 8

# ── Cancellation dampener (vendor fall-down weighs the score down) ──
# A "slow" cancel (PO sat > SLOW_CANCEL_THRESHOLD_DAYS before the vendor fell down)
# wasted more of our time, so it counts double a fast one. Pressure = weighted cancels
# / total POs; the dampener floors at MIN_DAMPENER so a vendor is never zeroed out.
SLOW_CANCEL_WEIGHT = 2.0
CANCEL_PENALTY_FACTOR = 0.5
MIN_DAMPENER = 0.4


def _cancel_dampener(cancel_count: int, slow_cancel_count: int, total_pos: int) -> float:
    """Multiplier in [MIN_DAMPENER, 1.0] reflecting cancellation pressure on the
    vendor."""
    if not total_pos or not cancel_count:
        return 1.0
    fast = cancel_count - slow_cancel_count
    weighted = fast * 1.0 + slow_cancel_count * SLOW_CANCEL_WEIGHT
    pressure = weighted / total_pos
    return max(MIN_DAMPENER, 1.0 - CANCEL_PENALTY_FACTOR * pressure)


# BuyPlan statuses that count as PO confirmed (V4: completed plans)
PO_CONFIRMED_STATUSES = {BuyPlanStatus.COMPLETED.value}
# BuyPlan statuses that count as awarded. Cancelled AND halted plans are NOT
# awarded — this set is the single source of truth and the SQL pre-filter in
# compute_all_vendor_scores excludes the same statuses so the layers agree.
AWARDED_STATUSES = {
    BuyPlanStatus.PENDING.value,
    BuyPlanStatus.ACTIVE.value,
    BuyPlanStatus.COMPLETED.value,
}
# BuyPlan statuses that are NOT awarded — excluded by the SQL pre-filter.
NON_AWARDED_STATUSES = {BuyPlanStatus.CANCELLED.value, BuyPlanStatus.HALTED.value}
# Quote statuses that count as "used in quote"
QUOTE_USED_STATUSES = {QuoteStatus.SENT.value, QuoteStatus.WON.value, QuoteStatus.LOST.value}


def compute_vendor_score(
    offer_count: int,
    stage_points_sum: float,
    avg_rating: float | None,
    *,
    cancel_count: int = 0,
    slow_cancel_count: int = 0,
    total_pos: int = 0,
) -> dict:
    """Pure calculation — no DB access.

    The cancellation aggregates are optional (default 0) so existing callers are
    unaffected; when present, the blended ``vendor_score`` is multiplied by
    ``_cancel_dampener`` (applied only when ``vendor_score`` is not None).

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

    # When no reviews exist, use advancement_score alone instead of
    # defaulting review_factor to 50 (which makes all scores converge)
    if avg_rating is not None:
        review_factor = min(100.0, max(0.0, (avg_rating / 5.0) * 100))
        vendor_score = advancement_score * ADVANCEMENT_WEIGHT + review_factor * REVIEW_WEIGHT
    else:
        vendor_score = advancement_score
    vendor_score = round(min(100.0, max(0.0, vendor_score)), 1)

    if vendor_score is not None:
        vendor_score = round(vendor_score * _cancel_dampener(cancel_count, slow_cancel_count, total_pos), 1)

    return {
        "vendor_score": vendor_score,
        "advancement_score": round(advancement_score, 1),
        "is_new_vendor": False,
    }


def compute_vendor_score_breakdown(
    offer_count: int,
    stage_points_sum: float,
    avg_rating: float | None,
    *,
    cancel_count: int = 0,
    slow_cancel_count: int = 0,
    total_pos: int = 0,
) -> list[tuple[str, float]]:
    """Deterministic driver contributions behind ``compute_vendor_score``.

    Re-runs the SAME advancement / review blend and cancellation dampener from the
    identical inputs and weight constants (``ADVANCEMENT_WEIGHT``/``REVIEW_WEIGHT``/
    ``_cancel_dampener``), returning post-dampener ``(label, contribution)`` pairs whose
    sum reconciles to ``vendor_score`` (within rounding). Below the cold-start floor
    (``MIN_OFFERS_FOR_SCORE``) there is no score, so the breakdown is empty.
    """
    if offer_count < MIN_OFFERS_FOR_SCORE:
        return []

    advancement_score = (stage_points_sum / (offer_count * MAX_STAGE_POINTS)) * 100
    advancement_score = min(100.0, max(0.0, advancement_score))
    dampener = _cancel_dampener(cancel_count, slow_cancel_count, total_pos)

    factors: list[tuple[str, float]] = []
    if avg_rating is not None:
        review_factor = min(100.0, max(0.0, (avg_rating / 5.0) * 100))
        factors.append(("Order advancement", round(advancement_score * ADVANCEMENT_WEIGHT * dampener, 1)))
        factors.append(("Buyer reviews", round(review_factor * REVIEW_WEIGHT * dampener, 1)))
    else:
        # No reviews → advancement carries the whole (un-blended) score.
        factors.append(("Order advancement", round(advancement_score * dampener, 1)))
    return factors


def _gather_vendor_score_inputs(db: Session, vendor_card_id: int) -> dict | None:
    """Gather the raw inputs behind ONE vendor's score.

    Returns the kwargs dict shared by ``compute_vendor_score`` and
    ``compute_vendor_score_breakdown`` — so the score and its hover breakdown are
    derived from byte-identical inputs — or ``None`` when the card is missing or sits
    below the cold-start offer floor (``MIN_OFFERS_FOR_SCORE``), i.e. there is no score
    to explain.
    """
    from app.models import Offer, VendorCard, VendorReview

    card = db.get(VendorCard, vendor_card_id)
    if not card:
        return None

    # Get all offers for this vendor
    offers = db.query(Offer.id).filter(Offer.vendor_card_id == vendor_card_id).all()
    offer_ids = {o.id for o in offers}

    if not offer_ids:
        # Fallback: match by normalized name
        norm = card.normalized_name
        offers = db.query(Offer.id).filter(Offer.vendor_name_normalized == norm).all()
        offer_ids = {o.id for o in offers}

    offer_count = len(offer_ids)
    if offer_count < MIN_OFFERS_FOR_SCORE:
        return None

    # Build sets of offer_ids at each stage
    quote_offer_ids = _get_quote_offer_ids(db, offer_ids)
    awarded_offer_ids = _get_buyplan_offer_ids(db, offer_ids, AWARDED_STATUSES)
    po_confirmed_offer_ids = _get_buyplan_offer_ids(db, offer_ids, PO_CONFIRMED_STATUSES)

    stage_points_sum = _calc_stage_points(offer_ids, quote_offer_ids, awarded_offer_ids, po_confirmed_offer_ids)

    # Avg rating
    avg_rating = db.query(func.avg(VendorReview.rating)).filter(VendorReview.vendor_card_id == vendor_card_id).scalar()
    if avg_rating is not None:
        avg_rating = float(avg_rating)

    # Cancellation pressure — same po_cancellations table the nightly batch reads.
    cancel_count, slow_cancel_count = _vendor_cancel_counts(db, vendor_card_id)

    return {
        "offer_count": offer_count,
        "stage_points_sum": stage_points_sum,
        "avg_rating": avg_rating,
        "cancel_count": cancel_count,
        "slow_cancel_count": slow_cancel_count,
        "total_pos": card.total_pos or 0,
    }


def compute_single_vendor_score(db: Session, vendor_card_id: int) -> dict:
    """Compute vendor score for one vendor.

    Returns same dict as compute_vendor_score.
    """
    inputs = _gather_vendor_score_inputs(db, vendor_card_id)
    if inputs is None:
        return {"vendor_score": None, "advancement_score": None, "is_new_vendor": True}
    return compute_vendor_score(**inputs)


def compute_single_vendor_score_breakdown(db: Session, vendor_card_id: int) -> list[tuple[str, float]]:
    """Deterministic (label, contribution) drivers behind ONE vendor's score.

    Threads the SAME inputs ``compute_single_vendor_score`` uses into
    ``compute_vendor_score_breakdown`` so the vendor-detail Score hover reconciles to the
    displayed score. Empty below the cold-start floor / for an unknown vendor.
    """
    inputs = _gather_vendor_score_inputs(db, vendor_card_id)
    if inputs is None:
        return []
    return compute_vendor_score_breakdown(**inputs)


def _vendor_cancel_counts(db: Session, vendor_card_id: int) -> tuple[int, int]:
    """(cancel_count, slow_cancel_count) over all POCancellation rows for one vendor."""
    from app.models.po_cancellation import POCancellation
    from app.services.po_cancellation_service import SLOW_CANCEL_THRESHOLD_DAYS

    rows = db.query(POCancellation.days_to_cancel).filter(POCancellation.vendor_card_id == vendor_card_id).all()
    cancel_count = len(rows)
    slow_cancel_count = sum(1 for (d,) in rows if d is not None and d > SLOW_CANCEL_THRESHOLD_DAYS)
    return cancel_count, slow_cancel_count


async def compute_all_vendor_scores(db: Session) -> dict:
    """Batch recompute vendor scores for ALL VendorCards.

    Preloads quote/buyplan offer-id sets for efficiency.
    Returns: {"updated": int, "skipped": int}
    """
    from app.models import Offer, Quote, VendorCard, VendorReview
    from app.models.buy_plan import BuyPlan, BuyPlanLine
    from app.vendor_utils import normalize_vendor_name

    now = datetime.now(UTC)

    # ── Preload all offer_ids grouped by vendor_card_id ──
    offer_rows = db.query(Offer.id, Offer.vendor_card_id, Offer.vendor_name).limit(50000).all()
    if len(offer_rows) >= 50000:
        logger.warning("Vendor score query hit {} limit — scores may be inaccurate", 50000)

    # Map vendor_card_id → set of offer_ids
    card_offer_ids: dict[int, set[int]] = {}
    # Map normalized_name → set of offer_ids (for vendors matched by name)
    name_offer_ids: dict[str, set[int]] = {}

    for oid, vcid, vname in offer_rows:
        if vcid:
            card_offer_ids.setdefault(vcid, set()).add(oid)
        if vname:
            norm = normalize_vendor_name(vname)
            name_offer_ids.setdefault(norm, set()).add(oid)

    # ── Preload quote line_items to find offer_ids used in quotes ──
    quotes = db.query(Quote.line_items, Quote.status).filter(Quote.status.in_(QUOTE_USED_STATUSES)).limit(10000).all()
    if len(quotes) >= 10000:
        logger.warning("Vendor score quote query hit {} limit — scores may be inaccurate", 10000)
    quote_offer_id_set: set[int] = set()
    for line_items, _status in quotes:
        if line_items:
            for li in line_items:
                oid = li.get("offer_id")
                if oid:
                    quote_offer_id_set.add(oid)

    # ── Preload buyplan lines (relational) ──
    bp_lines = (
        db.query(BuyPlanLine.offer_id, BuyPlan.status)
        .join(BuyPlan, BuyPlanLine.buy_plan_id == BuyPlan.id)
        .filter(BuyPlan.status.notin_(NON_AWARDED_STATUSES), BuyPlanLine.offer_id.isnot(None))
        .limit(50000)
        .all()
    )
    if len(bp_lines) >= 50000:
        logger.warning("Vendor score buyplan query hit {} limit — scores may be inaccurate", 50000)
    awarded_offer_id_set: set[int] = set()
    po_confirmed_offer_id_set: set[int] = set()
    for oid, bp_status in bp_lines:
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

    # ── Preload PO-cancellation counts grouped by vendor_card_id ──
    # SAME po_cancellations table compute_single_vendor_score reads, so inline (re-source)
    # and nightly scoring always agree.
    from sqlalchemy import case

    from app.models.po_cancellation import POCancellation
    from app.services.po_cancellation_service import SLOW_CANCEL_THRESHOLD_DAYS

    cancel_rows = (
        db.query(
            POCancellation.vendor_card_id,
            func.count(POCancellation.id),
            func.sum(case((POCancellation.days_to_cancel > SLOW_CANCEL_THRESHOLD_DAYS, 1), else_=0)),
        )
        .filter(POCancellation.vendor_card_id.isnot(None))
        .group_by(POCancellation.vendor_card_id)
        .all()
    )
    cancel_count_map: dict[int, int] = {}
    slow_cancel_map: dict[int, int] = {}
    for vcid, cnt, slow in cancel_rows:
        cancel_count_map[vcid] = cnt or 0
        slow_cancel_map[vcid] = int(slow or 0)

    # ── Iterate all VendorCards in batches ──
    total_count = db.query(func.count(VendorCard.id)).scalar() or 0
    updated = 0
    skipped = 0
    BATCH_SIZE = 1000

    for batch_offset in range(0, total_count, BATCH_SIZE):
        cards = db.query(VendorCard).order_by(VendorCard.id).offset(batch_offset).limit(BATCH_SIZE).all()

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

            result = compute_vendor_score(
                offer_count,
                stage_points_sum,
                avg_rating,
                cancel_count=cancel_count_map.get(card.id, 0),
                slow_cancel_count=slow_cancel_map.get(card.id, 0),
                total_pos=card.total_pos or 0,
            )

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
            logger.error(f"Vendor scoring flush failed at offset {batch_offset}: {e}")

    try:
        db.commit()
        logger.info(f"Vendor scoring: updated {updated} vendor cards, skipped {skipped}")
    except Exception as e:
        logger.error(f"Vendor scoring commit failed: {e}")
        db.rollback()
        return {"updated": 0, "skipped": skipped}

    return {"updated": updated, "skipped": skipped}


def _get_quote_offer_ids(db: Session, offer_ids: set[int]) -> set[int]:
    """Get offer_ids that appear in sent/won/lost Quote line_items."""
    from app.models import Quote

    quotes = db.query(Quote.line_items).filter(Quote.status.in_(QUOTE_USED_STATUSES)).limit(10000).all()
    found: set[int] = set()
    for (line_items,) in quotes:
        if line_items:
            for li in line_items:
                oid = li.get("offer_id")
                if oid and oid in offer_ids:
                    found.add(oid)
    return found


def _get_buyplan_offer_ids(db: Session, offer_ids: set[int], statuses: set[str]) -> set[int]:
    """Get offer_ids that appear in BuyPlanLine rows with given plan statuses."""
    from app.models.buy_plan import BuyPlan, BuyPlanLine

    rows = (
        db.query(BuyPlanLine.offer_id)
        .join(BuyPlan, BuyPlanLine.buy_plan_id == BuyPlan.id)
        .filter(BuyPlan.status.in_(statuses), BuyPlanLine.offer_id.isnot(None))
        .limit(50000)
        .all()
    )
    found: set[int] = set()
    for (oid,) in rows:
        if oid in offer_ids:
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
