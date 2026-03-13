"""Notification model — in-app notifications.

Stores admin notifications for system events.

Called by: models/__init__.py (re-exported for DB schema)
Depends on: models/base.py, models/auth.py
"""

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text

from app.models.base import Base


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    ticket_id = Column(Integer, ForeignKey("trouble_tickets.id", ondelete="SET NULL"), nullable=True)
    event_type = Column(String(50), nullable=False)  # diagnosed, prompt_ready, escalated, fixed, failed
    title = Column(String(500), nullable=False)
    body = Column(Text, nullable=True)
    is_read = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
