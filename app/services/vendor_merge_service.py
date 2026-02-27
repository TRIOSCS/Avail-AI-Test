"""Vendor merge service — reusable vendor card merge logic.

Extracted from admin.py to be callable from both the admin API endpoint
and the background auto-dedup service. Data is always merged, never erased:
alternate names added, sighting counts summed, FK references reassigned.

Called by: admin.py (vendor-merge endpoint), auto_dedup_service.py
Depends on: models
"""

from loguru import logger
from sqlalchemy.orm import Session

from ..models import VendorCard, VendorContact


def merge_vendor_cards(keep_id: int, remove_id: int, db: Session) -> dict:
    """Merge vendor card remove_id into keep_id.

    Reassigns all FK references, merges array fields, sums sighting counts,
    and deletes the removed card. Does NOT commit — caller must commit.

    Returns:
        {"ok": True, "kept": int, "removed": int, "reassigned": int}

    Raises:
        ValueError if cards not found or same id.
    """
    from ..models import (
        ActivityLog,
        BuyerVendorStats,
        EnrichmentQueue,
        Offer,
        ProspectContact,
        StockListHash,
        VendorMetricsSnapshot,
        VendorReview,
    )

    keep = db.get(VendorCard, keep_id)
    remove = db.get(VendorCard, remove_id)
    if not keep or not remove:
        raise ValueError("One or both vendor cards not found")
    if keep.id == remove.id:
        raise ValueError("Cannot merge a vendor with itself")

    # Merge array fields (emails, phones, contacts, alternate_names, domain_aliases)
    for field in ("emails", "phones", "contacts", "alternate_names", "domain_aliases"):
        existing = set(str(v) for v in (getattr(keep, field) or []))
        merged = list(getattr(keep, field) or [])
        for v in (getattr(remove, field) or []):
            if str(v) not in existing:
                merged.append(v)
                existing.add(str(v))
        setattr(keep, field, merged)

    # Add removed vendor's display_name as alternate name
    if remove.display_name and remove.display_name != keep.display_name:
        alts = list(keep.alternate_names or [])
        if remove.display_name not in alts:
            alts.append(remove.display_name)
            keep.alternate_names = alts

    # Sum sighting counts
    keep.sighting_count = (keep.sighting_count or 0) + (remove.sighting_count or 0)

    # Fill missing scalar fields from the removed card
    for field in ("domain", "website", "vendor_name_normalized"):
        if not getattr(keep, field, None) and getattr(remove, field, None):
            setattr(keep, field, getattr(remove, field))

    # Reassign FK references from remove → keep
    fk_tables = [
        (VendorContact, "vendor_card_id"),
        (VendorReview, "vendor_card_id"),
        (Offer, "vendor_card_id"),
        (VendorMetricsSnapshot, "vendor_card_id"),
        (StockListHash, "vendor_card_id"),
        (BuyerVendorStats, "vendor_card_id"),
        (ActivityLog, "vendor_card_id"),
        (EnrichmentQueue, "vendor_card_id"),
        (ProspectContact, "vendor_card_id"),
    ]
    reassigned = 0
    for model, col in fk_tables:
        try:
            count = db.query(model).filter(getattr(model, col) == remove.id).update(
                {col: keep.id}, synchronize_session="fetch"
            )
            reassigned += count
        except Exception as e:
            logger.debug("Vendor merge: skipped %s.%s reassignment: %s", model.__tablename__, col, e)

    # Delete the removed card
    db.delete(remove)
    db.flush()

    logger.info(
        "Vendor merge: kept %d (%s), removed %d (%s), reassigned %d records",
        keep.id, keep.display_name, remove_id, remove.display_name or "?", reassigned,
    )
    return {"ok": True, "kept": keep.id, "removed": remove_id, "reassigned": reassigned}
