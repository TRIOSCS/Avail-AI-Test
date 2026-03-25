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

from ..constants import ContactStatus
from ..models.offers import Contact, Offer
from ..models.sourcing import Sighting
from ..models.vendor_sighting_summary import VendorSightingSummary
from ..models.vendors import VendorCard
from ..vendor_utils import normalize_vendor_name


def compute_vendor_statuses(
    requirement_id: int,
    requisition_id: int,
    db: Session,
    vendor_names: list[str] | None = None,
) -> dict[str, str]:
    """Compute vendor status for each vendor on a requirement.

    Priority: blacklisted > offer-in > contacted > unavailable > sighting
    Uses batched lookups with consistent name normalization.
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

    # Normalize all names for consistent matching
    normalized = {vn: normalize_vendor_name(vn) for vn in vendor_names}
    norm_set = set(normalized.values())

    # Batch 1: Blacklisted vendor cards
    bl_cards = set(
        row[0]
        for row in db.query(VendorCard.normalized_name)
        .filter(VendorCard.normalized_name.in_(norm_set), VendorCard.is_blacklisted.is_(True))
        .all()
    )

    # Batch 2: Vendors with offers on THIS requirement
    offer_vendors = set(
        row[0] for row in db.query(Offer.vendor_name).filter(Offer.requirement_id == requirement_id).all()
    )

    # Batch 3: Vendors contacted on THIS requisition
    contacted_vendors = set(
        row[0]
        for row in db.query(Contact.vendor_name)
        .filter(
            Contact.requisition_id == requisition_id,
            Contact.status.in_([ContactStatus.SENT, ContactStatus.OPENED, ContactStatus.RESPONDED]),
        )
        .all()
    )

    # Batch 4: Vendors with ALL sightings unavailable on THIS requirement
    unavail_vendors: set[str] = set()
    sight_rows = (
        db.query(Sighting.vendor_name, Sighting.is_unavailable).filter(Sighting.requirement_id == requirement_id).all()
    )
    vendor_sights: dict[str, list[bool]] = {}
    for vn, is_u in sight_rows:
        vendor_sights.setdefault(vn, []).append(bool(is_u))
    for vn, flags in vendor_sights.items():
        if all(flags):
            unavail_vendors.add(vn)

    # Resolve statuses with priority: blacklisted > offer-in > contacted > unavailable > sighting
    result: dict[str, str] = {}
    for vn in vendor_names:
        norm = normalized[vn]
        if norm in bl_cards:
            result[vn] = "blacklisted"
        elif vn in offer_vendors or norm in {normalize_vendor_name(ov) for ov in offer_vendors}:
            result[vn] = "offer-in"
        elif vn in contacted_vendors or norm in {normalize_vendor_name(cv) for cv in contacted_vendors}:
            result[vn] = "contacted"
        elif vn in unavail_vendors:
            result[vn] = "unavailable"
        else:
            result[vn] = "sighting"
    return result
