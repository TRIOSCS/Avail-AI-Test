"""Unified Score Snapshot — cross-role performance scoring for combined leaderboard.

Stores 5 normalized category percentages (Prospecting, Execution, Follow-Through,
Closing, Depth) plus a weighted unified score (0-100). Enables buyers, sales,
and traders to compete on one leaderboard with fair cross-role normalization.

Called by: services/unified_score_service.py (daily compute via scheduler)
Depends on: models/performance.py (AvailScoreSnapshot, MultiplierScoreSnapshot)
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from .base import Base


class UnifiedScoreSnapshot(Base):
    """Monthly unified score snapshot — cross-role normalized scoring.

    Combines AvailScore metrics into 5 universal categories weighted to produce a single
    0-100 unified score. Traders average buyer + sales categories. AI blurbs (strength +
    improvement) are cached with 2-hour TTL.
    """

    __tablename__ = "unified_score_snapshot"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    month = Column(Date, nullable=False)

    # 5 category percentages (0-100 each)
    prospecting_pct = Column(Float, default=0)
    execution_pct = Column(Float, default=0)
    followthrough_pct = Column(Float, default=0)
    closing_pct = Column(Float, default=0)
    depth_pct = Column(Float, default=0)

    # Weighted total
    unified_score = Column(Float, default=0)  # 0-100
    rank = Column(Integer)

    # Source scores cached for display
    primary_role = Column(String(20))  # buyer / sales / trader
    avail_score_buyer = Column(Float)
    avail_score_sales = Column(Float)
    multiplier_points_buyer = Column(Float)
    multiplier_points_sales = Column(Float)

    # AI blurb
    ai_blurb_strength = Column(Text)
    ai_blurb_improvement = Column(Text)
    ai_blurb_generated_at = Column(DateTime)

    # Timestamps
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    user = relationship("User", foreign_keys=[user_id])

    __table_args__ = (
        Index("ix_uss_user_month", "user_id", "month", unique=True),
        Index("ix_uss_month_rank", "month", "rank"),
    )
