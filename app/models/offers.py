"""Offer, attachment, contact, and vendor response models."""

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
from sqlalchemy.orm import relationship

from .base import Base


class Offer(Base):
    """Vendor offer logged by a buyer for a specific MPN on a requisition."""

    __tablename__ = "offers"
    id = Column(Integer, primary_key=True)
    requisition_id = Column(
        Integer, ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False
    )
    requirement_id = Column(Integer, ForeignKey("requirements.id", ondelete="CASCADE"))

    vendor_card_id = Column(Integer, ForeignKey("vendor_cards.id"))
    vendor_name = Column(String(255), nullable=False)

    mpn = Column(String(255), nullable=False)
    manufacturer = Column(String(255))
    qty_available = Column(Integer)
    unit_price = Column(Numeric(12, 4))
    currency = Column(String(10), default="USD")
    lead_time = Column(String(100))
    date_code = Column(String(100))
    condition = Column(String(50))
    packaging = Column(String(100))
    firmware = Column(String(100))
    hardware_code = Column(String(100))
    moq = Column(Integer)
    valid_until = Column(Date)

    source = Column(String(50), default="manual")
    vendor_response_id = Column(Integer, ForeignKey("vendor_responses.id"))
    entered_by_id = Column(Integer, ForeignKey("users.id"))

    notes = Column(Text)
    status = Column(String(20), default="active")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # v1.3.0: Attribution fields — 14-day TTL with reconfirmation
    expires_at = Column(DateTime)
    reconfirmed_at = Column(DateTime)
    reconfirm_count = Column(Integer, default=0)
    attribution_status = Column(
        String(20), default="active"
    )  # active, expired, converted

    requisition = relationship("Requisition", back_populates="offers")
    requirement = relationship("Requirement", back_populates="offers")
    entered_by = relationship("User", foreign_keys=[entered_by_id])
    attachments = relationship(
        "OfferAttachment", back_populates="offer", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_offers_req", "requisition_id"),
        Index("ix_offers_requirement", "requirement_id"),
        Index("ix_offers_vendor", "vendor_card_id"),
        Index("ix_offers_mpn", "mpn"),
        Index("ix_offers_status", "status"),
        Index("ix_offers_entered_by", "entered_by_id"),
        Index("ix_offers_req_status", "requisition_id", "status"),
        Index("ix_offers_entered_created", "entered_by_id", "created_at"),
    )


class OfferAttachment(Base):
    """File attachment on a vendor offer (stored in OneDrive)."""

    __tablename__ = "offer_attachments"
    id = Column(Integer, primary_key=True)
    offer_id = Column(
        Integer, ForeignKey("offers.id", ondelete="CASCADE"), nullable=False
    )
    file_name = Column(String(500), nullable=False)
    onedrive_item_id = Column(String(500))
    onedrive_url = Column(Text)
    thumbnail_url = Column(Text)
    content_type = Column(String(100))
    size_bytes = Column(Integer)
    uploaded_by_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    offer = relationship("Offer", back_populates="attachments")
    uploaded_by = relationship("User", foreign_keys=[uploaded_by_id])

    __table_args__ = (Index("ix_offer_attachments_offer", "offer_id"),)


class Contact(Base):
    __tablename__ = "contacts"
    id = Column(Integer, primary_key=True)
    requisition_id = Column(Integer, ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    contact_type = Column(String(20), nullable=False)
    vendor_name = Column(String(255), nullable=False)
    vendor_contact = Column(String(255))
    parts_included = Column(JSON, default=list)
    subject = Column(String(500))
    details = Column(Text)
    status = Column(String(50), default="sent")
    status_updated_at = Column(DateTime)
    graph_message_id = Column(String(500))
    graph_conversation_id = Column(String(500))
    needs_review = Column(Boolean, default=False)
    parse_result_json = Column(JSON)
    parse_confidence = Column(Float)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    requisition = relationship("Requisition", back_populates="contacts")
    user = relationship("User", back_populates="contacts")

    __table_args__ = (
        Index("ix_contact_req", "requisition_id"),
        Index("ix_contact_status", "status"),
        Index("ix_contact_user_status", "user_id", "status", "created_at"),
        Index("ix_contact_vendor_name", "vendor_name"),
        Index("ix_contact_type_created", "contact_type", "created_at"),
        Index("ix_contact_type_vendor", "contact_type", "vendor_name"),
    )


class VendorResponse(Base):
    __tablename__ = "vendor_responses"
    id = Column(Integer, primary_key=True)
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=True)
    requisition_id = Column(Integer, ForeignKey("requisitions.id"), nullable=True)
    vendor_name = Column(String(255))
    vendor_email = Column(String(255))
    subject = Column(String(500))
    body = Column(Text)
    received_at = Column(DateTime)
    parsed_data = Column(JSON)
    confidence = Column(Float)
    classification = Column(String(50))
    needs_action = Column(Boolean, default=False)
    action_hint = Column(String(255))
    status = Column(String(50), default="new")
    message_id = Column(String(255), unique=True, index=True, nullable=True)
    graph_conversation_id = Column(String(500))
    scanned_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    match_method = Column(
        String(50)
    )  # conversation_id, subject_token, email_exact, domain, unmatched
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_vr_classification", "classification"),
        Index("ix_vr_requisition", "requisition_id"),
        Index("ix_vr_contact", "contact_id"),
        Index("ix_vr_scanned_by", "scanned_by_user_id"),
        Index("ix_vr_req_email", "requisition_id", "vendor_email"),
        Index("ix_vr_vendor_name", "vendor_name"),
        Index("ix_vr_received_email", "received_at", "vendor_email"),
    )
