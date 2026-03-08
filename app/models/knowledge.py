"""Knowledge Ledger — captures facts, Q&A, notes, and AI insights.

Multi-entity linkage via nullable FK columns to MPN, vendor, company,
requisition, and requirement. Supports Q&A threading via parent_id
self-referential FK. Expiry logic for price/lead-time facts.

Called by: services/knowledge_service.py, routers/knowledge.py
Depends on: models/base.py, models/auth.py
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
    Text,
)
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import relationship

from .base import Base


class KnowledgeEntry(Base):
    __tablename__ = "knowledge_entries"

    id = Column(Integer, primary_key=True, index=True)
    entry_type = Column(String(20), nullable=False)  # question, answer, fact, note, ai_insight
    content = Column(Text, nullable=False)
    source = Column(
        String(20), nullable=False, default="manual"
    )  # manual, ai_generated, system, email_parsed, teams_bot
    confidence = Column(Float, nullable=True)  # 0.0-1.0 for AI-generated
    expires_at = Column(DateTime(timezone=True), nullable=True)
    is_resolved = Column(Boolean, default=False, nullable=False)  # Q&A: marks question as answered
    parent_id = Column(Integer, ForeignKey("knowledge_entries.id", ondelete="SET NULL"), nullable=True)
    assigned_to_ids = Column(JSON, default=list)  # user IDs for Q&A routing

    # Who created it
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    # Multi-entity linkage (all nullable)
    mpn = Column(String(255), nullable=True)
    vendor_card_id = Column(Integer, ForeignKey("vendor_cards.id", ondelete="SET NULL"), nullable=True)
    company_id = Column(Integer, ForeignKey("companies.id", ondelete="SET NULL"), nullable=True)
    requisition_id = Column(Integer, ForeignKey("requisitions.id", ondelete="SET NULL"), nullable=True)
    requirement_id = Column(Integer, ForeignKey("requirements.id", ondelete="SET NULL"), nullable=True)

    # Phase 2: Teams Q&A routing
    nudged_at = Column(DateTime(timezone=True), nullable=True)
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    answered_via = Column(String(10), nullable=True)  # 'web' or 'teams'

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    creator = relationship("User", foreign_keys=[created_by])
    parent = relationship("KnowledgeEntry", remote_side=[id], foreign_keys=[parent_id])
    answers = relationship("KnowledgeEntry", foreign_keys=[parent_id], back_populates="parent")
    vendor_card = relationship("VendorCard", foreign_keys=[vendor_card_id])
    company = relationship("Company", foreign_keys=[company_id])
    requisition = relationship("Requisition", foreign_keys=[requisition_id])

    __table_args__ = (
        Index("ix_ke_requisition", "requisition_id", "created_at"),
        Index("ix_ke_mpn", "mpn"),
        Index("ix_ke_company", "company_id", "created_at"),
        Index("ix_ke_vendor", "vendor_card_id"),
        Index("ix_ke_parent", "parent_id"),
        Index("ix_ke_expires", "expires_at", postgresql_where="expires_at IS NOT NULL"),
    )


class KnowledgeConfig(Base):
    """Key-value config for knowledge ledger (e.g., daily_question_cap).

    Called by: services/teams_qa_service.py
    Depends on: models/base.py
    """

    __tablename__ = "knowledge_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(50), unique=True, nullable=False)
    value = Column(String(255), nullable=False)
