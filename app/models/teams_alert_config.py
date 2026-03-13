"""TeamsAlertConfig — per-user Teams DM alert preferences.

Stores optional webhook URL for fallback delivery and an enabled flag.
Graph API DM is the primary delivery method; webhook is the fallback.

Called by: app/jobs/knowledge_jobs.py
Depends on: app/models/base.py, app/models/auth.py (User)
"""

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text, Time
from sqlalchemy.orm import relationship

from .base import Base


class TeamsAlertConfig(Base):
    __tablename__ = "teams_alert_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    teams_webhook_url = Column(Text, nullable=True)
    alerts_enabled = Column(Boolean, nullable=False, default=True, server_default="true")
    priority_threshold = Column(String(20), nullable=False, default="medium", server_default="medium")
    batch_digest_enabled = Column(Boolean, nullable=False, default=True, server_default="true")
    quiet_hours_start = Column(Time, nullable=True)
    quiet_hours_end = Column(Time, nullable=True)
    knowledge_digest_hour = Column(Integer, nullable=False, default=14, server_default="14")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    user = relationship("User", foreign_keys=[user_id])

    __table_args__ = (Index("ix_teams_alert_config_user", "user_id"),)
