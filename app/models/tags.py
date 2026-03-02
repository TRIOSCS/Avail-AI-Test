"""Tagging models — AI classification tags on materials, propagated to entities.

Tags are applied to MaterialCards via a classification waterfall (existing data →
prefix lookup → Nexar → AI). Entity tags aggregate interactions and use a two-gate
visibility system (min_count AND min_percentage) to control display.

Called by: app.services.tagging, app.routers.tags, app.routers.tagging_admin
Depends on: app.models.base (Base), app.models.intelligence (MaterialCard)
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
from sqlalchemy.orm import relationship

from .base import Base


class Tag(Base):
    """A brand or commodity taxonomy tag."""

    __tablename__ = "tags"
    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    tag_type = Column(String(20), nullable=False)  # 'brand' or 'commodity'
    parent_id = Column(Integer, ForeignKey("tags.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    parent = relationship("Tag", remote_side=[id], foreign_keys=[parent_id])
    material_tags = relationship("MaterialTag", back_populates="tag", cascade="all, delete-orphan")
    entity_tags = relationship("EntityTag", back_populates="tag", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("name", "tag_type", name="uq_tags_name_type"),
        Index("ix_tags_tag_type", "tag_type"),
    )

    def __repr__(self):
        return f"<Tag id={self.id} name={self.name!r} type={self.tag_type!r}>"


class MaterialTag(Base):
    """Links a Tag to a MaterialCard with classification metadata."""

    __tablename__ = "material_tags"
    id = Column(Integer, primary_key=True)
    material_card_id = Column(Integer, ForeignKey("material_cards.id", ondelete="CASCADE"), nullable=False)
    tag_id = Column(Integer, ForeignKey("tags.id", ondelete="CASCADE"), nullable=False)
    confidence = Column(Float, nullable=False, default=0.0)
    source = Column(String(30), nullable=False)  # existing_data, prefix_lookup, nexar, ai_classified
    classified_at = Column(DateTime, nullable=True)

    tag = relationship("Tag", back_populates="material_tags")

    __table_args__ = (
        UniqueConstraint("material_card_id", "tag_id", name="uq_material_tags_card_tag"),
        Index("ix_material_tags_tag_id", "tag_id"),
        Index("ix_material_tags_source", "source"),
    )

    def __repr__(self):
        return f"<MaterialTag card={self.material_card_id} tag={self.tag_id} src={self.source!r}>"


class EntityTag(Base):
    """Propagated tag on a vendor/customer entity with interaction counts and visibility."""

    __tablename__ = "entity_tags"
    id = Column(Integer, primary_key=True)
    entity_type = Column(String(20), nullable=False)  # vendor_card, customer_site, company
    entity_id = Column(Integer, nullable=False)
    tag_id = Column(Integer, ForeignKey("tags.id", ondelete="CASCADE"), nullable=False)
    interaction_count = Column(Float, nullable=False, default=0)
    total_entity_interactions = Column(Float, nullable=False, default=0)
    is_visible = Column(Boolean, nullable=False, default=False)
    first_seen_at = Column(DateTime, nullable=True)
    last_seen_at = Column(DateTime, nullable=True)

    tag = relationship("Tag", back_populates="entity_tags")

    __table_args__ = (
        UniqueConstraint("entity_type", "entity_id", "tag_id", name="uq_entity_tags_type_id_tag"),
        Index("ix_entity_tags_type_tag_visible", "entity_type", "tag_id", "is_visible"),
        Index("ix_entity_tags_type_id", "entity_type", "entity_id"),
    )

    def __repr__(self):
        return f"<EntityTag {self.entity_type}:{self.entity_id} tag={self.tag_id} visible={self.is_visible}>"


class TagThresholdConfig(Base):
    """Visibility thresholds per entity_type + tag_type combination."""

    __tablename__ = "tag_threshold_config"
    id = Column(Integer, primary_key=True)
    entity_type = Column(String(20), nullable=False)
    tag_type = Column(String(20), nullable=False)
    min_count = Column(Integer, nullable=False)
    min_percentage = Column(Float, nullable=False)

    __table_args__ = (
        UniqueConstraint("entity_type", "tag_type", name="uq_threshold_entity_tag"),
    )

    def __repr__(self):
        return f"<TagThresholdConfig {self.entity_type}/{self.tag_type} min={self.min_count}/{self.min_percentage}>"
