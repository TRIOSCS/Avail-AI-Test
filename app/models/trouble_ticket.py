"""Trouble ticket model — unified bug reports + self-heal pipeline.

Tracks the full lifecycle: submitted -> diagnosed -> fix_proposed -> fix_in_progress ->
fix_applied -> awaiting_verification -> resolved (or escalated/fix_reverted).

Supports two sources:
- report_button: quick bug report via red chat bubble (formerly ErrorReport)
- ticket_form: structured ticket via sidebar Tickets view

Called by: routers/trouble_tickets.py, routers/error_reports.py, services/trouble_ticket_service.py
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
        Index("ix_trouble_tickets_source", "source"),
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

    # Columns from ErrorReport (unified in migration 043)
    screenshot_b64 = Column(Text)
    browser_info = Column(String(512))
    screen_size = Column(String(50))
    page_state = Column(Text)
    console_errors = Column(Text)
    current_view = Column(String(100))
    ai_prompt = Column(Text)
    admin_notes = Column(Text)
    resolved_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    source = Column(String(20))  # 'report_button' | 'ticket_form'
    legacy_error_report_id = Column(Integer)  # traceability to old error_reports

    # Agent testing context (migration 053)
    similarity_score = Column(Float)
    tested_area = Column(String(50))
    dom_snapshot = Column(Text)
    network_errors = Column(JSON)
    performance_timings = Column(JSON)
    reproduction_steps = Column(JSON)

    submitter = relationship("User", foreign_keys=[submitted_by])
    resolved_by = relationship("User", foreign_keys=[resolved_by_id])
    parent_ticket = relationship("TroubleTicket", remote_side=[id], foreign_keys=[parent_ticket_id])
