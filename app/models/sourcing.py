"""Core sourcing models — Requisitions, Requirements, Sightings."""

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    FetchedValue,
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


class Requisition(Base):
    __tablename__ = "requisitions"
    __table_args__ = (
        Index("ix_requisitions_status", "status"),
        Index("ix_requisitions_created_by", "created_by"),
        Index("ix_requisitions_site", "customer_site_id"),
        Index("ix_requisitions_created_at", "created_at"),
        Index("ix_requisitions_name", "name"),
        Index("ix_requisitions_customer_name", "customer_name"),
        Index("ix_requisitions_claimed_by", "claimed_by_id"),
        Index("ix_requisitions_urgency", "urgency"),
        Index("ix_requisitions_company", "company_id"),
    )
    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    customer_name = Column(String(255))
    customer_site_id = Column(Integer, ForeignKey("customer_sites.id", ondelete="SET NULL"))
    company_id = Column(Integer, ForeignKey("companies.id", ondelete="SET NULL"))
    status = Column(String(50), default="active")
    cloned_from_id = Column(Integer, ForeignKey("requisitions.id", ondelete="SET NULL"))
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    deadline = Column(String(50))  # ISO date or "ASAP"
    last_searched_at = Column(DateTime)
    offers_viewed_at = Column(DateTime)

    # Buyer claim — which buyer picked up this requisition for sourcing
    claimed_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    claimed_at = Column(DateTime)

    # Sales context — helps buyers prioritize
    urgency = Column(String(20), default="normal")  # normal | hot | critical
    opportunity_value = Column(Numeric(12, 2))  # Estimated deal value in USD

    # Audit trail
    updated_at = Column(DateTime)
    updated_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))

    creator = relationship("User", back_populates="requisitions", foreign_keys=[created_by])
    claimed_by = relationship("User", foreign_keys=[claimed_by_id])
    updated_by = relationship("User", foreign_keys=[updated_by_id])
    customer_site = relationship("CustomerSite", foreign_keys=[customer_site_id])
    company = relationship("Company", foreign_keys=[company_id])
    requirements = relationship("Requirement", back_populates="requisition", cascade="all, delete-orphan")
    attachments = relationship("RequisitionAttachment", back_populates="requisition", cascade="all, delete-orphan")
    contacts = relationship("Contact", back_populates="requisition", cascade="all, delete-orphan")
    offers = relationship("Offer", back_populates="requisition", cascade="all, delete-orphan")
    quotes = relationship("Quote", back_populates="requisition", cascade="all, delete-orphan")

    @validates("opportunity_value")
    def _validate_opportunity_value(self, _key, value):
        if value is not None and value < 0:
            raise ValueError("opportunity_value must be >= 0")
        return value

    @validates("status")
    def _validate_status(self, _key, value):
        from ..constants import RequisitionStatus

        valid = {e.value for e in RequisitionStatus}
        if value and value not in valid:
            from loguru import logger

            logger.warning("Unexpected requisition status: {}. Expected one of {}", value, valid)
        return value


class Requirement(Base):
    __tablename__ = "requirements"
    id = Column(Integer, primary_key=True)
    requisition_id = Column(Integer, ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False)
    material_card_id = Column(Integer, ForeignKey("material_cards.id", ondelete="SET NULL"))
    primary_mpn = Column(String(255))
    normalized_mpn = Column(String(255), index=True)
    oem_pn = Column(String(255))
    brand = Column(String(255))
    manufacturer = Column(String(255), nullable=False, server_default="")
    sku = Column(String(255))
    target_qty = Column(Integer, default=1)
    target_price = Column(Numeric(12, 4))
    substitutes = Column(JSON, default=list)
    substitutes_text = Column(Text, server_default=FetchedValue(), server_onupdate=FetchedValue())
    notes = Column(Text)
    firmware = Column(String(100))
    date_codes = Column(String(100))
    hardware_codes = Column(String(100))
    packaging = Column(String(100))
    condition = Column(String(50))
    description = Column(Text)  # Free-text part description
    package_type = Column(String(100))  # Physical package (QFP, BGA, SOIC, DIP, etc.)
    revision = Column(String(100))  # Part revision / version level
    customer_pn = Column(String(255))  # Customer's internal part number
    need_by_date = Column(Date)  # When customer needs the parts
    sale_notes = Column(Text)
    sourcing_status = Column(String(20), default="open")  # open | sourcing | offered | quoted | won | lost
    priority_score = Column(Float, nullable=True)  # AI-computed 0-100 for sort order
    assigned_buyer_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_searched_at = Column(UTCDateTime)

    requisition = relationship("Requisition", back_populates="requirements")
    material_card = relationship("MaterialCard", foreign_keys=[material_card_id], lazy="joined")
    attachments = relationship("RequirementAttachment", back_populates="requirement", cascade="all, delete-orphan")
    sightings = relationship("Sighting", back_populates="requirement", cascade="all, delete-orphan")
    offers = relationship("Offer", back_populates="requirement", cascade="all, delete-orphan")

    @validates("target_qty")
    def _validate_target_qty(self, _key, value):
        if value is not None and value < 0:
            raise ValueError("target_qty must be >= 0")
        return value

    @validates("target_price")
    def _validate_target_price(self, _key, value):
        if value is not None and value < 0:
            raise ValueError("target_price must be >= 0")
        return value

    @validates("priority_score")
    def _validate_priority_score(self, _key, value):
        if value is not None and not (0 <= value <= 100):
            raise ValueError("priority_score must be 0-100")
        return value

    @validates("primary_mpn", "customer_pn", "oem_pn")
    def _uppercase_mpn_fields(self, _key, value):
        return value.upper().strip() if value else value

    __table_args__ = (
        Index("ix_req_requisition", "requisition_id"),
        Index("ix_req_primary_mpn", "primary_mpn"),
        Index("ix_requirements_material_card", "material_card_id"),
        Index("ix_requirements_sourcing_status", "sourcing_status"),
    )


class Manufacturer(Base):
    """Manufacturer lookup for typeahead normalization.

    Called by: typeahead endpoint, startup seed
    Depends on: Base
    """

    __tablename__ = "manufacturers"
    id = Column(Integer, primary_key=True)
    canonical_name = Column(String(255), nullable=False, unique=True, index=True)
    aliases = Column(JSON, default=list)
    website = Column(String(500))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class Sighting(Base):
    __tablename__ = "sightings"
    id = Column(Integer, primary_key=True)
    requirement_id = Column(Integer, ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False)
    material_card_id = Column(Integer, ForeignKey("material_cards.id", ondelete="SET NULL"))
    vendor_name = Column(String(255), nullable=False)
    vendor_name_normalized = Column(String(255), index=True)
    vendor_email = Column(String(255))
    vendor_phone = Column(String(100))
    mpn_matched = Column(String(255))
    normalized_mpn = Column(String(255), index=True)
    manufacturer = Column(String(255), index=True)
    qty_available = Column(Integer)
    unit_price = Column(Numeric(12, 4))
    currency = Column(String(10), default="USD")
    moq = Column(Integer)
    source_type = Column(String(50), index=True)
    is_authorized = Column(Boolean, default=False)
    confidence = Column(Float, default=0.0)
    score = Column(Float, default=0.0)
    raw_data = Column(JSON)
    is_unavailable = Column(Boolean, default=False)

    # Richer attachment parsing (Email Mining v2 Upgrade 2)
    date_code = Column(String(50))
    packaging = Column(String(50))
    condition = Column(String(50))
    lead_time_days = Column(Integer)
    lead_time = Column(String(100))

    # v2.0: Excess list differentiation — links sighting to originating customer company
    source_company_id = Column(Integer, ForeignKey("companies.id", ondelete="SET NULL"))

    # NC integration: when the source data was fetched
    source_searched_at = Column(DateTime(timezone=True))

    # Evidence tier — provenance tag for data trust (T1–T7)
    evidence_tier = Column(String(4))
    # Multi-factor score breakdown (JSON: {trust, price, qty, freshness, completeness})
    score_components = Column(JSON)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    requirement = relationship("Requirement", back_populates="sightings")

    @validates("moq")
    def _coerce_moq(self, _key, value):
        if value is not None and value <= 0:
            return None
        return value

    @validates("qty_available")
    def _validate_qty_available(self, _key, value):
        if value is not None and value < 0:
            raise ValueError("qty_available must be >= 0")
        return value

    @validates("unit_price")
    def _validate_unit_price(self, _key, value):
        if value is not None and value < 0:
            raise ValueError("unit_price must be >= 0")
        return value

    @validates("confidence")
    def _validate_confidence(self, _key, value):
        if value is not None and value < 0.0:
            raise ValueError("confidence must be >= 0.0")
        return value

    @validates("score")
    def _validate_score(self, _key, value):
        if value is not None and not (0.0 <= value <= 100.0):
            raise ValueError("score must be 0.0-100.0")
        return value

    @validates("lead_time_days")
    def _validate_lead_time_days(self, _key, value):
        if value is not None and value < 0:
            raise ValueError("lead_time_days must be >= 0")
        return value

    __table_args__ = (
        Index("ix_sightings_vendor_name", "vendor_name"),
        Index("ix_sightings_vendor_norm", "vendor_name_normalized"),
        Index("ix_sight_req", "requirement_id"),
        Index("ix_sightings_source_company", "source_company_id"),
        Index("ix_sightings_req_vendor", "requirement_id", "vendor_name"),
        Index("ix_sightings_req_score", "requirement_id", score.desc()),
        Index("ix_sightings_material_card", "material_card_id"),
        Index("ix_sightings_mpn_vendor_norm", "normalized_mpn", "vendor_name_normalized"),
    )


class RequisitionAttachment(Base):
    """File attachment on a requisition (stored in OneDrive)."""

    __tablename__ = "requisition_attachments"
    id = Column(Integer, primary_key=True)
    requisition_id = Column(Integer, ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False, index=True)
    file_name = Column(String(500), nullable=False)
    onedrive_item_id = Column(String(500))
    onedrive_url = Column(Text)
    thumbnail_url = Column(Text)
    content_type = Column(String(100))
    size_bytes = Column(Integer)
    uploaded_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    requisition = relationship("Requisition", back_populates="attachments")
    uploaded_by = relationship("User", foreign_keys=[uploaded_by_id])


class RequirementAttachment(Base):
    """File attachment on a requirement (stored in OneDrive)."""

    __tablename__ = "requirement_attachments"
    id = Column(Integer, primary_key=True)
    requirement_id = Column(Integer, ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False, index=True)
    file_name = Column(String(500), nullable=False)
    onedrive_item_id = Column(String(500))
    onedrive_url = Column(Text)
    thumbnail_url = Column(Text)
    content_type = Column(String(100))
    size_bytes = Column(Integer)
    uploaded_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    requirement = relationship("Requirement", back_populates="attachments")
    uploaded_by = relationship("User", foreign_keys=[uploaded_by_id])
