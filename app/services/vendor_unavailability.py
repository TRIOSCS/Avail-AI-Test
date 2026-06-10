"""Durable vendor+part unavailability knowledge — record, clear, re-apply, exclude.

Business logic for VendorPartUnavailability records: marking a vendor's stock of a
part as gone (upsert per normalized MPN key, with reason/note/provenance), undoing
the mark, re-stamping ``Sighting.is_unavailable`` on freshly persisted rows so
re-searches never resurrect a dead vendor, batched intel lookup for rendering, and
RFQ-suggestion exclusion. The keys a vendor+requirement covers are the vendor's
sightings' ``normalize_mpn_key(mpn_matched)`` values plus the requirement's
primary-MPN key. Vendor matching is on ``Sighting.vendor_name_normalized`` via
``normalize_vendor_name()`` — never raw/lower(trim()) comparisons. Functions never
commit; callers own the transaction.

Called by: app/routers/sightings.py (mark-unavailable / mark-available routes),
           sighting-persistence paths via apply_to_fresh_sightings()
           (search_service, ICS/NC sighting writers, sources import,
           add-to-requisition picker, inventory jobs)
Depends on: VendorPartUnavailability, Sighting, ActivityLog models,
            UnavailabilityReason/ActivityType/Channel constants,
            normalize_vendor_name (app/vendor_utils),
            normalize_mpn_key (app/utils/normalization)
"""

from collections.abc import Iterable, Sequence
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from ..constants import ActivityType, Channel, UnavailabilityReason
from ..models.auth import User
from ..models.intelligence import ActivityLog
from ..models.sourcing import Requirement, Sighting
from ..models.vendor_part_unavailability import VendorPartUnavailability
from ..utils.normalization import normalize_mpn_key
from ..vendor_utils import normalize_vendor_name


def _keys_for_vendor(requirement: Requirement, sightings: Sequence[Sighting]) -> set[str]:
    """Normalized MPN keys this vendor+requirement covers.

    ``normalize_mpn_key(s.mpn_matched)`` for each sighting that has one, plus the
    requirement's primary-MPN key always. Empty/None keys are skipped.
    """
    keys = {normalize_mpn_key(s.mpn_matched) for s in sightings}
    keys.add(normalize_mpn_key(requirement.primary_mpn))
    keys.discard("")
    return keys


def _sighting_norm(sighting: Sighting) -> str:
    """Normalized vendor key for a sighting (column value, legacy-row fallback)."""
    return sighting.vendor_name_normalized or normalize_vendor_name(sighting.vendor_name or "")


def _vendor_sightings(db: Session, requirement: Requirement, vendor_norm: str) -> list[Sighting]:
    """The vendor's sightings for this requirement, matched on the normalized column."""
    return (
        db.query(Sighting)
        .filter(
            Sighting.requirement_id == requirement.id,
            Sighting.vendor_name_normalized == vendor_norm,
        )
        .all()
    )


def _log_activity(
    db: Session,
    requirement: Requirement,
    user: User | None,
    activity_type: ActivityType,
    vendor_name: str,
    notes: str,
) -> None:
    """One ActivityLog entry, direct-construction pattern (app/routers/sightings.py)."""
    db.add(
        ActivityLog(
            user_id=user.id if user else None,
            activity_type=activity_type,
            channel=Channel.SYSTEM,
            requirement_id=requirement.id,
            requisition_id=requirement.requisition_id,
            contact_name=vendor_name,
            notes=notes,
        )
    )


def record_unavailability(
    db: Session,
    requirement: Requirement,
    vendor_name: str,
    reason: UnavailabilityReason | str,
    note: str | None,
    user: User | None,
) -> int:
    """Record that this vendor's stock of this requirement's part(s) is gone.

    Upserts one VendorPartUnavailability per normalized MPN key (existing (vendor, key)
    row is updated — reason/note/created_by/created_at refreshed), flags the vendor's
    sightings, and writes ONE ActivityLog entry. Does NOT commit. Returns the number of
    records written.
    """
    reason = UnavailabilityReason(reason)
    note = (note or "").strip() or None
    vendor_norm = normalize_vendor_name(vendor_name)
    sightings = _vendor_sightings(db, requirement, vendor_norm)
    keys = _keys_for_vendor(requirement, sightings)

    now = datetime.now(timezone.utc)
    existing: dict[str, VendorPartUnavailability] = {}
    if keys:
        existing = {
            rec.normalized_mpn: rec
            for rec in db.query(VendorPartUnavailability)
            .filter(
                VendorPartUnavailability.vendor_name_normalized == vendor_norm,
                VendorPartUnavailability.normalized_mpn.in_(keys),
            )
            .all()
        }
    for key in keys:
        rec = existing.get(key)
        if rec is not None:
            rec.reason = reason.value
            rec.note = note
            rec.created_by_id = user.id if user else None
            rec.created_at = now
        else:
            db.add(
                VendorPartUnavailability(
                    vendor_name_normalized=vendor_norm,
                    normalized_mpn=key,
                    reason=reason.value,
                    note=note,
                    created_by_id=user.id if user else None,
                    created_at=now,
                )
            )

    for s in sightings:
        s.is_unavailable = True

    notes = f"Marked {vendor_name} unavailable for {requirement.primary_mpn}: {reason.label}"
    if note:
        notes += f" — {note}"
    _log_activity(db, requirement, user, ActivityType.VENDOR_UNAVAILABLE, vendor_name, notes)

    logger.info(
        "Recorded unavailability: vendor={} requirement={} keys={} reason={}",
        vendor_norm,
        requirement.id,
        sorted(keys),
        reason.value,
    )
    return len(keys)


def clear_unavailability(
    db: Session,
    requirement: Requirement,
    vendor_name: str,
    user: User | None,
) -> int:
    """Undo: delete the vendor's records for this requirement's keys and unflag
    its sightings.

    Writes a "marked available again" ActivityLog entry (the learned history
    survives in the timeline). Does NOT commit. Returns records deleted.
    """
    vendor_norm = normalize_vendor_name(vendor_name)
    sightings = _vendor_sightings(db, requirement, vendor_norm)
    keys = _keys_for_vendor(requirement, sightings)

    deleted = 0
    if keys:
        deleted = (
            db.query(VendorPartUnavailability)
            .filter(
                VendorPartUnavailability.vendor_name_normalized == vendor_norm,
                VendorPartUnavailability.normalized_mpn.in_(keys),
            )
            .delete(synchronize_session=False)
        )

    for s in sightings:
        s.is_unavailable = False

    _log_activity(
        db,
        requirement,
        user,
        ActivityType.VENDOR_AVAILABLE,
        vendor_name,
        f"Marked {vendor_name} available again for {requirement.primary_mpn}",
    )

    logger.info(
        "Cleared unavailability: vendor={} requirement={} records_deleted={}",
        vendor_norm,
        requirement.id,
        deleted,
    )
    return deleted


def unavailability_for_requirement(
    db: Session,
    requirement: Requirement,
    vendor_names: Sequence[str],
) -> dict[str, VendorPartUnavailability]:
    """Vendor display name -> most-recent matching record, for rendering reasons.

    Keys per vendor are that vendor's sighting MPN keys plus the requirement's primary
    key. One batched query — no N+1. Vendors with no matching record are absent from the
    result.
    """
    norm_by_display = {vn: normalize_vendor_name(vn) for vn in vendor_names}
    norms = {n for n in norm_by_display.values() if n}
    if not norms:
        return {}

    primary_key = normalize_mpn_key(requirement.primary_mpn)
    keys_by_norm: dict[str, set[str]] = {n: ({primary_key} if primary_key else set()) for n in norms}
    sight_rows = (
        db.query(Sighting.vendor_name_normalized, Sighting.vendor_name, Sighting.mpn_matched)
        .filter(Sighting.requirement_id == requirement.id)
        .all()
    )
    for vn_norm, vn, mpn_matched in sight_rows:
        norm = vn_norm or normalize_vendor_name(vn or "")
        if norm not in keys_by_norm:
            continue
        key = normalize_mpn_key(mpn_matched)
        if key:
            keys_by_norm[norm].add(key)

    all_keys = set().union(*keys_by_norm.values())
    if not all_keys:
        return {}

    # Most-recent first; per display vendor the first matching record wins.
    records = (
        db.query(VendorPartUnavailability)
        .filter(
            VendorPartUnavailability.vendor_name_normalized.in_(norms),
            VendorPartUnavailability.normalized_mpn.in_(all_keys),
        )
        .order_by(
            VendorPartUnavailability.created_at.desc(),
            VendorPartUnavailability.id.desc(),
        )
        .all()
    )
    result: dict[str, VendorPartUnavailability] = {}
    for display, norm in norm_by_display.items():
        keys = keys_by_norm.get(norm, set())
        for rec in records:
            if rec.vendor_name_normalized == norm and rec.normalized_mpn in keys:
                result[display] = rec
                break
    return result


def apply_to_fresh_sightings(
    db: Session,
    requirement: Requirement,
    sightings: Sequence[Sighting],
) -> int:
    """Re-stamp ``is_unavailable`` on just-created Sighting ORM objects.

    A sighting matches when its (vendor_name_normalized, normalize_mpn_key(mpn_matched
    or requirement.primary_mpn)) pair hits a durable record — one batched query over the
    pairs present. Does NOT commit. Returns the number of sightings flagged.
    """
    primary_key = normalize_mpn_key(requirement.primary_mpn)
    pair_for: list[tuple[Sighting, tuple[str, str]]] = []
    norms: set[str] = set()
    keys: set[str] = set()
    for s in sightings:
        norm = _sighting_norm(s)
        key = normalize_mpn_key(s.mpn_matched) or primary_key
        if not norm or not key:
            continue
        pair_for.append((s, (norm, key)))
        norms.add(norm)
        keys.add(key)
    if not pair_for:
        return 0

    record_pairs = {
        (v, m)
        for v, m in db.query(
            VendorPartUnavailability.vendor_name_normalized,
            VendorPartUnavailability.normalized_mpn,
        )
        .filter(
            VendorPartUnavailability.vendor_name_normalized.in_(norms),
            VendorPartUnavailability.normalized_mpn.in_(keys),
        )
        .all()
    }
    count = 0
    for s, pair in pair_for:
        if pair in record_pairs:
            s.is_unavailable = True
            count += 1
    if count:
        logger.info(
            "Re-stamped {} fresh sighting(s) unavailable for requirement {}",
            count,
            requirement.id,
        )
    return count


def excluded_vendor_norms(db: Session, requirements: Iterable[Requirement]) -> set[str]:
    """Vendor norms with a record on any of the requirements' primary-MPN keys.

    Deliberate boundary: matches primary keys only (no substitute-MPN matching).
    One query.
    """
    keys = {normalize_mpn_key(r.primary_mpn) for r in requirements}
    keys.discard("")
    if not keys:
        return set()
    return {
        row[0]
        for row in db.query(VendorPartUnavailability.vendor_name_normalized)
        .filter(VendorPartUnavailability.normalized_mpn.in_(keys))
        .distinct()
        .all()
    }
