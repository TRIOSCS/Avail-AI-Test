"""NetComponents search queue model.

Tracks parts that need to be searched on NetComponents marketplace.
Each requirement with an MPN gets a queue entry; the AI gate decides
whether to search or skip based on commodity classification.

Called by: nc_worker queue_manager, ai_gate, admin endpoints
Depends on: requirements, requisitions tables
"""

from datetime import datetime, timezone

from sqlalchemy import Column, ForeignKey, Index, Integer, SmallInteger, String, Text

from ..database import UTCDateTime
from .base import Base


class NcSearchQueue(Base):
    __tablename__ = "nc_search_queue"

    id = Column(Integer, primary_key=True)
    # Dedup is keyed on (requirement_id, normalized_mpn) so one requirement
    # can have multiple queue rows when the spec-code resolver enqueues
    # additional AVL MPNs alongside the primary MPN. Application-level
    # check lives in QueueManager.enqueue_search.
    requirement_id = Column(Integer, ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False)
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
    last_searched_at = Column(UTCDateTime(timezone=True))
    results_count = Column(Integer)
    error_message = Column(Text)
    # Spec-code resolver lineage — populated when this queue row was created
    # for an AVL MPN resolved from an OEM spec code (see SpecCodeResolver).
    resolved_via_spec_code = Column(String(64), nullable=True)
    created_at = Column(UTCDateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(UTCDateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

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
