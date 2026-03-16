"""vendor_detail_service.py — Vendor detail tab data fetching.

Extracts data-loading logic from routers/htmx_views.py vendor_tab() to keep
the router thin (HTTP + templates only).

Called by: routers/htmx_views.py
Depends on: models (VendorCard, VendorContact, Sighting, Offer, SourcingLead)
"""

from loguru import logger
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..models import Offer, Sighting, VendorCard, VendorContact


def get_vendor_overview_data(db: Session, vendor: VendorCard) -> dict:
    """Fetch overview tab data: recent sightings, safety info, contacts.

    Returns dict with keys: recent_sightings, contacts, safety_band,
    safety_summary, safety_flags, safety_score, safety_available.
    """
    from ..models.sourcing_lead import SourcingLead

    recent_sightings = (
        db.query(Sighting)
        .filter(Sighting.vendor_name_normalized == vendor.normalized_name)
        .order_by(Sighting.created_at.desc().nullslast())
        .limit(10)
        .all()
    )

    safety_band = None
    safety_summary = None
    safety_flags = None
    safety_score = None
    safety_available = False
    try:
        lead = (
            db.query(SourcingLead)
            .filter(SourcingLead.vendor_name_normalized == vendor.normalized_name)
            .order_by(SourcingLead.created_at.desc())
            .first()
        )
        if lead:
            safety_band = lead.vendor_safety_band
            safety_summary = lead.vendor_safety_summary
            safety_flags = lead.vendor_safety_flags
            safety_score = lead.vendor_safety_score
            safety_available = True
    except (SQLAlchemyError, ValueError) as exc:
        logger.warning("Failed to load safety data for vendor {}: {}", vendor.id, exc)

    contacts = (
        db.query(VendorContact)
        .filter(VendorContact.vendor_card_id == vendor.id)
        .order_by(VendorContact.interaction_count.desc().nullslast())
        .limit(20)
        .all()
    )

    return {
        "recent_sightings": recent_sightings,
        "contacts": contacts,
        "safety_band": safety_band,
        "safety_summary": safety_summary,
        "safety_flags": safety_flags,
        "safety_score": safety_score,
        "safety_available": safety_available,
    }


def get_vendor_contacts(db: Session, vendor_id: int, limit: int = 50) -> list:
    """Fetch vendor contacts for the contacts tab."""
    return (
        db.query(VendorContact)
        .filter(VendorContact.vendor_card_id == vendor_id)
        .order_by(VendorContact.interaction_count.desc().nullslast())
        .limit(limit)
        .all()
    )


def get_vendor_offers(db: Session, vendor_display_name: str, limit: int = 50) -> list:
    """Fetch offers for the vendor offers tab."""
    return (
        db.query(Offer)
        .filter(Offer.vendor_name == vendor_display_name)
        .order_by(Offer.created_at.desc().nullslast())
        .limit(limit)
        .all()
    )
