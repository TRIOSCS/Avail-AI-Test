"""The Broker Forum (TBF) sighting writer.

Converts parsed TbfSighting objects into AVAIL Sighting records,
matching the same patterns used by ICsource/NetComponents integrations.
Saves vendor email and phone from TBF results.

Called by: worker loop
Depends on: result_parser.TbfSighting, sighting model, vendor_utils
"""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from app.models import Requirement, Sighting
from app.services.vendor_unavailability import apply_to_fresh_sightings
from app.vendor_utils import normalize_vendor_name

from .mpn_normalizer import strip_packaging_suffixes
from .result_parser import TbfSighting


def save_tbf_sightings(
    db: Session,
    queue_item,
    tbf_sightings: list[TbfSighting],
) -> int:
    """Save parsed TBF sightings to the AVAIL sightings table.

    Deduplicates by vendor_name + mpn + quantity combo to avoid duplicate records.
    Returns count of sightings created.
    """
    req = db.get(Requirement, queue_item.requirement_id)
    if not req:
        logger.error("TBF sighting writer: requirement {} not found", queue_item.requirement_id)
        return 0

    material_card_id = req.material_card_id
    now = datetime.now(timezone.utc)

    # Build dedup set from existing TBF sightings for this requirement
    existing = (
        db.query(Sighting.vendor_name_normalized, Sighting.mpn_matched, Sighting.qty_available)
        .filter(
            Sighting.requirement_id == req.id,
            Sighting.source_type == "thebrokersite",
        )
        .all()
    )
    existing_keys = {((v or "").lower(), (m or "").lower(), q) for v, m, q in existing}

    created = 0
    created_rows: list[Sighting] = []
    for tbf in tbf_sightings:
        if not tbf.vendor_name:
            continue

        vendor_norm = normalize_vendor_name(tbf.vendor_name)
        mpn_norm = strip_packaging_suffixes(tbf.part_number)

        # Dedup check
        dedup_key = (vendor_norm.lower(), mpn_norm.lower(), tbf.quantity)
        if dedup_key in existing_keys:
            continue
        existing_keys.add(dedup_key)

        sighting = Sighting(
            requirement_id=req.id,
            material_card_id=material_card_id,
            vendor_name=tbf.vendor_name,
            vendor_name_normalized=vendor_norm,
            vendor_email=tbf.vendor_email or None,
            vendor_phone=tbf.vendor_phone or None,
            mpn_matched=tbf.part_number,
            normalized_mpn=mpn_norm,
            manufacturer=tbf.manufacturer,
            qty_available=tbf.quantity,
            currency=tbf.currency or "USD",
            source_type="thebrokersite",
            source_searched_at=now,
            confidence=0.6 if tbf.in_stock else 0.3,
            is_authorized=tbf.is_authorized,
            date_code=tbf.date_code or None,
            raw_data={
                "region": tbf.region,
                "country": tbf.country,
                "inventory_type": "in_stock" if tbf.in_stock else "brokered",
                "uploaded_date": tbf.uploaded_date,
                "vendor_company_id": tbf.vendor_company_id,
                "supplier_product_url": tbf.supplier_product_url,
                "price_breaks": tbf.price,
                "description": tbf.description,
            },
            created_at=now,
        )
        db.add(sighting)
        created_rows.append(sighting)
        created += 1

    if created:
        # Re-apply durable vendor+part unavailability knowledge before the
        # commit — async TBF results must not resurrect a dead vendor.
        apply_to_fresh_sightings(db, req, created_rows)
        db.commit()
        # Rebuild vendor-level summaries
        from app.services.sighting_aggregation import rebuild_vendor_summaries_from_sightings

        rebuild_vendor_summaries_from_sightings(db, req.id, tbf_sightings)
    logger.info(
        "TBF sighting writer: created {} sightings for requirement {} (from {} parsed)",
        created,
        req.id,
        len(tbf_sightings),
    )
    return created
