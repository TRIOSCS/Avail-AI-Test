"""Durable vendor+part unavailability knowledge — record, clear, re-apply, release.

Business logic for VendorPartUnavailability records: marking a vendor's stock of a
part as gone (upsert per normalized MPN key, with reason/note/provenance and a
per-key ``qty_at_mark`` snapshot), undoing the mark, re-stamping
``Sighting.is_unavailable`` on freshly persisted rows so re-searches never resurrect
a dead vendor, batched annotated intel lookup for rendering, RFQ-suggestion
exclusion, and the offer-hook release.

Temporal policy ("Two Windows, Real Proof",
docs/superpowers/specs/2026-06-10-unavailability-temporal-policy.md): suppression is
read-time bounded per reason class — LOT reasons ride a 30d window, ``not_really_there``
180d, ``different_part`` never expires — and releasable by real proof via the O1/O2/O3
override matrix embedded in ``apply_to_fresh_sightings`` (dispatched per source class:
LIVE → O1, HUMAN_DIRECT → O3, listing → O2). ``is_active()`` here is THE
single authority every read surface uses; ``Sighting.is_unavailable`` is only a render
cache. The keys a vendor+requirement covers are the vendor's sightings'
``normalize_mpn_key(mpn_matched)`` values plus the requirement's primary-MPN key.
Vendor matching goes through ONE shared helper (``sighting_vendor_norm``) with the
legacy NULL-column fallback — never raw/lower(trim()) comparisons, never bare column
equality. Functions never commit; callers own the transaction.

Called by: app/routers/sightings.py (mark-unavailable / mark-available routes),
           the five user-initiated offer sites via maybe_release_on_offer
           (routers/crm/offers.py create_offer + approve_offer,
           routers/htmx_views.py add_offer + save_parsed_offers,
           services/ai_offer_service.py save_freeform_offers),
           app/services/sighting_status.py (reader-authority Batch 4),
           sighting-persistence paths via apply_to_fresh_sightings()
           (search_service, ICS/NC sighting writers, sources import,
           add-to-requisition picker, inventory jobs)
Depends on: VendorPartUnavailability, Sighting, ActivityLog models,
            UnavailabilityReason/ActivityType/Channel constants,
            settings (unavailability_* knobs),
            normalize_vendor_name (app/vendor_utils),
            normalize_mpn_key (app/utils/normalization)
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final

from loguru import logger
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..config import settings
from ..constants import ActivityType, Channel, UnavailabilityReason
from ..models.auth import User
from ..models.intelligence import ActivityLog
from ..models.sourcing import Requirement, Sighting
from ..models.vendor_part_unavailability import VendorPartUnavailability
from ..utils.normalization import normalize_mpn_key
from ..vendor_utils import normalize_vendor_name

# ── Temporal-policy constants ─────────────────────────────────────────────────

# LOT reasons ride the short window (stock facts decay); not_really_there rides
# the long LISTING window; different_part (IDENTITY) never expires — hard-coded
# in _window_days, deliberately not a knob.
LOT_REASONS: Final[frozenset[UnavailabilityReason]] = frozenset(
    {
        UnavailabilityReason.BOUGHT_BY_US,
        UnavailabilityReason.SOLD_ELSEWHERE,
        UnavailabilityReason.BROKEN,
        UnavailabilityReason.OTHER,
    }
)

# Source trust classes, computed from source_type/is_authorized — NEVER the stored
# evidence_tier (NULL on 4 of 6 persistence paths). Everything not LIVE or
# HUMAN_DIRECT is listing-class by default: unknown source types can only be
# stamped, never trigger a release.
LIVE_SOURCES: Final[frozenset[str]] = frozenset({"digikey", "mouser", "element14"})
HUMAN_DIRECT_SOURCES: Final[frozenset[str]] = frozenset({"email_attachment"})

# The only two release_trigger values; written ONLY by override O3 and the offer hook.
RELEASE_TRIGGER_VENDOR_EMAIL: Final[str] = "vendor_email"
RELEASE_TRIGGER_OFFER_RECEIVED: Final[str] = "offer_received"

_CLASS_LIVE: Final[str] = "live"
_CLASS_HUMAN_DIRECT: Final[str] = "human_direct"
_CLASS_LISTING: Final[str] = "listing"

# Per-(active record, fresh row) verdicts from _override_verdict.
_VERDICT_STAMP: Final[str] = "stamp"  # no override fired — stamp is_unavailable
_VERDICT_SURFACE: Final[str] = "surface"  # O1/O2 — leave row unstamped, no record mutation
_VERDICT_RELEASE: Final[str] = "release"  # O3 — record-level release ('vendor_email')


@dataclass(frozen=True)
class UnavailabilityIntel:
    """Template-facing annotation of a record — policy state precomputed so Jinja
    renders the three row states without re-deriving any policy."""

    record: VendorPartUnavailability
    is_active: bool
    age_days: int
    release_trigger: str | None


# ── Policy helpers ────────────────────────────────────────────────────────────


def _as_utc(dt: datetime) -> datetime:
    """Tag naive datetimes (SQLite/legacy rows) as UTC so comparisons never mix naive
    and aware values."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _window_days(reason: UnavailabilityReason) -> int | None:
    """Active-window length for a reason class; None ⇒ never expires."""
    if reason in LOT_REASONS:
        return settings.unavailability_suppress_days
    if reason is UnavailabilityReason.NOT_REALLY_THERE:
        return settings.unavailability_listing_suppress_days
    # IDENTITY (different_part): never expires — identity knowledge, not stock state.
    return None


def is_active(record: VendorPartUnavailability, now: datetime | None = None) -> bool:
    """THE single active-suppression predicate — every read surface uses it.

    ``released_at IS NULL AND (reason == different_part OR created_at >= now −
    window(reason))``. Pure read-time computation: no cron, no lazy writes.
    """
    if record.released_at is not None:
        return False
    window = _window_days(UnavailabilityReason(record.reason))
    if window is None:
        return True
    if record.created_at is None:  # pre-flush default not applied yet → just created
        return True
    now = now or datetime.now(timezone.utc)
    return _as_utc(record.created_at) >= now - timedelta(days=window)


def _source_class(sighting: Sighting) -> str:
    """LIVE / HUMAN_DIRECT / listing-class (the default for everything else)."""
    if (sighting.source_type or "") in LIVE_SOURCES or sighting.is_authorized is True:
        return _CLASS_LIVE
    if (sighting.source_type or "") in HUMAN_DIRECT_SOURCES:
        return _CLASS_HUMAN_DIRECT
    return _CLASS_LISTING


def _override_verdict(record: VendorPartUnavailability, sighting: Sighting) -> str:
    """Override verdict for one (active record, fresh row) pair — DISPATCHED BY SOURCE
    CLASS, never priority order.

    The three overrides apply to mutually exclusive source classes, so each row is
    evaluated against exactly one: LIVE → O1, HUMAN_DIRECT → O3, listing-class → O2.
    Stronger evidence class always wins — a HUMAN_DIRECT row whose qty also clears the
    O2 jump must RELEASE the record (the vendor sent a stock list, S7), not merely
    surface the row; and O1 already subsumes any O2-shaped signal on LIVE rows (any
    qty difference triggers it).

    O1 live truth (LIVE → SURFACE): qty > 0 AND qty != qty_at_mark (NULL snapshot
    passes) — the equality-guard keeps a stale distributor echo showing the exact
    flagged qty stamped. Applies to ALL reasons incl. different_part (an authorized
    catalog match is identity evidence). Row-level only, no record mutation.

    O3 vendor document (HUMAN_DIRECT → RELEASE): qty > 0 — the caller stamps nothing
    and performs the record-level release ('vendor_email' + ActivityLog). Disabled for
    different_part (a qty claim doesn't fix identity).

    O2 restock (listing-class → SURFACE): snapshot and fresh qty both non-NULL AND
    fresh > snapshot AND fresh >= snapshot × factor (snapshot 0 ⇒ any fresh > 0 falls
    out of the strict-greater). NULL on either side = no signal, never "no change".
    No record mutation — stateless and self-healing. Disabled for different_part
    (more of the wrong part is still the wrong part).
    """
    qty = sighting.qty_available
    source_class = _source_class(sighting)
    if source_class == _CLASS_LIVE:
        if qty is not None and qty > 0 and (record.qty_at_mark is None or qty != record.qty_at_mark):
            return _VERDICT_SURFACE
        return _VERDICT_STAMP
    if source_class == _CLASS_HUMAN_DIRECT:
        if qty is not None and qty > 0 and record.reason != UnavailabilityReason.DIFFERENT_PART:
            return _VERDICT_RELEASE
        return _VERDICT_STAMP
    # Listing-class (the default) — O2 only.
    if record.reason == UnavailabilityReason.DIFFERENT_PART:
        return _VERDICT_STAMP
    snap = record.qty_at_mark
    if snap is not None and qty is not None and qty > snap and qty >= snap * settings.unavailability_qty_jump_factor:
        return _VERDICT_SURFACE
    return _VERDICT_STAMP


# ── Shared matching helpers ───────────────────────────────────────────────────


def sighting_vendor_norm(sighting: Sighting) -> str:
    """Normalized vendor key for a sighting — THE shared matching helper (CRITICAL-2).

    Column value with the legacy NULL-row fallback through
    ``normalize_vendor_name(vendor_name)``. record/clear/apply and the status
    computation all match through this; a bare column-equality filter would
    silently miss legacy rows whose ``vendor_name_normalized`` is NULL, leaving
    zombie flags.
    """
    return sighting.vendor_name_normalized or normalize_vendor_name(sighting.vendor_name or "")


def _vendor_sightings(db: Session, requirement: Requirement, vendor_norm: str) -> list[Sighting]:
    """The vendor's sightings for this requirement, matched via sighting_vendor_norm."""
    rows = db.query(Sighting).filter(Sighting.requirement_id == requirement.id).all()
    return [s for s in rows if sighting_vendor_norm(s) == vendor_norm]


def _keys_for_vendor(requirement: Requirement, sightings: Sequence[Sighting]) -> set[str]:
    """Normalized MPN keys this vendor+requirement covers.

    ``normalize_mpn_key(s.mpn_matched)`` for each sighting that has one, plus the
    requirement's primary-MPN key always. Empty/None keys are skipped.
    """
    keys = {normalize_mpn_key(s.mpn_matched) for s in sightings}
    keys.add(normalize_mpn_key(requirement.primary_mpn))
    keys.discard("")
    return keys


def _qty_snapshots(requirement: Requirement, sightings: Sequence[Sighting]) -> dict[str, int]:
    """Per-key snapshot: max non-NULL qty_available over the vendor's sightings
    whose key equals the record's key.

    Rows with empty ``mpn_matched`` count toward the primary-key record (mirroring
    apply_to_fresh_sightings' key fallback). Never max-across-keys, never a
    cross-key fallback.
    """
    primary_key = normalize_mpn_key(requirement.primary_mpn)
    snaps: dict[str, int] = {}
    for s in sightings:
        key = normalize_mpn_key(s.mpn_matched) or primary_key
        if not key or s.qty_available is None:
            continue
        if key not in snaps or s.qty_available > snaps[key]:
            snaps[key] = s.qty_available
    return snaps


def _mpn_display(requirement: Requirement, sightings: Sequence[Sighting]) -> str:
    """MPN text for ActivityLog notes — never interpolates None (MINOR-7): primary MPN,
    else a matched MPN, else "requirement #<id>"."""
    if requirement.primary_mpn:
        return requirement.primary_mpn
    for s in sightings:
        if s.mpn_matched:
            return s.mpn_matched
    return f"requirement #{requirement.id}"


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


def _release_record(
    db: Session,
    requirement: Requirement,
    record: VendorPartUnavailability,
    trigger: str,
    vendor_display: str,
    detail: str,
    user: User | None,
    now: datetime,
) -> None:
    """Record-level release: stamp released_at/release_trigger + one ActivityLog
    line."""
    record.released_at = now
    record.release_trigger = trigger
    _log_activity(
        db,
        requirement,
        user,
        ActivityType.VENDOR_AVAILABLE,
        vendor_display,
        detail,
    )
    logger.info(
        "Released unavailability record {} (vendor={} key={}) via {}",
        record.id,
        record.vendor_name_normalized,
        record.normalized_mpn,
        trigger,
    )


# ── Service surface ───────────────────────────────────────────────────────────


def record_unavailability(
    db: Session,
    requirement: Requirement,
    vendor_name: str,
    reason: UnavailabilityReason | str,
    note: str | None,
    user: User | None,
) -> int:
    """Record that this vendor's stock of this requirement's part(s) is gone.

    Upserts one VendorPartUnavailability per normalized MPN key — an existing
    (vendor, key) row is updated: reason/note/created_by/created_at refreshed,
    ``released_at``/``release_trigger`` NULLed, ``requirement_id`` provenance
    refreshed, and ``qty_at_mark`` re-snapshot per key keeping the old value when
    the new computation is NULL. Flags the vendor's sightings via the shared
    matching helper and writes ONE ActivityLog entry.

    Raises ValueError when the vendor name normalizes to nothing (IMPORTANT-4) or
    when zero MPN keys are derivable (CRITICAL-1) — nothing is written in either
    case, including no ActivityLog. Does NOT commit. Returns records written.
    """
    reason = UnavailabilityReason(reason)
    note = (note or "").strip() or None
    vendor_norm = normalize_vendor_name(vendor_name)
    if not vendor_norm:
        raise ValueError(f"vendor name {vendor_name!r} normalizes to nothing — cannot record unavailability")
    sightings = _vendor_sightings(db, requirement, vendor_norm)
    keys = _keys_for_vendor(requirement, sightings)
    if not keys:
        raise ValueError(
            f"no MPN keys derivable for requirement {requirement.id} "
            "(no primary-MPN key and no matched-sighting keys) — cannot record unavailability"
        )

    now = datetime.now(timezone.utc)
    snapshots = _qty_snapshots(requirement, sightings)
    existing: dict[str, VendorPartUnavailability] = {
        rec.normalized_mpn: rec
        for rec in db.query(VendorPartUnavailability)
        .filter(
            VendorPartUnavailability.vendor_name_normalized == vendor_norm,
            VendorPartUnavailability.normalized_mpn.in_(keys),
        )
        .all()
    }
    for key in keys:
        snapshot = snapshots.get(key)
        rec = existing.get(key)
        if rec is not None:
            rec.reason = reason.value
            rec.note = note
            rec.created_by_id = user.id if user else None
            rec.created_at = now
            if snapshot is not None:  # keep-old-on-NULL: no cross-requirement clobber
                rec.qty_at_mark = snapshot
            rec.released_at = None
            rec.release_trigger = None
            rec.requirement_id = requirement.id
        else:
            db.add(
                VendorPartUnavailability(
                    vendor_name_normalized=vendor_norm,
                    normalized_mpn=key,
                    reason=reason.value,
                    note=note,
                    created_by_id=user.id if user else None,
                    created_at=now,
                    qty_at_mark=snapshot,
                    requirement_id=requirement.id,
                )
            )

    for s in sightings:
        s.is_unavailable = True

    notes = f"Marked {vendor_name} unavailable for {_mpn_display(requirement, sightings)}: {reason.label}"
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
    """Undo: delete the vendor's records for this requirement and unflag its
    sightings.

    Delete predicate (IMPORTANT-3): vendor norm AND (key IN current keys OR
    ``requirement_id == requirement.id``) — the provenance arm catches records whose
    key no longer matches the requirement's current keys (no unclearable zombies).
    DELETE semantics are deliberate: an explicit human "forget it"; history survives
    in the ActivityLog. Auto-expiry and O1/O2 never delete.

    Raises ValueError on an empty vendor norm (IMPORTANT-4). Does NOT commit.
    Returns records deleted.
    """
    vendor_norm = normalize_vendor_name(vendor_name)
    if not vendor_norm:
        raise ValueError(f"vendor name {vendor_name!r} normalizes to nothing — cannot clear unavailability")
    sightings = _vendor_sightings(db, requirement, vendor_norm)
    keys = _keys_for_vendor(requirement, sightings)

    deleted = (
        db.query(VendorPartUnavailability)
        .filter(
            VendorPartUnavailability.vendor_name_normalized == vendor_norm,
            or_(
                VendorPartUnavailability.normalized_mpn.in_(sorted(keys)),
                VendorPartUnavailability.requirement_id == requirement.id,
            ),
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
        f"Marked {vendor_name} available again for {_mpn_display(requirement, sightings)}",
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
) -> dict[str, UnavailabilityIntel]:
    """Vendor display name -> annotated most-recent matching record, for rendering.

    Keys per vendor are that vendor's sighting MPN keys plus the requirement's primary
    key. One batched query — no N+1. Vendors with no matching record are absent from the
    result. Each entry carries the computed policy state (is_active, age_days,
    release_trigger) so templates render the three row states without re-deriving
    policy.
    """
    norm_by_display = {vn: normalize_vendor_name(vn) for vn in vendor_names}
    norms = {n for n in norm_by_display.values() if n}
    if not norms:
        return {}

    primary_key = normalize_mpn_key(requirement.primary_mpn)
    keys_by_norm: dict[str, set[str]] = {n: ({primary_key} if primary_key else set()) for n in norms}
    for s in db.query(Sighting).filter(Sighting.requirement_id == requirement.id).all():
        norm = sighting_vendor_norm(s)
        if norm not in keys_by_norm:
            continue
        key = normalize_mpn_key(s.mpn_matched)
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
    now = datetime.now(timezone.utc)
    result: dict[str, UnavailabilityIntel] = {}
    for display, norm in norm_by_display.items():
        keys = keys_by_norm.get(norm, set())
        for rec in records:
            if rec.vendor_name_normalized == norm and rec.normalized_mpn in keys:
                age = (now - _as_utc(rec.created_at)).days if rec.created_at else 0
                result[display] = UnavailabilityIntel(
                    record=rec,
                    is_active=is_active(rec, now),
                    age_days=max(0, age),
                    release_trigger=rec.release_trigger,
                )
                break
    return result


def apply_to_fresh_sightings(
    db: Session,
    requirement: Requirement,
    sightings: Sequence[Sighting],
) -> int:
    """Re-stamp ``is_unavailable`` on just-created Sighting ORM objects, applying the
    O1/O2/O3 suppression matrix.

    Each sighting matches on its candidate-key SET {normalize_mpn_key(mpn_matched),
    primary key} (both non-empty, IMPORTANT-5) against the full fetched records — one
    batched query. Per matched record: non-active record → skip (advisory rendering
    happens reader-side); else dispatch on the row's source class (LIVE → O1 only,
    HUMAN_DIRECT → O3 only, listing-class → O2 only — mutually exclusive classes, never
    priority order). SURFACE (O1/O2) → leave unstamped, no record mutation; RELEASE (O3:
    HUMAN_DIRECT, qty > 0, not different_part) → record-level release ('vendor_email') +
    one ActivityLog line, stamp nothing; STAMP otherwise. A row is stamped when ANY
    matching active record says stamp.

    Does NOT commit. Returns the number of sightings flagged.
    """
    primary_key = normalize_mpn_key(requirement.primary_mpn)
    candidates: list[tuple[Sighting, str, list[str]]] = []
    norms: set[str] = set()
    keys: set[str] = set()
    for s in sightings:
        norm = sighting_vendor_norm(s)
        cand_keys = sorted({k for k in (normalize_mpn_key(s.mpn_matched), primary_key) if k})
        if not norm or not cand_keys:
            continue
        candidates.append((s, norm, cand_keys))
        norms.add(norm)
        keys.update(cand_keys)
    if not candidates:
        return 0

    records: dict[tuple[str, str], VendorPartUnavailability] = {
        (rec.vendor_name_normalized, rec.normalized_mpn): rec
        for rec in db.query(VendorPartUnavailability)
        .filter(
            VendorPartUnavailability.vendor_name_normalized.in_(norms),
            VendorPartUnavailability.normalized_mpn.in_(keys),
        )
        .all()
    }
    if not records:
        return 0

    now = datetime.now(timezone.utc)
    count = 0
    for s, norm, cand_keys in candidates:
        stamp = False
        for key in cand_keys:
            rec = records.get((norm, key))
            if rec is None or not is_active(rec, now):
                continue  # no/expired/released record — never stamp from it
            verdict = _override_verdict(rec, s)  # class-dispatched: O1 / O2 / O3
            if verdict == _VERDICT_SURFACE:  # O1 live truth / O2 restock — row-level only
                continue
            if verdict == _VERDICT_RELEASE:
                # O3 vendor document — safe to write here: this path is reached
                # from a user-initiated router, not a background worker.
                _release_record(
                    db,
                    requirement,
                    rec,
                    RELEASE_TRIGGER_VENDOR_EMAIL,
                    s.vendor_name or rec.vendor_name_normalized,
                    (
                        f"Vendor email shows qty {s.qty_available} for "
                        f"{s.mpn_matched or _mpn_display(requirement, [s])} — "
                        f"released unavailability mark for {s.vendor_name or rec.vendor_name_normalized}"
                    ),
                    None,
                    now,
                )
                continue
            stamp = True
        if stamp:
            s.is_unavailable = True
            count += 1
    if count:
        logger.info(
            "Re-stamped {} fresh sighting(s) unavailable for requirement {}",
            count,
            requirement.id,
        )
    return count


def release_on_offer(
    db: Session,
    requirement: Requirement,
    vendor_name: str,
    user: User | None,
) -> int:
    """Offer hook: an incoming offer is proof of availability — release the vendor's
    matching ACTIVE records across the requirement's keys.

    All reasons except ``different_part`` (availability evidence never releases
    identity knowledge). Sets ``released_at``/``release_trigger='offer_received'``
    and writes one ActivityLog entry. No-op (0) when nothing matches. Does NOT
    commit. Returns records released.
    """
    vendor_norm = normalize_vendor_name(vendor_name)
    if not vendor_norm:
        return 0
    sightings = _vendor_sightings(db, requirement, vendor_norm)
    keys = _keys_for_vendor(requirement, sightings)
    if not keys:
        return 0

    records = (
        db.query(VendorPartUnavailability)
        .filter(
            VendorPartUnavailability.vendor_name_normalized == vendor_norm,
            VendorPartUnavailability.normalized_mpn.in_(sorted(keys)),
        )
        .all()
    )
    now = datetime.now(timezone.utc)
    released = 0
    for rec in records:
        if rec.reason == UnavailabilityReason.DIFFERENT_PART:
            continue
        if not is_active(rec, now):
            continue
        rec.released_at = now
        rec.release_trigger = RELEASE_TRIGGER_OFFER_RECEIVED
        released += 1
    if released:
        _log_activity(
            db,
            requirement,
            user,
            ActivityType.VENDOR_AVAILABLE,
            vendor_name,
            (
                f"Offer received from {vendor_name} — released "
                f"{released} unavailability mark(s) for {_mpn_display(requirement, sightings)}"
            ),
        )
        logger.info(
            "Offer release: vendor={} requirement={} records_released={}",
            vendor_norm,
            requirement.id,
            released,
        )
    return released


def maybe_release_on_offer(
    db: Session,
    requirement_id: int | None,
    vendor_name: str | None,
    user: User | None,
) -> int:
    """The single offer-hook gate every user-initiated offer site calls.

    Principle: ``released_at`` is written only by user-initiated proof — a person
    entering, saving, or approving an offer. Auto-created offers (background inbox
    monitor, excess auto-matching) are auto-mined evidence — same class as demoted
    stock-list re-uploads — and never release; clones are never proof. Those paths
    must NOT call this.

    Thin wrapper over ``release_on_offer``: resolves the requirement and no-ops (0)
    when ``requirement_id`` or ``vendor_name`` is missing. Does NOT commit.
    """
    if not requirement_id or not vendor_name or not vendor_name.strip():
        return 0
    requirement = db.get(Requirement, requirement_id)
    if requirement is None:
        return 0
    return release_on_offer(db, requirement, vendor_name, user)


def excluded_vendor_norms(db: Session, requirements: Iterable[Requirement]) -> set[str]:
    """Vendor norms with an ACTIVE record on any of the requirements' primary-MPN keys.

    Fetches full rows and filters with ``is_active`` in Python — expired/released
    records do not exclude (RFQ resumes, and the tab agrees). Deliberate boundary:
    matches primary keys only (no substitute-MPN matching). Logs a warning when a
    requirement contributes no derivable key (IMPORTANT-6) — it must not silently
    widen RFQ suggestions.
    """
    keys: set[str] = set()
    for r in requirements:
        key = normalize_mpn_key(r.primary_mpn)
        if key:
            keys.add(key)
        else:
            logger.warning(
                "RFQ exclusion: requirement {} contributes no derivable MPN key (primary_mpn={!r})",
                r.id,
                r.primary_mpn,
            )
    if not keys:
        return set()
    now = datetime.now(timezone.utc)
    rows = db.query(VendorPartUnavailability).filter(VendorPartUnavailability.normalized_mpn.in_(sorted(keys))).all()
    return {rec.vendor_name_normalized for rec in rows if is_active(rec, now)}
