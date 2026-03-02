"""Self-heal log — append-only record of every diagnosis and fix attempt.

Tracks patterns over time: which categories recur, which fixes succeed,
which risk tiers need human intervention.

Called by: services/diagnosis_service.py, services/execution_service.py
Depends on: models/base.py, models/trouble_ticket.py
"""

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, JSON, String

from app.models.base import Base


class SelfHealLog(Base):
    __tablename__ = "self_heal_log"
    __table_args__ = (
        Index("ix_self_heal_log_ticket_id", "ticket_id"),
        Index("ix_self_heal_log_created_at", "created_at"),
    )

    id = Column(Integer, primary_key=True)
    ticket_id = Column(Integer, ForeignKey("trouble_tickets.id", ondelete="CASCADE"), nullable=False)
    category = Column(String(20))
    risk_tier = Column(String(10))
    files_modified = Column(JSON)
    fix_succeeded = Column(Boolean)
    iterations_used = Column(Integer)
    cost_usd = Column(Float)
    user_verified = Column(Boolean)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
