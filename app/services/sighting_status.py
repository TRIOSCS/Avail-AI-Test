"""Derive vendor outreach status for the sightings tab.

Computes a status per vendor for a given requirement by checking:
- VendorCard.is_blacklisted -> "blacklisted"
- Offer exists for requirement + vendor -> "offer-in"
- Contact sent to vendor for requisition -> "contacted"
- All sighting rows flagged is_unavailable (legacy row flag, anchored on
  Sighting.vendor_name_normalized) OR a durable VendorPartUnavailability record
  matches (vendor norm, any of that vendor's sighting MPN keys ∪ the
  requirement's primary-MPN key) -> "unavailable"
- Default -> "sighting"

Called by: htmx_views.part_tab_sourcing
Depends on: models (VendorCard, Offer, Contact, Sighting, VendorSightingSummary,
            VendorPartUnavailability, Requirement), normalize_vendor_name,
            normalize_mpn_key
"""

from sqlalchemy.orm import Session

from ..constants import ContactStatus
from ..models.offers import Contact, Offer
from ..models.sourcing import Requirement, Sighting
from ..models.vendor_part_unavailability import VendorPartUnavailability
from ..models.vendor_sighting_summary import VendorSightingSummary
from ..models.vendors import VendorCard
from ..utils.normalization import normalize_mpn_key
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

    # Batch 4: unavailable — legacy all-rows-flagged (grouped by normalized vendor
    # name) OR a durable VendorPartUnavailability record on any of the vendor's
    # MPN keys (sighting matched-MPN keys ∪ requirement primary-MPN key).
    sight_rows = (
        db.query(
            Sighting.vendor_name,
            Sighting.vendor_name_normalized,
            Sighting.mpn_matched,
            Sighting.is_unavailable,
        )
        .filter(Sighting.requirement_id == requirement_id)
        .all()
    )
    primary_mpn = db.query(Requirement.primary_mpn).filter(Requirement.id == requirement_id).scalar()
    primary_key = normalize_mpn_key(primary_mpn)

    keys_by_norm: dict[str, set[str]] = {n: ({primary_key} if primary_key else set()) for n in norm_set}
    vendor_flags: dict[str, list[bool]] = {}
    for vn, vn_norm, mpn_matched, is_u in sight_rows:
        norm = vn_norm or normalize_vendor_name(vn or "")
        if not norm:
            continue
        vendor_flags.setdefault(norm, []).append(bool(is_u))
        if norm in keys_by_norm:
            key = normalize_mpn_key(mpn_matched)
            if key:
                keys_by_norm[norm].add(key)

    unavail_norms = {norm for norm, flags in vendor_flags.items() if all(flags)}

    all_keys = set().union(*keys_by_norm.values()) if keys_by_norm else set()
    if all_keys:
        record_pairs = {
            (v, m)
            for v, m in db.query(
                VendorPartUnavailability.vendor_name_normalized,
                VendorPartUnavailability.normalized_mpn,
            )
            .filter(
                VendorPartUnavailability.vendor_name_normalized.in_(norm_set),
                VendorPartUnavailability.normalized_mpn.in_(all_keys),
            )
            .all()
        }
        unavail_norms |= {
            norm for norm, keys in keys_by_norm.items() if any((norm, key) in record_pairs for key in keys)
        }

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
        elif norm in unavail_norms:
            result[vn] = "unavailable"
        else:
            result[vn] = "sighting"
    return result
