"""Core sourcing models — Requisitions, Requirements, Sightings."""

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
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


class Requisition(Base):
    __tablename__ = "requisitions"
    __table_args__ = (
        Index("ix_requisitions_status", "status"),
        Index("ix_requisitions_created_by", "created_by"),
        Index("ix_requisitions_site", "customer_site_id"),
        Index("ix_requisitions_created_at", "created_at"),
    )
    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    customer_name = Column(String(255))  # Legacy — kept for migration
    customer_site_id = Column(Integer, ForeignKey("customer_sites.id", ondelete="SET NULL"))
    status = Column(String(50), default="active")
    cloned_from_id = Column(Integer, ForeignKey("requisitions.id"))
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    deadline = Column(String(50))  # ISO date or "ASAP"
    last_searched_at = Column(DateTime)
    offers_viewed_at = Column(DateTime)

    creator = relationship("User", back_populates="requisitions")
    customer_site = relationship("CustomerSite", foreign_keys=[customer_site_id])
    requirements = relationship(
        "Requirement", back_populates="requisition", cascade="all, delete-orphan"
    )
    contacts = relationship(
        "Contact", back_populates="requisition", cascade="all, delete-orphan"
    )
    offers = relationship(
        "Offer", back_populates="requisition", cascade="all, delete-orphan"
    )
    quotes = relationship(
        "Quote", back_populates="requisition", cascade="all, delete-orphan"
    )


class Requirement(Base):
    __tablename__ = "requirements"
    id = Column(Integer, primary_key=True)
    requisition_id = Column(Integer, ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False)
    primary_mpn = Column(String(255))
    normalized_mpn = Column(String(255), index=True)
    oem_pn = Column(String(255))
    brand = Column(String(255))
    sku = Column(String(255))
    target_qty = Column(Integer, default=1)
    target_price = Column(Numeric(12, 4))
    substitutes = Column(JSON, default=list)
    notes = Column(Text)
    firmware = Column(String(100))
    date_codes = Column(String(100))
    hardware_codes = Column(String(100))
    packaging = Column(String(100))
    condition = Column(String(50))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    requisition = relationship("Requisition", back_populates="requirements")
    sightings = relationship(
        "Sighting", back_populates="requirement", cascade="all, delete-orphan"
    )
    offers = relationship(
        "Offer", back_populates="requirement", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_req_requisition", "requisition_id"),)


class Sighting(Base):
    __tablename__ = "sightings"
    id = Column(Integer, primary_key=True)
    requirement_id = Column(Integer, ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False)
    vendor_name = Column(String(255), nullable=False)
    vendor_email = Column(String(255))
    vendor_phone = Column(String(100))
    mpn_matched = Column(String(255))
    manufacturer = Column(String(255))
    qty_available = Column(Integer)
    unit_price = Column(Numeric(12, 4))
    currency = Column(String(10), default="USD")
    moq = Column(Integer)
    source_type = Column(String(50))
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
    source_company_id = Column(Integer, ForeignKey("companies.id"))

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    requirement = relationship("Requirement", back_populates="sightings")
    source_company = relationship("Company", foreign_keys=[source_company_id])

    __table_args__ = (
        Index("ix_sightings_vendor_name", "vendor_name"),
        Index("ix_sight_req", "requirement_id"),
        Index("ix_sightings_source_company", "source_company_id"),
        Index("ix_sightings_req_vendor", "requirement_id", "vendor_name"),
        Index("ix_sightings_req_score", "requirement_id", score.desc()),
    )
