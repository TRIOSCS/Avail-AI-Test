"""Sourcing lead models — canonical lead, evidence, and buyer feedback history.

Purpose:
- Store one canonical sourcing lead per vendor+part within a requirement.
- Preserve multiple evidence records per lead with source attribution.
- Track buyer status/outcome history without mutating raw evidence.

Business Rules Enforced:
- Lead uniqueness is requirement_id + vendor_name_normalized + part_number_matched.
- Buyer workflow status is attached to lead records (not evidence rows).
- Confidence and vendor safety are stored as separate dimensions.

Called by:
- app.services.sourcing_leads
- app.routers.requisitions.requirements

Depends on:
- app.models.base.Base
- app.models.sourcing (Requirement, Requisition)
- app.models.vendors (VendorCard)
"""

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from .base import Base


class SourcingLead(Base):
    __tablename__ = "sourcing_leads"

    id = Column(Integer, primary_key=True)
    lead_id = Column(String(64), nullable=False, unique=True, index=True)

    requirement_id = Column(Integer, ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False, index=True)
    requisition_id = Column(Integer, ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False, index=True)

    part_number_requested = Column(String(255), nullable=False)
    part_number_matched = Column(String(255), nullable=False)
    match_type = Column(String(32), nullable=False, default="exact")

    vendor_name = Column(String(255), nullable=False)
    vendor_name_normalized = Column(String(255), nullable=False, index=True)
    canonical_vendor_id = Column(String(128))
    vendor_card_id = Column(Integer, ForeignKey("vendor_cards.id", ondelete="SET NULL"), index=True)

    primary_source_type = Column(String(64), nullable=False)
    primary_source_name = Column(String(128), nullable=False)
    source_reference = Column(String(1000))
    source_first_seen_at = Column(DateTime(timezone=True))
    source_last_seen_at = Column(DateTime(timezone=True))

    contact_name = Column(String(255))
    contact_email = Column(String(255))
    contact_phone = Column(String(100))
    contact_url = Column(String(1000))
    location = Column(String(255))
    notes_for_buyer = Column(Text)
    suggested_next_action = Column(String(500))

    confidence_score = Column(Float, nullable=False, default=0.0)
    confidence_band = Column(String(16), nullable=False, default="low")
    freshness_score = Column(Float)
    source_reliability_score = Column(Float)
    contactability_score = Column(Float)
    historical_success_score = Column(Float)

    reason_summary = Column(Text, nullable=False, default="")
    risk_flags = Column(JSON, nullable=False, default=list)
    evidence_count = Column(Integer, nullable=False, default=0)
    corroborated = Column(Boolean, nullable=False, default=False)

    vendor_safety_score = Column(Float)
    vendor_safety_band = Column(String(24))
    vendor_safety_summary = Column(Text)
    vendor_safety_flags = Column(JSON, nullable=False, default=list)
    vendor_safety_last_checked_at = Column(DateTime(timezone=True))

    buyer_status = Column(String(32), nullable=False, default="new")
    buyer_owner_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), index=True)
    last_buyer_action_at = Column(DateTime(timezone=True))
    buyer_feedback_summary = Column(Text)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    requirement = relationship("Requirement", foreign_keys=[requirement_id])
    requisition = relationship("Requisition", foreign_keys=[requisition_id])
    vendor_card = relationship("VendorCard", foreign_keys=[vendor_card_id])
    evidence = relationship("LeadEvidence", back_populates="lead", cascade="all, delete-orphan")
    feedback_events = relationship("LeadFeedbackEvent", back_populates="lead", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint(
            "requirement_id",
            "vendor_name_normalized",
            "part_number_matched",
            name="uq_sourcing_lead_requirement_vendor_part",
        ),
        Index("ix_sourcing_leads_status", "buyer_status"),
        Index("ix_sourcing_leads_confidence", "confidence_score"),
        Index("ix_sourcing_leads_safety", "vendor_safety_score"),
        Index("ix_sourcing_leads_req_status", "requisition_id", "buyer_status"),
    )


class LeadEvidence(Base):
    __tablename__ = "lead_evidence"

    id = Column(Integer, primary_key=True)
    evidence_id = Column(String(64), nullable=False, unique=True, index=True)
    lead_id = Column(Integer, ForeignKey("sourcing_leads.id", ondelete="CASCADE"), nullable=False, index=True)

    signal_type = Column(String(64), nullable=False)
    source_type = Column(String(64), nullable=False)
    source_name = Column(String(128), nullable=False)
    source_reference = Column(String(1000))

    part_number_observed = Column(String(255))
    vendor_name_observed = Column(String(255))
    observed_text = Column(Text)
    observed_at = Column(DateTime(timezone=True))
    freshness_age_days = Column(Float)

    weight = Column(Float)
    confidence_impact = Column(Float)
    explanation = Column(Text)

    source_reliability_band = Column(String(16))
    verification_state = Column(String(32), default="raw")

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    lead = relationship("SourcingLead", back_populates="evidence")

    __table_args__ = (
        Index("ix_lead_evidence_source_type", "source_type"),
        Index("ix_lead_evidence_verification", "verification_state"),
    )


class LeadFeedbackEvent(Base):
    __tablename__ = "lead_feedback_events"

    id = Column(Integer, primary_key=True)
    lead_id = Column(Integer, ForeignKey("sourcing_leads.id", ondelete="CASCADE"), nullable=False, index=True)

    status = Column(String(32), nullable=False)
    note = Column(Text)
    reason_code = Column(String(64))
    contact_method = Column(String(32))
    contact_attempt_count = Column(Integer, default=0)

    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    lead = relationship("SourcingLead", back_populates="feedback_events")

    __table_args__ = (
        Index("ix_lead_feedback_lead_created", "lead_id", "created_at"),
        Index("ix_lead_feedback_status", "status"),
    )
