"""Faceted search data models.

What: CommoditySpecSchema, MaterialSpecFacet, MaterialSpecConflict tables.
Called by: spec_write_service, faceted search queries (SP3).
Depends on: Base from app.models.base, MaterialCard FK.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from app.models.base import Base


class CommoditySpecSchema(Base):
    """Metadata registry — defines what specs each commodity has."""

    __tablename__ = "commodity_spec_schemas"

    id = Column(Integer, primary_key=True)
    commodity = Column(String(100), nullable=False)
    spec_key = Column(String(100), nullable=False)
    display_name = Column(String(100), nullable=False)
    data_type = Column(String(20), nullable=False)  # enum, numeric, boolean
    unit = Column(String(20))  # Display unit: "GB", "pF"
    canonical_unit = Column(String(20))  # Storage unit after normalization
    enum_values = Column(JSONB)  # ["DDR3", "DDR4", "DDR5"] for enum types
    numeric_range = Column(JSONB)  # {"min": 0, "max": 1000000}
    sort_order = Column(Integer, default=0)
    is_filterable = Column(Boolean, default=True, server_default="true")
    is_primary = Column(Boolean, default=False, server_default="false")

    __table_args__ = (UniqueConstraint("commodity", "spec_key", name="uq_css_commodity_spec_key"),)


class MaterialSpecFacet(Base):
    """Denormalized, typed, indexed projection for fast faceted queries."""

    __tablename__ = "material_spec_facets"

    id = Column(Integer, primary_key=True)
    material_card_id = Column(
        Integer,
        ForeignKey("material_cards.id", ondelete="CASCADE"),
        nullable=False,
    )
    category = Column(String(100), nullable=False)
    spec_key = Column(String(100), nullable=False)
    value_text = Column(String(255))
    value_numeric = Column(Float)
    value_unit = Column(String(20))

    material_card = relationship("MaterialCard", back_populates="spec_facets")

    __table_args__ = (
        UniqueConstraint("material_card_id", "spec_key", name="uq_msf_card_spec"),
        Index("ix_msf_category_key", "category", "spec_key"),
        Index("ix_msf_category_key_text", "category", "spec_key", "value_text"),
        Index(
            "ix_msf_key_numeric",
            "spec_key",
            "value_numeric",
            postgresql_where="value_numeric IS NOT NULL",
        ),
        Index("ix_msf_key_text_card", "spec_key", "value_text", "material_card_id"),
        Index("ix_msf_card", "material_card_id"),
    )


class MaterialSpecConflict(Base):
    """Audit log for when sources disagree on a spec value."""

    __tablename__ = "material_spec_conflicts"

    id = Column(Integer, primary_key=True)
    material_card_id = Column(
        Integer,
        ForeignKey("material_cards.id", ondelete="CASCADE"),
        nullable=False,
    )
    spec_key = Column(String(100), nullable=False)
    existing_value = Column(String(255))
    existing_source = Column(String(50))
    existing_confidence = Column(Float)
    incoming_value = Column(String(255))
    incoming_source = Column(String(50))
    incoming_confidence = Column(Float)
    resolution = Column(String(20), nullable=False)  # kept_existing, overwrote, flagged
    resolved_by = Column(String(50), default="auto")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    material_card = relationship("MaterialCard")
