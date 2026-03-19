"""Price snapshot recording service.

Called by: search_service, materials router (stock import), inventory_jobs, material_card_service (merge).
Depends on: MaterialPriceSnapshot model.
"""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from app.models.price_snapshot import MaterialPriceSnapshot


def record_price_snapshot(
    db: Session,
    material_card_id: int,
    vendor_name: str,
    price: float | None,
    currency: str = "USD",
    quantity: int | None = None,
    source: str = "api_sighting",
) -> None:
    """Record a price observation.

    Skips if price is None.
    """
    if price is None:
        return

    snap = MaterialPriceSnapshot(
        material_card_id=material_card_id,
        vendor_name=vendor_name,
        price=price,
        currency=currency,
        quantity=quantity,
        source=source,
        recorded_at=datetime.now(timezone.utc),
    )
    db.add(snap)
    db.flush()
    logger.debug(f"Price snapshot: card={material_card_id} vendor={vendor_name} price={price}")
