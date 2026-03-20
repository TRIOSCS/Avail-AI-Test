"""ICsource sighting writer.

Converts parsed IcsSighting objects into AVAIL Sighting records,
matching the same patterns used by DigiKey/Mouser/OEMSecrets integrations.
Saves vendor email and phone from ICsource results.

Called by: worker loop
Depends on: result_parser.IcsSighting, sighting model, vendor_utils
"""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from app.models import Requirement, Sighting
from app.vendor_utils import normalize_vendor_name

from .mpn_normalizer import strip_packaging_suffixes
from .result_parser import IcsSighting


def save_ics_sightings(
    db: Session,
    queue_item,
    ics_sightings: list[IcsSighting],
) -> int:
    """Save parsed ICS sightings to the AVAIL sightings table.

    Deduplicates by vendor_name + mpn + quantity combo to avoid duplicate records.
    Returns count of sightings created.
    """
    req = db.get(Requirement, queue_item.requirement_id)
    if not req:
        logger.error("ICS sighting writer: requirement {} not found", queue_item.requirement_id)
        return 0

    material_card_id = req.material_card_id
    now = datetime.now(timezone.utc)

    # Build dedup set from existing ICS sightings for this requirement
    existing = (
        db.query(Sighting.vendor_name_normalized, Sighting.mpn_matched, Sighting.qty_available)
        .filter(
            Sighting.requirement_id == req.id,
            Sighting.source_type == "icsource",
        )
        .all()
    )
    existing_keys = {((v or "").lower(), (m or "").lower(), q) for v, m, q in existing}

    created = 0
    for ics in ics_sightings:
        if not ics.vendor_name:
            continue

        vendor_norm = normalize_vendor_name(ics.vendor_name)
        mpn_norm = strip_packaging_suffixes(ics.part_number)

        # Dedup check
        dedup_key = (vendor_norm.lower(), mpn_norm.lower(), ics.quantity)
        if dedup_key in existing_keys:
            continue
        existing_keys.add(dedup_key)

        sighting = Sighting(
            requirement_id=req.id,
            material_card_id=material_card_id,
            vendor_name=ics.vendor_name,
            vendor_name_normalized=vendor_norm,
            vendor_email=ics.vendor_email or None,
            vendor_phone=ics.vendor_phone or None,
            mpn_matched=ics.part_number,
            normalized_mpn=mpn_norm,
            manufacturer=ics.manufacturer,
            qty_available=ics.quantity,
            source_type="icsource",
            source_searched_at=now,
            confidence=0.6 if ics.in_stock else 0.3,
            date_code=ics.date_code or None,
            raw_data={
                "vendor_company_id": ics.vendor_company_id,
                "uploaded_date": ics.uploaded_date,
                "description": ics.description,
                "price": ics.price,
                "in_stock": ics.in_stock,
            },
            created_at=now,
        )
        db.add(sighting)
        created += 1

    if created:
        db.commit()
        # Rebuild vendor-level summaries
        from app.services.sighting_aggregation import rebuild_vendor_summaries_from_sightings

        rebuild_vendor_summaries_from_sightings(db, req.id, ics_sightings)
    logger.info(
        "ICS sighting writer: created {} sightings for requirement {} (from {} parsed)",
        created,
        req.id,
        len(ics_sightings),
    )
    return created
