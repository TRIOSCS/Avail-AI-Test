"""Self-Repair Service — extends integrity checks with active data repair.

Provides repair functions for common data problems that cause glitches,
dead ends, and wrong data in the UI. Designed to be run on-demand or
scheduled.

Called by: scheduler (optional), admin endpoints, tests
Depends on: app.models, app.services.integrity_service
"""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import (
    Offer,
    Requirement,
    VendorCard,
)


def expire_stale_offers(db: Session, days_old: int = 14) -> int:
    """Mark offers as expired when expires_at is in the past.

    Returns count of offers expired.
    """
    cutoff = datetime.now(timezone.utc)
    updated = (
        db.query(Offer)
        .filter(
            Offer.status == "active",
            Offer.attribution_status == "active",
            Offer.expires_at.isnot(None),
            Offer.expires_at < cutoff,
        )
        .update(
            {"attribution_status": "expired"},
            synchronize_session="fetch",
        )
    )
    if updated:
        db.commit()
        logger.info("SELF_REPAIR: expired %d stale offers", updated)
    return updated


def fix_zero_qty_requirements(db: Session) -> int:
    """Fix requirements with qty=0 or NULL by setting to 1 (minimum valid qty).

    Returns count fixed.
    """
    updated = (
        db.query(Requirement)
        .filter(
            (Requirement.target_qty == 0) | (Requirement.target_qty.is_(None)),
            Requirement.sourcing_status != "lost",
        )
        .update(
            {"target_qty": 1},
            synchronize_session="fetch",
        )
    )
    if updated:
        db.commit()
        logger.info("SELF_REPAIR: fixed %d requirements with zero/null qty", updated)
    return updated


def fix_zero_price_offers(db: Session) -> int:
    """Set zero-price active offers to status 'expired' (likely bad parse).

    Returns count fixed.
    """
    updated = (
        db.query(Offer)
        .filter(
            Offer.status == "active",
            Offer.unit_price <= 0,
        )
        .update(
            {"attribution_status": "expired"},
            synchronize_session="fetch",
        )
    )
    if updated:
        db.commit()
        logger.info("SELF_REPAIR: expired %d zero-price offers", updated)
    return updated


def deduplicate_vendor_names(db: Session) -> int:
    """Find vendor cards with duplicate normalized_name and merge them.

    Keeps the card with the most sightings, re-points FKs from dupes. Returns count of
    duplicates merged.
    """

    dupes = (
        db.query(VendorCard.normalized_name, func.count(VendorCard.id))
        .group_by(VendorCard.normalized_name)
        .having(func.count(VendorCard.id) > 1)
        .all()
    )
    merged = 0
    for name, count in dupes:
        # Sort by sighting_count desc, NULLs last (SQLite-compatible)
        from sqlalchemy import case

        cards = (
            db.query(VendorCard)
            .filter(VendorCard.normalized_name == name)
            .order_by(
                case((VendorCard.sighting_count.is_(None), 1), else_=0),
                VendorCard.sighting_count.desc(),
            )
            .all()
        )
        keeper = cards[0]
        for dupe in cards[1:]:
            # Re-point offers from duplicate to keeper
            db.query(Offer).filter(Offer.vendor_card_id == dupe.id).update(
                {"vendor_card_id": keeper.id}, synchronize_session="fetch"
            )
            # Merge sighting count
            keeper.sighting_count = (keeper.sighting_count or 0) + (dupe.sighting_count or 0)
            db.delete(dupe)
            merged += 1
    if merged:
        db.commit()
        logger.info("SELF_REPAIR: merged %d duplicate vendor cards", merged)
    return merged


def run_full_repair(db: Session) -> dict:
    """Run all self-repair functions and return a summary report.

    Safe to run repeatedly — all operations are idempotent.
    """
    report = {
        "stale_offers_expired": expire_stale_offers(db),
        "zero_qty_fixed": fix_zero_qty_requirements(db),
        "zero_price_expired": fix_zero_price_offers(db),
        "vendor_dupes_merged": deduplicate_vendor_names(db),
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.info("SELF_REPAIR_COMPLETE: %s", report)
    return report
