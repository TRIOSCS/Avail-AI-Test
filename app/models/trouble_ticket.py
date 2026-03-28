"""Trouble ticket model — bug reports and error tracking.

Supports two sources:
- report_button: quick bug report via red chat bubble (formerly ErrorReport)
- ticket_form: structured ticket via sidebar Tickets view

Called by: routers/error_reports.py, startup.py
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
    func,
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
        Index("ix_trouble_tickets_source_status_created", "source", "status", "created_at"),
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
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), server_default=func.now())
    updated_at = Column(DateTime, onupdate=lambda: datetime.now(timezone.utc), server_default=func.now())
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
    # Agent testing context (migration 053)
    similarity_score = Column(Float)
    tested_area = Column(String(50))
    dom_snapshot = Column(Text)
    network_errors = Column(JSON)
    performance_timings = Column(JSON)
    reproduction_steps = Column(JSON)

    # Trouble Ticket Redesign (2026-03-21)
    screenshot_path = Column(String(255))
    ai_summary = Column(Text)
    root_cause_group_id = Column(Integer, ForeignKey("root_cause_groups.id", ondelete="SET NULL"))

    root_cause_group = relationship("RootCauseGroup", foreign_keys=[root_cause_group_id])

    submitter = relationship("User", foreign_keys=[submitted_by])
    resolved_by = relationship("User", foreign_keys=[resolved_by_id])
    parent_ticket = relationship("TroubleTicket", remote_side=[id], foreign_keys=[parent_ticket_id])
