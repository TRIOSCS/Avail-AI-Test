"""Discovery batch model — tracks every enrichment/discovery run."""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from .base import Base


class DiscoveryBatch(Base):
    """Audit trail for every discovery or enrichment run.

    Tracks API credits consumed, dedup counts, and search filters so runs can be
    reproduced and costs monitored.
    """

    __tablename__ = "discovery_batches"

    id = Column(Integer, primary_key=True)
    batch_id = Column(String(100), unique=True, nullable=False)
    source = Column(String(50), nullable=False)
    segment = Column(String(100))
    regions = Column(JSONB, default=list)
    search_filters = Column(JSONB, default=dict)

    # Run status
    status = Column(String(20), default="running")
    prospects_found = Column(Integer, default=0)
    prospects_new = Column(Integer, default=0)
    prospects_updated = Column(Integer, default=0)
    credits_used = Column(Integer, default=0)
    error_message = Column(Text)

    started_at = Column(DateTime, nullable=False)
    completed_at = Column(DateTime)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_discovery_batches_status", "status"),
        Index("ix_discovery_batches_source_status", "source", "status"),
        Index("ix_discovery_batches_started_at", "started_at"),
    )
