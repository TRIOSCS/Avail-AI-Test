"""Trouble ticket model — user-submitted bug reports for the self-heal pipeline.

Tracks the full lifecycle: submitted -> diagnosed -> fix_proposed -> fix_in_progress ->
fix_applied -> awaiting_verification -> resolved (or escalated/fix_reverted).

Called by: routers/trouble_tickets.py, services/trouble_ticket_service.py
Depends on: models/base.py, models/auth.py (User FK)
"""

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
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


class TroubleTicket(Base):
    __tablename__ = "trouble_tickets"
    __table_args__ = (
        Index("ix_trouble_tickets_status", "status"),
        Index("ix_trouble_tickets_risk_tier", "risk_tier"),
        Index("ix_trouble_tickets_submitted_by", "submitted_by"),
        Index("ix_trouble_tickets_created_at", "created_at"),
    )

    id = Column(Integer, primary_key=True)
    ticket_number = Column(String(20), unique=True, nullable=False)
    submitted_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    status = Column(String(30), default="submitted", nullable=False)
    risk_tier = Column(String(10))
    category = Column(String(20))
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=False)
    current_page = Column(String(500))
    user_agent = Column(String(500))
    auto_captured_context = Column(JSON)
    sanitized_context = Column(JSON)
    diagnosis = Column(JSON)
    generated_prompt = Column(Text)
    file_mapping = Column(JSON)
    fix_branch = Column(String(200))
    fix_pr_url = Column(String(500))
    iterations_used = Column(Integer)
    cost_tokens = Column(Integer)
    cost_usd = Column(Float)
    resolution_notes = Column(Text)
    parent_ticket_id = Column(Integer, ForeignKey("trouble_tickets.id", ondelete="SET NULL"))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, onupdate=lambda: datetime.now(timezone.utc))
    diagnosed_at = Column(DateTime)
    resolved_at = Column(DateTime)

    submitter = relationship("User", foreign_keys=[submitted_by])
    parent_ticket = relationship("TroubleTicket", remote_side=[id], foreign_keys=[parent_ticket_id])
