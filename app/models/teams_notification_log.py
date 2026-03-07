"""TeamsNotificationLog — audit trail for Teams Adaptive Card posts.

Tracks every notification sent to Teams: event type, target channel,
success/failure, and error details. Used by admin dashboard for
troubleshooting and monitoring notification delivery.

Called by: app/services/teams.py (_log_notification)
Depends on: app/models/base.py
"""

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text

from .base import Base


class TeamsNotificationLog(Base):
    __tablename__ = "teams_notification_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(String(50), nullable=False, index=True)
    entity_id = Column(String(100), nullable=False)
    entity_name = Column(String(200), nullable=True)
    channel_id = Column(String(200), nullable=True)
    success = Column(Boolean, nullable=False, default=False)
    error_msg = Column(Text, nullable=True)
    user_id = Column(Integer, nullable=True)
    ai_priority = Column(String(20), nullable=True)
    ai_decision = Column(String(20), nullable=True)  # sent/batched/suppressed
    batch_id = Column(String(50), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False, index=True)
