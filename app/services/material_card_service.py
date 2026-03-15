"""material_card_service.py — Material card serialization, manufacturer inference, and merge.

Extracted from routers/materials.py to keep routers thin (HTTP only).
All functions take a db Session and return data — they do NOT commit.

Called by: routers/materials.py
Depends on: models (MaterialCard, MaterialVendorHistory, Sighting, Offer, Tag, MaterialTag),
            vendor_utils, audit_service
"""

from loguru import logger
from sqlalchemy.orm import Session

from ..models import (
    MaterialCard,
    MaterialVendorHistory,
    Offer,
    Requirement,
    Sighting,
)
from ..vendor_utils import normalize_vendor_name

MIN_MPN_PREFIX_LENGTH = 6  # Minimum prefix length for manufacturer inference
MIN_TAG_CONFIDENCE = 0.70  # Minimum confidence for material tag display


# -- Manufacturer Inference ---------------------------------------------------


def infer_manufacturer(db: Session, mpn: str) -> str | None:
    """Walk from longest to shortest prefix to find a known manufacturer."""
    for length in range(len(mpn) - 1, MIN_MPN_PREFIX_LENGTH, -1):
        prefix = mpn[:length]
        match = (
            db.query(MaterialCard)
            .filter(
                MaterialCard.normalized_mpn == prefix,
                MaterialCard.manufacturer.isnot(None),
                MaterialCard.manufacturer != "",
            )
            .first()
        )
        if match:
            return match.manufacturer
    return None


def backfill_missing_manufacturers(db: Session) -> int:
    """Bulk-update rows where manufacturer IS NULL/empty from prefix-match donors.

    Does NOT commit — caller must commit.
    """
    null_parts = (
        db.query(MaterialCard)
        .filter((MaterialCard.manufacturer.is_(None)) | (MaterialCard.manufacturer == ""))
        .all()
    )
    updated = 0
    for part in null_parts:
        inferred = infer_manufacturer(db, part.normalized_mpn)
        if inferred:
            part.manufacturer = inferred
            db.add(part)
            updated += 1
    return updated


# -- Serialization ------------------------------------------------------------


def serialize_material_card(card: MaterialCard, db: Session) -> dict:
    """Serialize a material card with vendor history, sightings, offers, and tags."""
    from ..models.tags import MaterialTag, Tag

    history = (
        db.query(MaterialVendorHistory)
        .filter_by(material_card_id=card.id)
        .order_by(MaterialVendorHistory.last_seen.desc())
        .all()
    )

    sightings = (
        db.query(Sighting)
        .filter(Sighting.material_card_id == card.id)
        .order_by(Sighting.created_at.desc())
        .limit(50)
        .all()
    )
    sightings_list = [
        {
            "id": s.id,
            "vendor_name": s.vendor_name,
            "qty_available": s.qty_available,
            "unit_price": s.unit_price,
            "currency": s.currency or "USD",
            "source_type": s.source_type,
            "is_authorized": s.is_authorized,
            "date_code": s.date_code,
            "condition": s.condition,
            "lead_time": s.lead_time,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in sightings
        if not s.is_unavailable
    ]

    tag_rows = (
        db.query(Tag.name, Tag.tag_type, MaterialTag.confidence, MaterialTag.source)
        .join(MaterialTag, MaterialTag.tag_id == Tag.id)
        .filter(MaterialTag.material_card_id == card.id, MaterialTag.confidence >= MIN_TAG_CONFIDENCE)
        .order_by(MaterialTag.confidence.desc())
        .all()
    )
    tags_list = [
        {"name": name, "type": tt, "confidence": round(float(conf), 2), "source": src}
        for name, tt, conf, src in tag_rows
    ]

    offers = (
        db.query(Offer)
        .filter(Offer.material_card_id == card.id)
        .order_by(Offer.created_at.desc())
        .limit(50)
        .all()
    )
    offers_list = [
        {
            "id": o.id,
            "vendor_name": o.vendor_name,
            "qty_available": o.qty_available,
            "unit_price": float(o.unit_price) if o.unit_price else None,
            "currency": o.currency or "USD",
            "lead_time": o.lead_time,
            "date_code": o.date_code,
            "condition": o.condition,
            "status": o.status,
            "source": o.source,
            "created_at": o.created_at.isoformat() if o.created_at else None,
        }
        for o in offers
    ]

    return {
        "id": card.id,
        "normalized_mpn": card.normalized_mpn,
        "display_mpn": card.display_mpn,
        "manufacturer": card.manufacturer,
        "description": card.description,
        "search_count": card.search_count or 0,
        "last_searched_at": card.last_searched_at.isoformat() if card.last_searched_at else None,
        "vendor_count": len(history),
        "vendor_history": [
            {
                "id": vh.id,
                "vendor_name": vh.vendor_name,
                "source_type": vh.source_type,
                "is_authorized": vh.is_authorized,
                "first_seen": vh.first_seen.isoformat() if vh.first_seen else None,
                "last_seen": vh.last_seen.isoformat() if vh.last_seen else None,
                "times_seen": vh.times_seen or 1,
                "last_qty": vh.last_qty,
                "last_price": vh.last_price,
                "last_currency": vh.last_currency,
                "last_manufacturer": vh.last_manufacturer,
                "vendor_sku": vh.vendor_sku,
            }
            for vh in history
        ],
        "sightings": sightings_list,
        "offers": offers_list,
        "tags": tags_list,
        "lifecycle_status": card.lifecycle_status,
        "package_type": card.package_type,
        "category": card.category,
        "rohs_status": card.rohs_status,
        "pin_count": card.pin_count,
        "datasheet_url": card.datasheet_url,
        "cross_references": card.cross_references or [],
        "specs_summary": card.specs_summary,
        "enrichment_source": card.enrichment_source,
        "enriched_at": card.enriched_at.isoformat() if card.enriched_at else None,
        "created_at": card.created_at.isoformat() if card.created_at else None,
        "updated_at": card.updated_at.isoformat() if card.updated_at else None,
    }


# -- Merge --------------------------------------------------------------------


def merge_material_cards(db: Session, source_id: int, target_id: int, user_email: str) -> dict:
    """Merge two material cards: re-point all linked records from source to target.

    Does NOT commit — caller must commit.

    Returns dict with merge metrics: reassigned counts, vh_merged, vh_moved.
    Raises ValueError if cards not found or same id.
    """
    if source_id == target_id:
        raise ValueError("Cannot merge a card with itself")

    source = db.get(MaterialCard, source_id)
    target = db.get(MaterialCard, target_id)
    if not source:
        raise ValueError(f"Source card {source_id} not found")
    if not target:
        raise ValueError(f"Target card {target_id} not found")

    # 1. Re-point requirements, sightings, offers
    reassigned = {}
    for model, name in [(Requirement, "requirements"), (Sighting, "sightings"), (Offer, "offers")]:
        count = (
            db.query(model)
            .filter(model.material_card_id == source_id)
            .update({model.material_card_id: target_id}, synchronize_session="fetch")
        )
        reassigned[name] = count

    # 2. Merge vendor histories
    source_vhs = db.query(MaterialVendorHistory).filter_by(material_card_id=source_id).all()
    target_vhs = {
        normalize_vendor_name(vh.vendor_name): vh
        for vh in db.query(MaterialVendorHistory).filter_by(material_card_id=target_id).all()
    }

    vh_merged = 0
    vh_moved = 0
    for svh in source_vhs:
        vn_key = normalize_vendor_name(svh.vendor_name)
        tvh = target_vhs.get(vn_key)
        if tvh:
            tvh.times_seen = (tvh.times_seen or 1) + (svh.times_seen or 1)
            if svh.first_seen and (not tvh.first_seen or svh.first_seen < tvh.first_seen):
                tvh.first_seen = svh.first_seen
            if svh.last_seen and (not tvh.last_seen or svh.last_seen > tvh.last_seen):
                tvh.last_seen = svh.last_seen
                if svh.last_qty is not None:
                    tvh.last_qty = svh.last_qty
                if svh.last_price is not None:
                    tvh.last_price = svh.last_price
                if svh.last_currency:
                    tvh.last_currency = svh.last_currency
                if svh.last_manufacturer:
                    tvh.last_manufacturer = svh.last_manufacturer
                if svh.vendor_sku:
                    tvh.vendor_sku = svh.vendor_sku
            if svh.is_authorized:
                tvh.is_authorized = True
            db.delete(svh)
            vh_merged += 1
        else:
            svh.material_card_id = target_id
            vh_moved += 1

    # 3. Merge card metadata
    target.search_count = (target.search_count or 0) + (source.search_count or 0)
    if not target.manufacturer and source.manufacturer:
        target.manufacturer = source.manufacturer
    if not target.description and source.description:
        target.description = source.description
    for field in (
        "lifecycle_status",
        "package_type",
        "category",
        "rohs_status",
        "pin_count",
        "datasheet_url",
        "specs_summary",
    ):
        if getattr(target, field) is None and getattr(source, field) is not None:
            setattr(target, field, getattr(source, field))

    # 4. Audit log
    from ..services.audit_service import log_audit

    log_audit(
        db,
        material_card_id=target_id,
        action="merged",
        old_card_id=source_id,
        new_card_id=target_id,
        normalized_mpn=target.normalized_mpn,
        details={
            "source_mpn": source.normalized_mpn,
            "reassigned": reassigned,
            "vh_merged": vh_merged,
            "vh_moved": vh_moved,
        },
        created_by=user_email,
    )

    # 5. Delete source card
    db.delete(source)

    logger.info(
        "Material card merged: source_id={} target_id={} source_mpn={} target_mpn={} "
        "reassigned={} vh_merged={} vh_moved={}",
        source_id,
        target_id,
        source.normalized_mpn,
        target.normalized_mpn,
        reassigned,
        vh_merged,
        vh_moved,
    )

    return {
        "ok": True,
        "target_card_id": target_id,
        "source_card_id": source_id,
        "reassigned": reassigned,
        "vendor_histories_merged": vh_merged,
        "vendor_histories_moved": vh_moved,
    }
