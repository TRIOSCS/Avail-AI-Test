"""Purchase history upsert service.

Provides upsert_purchase() — called by offer/quote won hooks and future import scripts
to maintain the customer_part_history table.

Also provides record_buyplan_purchase_history() — called by the buy-plan completion hook
to feed CPH rows from verified buy-plan lines.
"""

from datetime import datetime, timezone
from decimal import Decimal

from loguru import logger
from sqlalchemy.orm import Session

from ..models import CustomerPartHistory, MaterialCard


def upsert_purchase(
    db: Session,
    *,
    company_id: int,
    material_card_id: int,
    source: str,
    unit_price: Decimal | float | None = None,
    quantity: int | None = None,
    purchased_at: datetime | None = None,
    source_ref: str | None = None,
) -> CustomerPartHistory:
    """Insert or update a customer_part_history record.

    Keyed on (company_id, material_card_id, source).
    On conflict: increments purchase_count, updates rolling avg price,
    accumulates total_quantity.
    """
    now = purchased_at or datetime.now(timezone.utc)

    # Get display MPN from material card
    card = db.get(MaterialCard, material_card_id)
    mpn = card.display_mpn if card else ""

    existing = (
        db.query(CustomerPartHistory)
        .filter_by(
            company_id=company_id,
            material_card_id=material_card_id,
            source=source,
        )
        .first()
    )

    price = Decimal(str(unit_price)) if unit_price is not None else None
    qty = int(quantity) if quantity is not None else None

    if existing:
        existing.purchase_count = (existing.purchase_count or 1) + 1
        existing.last_purchased_at = now
        if price is not None:
            existing.last_unit_price = price
            # Rolling average: (old_avg * old_count + new_price) / new_count
            old_avg = existing.avg_unit_price or price
            existing.avg_unit_price = (old_avg * (existing.purchase_count - 1) + price) / existing.purchase_count
        if qty is not None:
            existing.last_quantity = qty
            existing.total_quantity = (existing.total_quantity or 0) + qty
        if source_ref:
            existing.source_ref = source_ref
        logger.info(
            "CPH_UPSERT: updated company={} card={} source={} count={}",
            company_id,
            material_card_id,
            source,
            existing.purchase_count,
        )
        return existing

    record = CustomerPartHistory(
        company_id=company_id,
        material_card_id=material_card_id,
        mpn=mpn,
        source=source,
        last_purchased_at=now,
        purchase_count=1,
        last_unit_price=price,
        avg_unit_price=price,
        last_quantity=qty,
        total_quantity=qty or 0,
        source_ref=source_ref,
    )
    db.add(record)
    logger.info(
        "CPH_UPSERT: created company={} card={} source={}",
        company_id,
        material_card_id,
        source,
    )
    return record


def record_buyplan_purchase_history(db: Session, plan, *, refresh: bool = True) -> list[int]:
    """Record customer_part_history from a COMPLETED buy plan's verified lines.

    Idempotent via plan.purchase_history_recorded_at. Returns affected material_card_ids.
    Best-effort: callers must not let CPH errors break buy-plan completion.
    """
    from app.constants import BuyPlanLineStatus  # local import avoids cycles

    if plan.purchase_history_recorded_at is not None:
        return []

    req = plan.requisition
    site = req.customer_site if req else None
    company_id = site.company_id if site else None
    if not company_id:
        logger.warning("BUYPLAN_CPH: plan {} has no customer company — skipping", plan.id)
        plan.purchase_history_recorded_at = datetime.now(timezone.utc)
        db.flush()
        return []

    affected: list[int] = []
    for line in plan.lines:
        if line.status != BuyPlanLineStatus.VERIFIED.value:
            continue
        card_id = None
        if line.requirement_id and line.requirement:
            card_id = line.requirement.material_card_id
        if not card_id and line.offer_id and line.offer:
            card_id = line.offer.material_card_id
        if not card_id:
            logger.info(
                "BUYPLAN_CPH: plan {} line {} has no material_card — skipping",
                plan.id,
                line.id,
            )
            continue
        upsert_purchase(
            db,
            company_id=company_id,
            material_card_id=card_id,
            source="buy_plan",
            unit_price=line.unit_sell,
            quantity=line.quantity,
            purchased_at=plan.completed_at,
            source_ref=plan.sales_order_number,
        )
        affected.append(card_id)

    plan.purchase_history_recorded_at = datetime.now(timezone.utc)
    db.flush()
    logger.info(
        "BUYPLAN_CPH: plan {} recorded {} parts for company {}",
        plan.id,
        len(affected),
        company_id,
    )
    if refresh and affected:
        refresh_matches_for_cards(db, affected)
    return affected


def refresh_matches_for_cards(db: Session, card_ids: list[int], *, per_card_limit: int = 5) -> int:
    """Re-run proactive matching for live offers of the given cards (immediate
    surfacing).

    Best-effort. Bounded to the newest `per_card_limit` offers per card so completion
    stays cheap. Engine dedup prevents duplicate matches.
    """
    from app.models import Offer
    from app.services.proactive_matching import find_matches_for_offer

    created = 0
    for card_id in set(card_ids):
        from app.constants import OfferStatus

        _LIVE_STATUSES = [OfferStatus.ACTIVE.value, OfferStatus.APPROVED.value]
        offers = (
            db.query(Offer.id)
            .filter(
                Offer.material_card_id == card_id,
                Offer.is_stale.isnot(True),
                Offer.status.in_(_LIVE_STATUSES),
            )
            .order_by(Offer.created_at.desc())
            .limit(per_card_limit)
            .all()
        )
        for (offer_id,) in offers:
            try:
                created += len(find_matches_for_offer(offer_id, db))
            except Exception:  # noqa: BLE001
                logger.exception("BUYPLAN_CPH: match refresh failed for offer {}", offer_id)
    return created
