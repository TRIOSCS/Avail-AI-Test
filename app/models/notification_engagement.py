"""NotificationEngagement — tracks user interaction with notifications.

Records delivery, clicks, dismissals, and suppressions to enable
AI-driven alert prioritization and noise reduction.

Called by: app/services/notify_intelligence.py
Depends on: app/models/base.py, app/models/auth.py (User)
"""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import relationship

from .base import Base


class NotificationEngagement(Base):
    __tablename__ = "notification_engagement"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    event_type = Column(String(50), nullable=False)
    entity_id = Column(String(100), nullable=False)
    delivery_method = Column(String(20), nullable=False, default="dm")  # channel/dm
    action = Column(String(20), nullable=False)  # delivered/clicked/dismissed/responded/suppressed/batched
    response_time_s = Column(Float, nullable=True)
    ai_priority = Column(String(20), nullable=True)  # critical/high/medium/low/noise
    ai_confidence = Column(Float, nullable=True)
    suppression_reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    user = relationship("User", foreign_keys=[user_id])

    __table_args__ = (
        Index("ix_notif_engage_user_event", "user_id", "event_type"),
        Index("ix_notif_engage_created", "created_at"),
        Index("ix_notif_engage_user_action", "user_id", "action"),
    )
