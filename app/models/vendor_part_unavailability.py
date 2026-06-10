"""VendorPartUnavailability — durable "this vendor's stock of this part is gone" fact.

One row per (normalized vendor name, normalized MPN) pair, recording WHY the part is
unavailable from that vendor (UnavailabilityReason), an optional free-text note, and
provenance (who recorded it, when). Outlives scraped Sighting rows: re-searches that
delete + recreate sightings re-stamp ``is_unavailable`` from these records, and RFQ
vendor suggestions exclude matching vendors. Marking again for an existing key is an
update, not a new row (unique constraint on the pair).

Called by: app/services/vendor_unavailability.py (record/clear/apply/exclude),
           app/services/sighting_status.py (durable unavailable-status branch)
Depends on: app/constants.UnavailabilityReason (reason vocabulary), Base, users table
"""

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
    created_at = Column(UTCDateTime, server_default=func.now())

    created_by = relationship("User", foreign_keys=[created_by_id])

    @validates("reason")
    def _validate_reason(self, _key, value):
        from ..constants import UnavailabilityReason

        return UnavailabilityReason(value).value  # raises ValueError on unknown

    __table_args__ = (
        Index("ix_vendor_part_unavail_vendor", "vendor_name_normalized"),
        Index("ix_vendor_part_unavail_mpn", "normalized_mpn"),
        UniqueConstraint("vendor_name_normalized", "normalized_mpn", name="uq_vendor_part_unavail_vendor_mpn"),
    )
