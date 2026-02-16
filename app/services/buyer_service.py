"""Buyer profile service — CRUD for routing attributes.

Each buyer gets commodity, geography, and brand assignments that feed
the routing engine. Simple get/set, no magic.

Usage:
    from app.services.buyer_service import get_profile, upsert_profile, list_profiles
"""

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import BuyerProfile, User

log = logging.getLogger("avail.buyer")

# Valid values for dropdowns / validation
VALID_COMMODITIES = {
    "semiconductors",
    "pc_server_parts",
    "networking",
    "storage",
    "memory",
    "passives",
    "connectors",
}
VALID_GEOGRAPHIES = {"apac", "emea", "americas", "global"}
VALID_USAGE_TYPES = {"sourcing_to_buy", "selling_trading", "backup_buying"}


def get_profile(user_id: int, db: Session) -> BuyerProfile | None:
    """Get buyer profile by user_id."""
    return db.query(BuyerProfile).filter(BuyerProfile.user_id == user_id).first()


def upsert_profile(user_id: int, data: dict, db: Session) -> BuyerProfile:
    """Create or update a buyer profile.

    data keys: primary_commodity, secondary_commodity, primary_geography,
               brand_specialties, brand_material_types, brand_usage_types
    """
    profile = db.query(BuyerProfile).filter(BuyerProfile.user_id == user_id).first()

    if not profile:
        profile = BuyerProfile(user_id=user_id)
        db.add(profile)

    # Set fields from data, only if provided
    for field in ("primary_commodity", "secondary_commodity", "primary_geography"):
        if field in data:
            setattr(profile, field, data[field])

    for field in ("brand_specialties", "brand_material_types", "brand_usage_types"):
        if field in data:
            val = data[field]
            if isinstance(val, str):
                val = [v.strip() for v in val.split(",") if v.strip()]
            setattr(profile, field, val)

    profile.updated_at = datetime.now(timezone.utc)
    db.flush()

    log.info(
        f"Buyer profile upserted for user {user_id}: "
        f"commodity={profile.primary_commodity}/{profile.secondary_commodity}, "
        f"geo={profile.primary_geography}, brands={profile.brand_specialties}"
    )
    return profile


def list_profiles(db: Session) -> list[dict]:
    """List all buyer profiles with user info for admin view."""
    profiles = (
        db.query(BuyerProfile, User).join(User, BuyerProfile.user_id == User.id).all()
    )

    return [
        {
            "user_id": profile.user_id,
            "user_name": user.name,
            "user_email": user.email,
            "primary_commodity": profile.primary_commodity,
            "secondary_commodity": profile.secondary_commodity,
            "primary_geography": profile.primary_geography,
            "brand_specialties": profile.brand_specialties or [],
            "brand_material_types": profile.brand_material_types or [],
            "brand_usage_types": profile.brand_usage_types or [],
            "updated_at": profile.updated_at.isoformat()
            if profile.updated_at
            else None,
        }
        for profile, user in profiles
    ]


def delete_profile(user_id: int, db: Session) -> bool:
    """Delete a buyer profile."""
    profile = db.query(BuyerProfile).filter(BuyerProfile.user_id == user_id).first()
    if profile:
        db.delete(profile)
        db.flush()
        return True
    return False
