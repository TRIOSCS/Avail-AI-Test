"""Vendor models — VendorCard, VendorContact, VendorReview."""

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import relationship

from .base import Base


class VendorCard(Base):
    __tablename__ = "vendor_cards"
    id = Column(Integer, primary_key=True)
    normalized_name = Column(String(255), nullable=False, unique=True, index=True)
    display_name = Column(String(255), nullable=False)
    domain = Column(String(255), index=True)
    domain_aliases = Column(JSON, default=list)
    website = Column(String(500))
    emails = Column(JSON, default=list)
    phones = Column(JSON, default=list)
    contacts = Column(JSON, default=list)
    alternate_names = Column(JSON, default=list)
    sighting_count = Column(Integer, default=0)
    is_blacklisted = Column(Boolean, default=False)
    source = Column(String(50))
    raw_response = Column(Text)

    # Enrichment fields (shared structure with Company)
    linkedin_url = Column(String(500))
    legal_name = Column(String(500))
    employee_size = Column(String(50))
    hq_city = Column(String(255))
    hq_state = Column(String(100))
    hq_country = Column(String(100))
    industry = Column(String(255))

    last_enriched_at = Column(DateTime)
    enrichment_source = Column(String(50))

    # Acctivate sync fields — behavioral truth
    acctivate_vendor_id = Column(String(255), index=True)
    cancellation_rate = Column(Float)
    rma_rate = Column(Float)
    acctivate_total_orders = Column(Integer)
    acctivate_total_units = Column(Integer)
    acctivate_last_order_date = Column(Date)
    last_synced_at = Column(DateTime)

    # Engagement scoring (Email Mining v2 Upgrade 4)
    total_outreach = Column(Integer, default=0)
    total_responses = Column(Integer, default=0)
    total_wins = Column(Integer, default=0)
    ghost_rate = Column(Float)
    response_velocity_hours = Column(Float)
    last_contact_at = Column(DateTime)
    relationship_months = Column(Integer)
    engagement_score = Column(Float)
    engagement_computed_at = Column(DateTime)

    # v1.3.0: Vendor scorecard fields
    avg_response_hours = Column(Float)
    overall_win_rate = Column(Float)
    total_pos = Column(Integer, default=0)
    total_revenue = Column(Numeric(14, 2), default=0)
    last_activity_at = Column(DateTime)

    # AI-generated material intelligence
    brand_tags = Column(JSON, default=list)
    commodity_tags = Column(JSON, default=list)
    material_tags_updated_at = Column(DateTime)

    # Deep enrichment tracking
    deep_enrichment_at = Column(DateTime)
    specialty_confidence = Column(Float)
    email_history_scanned_at = Column(DateTime)

    # Full-text search (PostgreSQL tsvector, managed by trigger)
    search_vector = Column(TSVECTOR)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    reviews = relationship(
        "VendorReview", back_populates="vendor_card", cascade="all, delete-orphan"
    )
    vendor_contacts = relationship(
        "VendorContact", back_populates="vendor_card", cascade="all, delete-orphan"
    )


class VendorContact(Base):
    __tablename__ = "vendor_contacts"
    id = Column(Integer, primary_key=True)
    vendor_card_id = Column(
        Integer, ForeignKey("vendor_cards.id", ondelete="CASCADE"), nullable=False
    )
    contact_type = Column(String(20), default="company")
    full_name = Column(String(255))
    title = Column(String(255))
    label = Column(String(100))
    email = Column(String(255))
    phone = Column(String(100))
    phone_type = Column(String(20))
    linkedin_url = Column(String(500))
    source = Column(String(50), nullable=False)
    is_verified = Column(Boolean, default=False)
    confidence = Column(Integer, default=50)
    interaction_count = Column(Integer, default=0)
    last_interaction_at = Column(DateTime)
    first_seen_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_seen_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    vendor_card = relationship("VendorCard", back_populates="vendor_contacts")

    __table_args__ = (
        Index("ix_vendor_contacts_card", "vendor_card_id"),
        Index("ix_vendor_contacts_email", "email"),
        Index("ix_vendor_contacts_card_email", "vendor_card_id", "email", unique=True),
    )


class VendorReview(Base):
    __tablename__ = "vendor_reviews"
    id = Column(Integer, primary_key=True)
    vendor_card_id = Column(Integer, ForeignKey("vendor_cards.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    rating = Column(Integer, nullable=False)
    comment = Column(String(500))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    vendor_card = relationship("VendorCard", back_populates="reviews")
    user = relationship("User")

    __table_args__ = (
        Index("ix_review_vendor", "vendor_card_id"),
        Index("ix_review_user", "user_id"),
    )
