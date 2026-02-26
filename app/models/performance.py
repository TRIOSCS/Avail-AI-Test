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


class AvailScoreSnapshot(Base):
    """Monthly Avail Score snapshot — behavior + outcome scoring for bonus ranking.

    Stores 10 individual metric scores (0–10 each) split into behaviors (50 max)
    and outcomes (50 max) for a total of 0–100. Used for buyer and sales leaderboards
    with financial bonus for 1st/2nd place.

    Called by: services/avail_score_service.py (daily compute), routers/performance.py
    """

    __tablename__ = "avail_score_snapshot"
    id = Column(Integer, primary_key=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    month = Column(Date, nullable=False)
    role_type = Column(String(20), nullable=False)  # 'buyer' or 'sales'

    # Behavior metrics (0–10 each, 50 max total)
    b1_score = Column(Float, default=0)
    b1_label = Column(String(50))
    b1_raw = Column(String(100))  # human-readable raw value, e.g. "4.2h avg"
    b2_score = Column(Float, default=0)
    b2_label = Column(String(50))
    b2_raw = Column(String(100))
    b3_score = Column(Float, default=0)
    b3_label = Column(String(50))
    b3_raw = Column(String(100))
    b4_score = Column(Float, default=0)
    b4_label = Column(String(50))
    b4_raw = Column(String(100))
    b5_score = Column(Float, default=0)
    b5_label = Column(String(50))
    b5_raw = Column(String(100))

    behavior_total = Column(Float, default=0)  # sum of b1–b5

    # Outcome metrics (0–10 each, 50 max total)
    o1_score = Column(Float, default=0)
    o1_label = Column(String(50))
    o1_raw = Column(String(100))
    o2_score = Column(Float, default=0)
    o2_label = Column(String(50))
    o2_raw = Column(String(100))
    o3_score = Column(Float, default=0)
    o3_label = Column(String(50))
    o3_raw = Column(String(100))
    o4_score = Column(Float, default=0)
    o4_label = Column(String(50))
    o4_raw = Column(String(100))
    o5_score = Column(Float, default=0)
    o5_label = Column(String(50))
    o5_raw = Column(String(100))

    outcome_total = Column(Float, default=0)  # sum of o1–o5

    # Composite
    total_score = Column(Float, default=0)  # behavior_total + outcome_total (0–100)
    rank = Column(Integer)
    qualified = Column(Boolean, default=False)  # meets minimum activity threshold
    bonus_amount = Column(Float, default=0)  # $500/$250 or 0

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    user = relationship("User", foreign_keys=[user_id])

    __table_args__ = (
        Index("ix_ass_user_month", "user_id", "month", "role_type", unique=True),
        Index("ix_ass_month_role_rank", "month", "role_type", "rank"),
        Index("ix_ass_month_role_score", "month", "role_type", "total_score"),
    )


class MultiplierScoreSnapshot(Base):
    """Monthly multiplier score snapshot — competitive points for bonus determination.

    Tracks offer pipeline progression points (non-stacking: each offer earns
    only its highest tier) plus bonus points from RFQs/stock lists (buyer)
    or new accounts (sales).  Column-based breakdown matches AvailScoreSnapshot
    pattern for queryability.

    Called by: services/multiplier_score_service.py (daily), routers/performance.py
    """

    __tablename__ = "multiplier_score_snapshot"
    id = Column(Integer, primary_key=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    month = Column(Date, nullable=False)
    role_type = Column(String(20), nullable=False)  # 'buyer' or 'sales'

    # Totals
    offer_points = Column(Float, default=0)  # from pipeline progression
    bonus_points = Column(Float, default=0)  # from RFQs/stock lists or accounts
    total_points = Column(Float, default=0)  # offer_points + bonus_points

    # Buyer breakdown: offer pipeline tiers (non-stacking)
    offers_total = Column(Integer, default=0)
    offers_base_count = Column(Integer, default=0)
    offers_base_pts = Column(Float, default=0)
    offers_quoted_count = Column(Integer, default=0)
    offers_quoted_pts = Column(Float, default=0)
    offers_bp_count = Column(Integer, default=0)
    offers_bp_pts = Column(Float, default=0)
    offers_po_count = Column(Integer, default=0)
    offers_po_pts = Column(Float, default=0)
    rfqs_sent_count = Column(Integer, default=0)
    rfqs_sent_pts = Column(Float, default=0)
    stock_lists_count = Column(Integer, default=0)
    stock_lists_pts = Column(Float, default=0)

    # Sales breakdown: quotes + proactive + accounts
    quotes_sent_count = Column(Integer, default=0)
    quotes_sent_pts = Column(Float, default=0)
    quotes_won_count = Column(Integer, default=0)
    quotes_won_pts = Column(Float, default=0)
    proactive_sent_count = Column(Integer, default=0)
    proactive_sent_pts = Column(Float, default=0)
    proactive_converted_count = Column(Integer, default=0)
    proactive_converted_pts = Column(Float, default=0)
    new_accounts_count = Column(Integer, default=0)
    new_accounts_pts = Column(Float, default=0)

    rank = Column(Integer)
    avail_score = Column(Float, default=0)  # cached for qualification check
    qualified = Column(Boolean, default=False)
    bonus_amount = Column(Float, default=0)  # $500/$250 or 0

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    user = relationship("User", foreign_keys=[user_id])

    __table_args__ = (
        Index("ix_mss_user_month", "user_id", "month", "role_type", unique=True),
        Index("ix_mss_month_role_rank", "month", "role_type", "rank"),
        Index("ix_mss_month_role_points", "month", "role_type", "total_points"),
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


