"""Derive vendor outreach status for the sightings tab.

Computes a status per vendor for a given requirement by checking:
- VendorCard.is_blacklisted -> "blacklisted"
- Offer exists for requirement + vendor -> "offer-in"
- Reader-authority rule (the record predicate is the only authority;
  Sighting.is_unavailable is a render cache): vendor is "unavailable" iff
  (an ACTIVE VendorPartUnavailability record matches — vendor norm × any of
  that vendor's sighting MPN keys ∪ the requirement's primary-MPN key — AND the
  vendor has NO unstamped sighting row) OR (no matching record at all AND all
  rows flagged — true legacy). Rows win: one override-surfaced row flips the
  pill off; an expired/released record's stale stamped rows never pin it.
- Contact sent to vendor for requisition -> "contacted"
- Default -> "sighting"

Precedence: blacklisted > offer-in > unavailable > contacted > sighting.
unavailable outranks contacted because contacted is a step and unavailable is
its answer — a mark made after contacting must be visible; offer-in still
dominates everything but blacklisted.

Called by: htmx_views.part_tab_sourcing
Depends on: models (VendorCard, Offer, Contact, Sighting, VendorSightingSummary,
            VendorPartUnavailability, Requirement), normalize_vendor_name,
            normalize_mpn_key, vendor_unavailability (is_active authority +
            sighting_vendor_norm shared matching helper)
"""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from ..constants import ContactStatus
from ..models.offers import Contact, Offer
from ..models.sourcing import Requirement, Sighting
from ..models.vendor_part_unavailability import VendorPartUnavailability
from ..models.vendor_sighting_summary import VendorSightingSummary
from ..models.vendors import VendorCard
from ..utils.normalization import normalize_mpn_key
from ..vendor_utils import normalize_vendor_name
from .vendor_unavailability import is_active, sighting_vendor_norm


def compute_vendor_statuses(
    requirement_id: int,
    requisition_id: int,
    db: Session,
    vendor_names: list[str] | None = None,
) -> dict[str, str]:
    """Compute vendor status for each vendor on a requirement.

    Priority: blacklisted > offer-in > unavailable > contacted > sighting
    (contacted is a step; unavailable is its answer — a mark made after
    contacting must be visible). Uses batched lookups with consistent name
    normalization.
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

    # Batch 4: unavailable — reader-authority rule. The is_active record predicate
    # is the only authority; row flags are a render cache. A vendor is unavailable
    # iff (an active record matches AND it has NO unstamped row) OR (no matching
    # record at all AND all rows flagged — true legacy, restricted to vendors with
    # no record). Vendor matching goes through the shared sighting_vendor_norm
    # helper (legacy NULL-norm rows included).
    sight_rows = db.query(Sighting).filter(Sighting.requirement_id == requirement_id).all()
    requirement = db.get(Requirement, requirement_id)
    if requirement is None:
        logger.warning(
            "compute_vendor_statuses: requirement {} not found — treating it as key-less",
            requirement_id,
        )
        primary_key = ""
    else:
        primary_key = normalize_mpn_key(requirement.primary_mpn)

    keys_by_norm: dict[str, set[str]] = {n: ({primary_key} if primary_key else set()) for n in norm_set}
    vendor_flags: dict[str, list[bool]] = {}
    for s in sight_rows:
        norm = sighting_vendor_norm(s)
        if not norm:
            continue
        vendor_flags.setdefault(norm, []).append(bool(s.is_unavailable))
        if norm in keys_by_norm:
            key = normalize_mpn_key(s.mpn_matched)
            if key:
                keys_by_norm[norm].add(key)

    records_by_norm: dict[str, list[VendorPartUnavailability]] = {}
    all_keys = set().union(*keys_by_norm.values()) if keys_by_norm else set()
    if all_keys:
        matching_records = (
            db.query(VendorPartUnavailability)
            .filter(
                VendorPartUnavailability.vendor_name_normalized.in_(norm_set),
                VendorPartUnavailability.normalized_mpn.in_(all_keys),
            )
            .all()
        )
        for rec in matching_records:
            if rec.normalized_mpn in keys_by_norm.get(rec.vendor_name_normalized, set()):
                records_by_norm.setdefault(rec.vendor_name_normalized, []).append(rec)

    now = datetime.now(timezone.utc)
    unavail_norms: set[str] = set()
    for norm in norm_set:
        matching = records_by_norm.get(norm, [])
        flags = vendor_flags.get(norm, [])
        if matching:
            # Rows-win: any unstamped row flips the pill off "unavailable".
            if any(is_active(rec, now) for rec in matching) and all(flags):
                unavail_norms.add(norm)
        elif flags and all(flags):
            unavail_norms.add(norm)  # true legacy: flagged rows, no record at all

    # Resolve statuses with priority: blacklisted > offer-in > unavailable >
    # contacted > sighting. unavailable outranks contacted: contacted is a step
    # and unavailable is its answer — a mark made after contacting must be
    # visible. offer-in still dominates everything but blacklisted.
    # Normalize the offer/contacted vendor names once (the DB rows carry raw
    # names) rather than rebuilding the sets on every vendor iteration.
    offer_norms = {normalize_vendor_name(ov) for ov in offer_vendors}
    contacted_norms = {normalize_vendor_name(cv) for cv in contacted_vendors}
    result: dict[str, str] = {}
    for vn in vendor_names:
        norm = normalized[vn]
        if norm in bl_cards:
            result[vn] = "blacklisted"
        elif vn in offer_vendors or norm in offer_norms:
            result[vn] = "offer-in"
        elif norm in unavail_norms:
            result[vn] = "unavailable"
        elif vn in contacted_vendors or norm in contacted_norms:
            result[vn] = "contacted"
        else:
            result[vn] = "sighting"
    return result
