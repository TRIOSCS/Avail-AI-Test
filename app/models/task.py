"""RequisitionTask — pipeline-style task board per requisition.

Tracks sourcing, sales, and general tasks through pipeline stages.
Auto-generated from system events (offers, RFQs, quotes) and manually
by buyers. AI priority scoring and risk alerts for task management.

Called by: services/task_service.py, routers/task.py
Depends on: models/base.py, models/auth.py, models/sourcing.py
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Column, DateTime, Float, ForeignKey, Index, Integer, String, Text,
)
from sqlalchemy.orm import relationship

from .base import Base


class RequisitionTask(Base):
    __tablename__ = "requisition_tasks"

    id = Column(Integer, primary_key=True, index=True)
    requisition_id = Column(Integer, ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)

    # sourcing | sales | general
    task_type = Column(String(20), nullable=False, default="general")
    # todo | in_progress | done
    status = Column(String(20), nullable=False, default="todo")
    # 1=low, 2=medium, 3=high
    priority = Column(Integer, nullable=False, default=2)

    # AI-computed fields
    ai_priority_score = Column(Float, nullable=True)  # 0.0-1.0, higher = more urgent
    ai_risk_flag = Column(String(255), nullable=True)  # risk alert text

    # Assignment
    assigned_to_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    # Origin tracking
    source = Column(String(20), nullable=False, default="manual")  # manual | system | ai
    source_ref = Column(String(100), nullable=True)  # e.g. "offer:123", "rfq:456"

    # Dates
    due_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    requisition = relationship("Requisition", foreign_keys=[requisition_id])
    assignee = relationship("User", foreign_keys=[assigned_to_id])
    creator = relationship("User", foreign_keys=[created_by])

    __table_args__ = (
        Index("ix_rt_req_status", "requisition_id", "status"),
        Index("ix_rt_assignee_status", "assigned_to_id", "status"),
        Index("ix_rt_status_due", "status", "due_at"),
    )
