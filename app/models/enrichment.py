"""Enrichment models — jobs, queue, signatures, prospects, intel cache."""

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
    String,
    Text,
)
from sqlalchemy.orm import relationship

from .base import Base


class EnrichmentJob(Base):
    """Tracks bulk enrichment runs (backfill, scheduled, manual)."""

    __tablename__ = "enrichment_jobs"
    id = Column(Integer, primary_key=True)
    job_type = Column(String(50), nullable=False)
    status = Column(String(20), default="pending")
    total_items = Column(Integer, default=0)
    processed_items = Column(Integer, default=0)
    enriched_items = Column(Integer, default=0)
    error_count = Column(Integer, default=0)
    scope = Column(JSON, default=dict)
    started_by_id = Column(Integer, ForeignKey("users.id"))
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    error_log = Column(JSON, default=list)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    started_by = relationship("User", foreign_keys=[started_by_id])

    __table_args__ = (
        Index("ix_ej_status", "status"),
        Index("ix_ej_type_status", "job_type", "status"),
    )


class EnrichmentQueue(Base):
    """Pending enrichment results for review or auto-apply."""

    __tablename__ = "enrichment_queue"
    id = Column(Integer, primary_key=True)

    # Polymorphic target
    vendor_card_id = Column(Integer, ForeignKey("vendor_cards.id", ondelete="CASCADE"))
    company_id = Column(Integer, ForeignKey("companies.id", ondelete="CASCADE"))
    vendor_contact_id = Column(Integer, ForeignKey("vendor_contacts.id", ondelete="CASCADE"))

    enrichment_type = Column(String(50), nullable=False)
    field_name = Column(String(100), nullable=False)
    current_value = Column(Text)
    proposed_value = Column(Text, nullable=False)

    confidence = Column(Float, nullable=False, default=0.5)
    source = Column(String(50), nullable=False)

    status = Column(String(20), default="pending")
    batch_job_id = Column(Integer, ForeignKey("enrichment_jobs.id", ondelete="SET NULL"))

    reviewed_by_id = Column(Integer, ForeignKey("users.id"))
    reviewed_at = Column(DateTime)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    vendor_card = relationship("VendorCard", foreign_keys=[vendor_card_id])
    company = relationship("Company", foreign_keys=[company_id])
    vendor_contact = relationship("VendorContact", foreign_keys=[vendor_contact_id])
    batch_job = relationship("EnrichmentJob", foreign_keys=[batch_job_id])
    reviewed_by = relationship("User", foreign_keys=[reviewed_by_id])

    __table_args__ = (
        Index("ix_eq_status", "status"),
        Index("ix_eq_vendor", "vendor_card_id"),
        Index("ix_eq_company", "company_id"),
        Index("ix_eq_batch", "batch_job_id"),
        Index("ix_eq_status_created", "status", "created_at"),
        Index("ix_eq_status_source", "status", "source"),
    )


class EmailSignatureExtract(Base):
    """Cached signature parses per sender (dedup)."""

    __tablename__ = "email_signature_extracts"
    id = Column(Integer, primary_key=True)
    sender_email = Column(String(255), nullable=False, unique=True)
    sender_name = Column(String(255))

    # Extracted fields
    full_name = Column(String(255))
    title = Column(String(255))
    company_name = Column(String(255))
    phone = Column(String(100))
    mobile = Column(String(100))
    website = Column(String(500))
    address = Column(Text)
    linkedin_url = Column(String(500))

    extraction_method = Column(String(20))
    confidence = Column(Float, default=0.5)
    seen_count = Column(Integer, default=1)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_ese_email", "sender_email", unique=True),
        Index("ix_ese_company", "company_name"),
    )


class ProspectContact(Base):
    """Enriched contacts found via Apollo/web search for customers and vendors."""

    __tablename__ = "prospect_contacts"
    id = Column(Integer, primary_key=True)
    customer_site_id = Column(
        Integer, ForeignKey("customer_sites.id", ondelete="SET NULL")
    )
    vendor_card_id = Column(Integer, ForeignKey("vendor_cards.id", ondelete="SET NULL"))

    full_name = Column(String(255), nullable=False)
    title = Column(String(255))
    email = Column(String(255))
    email_status = Column(String(20))
    phone = Column(String(100))
    linkedin_url = Column(String(500))

    source = Column(String(50), nullable=False)
    confidence = Column(String(10), nullable=False)
    found_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    verified_at = Column(DateTime)

    is_saved = Column(Boolean, default=False)
    saved_by_id = Column(Integer, ForeignKey("users.id"))
    notes = Column(Text)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_prospect_contacts_site", "customer_site_id"),
        Index("ix_prospect_contacts_vendor", "vendor_card_id"),
        Index("ix_prospect_contacts_email", "email"),
    )


class IntelCache(Base):
    """Cached intelligence data with TTL."""

    __tablename__ = "intel_cache"
    id = Column(Integer, primary_key=True)
    cache_key = Column(String(500), nullable=False, unique=True, index=True)
    data = Column(JSON, nullable=False)
    ttl_days = Column(Integer, nullable=False, default=7)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime, nullable=False)
