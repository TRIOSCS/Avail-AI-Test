"""VendorSightingSummary — materialized vendor-level sighting aggregation.

One row per (requirement, vendor) pair. Pre-computes aggregated qty, avg price,
best price, listing count, and score for instant display in the sourcing tab.
Rebuilt when sightings are upserted or deleted.

Called by: sighting_aggregation service, htmx_views sourcing tab
Depends on: Requirement model
"""

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import relationship

from .base import Base


class VendorSightingSummary(Base):
    __tablename__ = "vendor_sighting_summary"
    __table_args__ = (
        UniqueConstraint("requirement_id", "vendor_name", name="uq_vss_req_vendor"),
        Index("ix_vss_requirement", "requirement_id"),
        Index("ix_vss_vendor", "vendor_name"),
        Index("ix_vss_score", "score"),
    )

    id = Column(Integer, primary_key=True)
    requirement_id = Column(Integer, ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False)
    vendor_name = Column(String, nullable=False)
    vendor_phone = Column(String, nullable=True)
    estimated_qty = Column(Integer, nullable=True)
    avg_price = Column(Float, nullable=True)
    best_price = Column(Float, nullable=True)
    listing_count = Column(Integer, nullable=False, default=0)
    source_types = Column(JSON, nullable=True)
    score = Column(Float, nullable=True)
    tier = Column(String(20), nullable=True)
    updated_at = Column(DateTime, nullable=True)

    requirement = relationship("Requirement", back_populates="vendor_summaries")
