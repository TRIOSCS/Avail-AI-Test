"""OemCrosswalk — permanent OEM spare→canonical-MPN cache (PartSurfer/PSREF lookups).

Each row caches one grounded web resolution of an OEM/system-vendor spare PN
(HP/HPE ``918042-001``-style for Phase A) to the canonical manufacturer MPN it
relabels, including NEGATIVE rows (``no_match``): a spare costs exactly one web call
ever. ``resolved`` rows are permanent; ``no_match`` rows block re-resolution for 90
days from ``looked_up_at`` (NO_MATCH_RETRY_DAYS in oem_crosswalk_enrich) and are
updated in place on retry. ``no_match`` rows store ``source_domain = ''`` (a NOT NULL
sentinel, never NULL — NULLs are pairwise-distinct inside a UNIQUE constraint, so a
nullable domain would let duplicate negatives accumulate), which makes
``uq_oem_crosswalk_edge`` enforce ONE negative row per (spare_norm, vendor) at the DB
level. ``ck_oem_crosswalk_status_canonical`` enforces the status×canonical invariant
(canonical_mpn_norm is non-NULL iff status='resolved'); both writers go through the
single ``oem_crosswalk_enrich.apply_resolution`` helper that maintains it.

Called by: app/services/enrichment_worker/worker.py (Pass A resolution upsert),
app/services/oem_crosswalk_enrich.py (Pass B deterministic writer + freshness),
app/management/backfill_oem_crosswalk.py (paced drain CLI).
Depends on: app/constants.OemCrosswalkStatus (status vocabulary), Base, UTCDateTime.
"""

from datetime import UTC, datetime

from sqlalchemy import JSON, CheckConstraint, Column, Float, Index, Integer, String, Text, UniqueConstraint
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

    # NULL iff status == no_match (ck_oem_crosswalk_status_canonical pins the
    # canonical_mpn_norm leg; apply_resolution maintains the rest).
    canonical_mpn_raw = Column(String(64))
    canonical_mpn_norm = Column(String(64))
    canonical_manufacturer = Column(String(128))
    title = Column(Text)  # OEM page part title/description verbatim
    confidence = Column(Float)  # resolver confidence (>= 0.90 when resolved)
    source_url = Column(Text)
    # '' (NOT NULL sentinel) on no_match rows so uq_oem_crosswalk_edge dedupes
    # negatives at the DB level — NULLs are pairwise-distinct in a UNIQUE constraint.
    source_domain = Column(String(128), nullable=False, default="", server_default="")
    payload = Column(JSON)  # full raw extraction (forensics)

    # Drives the negative-cache retry window (no_match rows stale after 90 days).
    looked_up_at = Column(UTCDateTime, nullable=False)

    created_at = Column(UTCDateTime, default=lambda: datetime.now(UTC))
    updated_at = Column(
        UTCDateTime,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
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
        # status×canonical nullability invariant — portable boolean equality (PG
        # bool = bool; SQLite 1/0 = 1/0): resolved rows MUST carry a canonical norm,
        # no_match rows must NOT.
        CheckConstraint(
            "(status = 'resolved') = (canonical_mpn_norm IS NOT NULL)",
            name="ck_oem_crosswalk_status_canonical",
        ),
    )
