"""Root cause grouping for trouble tickets — AI-generated categories.

Called by: routers/error_reports.py (batch analyze)
Depends on: models/base.py
"""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String, Text

from .base import Base


class RootCauseGroup(Base):
    __tablename__ = "root_cause_groups"

    id = Column(Integer, primary_key=True)
    title = Column(String(200), nullable=False)
    suggested_fix = Column(Text)
    status = Column(String(30), default="open", nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, onupdate=lambda: datetime.now(timezone.utc))
