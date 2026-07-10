"""Trouble ticket model — bug reports and error tracking.

Supports two sources:
- report_button: quick bug report via red chat bubble (formerly ErrorReport)
- ticket_form: structured ticket via sidebar Tickets view

Called by: routers/error_reports.py, startup.py
Depends on: models/base.py, models/auth.py (User FK)
"""

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import relationship, validates

from ..database import UTCDateTime
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
        Index("ix_trouble_tickets_ticket_type", "ticket_type"),
    )

    id = Column(Integer, primary_key=True)
    ticket_number = Column(String(20), unique=True, nullable=False)
    submitted_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    status = Column(String(30), default="submitted", nullable=False)
    # Kind discriminator (bug | feature). server_default 'bug' → existing rows read
    # as bugs; the app always writes a TicketType value, never a raw string.
    ticket_type = Column(String(20), nullable=False, server_default="bug")
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
    created_at = Column(UTCDateTime, default=lambda: datetime.now(UTC), server_default=func.now())
    updated_at = Column(UTCDateTime, onupdate=lambda: datetime.now(UTC), server_default=func.now())
    diagnosed_at = Column(UTCDateTime)
    resolved_at = Column(UTCDateTime)

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

    @validates("status")
    def _validate_status(self, _key, value):
        from ..constants import TicketStatus

        valid = {e.value for e in TicketStatus}
        if value and value not in valid:
            raise ValueError(f"Invalid ticket status: {value!r}. Valid: {valid}")
        return value

    @validates("ticket_type")
    def _validate_ticket_type(self, _key, value):
        from ..constants import TicketType

        valid = {e.value for e in TicketType}
        if value and value not in valid:
            raise ValueError(f"Invalid ticket type: {value!r}. Valid: {valid}")
        return value

    submitter = relationship("User", foreign_keys=[submitted_by])
    resolved_by = relationship("User", foreign_keys=[resolved_by_id])
    parent_ticket = relationship("TroubleTicket", remote_side=[id], foreign_keys=[parent_ticket_id])
