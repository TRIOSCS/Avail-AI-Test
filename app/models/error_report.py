"""Error report / trouble ticket model."""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import relationship

from .base import Base


class ErrorReport(Base):
    __tablename__ = "error_reports"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String(255), nullable=False)
    description = Column(Text)
    screenshot_b64 = Column(Text)

    # Auto-captured context
    current_url = Column(String(2048))
    current_view = Column(String(100))
    browser_info = Column(String(512))
    screen_size = Column(String(50))
    console_errors = Column(Text)  # JSON string
    page_state = Column(Text)  # JSON string

    # Gradient-generated Claude Code prompt
    ai_prompt = Column(Text)

    # Status workflow: open → in_progress → resolved | closed
    status = Column(String(20), default="open", nullable=False)
    admin_notes = Column(Text)
    resolved_at = Column(DateTime)
    resolved_by_id = Column(Integer, ForeignKey("users.id"))

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    reporter = relationship("User", foreign_keys=[user_id])
    resolved_by = relationship("User", foreign_keys=[resolved_by_id])

    __table_args__ = (
        Index("ix_error_reports_user_created", "user_id", "created_at"),
        Index("ix_error_reports_status_created", "status", "created_at"),
    )
