"""Vendor models — VendorCard, VendorContact, VendorReview, VendorCardAttachment,
VendorContactAttachment.

Called by: routers/htmx_views.py, routers/vendor_contacts.py, routers/vendors_crud.py,
           services/strategic_vendor_service.py, tests
Depends on: database.UTCDateTime, models.base.Base
"""

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import relationship, validates

from ..database import UTCDateTime
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
    normalized_phones = Column(JSON, default=list)  # E.164 list, derived from phones
    contacts = Column(JSON, default=list)
    alternate_names = Column(JSON, default=list)
    sighting_count = Column(Integer, default=0)
    is_blacklisted = Column(Boolean, default=False)
    is_broadcast = Column(Boolean, default=False)  # Always include in stock inquiries
    source = Column(String(50))

    # Enrichment fields (shared structure with Company)
    linkedin_url = Column(String(500))
    legal_name = Column(String(500))
    employee_size = Column(String(50))
    hq_city = Column(String(255))
    hq_state = Column(String(100))
    hq_country = Column(String(100))
    industry = Column(String(255))

    last_enriched_at = Column(UTCDateTime)
    enrichment_source = Column(String(50))

    # Firmographic / provenance enrichment (Explorium+Clay blending)
    ticker = Column(String(20))
    naics = Column(String(20))
    revenue_range = Column(String(50))
    enrichment_provenance = Column(JSONB, default=dict, server_default="{}")

    cancellation_rate = Column(Float)
    # PO-cancellation performance (po_cancellation_service): avg days from PO-cut to
    # cancel (longer = worse), and count of "slow" cancels (> threshold days) which
    # weigh the vendor score down harder. Refreshed inline at re-source + nightly.
    avg_days_to_cancel = Column(Float)
    slow_cancel_count = Column(Integer, default=0)

    # Engagement scoring (Email Mining v2 Upgrade 4)
    total_outreach = Column(Integer, default=0)
    total_responses = Column(Integer, default=0)
    total_wins = Column(Integer, default=0)
    ghost_rate = Column(Float)
    response_velocity_hours = Column(Float)
    last_contact_at = Column(UTCDateTime)
    relationship_months = Column(Integer)
    engagement_score = Column(Float)
    engagement_computed_at = Column(UTCDateTime)

    # Unified vendor score (order advancement based)
    vendor_score = Column(Float)  # 0-100 unified score, or None
    advancement_score = Column(Float)  # 0-100 raw advancement component
    is_new_vendor = Column(Boolean, default=True)
    vendor_score_computed_at = Column(UTCDateTime)

    # v1.3.0: Vendor scorecard fields
    avg_response_hours = Column(Float)
    overall_win_rate = Column(Float)
    total_pos = Column(Integer, default=0)
    total_revenue = Column(Numeric(12, 4), default=0)
    last_activity_at = Column(UTCDateTime)
    last_outbound_at = Column(UTCDateTime)
    last_reply_at = Column(UTCDateTime)

    # AI-generated material intelligence
    brand_tags = Column(JSONB, default=list)
    commodity_tags = Column(JSONB, default=list)
    material_tags_updated_at = Column(UTCDateTime)

    # Email health scoring (Phase 5)
    email_health_score = Column(Float)  # 0-100 composite score
    email_health_computed_at = Column(UTCDateTime)
    response_rate = Column(Float)  # 0.0-1.0 ratio
    quote_quality_rate = Column(Float)  # 0.0-1.0 ratio

    # Deep enrichment tracking
    deep_enrichment_at = Column(UTCDateTime)

    # Additional details (key:value pairs, mirrors Company.custom_fields)
    custom_fields = Column(JSONB, default=dict, server_default="{}")

    # Full-text search (PostgreSQL tsvector, managed by trigger)
    search_vector = Column(TSVECTOR)

    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        UTCDateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    reviews = relationship("VendorReview", back_populates="vendor_card", cascade="all, delete-orphan")
    vendor_contacts = relationship("VendorContact", back_populates="vendor_card", cascade="all, delete-orphan")
    strategic_vendors = relationship("StrategicVendor", back_populates="vendor_card")
    attachments = relationship("VendorCardAttachment", back_populates="vendor_card", cascade="all, delete-orphan")

    # --- Validators ---
    @validates(
        "response_rate",
        "cancellation_rate",
        "ghost_rate",
        "overall_win_rate",
        "quote_quality_rate",
    )
    def _validate_rate(self, _key, value):
        if value is not None and not (0.0 <= value <= 1.0):
            raise ValueError(f"Rate {_key} must be 0.0-1.0, got {value}")
        return value

    @validates("vendor_score", "advancement_score", "email_health_score")
    def _validate_score(self, _key, value):
        if value is not None and not (0 <= value <= 100):
            raise ValueError(f"Score {_key} must be 0-100, got {value}")
        return value

    @validates("phones")
    def _sync_normalized_phones(self, _key, value):
        """Keep normalized_phones (list of E.164) in sync with phones on every write."""
        from ..utils.phone import normalize_e164

        if not value:
            self.normalized_phones = []
            return value
        normalized = [normalize_e164(p) for p in value if p]
        self.normalized_phones = [n for n in normalized if n is not None]
        return value

    @validates("custom_fields")
    def _validate_custom_fields(self, _key, value):
        """Cap: max 30 keys, key max 60 chars, value max 500 chars."""
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("custom_fields must be a dict")
        if len(value) > 30:
            raise ValueError("custom_fields: max 30 keys")
        for k, v in value.items():
            if len(str(k)) > 60:
                raise ValueError(f"custom_fields key too long: {k!r}")
            if len(str(v)) > 500:
                raise ValueError(f"custom_fields value too long for key {k!r}")
        return value

    __table_args__ = (
        Index("ix_vendor_cards_created_at", "created_at"),
        Index("ix_vendor_cards_score_computed_at", "vendor_score_computed_at"),
        Index(
            "ix_vendor_cards_active",
            "created_at",
            postgresql_where=Column("is_blacklisted").is_(False),
        ),
    )


class VendorContact(Base):
    __tablename__ = "vendor_contacts"
    id = Column(Integer, primary_key=True)
    vendor_card_id = Column(Integer, ForeignKey("vendor_cards.id", ondelete="CASCADE"), nullable=False)
    contact_type = Column(String(20), default="company")
    full_name = Column(String(255))
    first_name = Column(String(100))
    last_name = Column(String(100))
    title = Column(String(255))
    label = Column(String(100))
    email = Column(String(255))
    phone = Column(String(100))
    normalized_phone = Column(String(20), index=True)
    phone_mobile = Column(String(100))
    phone_type = Column(String(20))
    linkedin_url = Column(String(500))
    source = Column(String(50), nullable=False)
    is_verified = Column(Boolean, default=False)
    confidence = Column(Integer, default=50)
    interaction_count = Column(Integer, default=0)
    last_interaction_at = Column(UTCDateTime)
    first_seen_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))
    last_seen_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))

    # Contact intelligence (computed nightly)
    relationship_score = Column(Float)  # 0-100
    activity_trend = Column(String(20))  # warming/stable/cooling/dormant
    score_computed_at = Column(UTCDateTime)

    # OOO detection (from AI email classification)
    is_ooo = Column(Boolean, default=False)
    ooo_return_date = Column(UTCDateTime)
    last_outbound_at = Column(UTCDateTime)
    last_reply_at = Column(UTCDateTime)

    # Primary contact flag (one per vendor card; set-primary clears others)
    is_primary = Column(Boolean, nullable=False, default=False, server_default="false")

    vendor_card = relationship("VendorCard", back_populates="vendor_contacts")
    attachments = relationship("VendorContactAttachment", back_populates="vendor_contact", cascade="all, delete-orphan")

    # --- Validators ---
    @validates("email")
    def _validate_email(self, _key, value):
        if value and "@" not in value:
            raise ValueError(f"Invalid email: {value!r} (missing '@')")
        return value

    @validates("confidence")
    def _validate_confidence(self, _key, value):
        if value is not None and not (0 <= value <= 100):
            raise ValueError(f"Confidence must be 0-100, got {value}")
        return value

    @validates("phone")
    def _sync_normalized_phone(self, _key, value):
        """Keep normalized_phone (E.164) in sync with phone on every write."""
        from ..utils.phone import normalize_e164

        self.normalized_phone = normalize_e164(value)
        return value

    __table_args__ = (
        Index("ix_vendor_contacts_card", "vendor_card_id"),
        Index("ix_vendor_contacts_email", "email"),
        Index("ix_vendor_contacts_card_email", "vendor_card_id", "email", unique=True),
    )


class VendorReview(Base):
    __tablename__ = "vendor_reviews"
    id = Column(Integer, primary_key=True)
    vendor_card_id = Column(Integer, ForeignKey("vendor_cards.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    rating = Column(Integer, nullable=False)
    comment = Column(String(500))
    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))

    vendor_card = relationship("VendorCard", back_populates="reviews")
    user = relationship("User")

    # --- Validators ---
    @validates("rating")
    def _validate_rating(self, _key, value):
        if value is not None and not (1 <= value <= 5):
            raise ValueError(f"Rating must be 1-5, got {value}")
        return value

    __table_args__ = (
        Index("ix_review_vendor", "vendor_card_id"),
        Index("ix_review_user", "user_id"),
    )


class VendorCardAttachment(Base):
    """File attachment on a vendor card (stored in OneDrive or company SharePoint
    library).

    Mirrors CompanyAttachment shape exactly.
    library_drive_id NULL  → OneDrive fallback row (user token)
    library_drive_id set   → company SharePoint library row (app token)

    Called by: app/routers/attachments_extra.py, app/services/attachment_service.py
    Depends on: VendorCard, User
    """

    __tablename__ = "vendor_card_attachments"
    id = Column(Integer, primary_key=True)
    vendor_card_id = Column(Integer, ForeignKey("vendor_cards.id", ondelete="CASCADE"), nullable=False)
    file_name = Column(String(500), nullable=False)
    library_item_id = Column(String(500))
    library_drive_id = Column(String(200))
    library_web_url = Column(Text)
    thumbnail_url = Column(Text)
    content_type = Column(String(100))
    size_bytes = Column(Integer)
    uploaded_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))

    vendor_card = relationship("VendorCard", back_populates="attachments")
    uploaded_by = relationship("User", foreign_keys=[uploaded_by_id])

    __table_args__ = (
        Index("ix_vendor_card_attachments_card", "vendor_card_id"),
        Index("ix_vendor_card_attachments_item", "library_item_id"),
    )


class VendorContactAttachment(Base):
    """File attachment on a vendor contact (stored in OneDrive or company SharePoint
    library).

    Mirrors SiteContactAttachment shape exactly.
    library_drive_id NULL  → OneDrive fallback row (user token)
    library_drive_id set   → company SharePoint library row (app token)

    Called by: app/routers/attachments_extra.py, app/services/attachment_service.py
    Depends on: VendorContact, User
    """

    __tablename__ = "vendor_contact_attachments"
    id = Column(Integer, primary_key=True)
    vendor_contact_id = Column(Integer, ForeignKey("vendor_contacts.id", ondelete="CASCADE"), nullable=False)
    file_name = Column(String(500), nullable=False)
    library_item_id = Column(String(500))
    library_drive_id = Column(String(200))
    library_web_url = Column(Text)
    thumbnail_url = Column(Text)
    content_type = Column(String(100))
    size_bytes = Column(Integer)
    uploaded_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))

    vendor_contact = relationship("VendorContact", back_populates="attachments")
    uploaded_by = relationship("User", foreign_keys=[uploaded_by_id])

    __table_args__ = (
        Index("ix_vendor_contact_attachments_contact", "vendor_contact_id"),
        Index("ix_vendor_contact_attachments_item", "library_item_id"),
    )
