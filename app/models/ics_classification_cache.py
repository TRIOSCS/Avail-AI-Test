"""ICsource classification cache model.

Caches AI gate decisions so we don't re-classify the same part number
across different requisitions or worker restarts. Persists classifications
to the database rather than keeping them only in memory.

Business Rules:
- Unique constraint on (normalized_mpn, manufacturer) prevents duplicate entries
- COALESCE handles NULL manufacturers as empty string for uniqueness
- Classifications are immutable once cached — no update needed

Called by: ics_worker.ai_gate (process_ai_gate)
Depends on: nothing (standalone table)
"""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String, UniqueConstraint

from .base import Base


class IcsClassificationCache(Base):
    __tablename__ = "ics_classification_cache"

    id = Column(Integer, primary_key=True)
    normalized_mpn = Column(String(100), nullable=False)
    manufacturer = Column(String(200))
    commodity_class = Column(String(50), nullable=False)
    gate_decision = Column(String(20), nullable=False)
    gate_reason = Column(String(200))
    classified_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint(
            "normalized_mpn",
            "manufacturer",
            name="uq_ics_cache_mpn_mfr",
        ),
    )
