"""PartsurferDescNegative — durable negative cache for PartSurfer DESCRIPTION misses.

Each row records that one normalized HP/HPE spare PN produced NO usable PartSurfer
DESCRIPTION, so the enrichment worker's ``_partsurfer_desc_pass`` stops re-fetching it
live from partsurfer.hpe.com every batch. This is a DIFFERENT sub-resource from the
``oem_crosswalk`` cache: oem_crosswalk caches the spare->CANONICAL-MPN web resolution
(Pass A, Claude web_search); this caches the spare->verbatim-DESCRIPTION direct fetch.
A spare can miss one and hit the other, so the two negatives are kept independently
(reusing oem_crosswalk's ``(spare_norm, 'hpe', '')`` no_match key would conflate them
-- a "no description" would wrongly block the canonical resolution, and vice-versa).

Two miss reasons drive two retry windows (``PARTSURFER_NO_RESULT_RETRY_DAYS`` /
``PARTSURFER_UNGRAMMATICAL_RETRY_DAYS`` in the service):
- ``no_result`` -- the fetch returned no description (404/3xx, missing/empty
  ``lblDescription``). PartSurfer genuinely catalogs nothing for this spare -> long
  (90-day) window, the same policy as oem_crosswalk no_match.
- ``ungrammatical`` -- a description WAS returned but the desc-grammar declined to
  categorize it (an opaque/truncated reply). That is NOT evidence the part is absent,
  only that the grammar could not parse this reply, so it is a SHORT-retry negative
  (the grammar improves over time) -- never poisons a permanent cache.

``retry_after`` is stored per row (= ``looked_up_at`` + the reason's window) so the
selector is a single indexed comparison and the policy is auditable on the row. A
throttle/outage (``PartSurferTransient``) is NEVER cached -- only ``None`` no-results
and grammar declines are.

Called by: app/services/enrichment_worker/partsurfer_negative_cache.py (selector +
writer), app/services/enrichment_worker/worker.py (the desc pass consults/records it).
Depends on: app/database.UTCDateTime, Base.
"""

from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Column, Index, Integer, String, UniqueConstraint

from ..database import UTCDateTime
from .base import Base

# The two miss reasons a row may record (see module docstring for the policy each drives).
PARTSURFER_NEGATIVE_REASONS: frozenset[str] = frozenset({"no_result", "ungrammatical"})


class PartsurferDescNegative(Base):
    __tablename__ = "partsurfer_desc_negative"

    id = Column(Integer, primary_key=True)

    # norm = normalize_mpn_key(display_mpn) -- the dedup + lookup key (one row per spare).
    spare_norm = Column(String(64), nullable=False)
    spare_raw = Column(String(64), nullable=False)  # last-seen display form (forensics)

    # PARTSURFER_NEGATIVE_REASONS member -- drives the retry window.
    reason = Column(String(16), nullable=False)

    # When the miss was recorded, and when re-fetching becomes allowed again. Stored
    # explicitly so the selector is one indexed comparison (looked_up_at + window).
    looked_up_at = Column(UTCDateTime, nullable=False)
    retry_after = Column(UTCDateTime, nullable=False)

    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        UTCDateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        # One row per spare -- the upsert key. Named to match migration 125 exactly.
        UniqueConstraint("spare_norm", name="uq_partsurfer_neg_spare_norm"),
        # Selector filters on retry_after (skip rows still blocked) -- index it.
        Index("ix_partsurfer_neg_retry_after", "retry_after"),
        CheckConstraint(
            "reason IN ('no_result', 'ungrammatical')",
            name="ck_partsurfer_neg_reason",
        ),
    )
