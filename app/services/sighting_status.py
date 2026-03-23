"""Derive vendor outreach status for the sightings tab.

Computes a status per vendor for a given requirement by checking:
- VendorCard.is_blacklisted -> "blacklisted"
- Offer exists for requirement + vendor -> "offer-in"
- Contact sent to vendor for requisition -> "contacted"
- All sightings marked is_unavailable -> "unavailable"
- Default -> "sighting"

Called by: htmx_views.part_tab_sourcing
Depends on: models (VendorCard, Offer, Contact, Sighting, VendorSightingSummary)
"""

from sqlalchemy.orm import Session

from ..models.offers import Contact, Offer
from ..models.sourcing import Sighting
from ..models.vendor_sighting_summary import VendorSightingSummary
from ..models.vendors import VendorCard


def compute_vendor_statuses(
    requirement_id: int,
    requisition_id: int,
    db: Session,
    vendor_names: list[str] | None = None,
) -> dict[str, str]:
    """Return {vendor_name: status} for all vendors with sightings on this requirement.

    Priority order: blacklisted > offer-in > contacted > unavailable > sighting.

    Args:
        vendor_names: Pre-fetched list to skip the VendorSightingSummary query.
    """
    if vendor_names is None:
        summaries = (
            db.query(VendorSightingSummary.vendor_name)
            .filter(VendorSightingSummary.requirement_id == requirement_id)
            .all()
        )
        vendor_names = [s.vendor_name for s in summaries]
    if not vendor_names:
        return {}

    # Blacklisted vendors
    blacklisted_names: set[str] = set()
    blacklisted_cards = db.query(VendorCard.normalized_name).filter(VendorCard.is_blacklisted.is_(True)).all()
    bl_normalized = {c.normalized_name for c in blacklisted_cards}
    for vn in vendor_names:
        if vn.strip().lower() in bl_normalized:
            blacklisted_names.add(vn)

    # Vendors with offers on this requirement
    offers = db.query(Offer.vendor_name).filter(Offer.requirement_id == requirement_id).all()
    offer_vendors = {o.vendor_name for o in offers}

    # Vendors contacted for this requisition
    contacts = (
        db.query(Contact.vendor_name)
        .filter(
            Contact.requisition_id == requisition_id,
            Contact.status.in_(["sent", "delivered", "opened"]),
        )
        .all()
    )
    contacted_vendors = {c.vendor_name for c in contacts}

    # Vendors where all sightings are unavailable
    sightings = (
        db.query(Sighting.vendor_name, Sighting.is_unavailable).filter(Sighting.requirement_id == requirement_id).all()
    )
    vendor_avail: dict[str, list[bool]] = {}
    for s in sightings:
        vendor_avail.setdefault(s.vendor_name, []).append(bool(s.is_unavailable))
    unavailable_vendors: set[str] = set()
    for vn, flags in vendor_avail.items():
        if flags and all(flags):
            unavailable_vendors.add(vn)

    # Build status dict with priority ordering
    statuses: dict[str, str] = {}
    for vn in vendor_names:
        if vn in blacklisted_names:
            statuses[vn] = "blacklisted"
        elif vn in offer_vendors:
            statuses[vn] = "offer-in"
        elif vn in contacted_vendors:
            statuses[vn] = "contacted"
        elif vn in unavailable_vendors:
            statuses[vn] = "unavailable"
        else:
            statuses[vn] = "sighting"

    return statuses
