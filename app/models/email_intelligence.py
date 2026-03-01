"""EmailIntelligence model — AI-classified email data for inbox mining.

Stores classification results, pricing intelligence, and thread summaries
from AI-powered inbox analysis.

Called by: connectors/email_mining.py, services/email_intelligence_service.py
Depends on: models/base.py
"""

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
)

from .base import Base


class EmailIntelligence(Base):
    __tablename__ = "email_intelligence"

    id = Column(Integer, primary_key=True)
    message_id = Column(String(255), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    sender_email = Column(String(255), nullable=False)
    sender_domain = Column(String(255), nullable=False, index=True)

    # AI classification
    classification = Column(
        String(20), nullable=False
    )  # offer, stock_list, quote_reply, general, ooo, spam
    confidence = Column(Float, nullable=False, default=0.0)
    has_pricing = Column(Boolean, default=False)

    # Extracted intelligence
    parts_detected = Column(JSON, default=list)
    brands_detected = Column(JSON, default=list)
    commodities_detected = Column(JSON, default=list)
    parsed_quotes = Column(JSON)  # From ai_email_parser for offer emails

    # Email metadata
    subject = Column(String(500))
    received_at = Column(DateTime)
    conversation_id = Column(String(255), index=True)

    # Processing state
    auto_applied = Column(Boolean, default=False)
    needs_review = Column(Boolean, default=False)

    # Thread summary (Phase 4)
    thread_summary = Column(JSON)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_email_intel_user_received", "user_id", "received_at"),
        Index("ix_email_intel_classification", "classification"),
        Index("ix_email_intel_needs_review", "needs_review"),
    )
