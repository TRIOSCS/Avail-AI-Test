"""Strategic Vendor Service — business logic for buyer-vendor assignments.

Enforces the 10-vendor cap, 39-day TTL, one-buyer-per-vendor rule,
and handles claiming, dropping, replacing, and expiring strategic vendors.

Called by: routers/strategic.py, routers/crm/offers.py, email_service.py,
           jobs/offers_jobs.py
Depends on: models/strategic.py, models/vendors.py, models/auth.py
"""

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import and_, func
from sqlalchemy.orm import Session, joinedload

from app.models.strategic import StrategicVendor
from app.models.vendors import VendorCard

MAX_STRATEGIC_VENDORS = 10
TTL_DAYS = 39


def _ensure_utc(dt: datetime) -> datetime:
    """Ensure a datetime is tz-aware UTC (SQLite strips tzinfo)."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def get_my_strategic(db: Session, user_id: int) -> list[StrategicVendor]:
    """Return all active strategic vendors for a buyer."""
    return (
        db.query(StrategicVendor)
        .options(joinedload(StrategicVendor.vendor_card))
        .filter(
            StrategicVendor.user_id == user_id,
            StrategicVendor.released_at.is_(None),
        )
        .order_by(StrategicVendor.expires_at.asc())
        .all()
    )


def active_count(db: Session, user_id: int) -> int:
    """Count active strategic vendors for a buyer."""
    return (
        db.query(func.count(StrategicVendor.id))
        .filter(
            StrategicVendor.user_id == user_id,
            StrategicVendor.released_at.is_(None),
        )
        .scalar()
    )


def get_vendor_owner(db: Session, vendor_card_id: int) -> StrategicVendor | None:
    """Return the active strategic record for a vendor, or None if open pool."""
    return (
        db.query(StrategicVendor)
        .options(joinedload(StrategicVendor.user))
        .filter(
            StrategicVendor.vendor_card_id == vendor_card_id,
            StrategicVendor.released_at.is_(None),
        )
        .first()
    )


def claim_vendor(
    db: Session, user_id: int, vendor_card_id: int
) -> tuple[StrategicVendor | None, str | None]:
    """Claim a vendor as strategic. Returns (record, error_message)."""
    # Check cap
    count = active_count(db, user_id)
    if count >= MAX_STRATEGIC_VENDORS:
        return None, f"Already at {MAX_STRATEGIC_VENDORS} strategic vendors. Drop one first."

    # Check if already claimed by someone
    existing = get_vendor_owner(db, vendor_card_id)
    if existing:
        if existing.user_id == user_id:
            return None, "You already have this vendor as strategic."
        return None, f"This vendor is already claimed by another buyer."

    # Verify vendor exists
    vendor = db.query(VendorCard).filter(VendorCard.id == vendor_card_id).first()
    if not vendor:
        return None, "Vendor not found."

    now = datetime.now(timezone.utc)
    record = StrategicVendor(
        user_id=user_id,
        vendor_card_id=vendor_card_id,
        claimed_at=now,
        expires_at=now + timedelta(days=TTL_DAYS),
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    logger.info(
        "Strategic vendor claimed: user={} vendor={} expires={}",
        user_id, vendor_card_id, record.expires_at,
    )
    return record, None


def drop_vendor(
    db: Session, user_id: int, vendor_card_id: int
) -> tuple[bool, str | None]:
    """Drop a strategic vendor back to open pool. Returns (success, error)."""
    record = (
        db.query(StrategicVendor)
        .filter(
            StrategicVendor.user_id == user_id,
            StrategicVendor.vendor_card_id == vendor_card_id,
            StrategicVendor.released_at.is_(None),
        )
        .first()
    )
    if not record:
        return False, "Vendor is not in your strategic list."

    record.released_at = datetime.now(timezone.utc)
    record.release_reason = "dropped"
    db.commit()
    logger.info("Strategic vendor dropped: user={} vendor={}", user_id, vendor_card_id)
    return True, None


def replace_vendor(
    db: Session, user_id: int, drop_vendor_id: int, claim_vendor_id: int
) -> tuple[StrategicVendor | None, str | None]:
    """Atomic swap: drop one vendor, claim another. Returns (new_record, error)."""
    if drop_vendor_id == claim_vendor_id:
        return None, "Cannot replace a vendor with itself."

    success, err = drop_vendor(db, user_id, drop_vendor_id)
    if not success:
        return None, err

    record, err = claim_vendor(db, user_id, claim_vendor_id)
    if not record:
        # Rollback the drop — re-claim the old vendor
        db.rollback()
        return None, err

    return record, None


def record_offer(db: Session, vendor_card_id: int) -> bool:
    """Reset the 39-day clock when an offer comes in for a strategic vendor.

    Called from offer creation (manual + AI-parsed). Returns True if a
    strategic record was updated.
    """
    record = (
        db.query(StrategicVendor)
        .filter(
            StrategicVendor.vendor_card_id == vendor_card_id,
            StrategicVendor.released_at.is_(None),
        )
        .first()
    )
    if not record:
        return False

    now = datetime.now(timezone.utc)
    record.last_offer_at = now
    record.expires_at = now + timedelta(days=TTL_DAYS)
    db.commit()
    logger.info(
        "Strategic vendor clock reset: vendor={} new_expires={}",
        vendor_card_id, record.expires_at,
    )
    return True


def expire_stale(db: Session) -> int:
    """Expire all strategic vendors past their TTL. Returns count expired."""
    now = datetime.now(timezone.utc)
    stale = (
        db.query(StrategicVendor)
        .filter(
            StrategicVendor.expires_at < now,
            StrategicVendor.released_at.is_(None),
        )
        .all()
    )
    for record in stale:
        record.released_at = now
        record.release_reason = "expired"
    db.commit()

    if stale:
        logger.info("Expired {} strategic vendor assignments", len(stale))
    return len(stale)


def get_expiring_soon(db: Session, days: int = 7) -> list[StrategicVendor]:
    """Return strategic vendors expiring within N days."""
    cutoff = datetime.now(timezone.utc) + timedelta(days=days)
    return (
        db.query(StrategicVendor)
        .options(
            joinedload(StrategicVendor.user),
            joinedload(StrategicVendor.vendor_card),
        )
        .filter(
            StrategicVendor.expires_at < cutoff,
            StrategicVendor.released_at.is_(None),
        )
        .order_by(StrategicVendor.expires_at.asc())
        .all()
    )


def get_vendor_status(db: Session, vendor_card_id: int) -> dict | None:
    """Return status dict for a vendor: owner info + days remaining."""
    record = get_vendor_owner(db, vendor_card_id)
    if not record:
        return None

    now = datetime.now(timezone.utc)
    expires = _ensure_utc(record.expires_at)
    days_left = max(0, (expires - now).days)
    return {
        "vendor_card_id": vendor_card_id,
        "owner_user_id": record.user_id,
        "owner_name": record.user.name if record.user else None,
        "claimed_at": record.claimed_at.isoformat(),
        "last_offer_at": record.last_offer_at.isoformat() if record.last_offer_at else None,
        "expires_at": record.expires_at.isoformat(),
        "days_remaining": days_left,
    }


def get_open_pool(
    db: Session, limit: int = 50, offset: int = 0, search: str | None = None
) -> tuple[list[VendorCard], int]:
    """Return vendors not claimed by any buyer. Returns (vendors, total_count)."""
    claimed_ids = (
        db.query(StrategicVendor.vendor_card_id)
        .filter(StrategicVendor.released_at.is_(None))
        .subquery()
    )
    q = db.query(VendorCard).filter(VendorCard.id.notin_(claimed_ids))

    if search:
        q = q.filter(VendorCard.display_name.ilike(f"%{search}%"))

    total = q.count()
    vendors = q.order_by(VendorCard.display_name).offset(offset).limit(limit).all()
    return vendors, total
