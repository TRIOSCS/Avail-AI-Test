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
        for v in getattr(remove, field) or []:
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
            count = (
                db.query(model)
                .filter(getattr(model, col) == remove.id)
                .update({col: keep.id}, synchronize_session="fetch")
            )
            reassigned += count
        except Exception as e:
            logger.error("Vendor merge: FK reassignment failed on {}.{}: {}", model.__tablename__, col, e)
            raise ValueError(f"Vendor merge aborted — failed to reassign {model.__tablename__}.{col}: {e}") from e

    # Delete the removed card
    db.delete(remove)
    db.flush()

    logger.info(
        "Vendor merge: kept {} ({}), removed {} ({}), reassigned {} records",
        keep.id,
        keep.display_name,
        remove_id,
        remove.display_name or "?",
        reassigned,
    )
    return {"ok": True, "kept": keep.id, "removed": remove_id, "reassigned": reassigned}


def delete_vendor_cards(id_a: int, id_b: int, db: Session) -> dict:
    """Delete BOTH vendor cards in a dedup pair (neither is worth keeping).

    Soft references that merely *point at* the card and outlive it (offers, stock-list
    hashes, activity log, prospect contacts, enrichment-queue rows) have their
    ``vendor_card_id`` NULLed so the record survives unlinked, exactly as merge reassigns
    FKs rather than cascading. Card-scoped children that are meaningless without the card
    and are declared NOT-NULL ``ondelete="CASCADE"`` at the DB level (vendor contacts,
    reviews, metrics snapshots, buyer-vendor stats) are NOT NULLed — that would raise a
    NotNullViolation on Postgres — they cascade-delete with the card via ``db.delete(card)``
    (DB ``ON DELETE CASCADE`` + the ORM ``cascade="all, delete-orphan"`` on
    ``VendorCard.reviews``/``vendor_contacts``/``attachments``). Mirrors the
    ``delete_companies`` detach-vs-cascade split. Does NOT commit — caller must commit.

    Returns:
        {"ok": True, "deleted": [int, int], "detached": int}

    Raises:
        ValueError if either card is missing or the two ids are identical.
    """
    from ..models import (
        ActivityLog,
        EnrichmentQueue,
        Offer,
        ProspectContact,
        StockListHash,
    )

    if id_a == id_b:
        raise ValueError("Cannot delete a vendor pair with identical ids")
    card_a = db.get(VendorCard, id_a)
    card_b = db.get(VendorCard, id_b)
    if not card_a or not card_b:
        raise ValueError("One or both vendor cards not found")

    ids = [id_a, id_b]

    # Detach soft references (nullable / SET NULL columns) — these records outlive the
    # card. The four NOT-NULL CASCADE children (VendorContact, VendorReview,
    # VendorMetricsSnapshot, BuyerVendorStats) are deliberately ABSENT here: NULLing a
    # NOT-NULL column raises NotNullViolation on Postgres. They cascade-delete below.
    detach_tables = [
        (Offer, "vendor_card_id"),
        (StockListHash, "vendor_card_id"),
        (ActivityLog, "vendor_card_id"),
        (ProspectContact, "vendor_card_id"),
        (EnrichmentQueue, "vendor_card_id"),  # nullable column despite DB ondelete=CASCADE
    ]
    detached = 0
    for model, col in detach_tables:
        try:
            count = (
                db.query(model).filter(getattr(model, col).in_(ids)).update({col: None}, synchronize_session="fetch")
            )
            detached += count
        except Exception as e:
            logger.error("Vendor delete-both: FK detach failed on {}.{}: {}", model.__tablename__, col, e)
            raise ValueError(f"Vendor delete aborted — failed to detach {model.__tablename__}.{col}: {e}") from e

    # NOT-NULL CASCADE children cascade-delete with the card (DB ON DELETE CASCADE +
    # ORM all, delete-orphan on reviews/vendor_contacts/attachments).
    db.delete(card_a)
    db.delete(card_b)
    db.flush()

    logger.info("Vendor delete-both: removed {} + {}, detached {} records", id_a, id_b, detached)
    return {"ok": True, "deleted": [id_a, id_b], "detached": detached}
