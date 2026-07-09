"""VendorPartUnavailability — durable "this vendor's stock of this part is gone" fact.

One row per (normalized vendor name, normalized MPN, condition) triple, recording WHY
the part is unavailable from that vendor (UnavailabilityReason), an optional free-text
note, and provenance (who recorded it, when, from which requirement). Outlives scraped
Sighting rows: re-searches that delete + recreate sightings re-stamp ``is_unavailable``
from these records, and RFQ vendor suggestions exclude matching vendors. Marking again
for an existing key is an upsert per ``(vendor, mpn, condition)``; distinct conditions
coexist as separate rows.

Temporal policy ("Two Windows, Real Proof" — docs/superpowers/specs/
2026-06-10-unavailability-temporal-policy.md): suppression is read-time bounded per
reason class via the ``is_active`` predicate in app/services/vendor_unavailability.py
— the ONLY authority; ``Sighting.is_unavailable`` is a render cache. ``qty_at_mark``
is the per-key qty snapshot powering the O2 restock override; ``released_at`` /
``release_trigger`` are written ONLY by override O3 (buyer-routed vendor email) and
the offer hook; ``requirement_id`` is clear-time provenance (SET NULL — knowledge
outlives requirements).

Called by: app/services/vendor_unavailability.py (record/clear/apply/exclude/release),
           app/services/sighting_status.py (reader-authority unavailable-status branch)
Depends on: app/constants.UnavailabilityReason (reason vocabulary), Base, users table,
            requirements table
"""

from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, Column, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.orm import relationship, validates

from ..database import UTCDateTime
from .base import Base


class VendorPartUnavailability(Base):
    __tablename__ = "vendor_part_unavailability"

    id = Column(Integer, primary_key=True)

    # normalize_vendor_name() of the vendor display name (app/vendor_utils.py) —
    # re-normalized via @validates, so an un-normalized write is unrepresentable.
    vendor_name_normalized = Column(String(255), nullable=False)
    # normalize_mpn_key() canonical dash-stripped key (app/utils/normalization.py) —
    # same key space offers use, so matched-MPN and primary-MPN lookups line up.
    # Re-normalized via @validates.
    normalized_mpn = Column(String(255), nullable=False)
    condition = Column(String(16))  # NULL = all-conditions catch-all; else new/refurb/used

    reason = Column(String(32), nullable=False)  # UnavailabilityReason value
    note = Column(Text)  # free-text "what we learned"

    created_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    # Dual default (Python + server) — sibling pattern (e.g. OemSpecCode); avoids
    # None-before-flush in tests while keeping a DB-side default for raw inserts.
    # NOT NULL: both defaults guarantee a value, so is_active's None branch is
    # provably pre-flush-only (a persisted NULL would never expire — corrupt row).
    created_at = Column(
        UTCDateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    # Per-`(key, condition)` snapshot (a `NULL`/all-conditions row snapshots across
    # the part's sightings; a specific-condition row scopes to same-condition
    # sightings). Re-mark keeps the old value when the new computation is NULL.
    # NULL ⇒ the O2 restock override never fires (fail-closed for records created
    # before the policy-columns migration).
    qty_at_mark = Column(Integer)
    # Written ONLY by override O3 (buyer-routed vendor email) and the offer hook —
    # both user-initiated paths, both via release(). NULLed on re-mark (re_arm()).
    # Non-NULL ⇒ record not active. Moves WITH release_trigger — pair enforced by
    # the ck_vendor_part_unavail_release_pair CHECK below.
    released_at = Column(UTCDateTime)
    # ReleaseTrigger value (vendor_email | offer_received) — renders the advisory
    # hint copy via ReleaseTrigger.label. Validated on write (None allowed).
    release_trigger = Column(String(32))
    # Provenance: the requirement the mark was made from (refreshed on re-mark).
    # SET NULL, not CASCADE — knowledge outlives requirements. Lets
    # clear_unavailability find records whose key no longer matches the
    # requirement's current keys (zombie-record fix, IMPORTANT-3).
    requirement_id = Column(Integer, ForeignKey("requirements.id", ondelete="SET NULL"))

    created_by = relationship("User", foreign_keys=[created_by_id])

    @validates("reason")
    def _validate_reason(self, _key, value):
        from ..constants import UnavailabilityReason

        return UnavailabilityReason(value).value  # raises ValueError on unknown

    @validates("release_trigger")
    def _validate_release_trigger(self, _key, value):
        if value is None:  # NULL is the armed state — only the pair CHECK couples it to released_at
            return None
        from ..constants import ReleaseTrigger

        return ReleaseTrigger(value).value  # raises ValueError on unknown

    @validates("condition")
    def _validate_condition(self, _key, value):
        if value is None:
            return None
        if value not in {"new", "refurb", "used"}:
            raise ValueError(f"condition={value!r} not in new/refurb/used")
        return value

    @validates("vendor_name_normalized", "normalized_mpn")
    def _validate_normalized_keys(self, key, value):
        """Re-normalize through the canonical helpers (Requirement @validates precedent)
        — a row whose key isn't normalizer output silently never matches
        record/clear/status/exclusion lookups, so make it unrepresentable."""
        if key == "vendor_name_normalized":
            from ..vendor_utils import normalize_vendor_name

            normalized = normalize_vendor_name(value or "")
        else:
            from ..utils.normalization import normalize_mpn_key

            normalized = normalize_mpn_key(value)
        if not normalized:
            raise ValueError(f"{key}={value!r} normalizes to nothing — record would be unmatchable")
        return normalized

    def release(self, trigger: str, now: datetime) -> None:
        """THE release transition — sets the released_at/release_trigger pair together
        (O3 and the offer hook both route through here)."""
        from ..constants import ReleaseTrigger

        self.release_trigger = ReleaseTrigger(trigger).value  # type: ignore[assignment]  # instrumented attr write (legacy Column model)
        self.released_at = now  # type: ignore[assignment]  # instrumented attr write (legacy Column model)

    def re_arm(self) -> None:
        """Re-mark transition: NULL the release pair together so the record
        suppresses again."""
        self.released_at = None
        self.release_trigger = None

    __table_args__ = (
        Index("ix_vendor_part_unavail_vendor", "vendor_name_normalized"),
        Index("ix_vendor_part_unavail_mpn", "normalized_mpn"),
        Index("ix_vendor_part_unavail_req", "requirement_id"),
        # Partial unique: one row per (vendor, mpn, condition) where condition IS NOT NULL.
        # Both dialect predicates are required — sqlite_where keeps SQLite from emitting a
        # full unique index that would break the coexistence invariant.
        Index(
            "uq_vpu_vendor_mpn_condition",
            "vendor_name_normalized",
            "normalized_mpn",
            "condition",
            unique=True,
            postgresql_where=text("condition IS NOT NULL"),
            sqlite_where=text("condition IS NOT NULL"),
        ),
        # Partial unique: one all-conditions catch-all row per (vendor, mpn) where condition IS NULL.
        Index(
            "uq_vpu_vendor_mpn_allcond",
            "vendor_name_normalized",
            "normalized_mpn",
            unique=True,
            postgresql_where=text("condition IS NULL"),
            sqlite_where=text("condition IS NULL"),
        ),
        # released_at ⇔ release_trigger move together (release()/re_arm() are the
        # only writers) — DB-enforced so a half-released record (advisory UI would
        # render it as merely expired) is unrepresentable. Mirrors migration 103.
        CheckConstraint(
            "(released_at IS NULL) = (release_trigger IS NULL)",
            name="ck_vendor_part_unavail_release_pair",
        ),
    )
