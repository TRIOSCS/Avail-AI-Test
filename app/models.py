"""All database tables."""
from sqlalchemy import (
    Column, String, Integer, DateTime, Text, ForeignKey,
    Numeric, ARRAY, Boolean, Float, Index
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.sql import func
import uuid

Base = declarative_base()


class User(Base):
    __tablename__ = "users"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False)
    display_name = Column(String(255), nullable=False)
    microsoft_id = Column(String(255), unique=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_login_at = Column(DateTime(timezone=True))


class Vendor(Base):
    """A company we buy parts from. Reliability stats update automatically."""
    __tablename__ = "vendors"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False)
    name_normalized = Column(String(255), nullable=False, unique=True)

    vendor_type = Column(String(50), default="broker")   # distributor | broker
    tier = Column(Integer, default=0)                     # 0=unrated, 1=top, 2=good, 3=marginal
    is_authorized = Column(Boolean, default=False)

    email = Column(String(255))
    phone = Column(String(100))
    website = Column(String(500))
    country = Column(String(100))
    contact_name = Column(String(255))

    # These update automatically as you send RFQs and get replies
    total_outreach = Column(Integer, default=0)
    total_responses = Column(Integer, default=0)
    total_wins = Column(Integer, default=0)
    avg_response_hours = Column(Float)

    red_flags = Column(JSONB, default=list)   # ["suspicious_pricing", "slow_responder"]
    is_blocked = Column(Boolean, default=False)
    notes = Column(Text)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    sightings = relationship("Sighting", back_populates="vendor")
    aliases = relationship("VendorAlias", back_populates="vendor")

    __table_args__ = (
        Index("idx_vendors_name", "name_normalized"),
    )

    @property
    def response_rate(self) -> float:
        if not self.total_outreach:
            return 0.0
        return (self.total_responses / self.total_outreach) * 100


class VendorAlias(Base):
    """Alternate names for the same vendor (for deduplication)."""
    __tablename__ = "vendor_aliases"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vendor_id = Column(UUID(as_uuid=True), ForeignKey("vendors.id", ondelete="CASCADE"), nullable=False)
    alias_normalized = Column(String(255), nullable=False, unique=True)
    vendor = relationship("Vendor", back_populates="aliases")


class Sighting(Base):
    """Every time we see a vendor has a part — from search, upload, or email reply."""
    __tablename__ = "sightings"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vendor_id = Column(UUID(as_uuid=True), ForeignKey("vendors.id"), nullable=False)

    part_number = Column(String(255), nullable=False)
    part_number_normalized = Column(String(255), nullable=False)
    manufacturer = Column(String(255))

    quantity = Column(Integer)
    price = Column(Numeric(12, 4))
    currency = Column(String(10), default="USD")
    lead_time_days = Column(Integer)
    lead_time_text = Column(String(100))
    condition = Column(String(50))          # new | refurb | pulled | unknown
    date_code = Column(String(50))
    country_of_origin = Column(String(100))

    source_type = Column(String(50), nullable=False)  # octopart | brokerbin | upload | email_reply
    source_url = Column(String(1000))

    confidence = Column(Integer, default=3)             # 1-5 (5 = verified stock)
    evidence_type = Column(String(50), default="active_listing")
    is_exact_match = Column(Boolean, default=True)
    match_type = Column(String(50), default="exact")
    matched_part = Column(String(255))
    raw_data = Column(JSONB)

    seen_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    upload_id = Column(UUID(as_uuid=True), ForeignKey("uploads.id"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    vendor = relationship("Vendor", back_populates="sightings")
    upload = relationship("Upload", back_populates="sightings")

    __table_args__ = (
        Index("idx_sightings_part", "part_number_normalized"),
        Index("idx_sightings_vendor", "vendor_id"),
        Index("idx_sightings_seen", "seen_at"),
        Index("idx_sightings_vp", "vendor_id", "part_number_normalized", "source_type"),
    )


class Upload(Base):
    """A CSV/Excel file uploaded by a user."""
    __tablename__ = "uploads"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    filename = Column(String(500), nullable=False)
    file_size_bytes = Column(Integer)
    row_count = Column(Integer, default=0)
    sighting_count = Column(Integer, default=0)
    error_count = Column(Integer, default=0)
    status = Column(String(50), default="pending")  # pending | complete | failed
    error_message = Column(Text)
    column_mapping = Column(JSONB)
    uploaded_at = Column(DateTime(timezone=True), server_default=func.now())

    sightings = relationship("Sighting", back_populates="upload")

    __table_args__ = (
        Index("idx_uploads_user", "user_id"),
    )


class OutreachLog(Base):
    """Every RFQ email we send. Tracks the email thread for reply detection."""
    __tablename__ = "outreach_log"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    vendor_id = Column(UUID(as_uuid=True), ForeignKey("vendors.id"))
    part_number_normalized = Column(String(255), nullable=False)
    email_subject = Column(String(500))
    email_body = Column(Text)
    sent_at = Column(DateTime(timezone=True), server_default=func.now())

    # Graph API thread tracking (so we can find replies later)
    graph_message_id = Column(String(500))
    graph_conversation_id = Column(String(500))
    recipient_email = Column(String(255))

    # Updated automatically when a reply is detected
    responded = Column(Boolean)
    responded_at = Column(DateTime(timezone=True))
    response_was_positive = Column(Boolean)
    response_hours = Column(Float)
    won = Column(Boolean, default=False)

    vendor = relationship("Vendor")

    __table_args__ = (
        Index("idx_outreach_vp", "vendor_id", "part_number_normalized"),
        Index("idx_outreach_conv", "graph_conversation_id"),
        Index("idx_outreach_pending", "responded", "sent_at"),
    )


class VendorResponse(Base):
    """An AI-parsed quote extracted from a vendor reply email."""
    __tablename__ = "vendor_responses"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    outreach_log_id = Column(UUID(as_uuid=True), ForeignKey("outreach_log.id", ondelete="CASCADE"), nullable=False)
    vendor_id = Column(UUID(as_uuid=True), ForeignKey("vendors.id"), nullable=False)

    # Email metadata
    graph_reply_id = Column(String(500))
    reply_received_at = Column(DateTime(timezone=True), nullable=False)
    reply_from_email = Column(String(255))
    reply_from_name = Column(String(255))
    reply_body_text = Column(Text)

    # AI-extracted quote data
    part_number = Column(String(255))
    part_number_normalized = Column(String(255))
    has_stock = Column(Boolean)
    quoted_price = Column(Numeric(12, 4))
    quoted_currency = Column(String(10), default="USD")
    quoted_quantity = Column(Integer)
    quoted_moq = Column(Integer)
    quoted_lead_time_days = Column(Integer)
    quoted_lead_time_text = Column(String(200))
    quoted_condition = Column(String(50))
    quoted_date_code = Column(String(50))
    quoted_manufacturer = Column(String(255))

    # AI parsing info
    parse_confidence = Column(Float, default=0.0)
    parse_model = Column(String(100))
    parse_raw = Column(JSONB)
    parse_notes = Column(Text)

    # Workflow status
    status = Column(String(50), default="parsed")  # parsed | approved | rejected | sighting_created
    sighting_id = Column(UUID(as_uuid=True), ForeignKey("sightings.id"))
    reviewed_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    outreach_log = relationship("OutreachLog")
    vendor = relationship("Vendor")
    sighting = relationship("Sighting")

    __table_args__ = (
        Index("idx_vr_outreach", "outreach_log_id"),
        Index("idx_vr_status", "status"),
    )


class SearchLog(Base):
    __tablename__ = "search_log"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    part_numbers = Column(ARRAY(Text))
    result_count = Column(Integer)
    sources_queried = Column(ARRAY(Text))
    duration_ms = Column(Integer)
    searched_at = Column(DateTime(timezone=True), server_default=func.now())
