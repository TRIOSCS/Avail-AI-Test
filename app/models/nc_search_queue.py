"""NetComponents search queue model.

Tracks parts that need to be searched on NetComponents marketplace.
Each requirement with an MPN gets a queue entry; the AI gate decides
whether to search or skip based on commodity classification.

Called by: nc_worker queue_manager, ai_gate, admin endpoints
Depends on: requirements, requisitions tables
"""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, SmallInteger, String, Text

from .base import Base


class NcSearchQueue(Base):
    __tablename__ = "nc_search_queue"

    id = Column(Integer, primary_key=True)
    requirement_id = Column(Integer, ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False, unique=True)
    requisition_id = Column(Integer, ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False)
    mpn = Column(String(100), nullable=False)
    normalized_mpn = Column(String(100), nullable=False)
    manufacturer = Column(String(200))
    description = Column(Text)
    commodity_class = Column(String(50))
    gate_decision = Column(String(20))
    gate_reason = Column(String(200))
    priority = Column(SmallInteger, default=3)
    status = Column(String(20), default="pending")
    search_count = Column(Integer, default=0)
    last_searched_at = Column(DateTime)
    results_count = Column(Integer)
    error_message = Column(Text)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index(
            "ix_nc_queue_poll",
            "status",
            "priority",
            "created_at",
            postgresql_where=(Column("status") == "queued"),
        ),
        Index(
            "ix_nc_queue_dedup",
            "normalized_mpn",
            last_searched_at.desc(),
            postgresql_where=(Column("status") == "completed"),
        ),
    )
