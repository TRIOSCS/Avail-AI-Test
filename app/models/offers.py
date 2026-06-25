"""Offer, attachment, contact, and vendor response models."""

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import relationship, validates

from ..database import UTCDateTime
from .base import Base


class Offer(Base):
    """Vendor offer logged by a buyer for a specific MPN on a requisition.

    requisition_id is nullable: unsolicited inbound vendor emails (Tier-5 fallback
    in poll_inbox) produce Offers without a matching requisition.  These are still
    proactive-eligible as long as material_card_id is resolved.
    """

    __tablename__ = "offers"
    id = Column(Integer, primary_key=True)
    requisition_id = Column(Integer, ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=True)
    requirement_id = Column(Integer, ForeignKey("requirements.id", ondelete="CASCADE"))
    material_card_id = Column(Integer, ForeignKey("material_cards.id", ondelete="SET NULL"))

    vendor_card_id = Column(Integer, ForeignKey("vendor_cards.id", ondelete="SET NULL"))
    vendor_name = Column(String(255), nullable=False)
    vendor_name_normalized = Column(String(255))

    mpn = Column(String(255), nullable=False)
    normalized_mpn = Column(String(255), index=True)
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
    spq = Column(Integer)  # Standard Pack Quantity (vendor's minimum shipping unit)
    valid_until = Column(Date)
    warranty = Column(String(100))
    country_of_origin = Column(String(100))

    # --- Qualification capture (standardized buyer qualification at offer entry) ---
    qualification_status = Column(String(20))  # QualificationStatus snapshot for filter/report
    qualification_note = Column(Text)  # system-composed standardized note (NOT free notes)
    qualification = Column(JSON)  # condition-specific detail + pending vendor requests

    source = Column(String(50), default="manual")
    vendor_response_id = Column(Integer, ForeignKey("vendor_responses.id", ondelete="SET NULL"))
    entered_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))

    # Evidence tier — provenance tag for data trust (T1–T7)
    evidence_tier = Column(String(4))
    # AI parse confidence (0.0–1.0) for email-parsed offers
    parse_confidence = Column(Float)
    # Promotion audit trail — when a human reviews and promotes a T4 offer
    promoted_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    promoted_at = Column(UTCDateTime)

    # Spec-code resolver lineage — populated when this offer was sourced
    # against an AVL MPN resolved from an OEM spec code (see SpecCodeResolver).
    resolved_via_spec_code = Column(String(64), nullable=True)
    source_mpn = Column(String(255), nullable=True)

    excess_line_item_id = Column(Integer, ForeignKey("excess_line_items.id", ondelete="SET NULL"))

    notes = Column(Text)
    status = Column(String(20), default="active")  # active | sold
    is_stale = Column(
        Boolean, nullable=False, default=False, server_default="false"
    )  # display-only: True if >14 days old
    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))

    # Audit trail
    updated_at = Column(UTCDateTime)
    updated_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))

    # Approval workflow
    approved_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    approved_at = Column(UTCDateTime)

    # Quote candidate selection — sales picks offers for quoting
    selected_for_quote = Column(Boolean, nullable=False, default=False, server_default="false")
    selected_at = Column(UTCDateTime)

    # v1.3.0: Attribution fields — 14-day TTL with reconfirmation
    expires_at = Column(UTCDateTime)
    reconfirmed_at = Column(UTCDateTime)
    reconfirm_count = Column(Integer, default=0)
    attribution_status = Column(String(20), default="active")  # active, expired, converted

    # --- Validators ---
    @validates("unit_price")
    def _validate_unit_price(self, _key, value):
        if value is not None and value < 0:
            raise ValueError(f"unit_price must be >= 0, got {value}")
        return value

    @validates("parse_confidence")
    def _validate_parse_confidence(self, _key, value):
        if value is not None and not (0.0 <= value <= 1.0):
            raise ValueError(f"parse_confidence must be 0.0-1.0, got {value}")
        return value

    @validates("status")
    def _validate_status(self, _key, value):
        from ..constants import OfferStatus

        valid = {e.value for e in OfferStatus}
        if value and value not in valid:
            from loguru import logger

            logger.warning("Unexpected offer status: {}. Expected one of {}", value, valid)
        return value

    @validates("qty_available")
    def _validate_qty_available(self, _key, value):
        if value is not None and value < 0:
            raise ValueError(f"qty_available must be >= 0, got {value}")
        return value

    @validates("condition")
    def _validate_condition(self, _key, value):
        from ..constants import OfferCondition

        valid = {e.value for e in OfferCondition}
        if value and value not in valid:
            from loguru import logger

            logger.warning("Unexpected offer condition: {}. Expected one of {}", value, valid)
        return value

    @property
    def qualification_summary(self) -> dict:
        """Live qualification badge/meter (display reads this; column is the
        snapshot)."""
        from app.services.offer_qualification import compute_status, meter

        data = {
            "manufacturer": self.manufacturer,
            "packaging": self.packaging,
            "date_code": self.date_code,
            **{
                k: (self.qualification or {}).get(k)
                for k in ("usage", "refurbished_by", "refurb_process", "cert_doc", "part_condition")
            },
        }
        has_images = bool(self.attachments)
        filled, total = meter(self.condition, data, has_images)
        return {
            "status": compute_status(self.condition, data, has_images),
            "filled": filled,
            "total": total,
            "note": self.qualification_note,
        }

    requisition = relationship("Requisition", back_populates="offers")
    requirement = relationship("Requirement", back_populates="offers")
    vendor_card = relationship("VendorCard", foreign_keys=[vendor_card_id])
    entered_by = relationship("User", foreign_keys=[entered_by_id])
    updated_by = relationship("User", foreign_keys=[updated_by_id])
    approved_by = relationship("User", foreign_keys=[approved_by_id])
    attachments = relationship("OfferAttachment", back_populates="offer", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_offers_req", "requisition_id"),
        Index("ix_offers_requirement", "requirement_id"),
        Index("ix_offers_vendor", "vendor_card_id"),
        Index("ix_offers_mpn", "mpn"),
        Index("ix_offers_status", "status"),
        Index("ix_offers_qualification_status", "qualification_status"),
        Index("ix_offers_entered_by", "entered_by_id"),
        Index("ix_offers_req_status", "requisition_id", "status"),
        Index("ix_offers_entered_created", "entered_by_id", "created_at"),
        Index("ix_offers_req_created", "requisition_id", "created_at"),
        Index("ix_offers_vendor_name", "vendor_name"),
        Index("ix_offers_vendor_norm", "vendor_name_normalized"),
        Index("ix_offers_material_card", "material_card_id"),
    )


class OfferAttachment(Base):
    """File attachment on a vendor offer (stored in OneDrive or company SharePoint
    library).

    library_drive_id NULL  → OneDrive fallback row (user token, item in /me/drive)
    library_drive_id set   → company SharePoint library row (app token)
    """

    __tablename__ = "offer_attachments"
    id = Column(Integer, primary_key=True)
    offer_id = Column(Integer, ForeignKey("offers.id", ondelete="CASCADE"), nullable=False)
    file_name = Column(String(500), nullable=False)
    library_item_id = Column(String(500))
    library_drive_id = Column(String(200))
    library_web_url = Column(Text)
    thumbnail_url = Column(Text)
    content_type = Column(String(100))
    size_bytes = Column(Integer)
    uploaded_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))

    offer = relationship("Offer", back_populates="attachments")
    uploaded_by = relationship("User", foreign_keys=[uploaded_by_id])

    __table_args__ = (Index("ix_offer_attachments_offer", "offer_id"),)


class Contact(Base):
    __tablename__ = "contacts"
    id = Column(Integer, primary_key=True)
    requisition_id = Column(Integer, ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    contact_type = Column(String(20), nullable=False)
    vendor_name = Column(String(255), nullable=False)
    vendor_name_normalized = Column(String(255))
    vendor_contact = Column(String(255))
    parts_included = Column(JSON, default=list)
    subject = Column(String(500))
    details = Column(Text)
    status = Column(String(50), default="sent")
    status_updated_at = Column(UTCDateTime)
    graph_message_id = Column(String(500))
    graph_conversation_id = Column(String(500))
    sent_at = Column(UTCDateTime, nullable=True)
    needs_review = Column(Boolean, default=False)
    parse_result_json = Column(JSON)
    parse_confidence = Column(Float)
    error_message = Column(String(500))  # Error detail when status="failed"
    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))

    requisition = relationship("Requisition", back_populates="contacts")
    user = relationship("User", back_populates="contacts")

    __table_args__ = (
        Index("ix_contact_req", "requisition_id"),
        Index("ix_contact_status", "status"),
        Index("ix_contact_user_status", "user_id", "status", "created_at"),
        Index("ix_contact_vendor_name", "vendor_name"),
        Index("ix_contacts_vendor_norm", "vendor_name_normalized"),
        Index("ix_contact_type_created", "contact_type", "created_at"),
        Index("ix_contact_type_vendor", "contact_type", "vendor_name"),
    )


class VendorResponse(Base):
    __tablename__ = "vendor_responses"
    id = Column(Integer, primary_key=True)
    contact_id = Column(Integer, ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True)
    requisition_id = Column(Integer, ForeignKey("requisitions.id", ondelete="SET NULL"), nullable=True)
    vendor_name = Column(String(255))
    vendor_email = Column(String(255))
    subject = Column(String(500))
    body = Column(Text)
    received_at = Column(UTCDateTime)
    parsed_data = Column(JSON)
    confidence = Column(Float)
    classification = Column(String(50))
    needs_action = Column(Boolean, default=False)
    action_hint = Column(String(255))
    status = Column(String(50), default="new")
    message_id = Column(String(255), unique=True, index=True, nullable=True)
    graph_conversation_id = Column(String(500))
    scanned_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))
    match_method = Column(String(50), nullable=True)

    __table_args__ = (
        Index("ix_vr_classification", "classification"),
        Index("ix_vr_requisition", "requisition_id"),
        Index("ix_vr_contact", "contact_id"),
        Index("ix_vr_scanned_by", "scanned_by_user_id"),
        Index("ix_vr_req_email", "requisition_id", "vendor_email"),
        Index("ix_vr_vendor_name", "vendor_name"),
        Index("ix_vr_received_email", "received_at", "vendor_email"),
        Index("ix_vr_received_status", "received_at", "status"),
    )
