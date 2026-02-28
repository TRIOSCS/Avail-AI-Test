"""NetComponents sighting writer.

Converts parsed NcSighting objects into AVAIL Sighting records,
matching the same patterns used by DigiKey/Mouser/OEMSecrets integrations.
Now includes price break data and supplier product URLs.

Called by: worker loop
Depends on: result_parser.NcSighting, sighting model, vendor_utils
"""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from app.models import Requirement, Sighting
from app.vendor_utils import normalize_vendor_name

from .mpn_normalizer import normalize_mpn
from .result_parser import NcSighting


def save_nc_sightings(
    db: Session,
    queue_item,
    nc_sightings: list[NcSighting],
) -> int:
    """Save parsed NC sightings to the AVAIL sightings table.

    Deduplicates by vendor_name + mpn + quantity combo to avoid duplicate records.
    Returns count of sightings created.
    """
    req = db.get(Requirement, queue_item.requirement_id)
    if not req:
        logger.error("NC sighting writer: requirement {} not found", queue_item.requirement_id)
        return 0

    material_card_id = req.material_card_id
    now = datetime.now(timezone.utc)

    # Build dedup set from existing NC sightings for this requirement
    existing = (
        db.query(Sighting.vendor_name_normalized, Sighting.mpn_matched, Sighting.qty_available)
        .filter(
            Sighting.requirement_id == req.id,
            Sighting.source_type == "netcomponents",
        )
        .all()
    )
    existing_keys = {
        ((v or "").lower(), (m or "").lower(), q)
        for v, m, q in existing
    }

    created = 0
    for nc in nc_sightings:
        if not nc.vendor_name:
            continue

        vendor_norm = normalize_vendor_name(nc.vendor_name)
        mpn_norm = normalize_mpn(nc.part_number)

        # Dedup check
        dedup_key = (vendor_norm.lower(), mpn_norm.lower(), nc.quantity)
        if dedup_key in existing_keys:
            continue
        existing_keys.add(dedup_key)

        # Extract best unit price from price breaks (lowest qty tier = unit price)
        unit_price = None
        currency = nc.currency
        if nc.price_breaks:
            unit_price = nc.price_breaks[0].price

        # Build raw_data with all NC-specific fields
        raw_data = {
            "region": nc.region,
            "country": nc.country,
            "inventory_type": nc.inventory_type,
            "uploaded_date": nc.uploaded_date,
            "is_sponsor": nc.is_sponsor,
            "description": nc.description,
            "supplier_product_url": nc.supplier_product_url,
        }
        if nc.price_breaks:
            raw_data["price_breaks"] = [
                {"price": pb.price, "min_qty": pb.min_qty}
                for pb in nc.price_breaks
            ]

        sighting = Sighting(
            requirement_id=req.id,
            material_card_id=material_card_id,
            vendor_name=nc.vendor_name,
            vendor_name_normalized=vendor_norm,
            mpn_matched=nc.part_number,
            normalized_mpn=mpn_norm,
            manufacturer=nc.manufacturer,
            qty_available=nc.quantity,
            unit_price=unit_price,
            currency=currency,
            source_type="netcomponents",
            source_searched_at=now,
            is_authorized=nc.is_authorized,
            confidence=0.6 if nc.inventory_type == "in_stock" else 0.3,
            date_code=nc.date_code or None,
            raw_data=raw_data,
            created_at=now,
        )
        db.add(sighting)
        created += 1

    if created:
        db.commit()
    logger.info(
        "NC sighting writer: created {} sightings for requirement {} (from {} parsed)",
        created,
        req.id,
        len(nc_sightings),
    )
    return created
