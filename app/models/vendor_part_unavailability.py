"""VendorPartUnavailability — durable "this vendor's stock of this part is gone" fact.

One row per (normalized vendor name, normalized MPN) pair, recording WHY the part is
unavailable from that vendor (UnavailabilityReason), an optional free-text note, and
provenance (who recorded it, when, from which requirement). Outlives scraped Sighting
rows: re-searches that delete + recreate sightings re-stamp ``is_unavailable`` from
these records, and RFQ vendor suggestions exclude matching vendors. Marking again for
an existing key is an update, not a new row (unique constraint on the pair).

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

from datetime import datetime, timezone

from sqlalchemy import Column, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import relationship, validates

from ..database import UTCDateTime
from .base import Base


class VendorPartUnavailability(Base):
    __tablename__ = "vendor_part_unavailability"

    id = Column(Integer, primary_key=True)

    # normalize_vendor_name() of the vendor display name (app/vendor_utils.py).
    vendor_name_normalized = Column(String(255), nullable=False)
    # normalize_mpn_key() canonical dash-stripped key (app/utils/normalization.py) —
    # same key space offers use, so matched-MPN and primary-MPN lookups line up.
    normalized_mpn = Column(String(255), nullable=False)

    reason = Column(String(32), nullable=False)  # UnavailabilityReason value
    note = Column(Text)  # free-text "what we learned"

    created_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    # Dual default (Python + server) — sibling pattern (e.g. OemSpecCode); avoids
    # None-before-flush in tests while keeping a DB-side default for raw inserts.
    created_at = Column(
        UTCDateTime,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    # Per-key qty snapshot at mark/re-mark: max non-NULL qty_available over the
    # vendor's sightings whose normalize_mpn_key(mpn_matched) equals THIS record's
    # key (empty-mpn rows count toward the primary-key record). Never cross-key.
    # Re-mark keeps the old value when the new computation is NULL. NULL ⇒ the O2
    # restock override never fires (fail-closed for legacy/pre-098 records).
    qty_at_mark = Column(Integer)
    # Written ONLY by override O3 (buyer-routed vendor email) and the offer hook —
    # both user-initiated paths. NULLed on re-mark. Non-NULL ⇒ record not active.
    released_at = Column(UTCDateTime)
    # 'vendor_email' | 'offer_received' — renders the advisory hint copy.
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

    __table_args__ = (
        Index("ix_vendor_part_unavail_vendor", "vendor_name_normalized"),
        Index("ix_vendor_part_unavail_mpn", "normalized_mpn"),
        Index("ix_vendor_part_unavail_req", "requirement_id"),
        UniqueConstraint("vendor_name_normalized", "normalized_mpn", name="uq_vendor_part_unavail_vendor_mpn"),
    )
