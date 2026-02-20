"""Performance tracking models — scorecards and leaderboards."""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import relationship

from .base import Base


class VendorMetricsSnapshot(Base):
    """Daily snapshot of vendor performance metrics (90-day rolling window)."""

    __tablename__ = "vendor_metrics_snapshot"
    id = Column(Integer, primary_key=True)
    vendor_card_id = Column(
        Integer, ForeignKey("vendor_cards.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_date = Column(Date, nullable=False)

    response_rate = Column(Float)
    quote_accuracy = Column(Float)
    on_time_delivery = Column(Float)
    cancellation_rate = Column(Float)
    rma_rate = Column(Float)
    lead_time_accuracy = Column(Float)
    quote_conversion = Column(Float)
    po_conversion = Column(Float)
    avg_review_rating = Column(Float)

    composite_score = Column(Float)
    interaction_count = Column(Integer, default=0)
    is_sufficient_data = Column(Boolean, default=False)

    rfqs_sent = Column(Integer, default=0)
    rfqs_answered = Column(Integer, default=0)
    pos_in_window = Column(Integer, default=0)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    vendor_card = relationship("VendorCard", foreign_keys=[vendor_card_id])

    __table_args__ = (
        Index("ix_vms_vendor_date", "vendor_card_id", "snapshot_date", unique=True),
        Index("ix_vms_date", "snapshot_date"),
        Index("ix_vms_composite", "composite_score"),
    )


class BuyerLeaderboardSnapshot(Base):
    """Monthly buyer leaderboard snapshot with multiplier scoring."""

    __tablename__ = "buyer_leaderboard_snapshot"
    id = Column(Integer, primary_key=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    month = Column(Date, nullable=False)

    offers_logged = Column(Integer, default=0)
    offers_quoted = Column(Integer, default=0)
    offers_in_buyplan = Column(Integer, default=0)
    offers_po_confirmed = Column(Integer, default=0)
    stock_lists_uploaded = Column(Integer, default=0)

    points_offers = Column(Integer, default=0)
    points_quoted = Column(Integer, default=0)
    points_buyplan = Column(Integer, default=0)
    points_po = Column(Integer, default=0)
    points_stock = Column(Integer, default=0)
    total_points = Column(Integer, default=0)

    rank = Column(Integer)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    user = relationship("User", foreign_keys=[user_id])

    __table_args__ = (
        Index("ix_bls_user_month", "user_id", "month", unique=True),
        Index("ix_bls_month_rank", "month", "rank"),
        Index("ix_bls_month_points", "month", "total_points"),
    )


class StockListHash(Base):
    """Deduplication hashes for uploaded stock lists."""

    __tablename__ = "stock_list_hashes"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    content_hash = Column(String(64), nullable=False)
    vendor_card_id = Column(Integer, ForeignKey("vendor_cards.id"))
    file_name = Column(String(500))
    row_count = Column(Integer)
    first_seen_at = Column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    last_seen_at = Column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    upload_count = Column(Integer, default=1)

    user = relationship("User", foreign_keys=[user_id])

    __table_args__ = (
        Index("ix_slh_hash", "content_hash"),
        Index("ix_slh_user_hash", "user_id", "content_hash", unique=True),
        Index("ix_slh_vendor", "vendor_card_id"),
    )


class BuyerVendorStats(Base):
    """Per-buyer performance with a specific vendor. Auto-populated."""

    __tablename__ = "buyer_vendor_stats"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    vendor_card_id = Column(Integer, ForeignKey("vendor_cards.id"), nullable=False)

    rfqs_sent = Column(Integer, default=0)
    responses_received = Column(Integer, default=0)
    response_rate = Column(Float)
    offers_logged = Column(Integer, default=0)
    offers_won = Column(Integer, default=0)
    win_rate = Column(Float)
    avg_response_hours = Column(Float)
    last_contact_at = Column(DateTime)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    user = relationship("User", foreign_keys=[user_id])
    vendor_card = relationship("VendorCard", foreign_keys=[vendor_card_id])

    __table_args__ = (
        Index("ix_bvs_vendor", "vendor_card_id"),
        Index("ix_bvs_user", "user_id"),
        Index("ix_bvs_unique", "user_id", "vendor_card_id", unique=True),
    )


