"""OemCrosswalk — permanent OEM spare→canonical-MPN cache (PartSurfer/PSREF lookups).

Each row caches one grounded web resolution of an OEM/system-vendor spare PN
(HP/HPE ``918042-001``-style for Phase A) to the canonical manufacturer MPN it
relabels, including NEGATIVE rows (``no_match``): a spare costs exactly one web call
ever. ``resolved`` rows are permanent; ``no_match`` rows block re-resolution for 90
days from ``looked_up_at`` (NO_MATCH_RETRY_DAYS in oem_crosswalk_enrich) and are
updated in place on retry.

Called by: app/services/enrichment_worker/worker.py (Pass A resolution upsert),
app/services/oem_crosswalk_enrich.py (Pass B deterministic writer + freshness),
app/management/backfill_oem_crosswalk.py (paced drain CLI).
Depends on: app/constants.OemCrosswalkStatus (status vocabulary), Base, UTCDateTime.
"""

from datetime import datetime, timezone

from sqlalchemy import JSON, Column, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import validates

from ..database import UTCDateTime
from .base import Base

# Vendors the crosswalk accepts — the classify_oem_vendor labels that have a stable
# official lookup surface (Phase A: HP/HPE PartSurfer; Phase B: Lenovo PSREF). Dell/
# Acer/ASUS classifier labels stay on the ephemeral cross_reference_mpn path.
VALID_CROSSWALK_VENDORS: frozenset[str] = frozenset({"hpe", "lenovo"})


class OemCrosswalk(Base):
    __tablename__ = "oem_crosswalk"

    id = Column(Integer, primary_key=True)

    # raw = the spare PN as displayed on the card; norm = normalize_mpn_key(raw)
    # (the join key Pass B matches batch cards' display_mpn against).
    spare_raw = Column(String(64), nullable=False)
    spare_norm = Column(String(64), nullable=False)
    vendor = Column(String(16), nullable=False)  # VALID_CROSSWALK_VENDORS member
    status = Column(String(16), nullable=False)  # OemCrosswalkStatus value

    # NULL iff status == no_match.
    canonical_mpn_raw = Column(String(64))
    canonical_mpn_norm = Column(String(64))
    canonical_manufacturer = Column(String(128))
    title = Column(Text)  # OEM page part title/description verbatim
    confidence = Column(Float)  # resolver confidence (>= 0.90 when resolved)
    source_url = Column(Text)
    source_domain = Column(String(128))
    payload = Column(JSON)  # full raw extraction (forensics)

    # Drives the negative-cache retry window (no_match rows stale after 90 days).
    looked_up_at = Column(UTCDateTime, nullable=False)

    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        UTCDateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    @validates("vendor")
    def _validate_vendor(self, _key, value):
        if value not in VALID_CROSSWALK_VENDORS:
            raise ValueError(f"oem_crosswalk.vendor must be one of {sorted(VALID_CROSSWALK_VENDORS)}, got {value!r}")
        return value

    @validates("status")
    def _validate_status(self, _key, value):
        from ..constants import OemCrosswalkStatus

        return OemCrosswalkStatus(value).value  # raises ValueError on unknown

    __table_args__ = (
        Index("ix_oem_crosswalk_spare_norm", "spare_norm"),
        Index("ix_oem_crosswalk_canonical_norm", "canonical_mpn_norm"),
        Index("ix_oem_crosswalk_status", "status"),
        UniqueConstraint("spare_norm", "vendor", "source_domain", name="uq_oem_crosswalk_edge"),
    )
