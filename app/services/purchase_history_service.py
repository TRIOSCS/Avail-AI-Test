"""Purchase history upsert service.

Provides upsert_purchase() — called by offer/quote won hooks
and future import scripts to maintain the customer_part_history table.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from ..models import CustomerPartHistory, MaterialCard

log = logging.getLogger(__name__)


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
            existing.avg_unit_price = (
                old_avg * (existing.purchase_count - 1) + price
            ) / existing.purchase_count
        if qty is not None:
            existing.last_quantity = qty
            existing.total_quantity = (existing.total_quantity or 0) + qty
        if source_ref:
            existing.source_ref = source_ref
        log.info(
            "CPH_UPSERT: updated company=%d card=%d source=%s count=%d",
            company_id, material_card_id, source, existing.purchase_count,
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
    log.info(
        "CPH_UPSERT: created company=%d card=%d source=%s",
        company_id, material_card_id, source,
    )
    return record
