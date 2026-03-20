"""Excess Inventory & Bid Collection models.

Data models for managing customer excess/surplus inventory and collecting
bids from potential buyers. This is the reverse of sourcing: customer has
parts to sell, Trio finds buyers.

Business Rules:
- ExcessList belongs to a Company (the seller) and is owned by a User (salesperson/trader)
- ExcessLineItems cascade-delete with their parent ExcessList
- Bids can come from Companies (customers) or VendorCards (vendors)
- BidSolicitations track outbound bid request emails

Called by: routers/excess.py, services/excess.py (Phase 2+)
Depends on: models/base, models with Company, User, VendorCard, CustomerSite
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from .base import Base


class ExcessList(Base):
    """Customer excess/surplus inventory list — analogous to Requisition."""

    __tablename__ = "excess_lists"
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    customer_site_id = Column(Integer, ForeignKey("customer_sites.id", ondelete="SET NULL"), nullable=True)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    title = Column(String(255), nullable=False)
    status = Column(String(20), default="draft")  # draft, active, bidding, closed, expired
    source_filename = Column(String(255), nullable=True)
    notes = Column(Text, nullable=True)
    total_line_items = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, onupdate=lambda: datetime.now(timezone.utc))

    company = relationship("Company", foreign_keys=[company_id])
    customer_site = relationship("CustomerSite", foreign_keys=[customer_site_id])
    owner = relationship("User", foreign_keys=[owner_id])
    line_items = relationship("ExcessLineItem", back_populates="excess_list", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_excess_lists_company", "company_id"),
        Index("ix_excess_lists_owner", "owner_id"),
        Index("ix_excess_lists_status", "status"),
    )


class ExcessLineItem(Base):
    """Individual part for sale within an ExcessList — analogous to Requirement."""

    __tablename__ = "excess_line_items"
    id = Column(Integer, primary_key=True)
    excess_list_id = Column(Integer, ForeignKey("excess_lists.id", ondelete="CASCADE"), nullable=False)
    part_number = Column(String(100), nullable=False, index=True)
    normalized_part_number = Column(String(100), nullable=True, index=True)
    manufacturer = Column(String(255), nullable=True)
    quantity = Column(Integer, nullable=False)
    date_code = Column(String(50), nullable=True)
    condition = Column(String(50), default="New")
    asking_price = Column(Numeric(12, 4), nullable=True)
    market_price = Column(Numeric(12, 4), nullable=True)
    demand_score = Column(Integer, nullable=True)  # 0–100
    demand_match_count = Column(Integer, default=0)
    status = Column(String(20), default="available")  # available, bidding, awarded, withdrawn
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, onupdate=lambda: datetime.now(timezone.utc))

    excess_list = relationship("ExcessList", back_populates="line_items")
    bids = relationship("Bid", back_populates="excess_line_item", cascade="all, delete-orphan")
    solicitations = relationship("BidSolicitation", back_populates="excess_line_item", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_excess_line_items_list", "excess_list_id"),
        Index("ix_excess_line_items_status", "status"),
    )


class BidSolicitation(Base):
    """Outbound bid request sent to a potential buyer — analogous to RFQ tracking."""

    __tablename__ = "bid_solicitations"
    id = Column(Integer, primary_key=True)
    excess_line_item_id = Column(Integer, ForeignKey("excess_line_items.id", ondelete="CASCADE"), nullable=False)
    contact_id = Column(Integer, nullable=False)  # generic FK — no EmailTrack model exists
    sent_by = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    email_track_id = Column(Integer, nullable=True)  # reserved for future email tracking FK
    recipient_email = Column(String(255), nullable=True)
    recipient_name = Column(String(255), nullable=True)
    graph_message_id = Column(String(500), nullable=True)  # Graph API message ID for tracking
    subject = Column(String(500), nullable=True)
    body_preview = Column(Text, nullable=True)  # first ~200 chars of email body
    response_received_at = Column(DateTime, nullable=True)
    parsed_bid_id = Column(
        Integer, ForeignKey("bids.id", ondelete="SET NULL", use_alter=True), nullable=True
    )  # auto-created bid
    status = Column(String(20), default="pending")  # pending, sent, responded, expired, failed
    sent_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    excess_line_item = relationship("ExcessLineItem", back_populates="solicitations")
    sent_by_user = relationship("User", foreign_keys=[sent_by])
    parsed_bid = relationship("Bid", foreign_keys=[parsed_bid_id])

    __table_args__ = (
        Index("ix_bid_solicitations_line_item", "excess_line_item_id"),
        Index("ix_bid_solicitations_contact", "contact_id"),
        Index("ix_bid_solicitations_graph_msg", "graph_message_id"),
    )


class Bid(Base):
    """Incoming bid from a potential buyer — analogous to Offer."""

    __tablename__ = "bids"
    id = Column(Integer, primary_key=True)
    excess_line_item_id = Column(Integer, ForeignKey("excess_line_items.id", ondelete="CASCADE"), nullable=False)
    bidder_company_id = Column(Integer, ForeignKey("companies.id", ondelete="SET NULL"), nullable=True)
    bidder_vendor_card_id = Column(Integer, ForeignKey("vendor_cards.id", ondelete="SET NULL"), nullable=True)
    bidder_contact_id = Column(Integer, nullable=True)
    unit_price = Column(Numeric(12, 4), nullable=False)
    quantity_wanted = Column(Integer, nullable=False)
    lead_time_days = Column(Integer, nullable=True)
    status = Column(String(20), default="pending")  # pending, accepted, rejected, expired, withdrawn
    source = Column(String(20), default="manual")  # manual, email_parsed, phone
    notes = Column(Text, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, onupdate=lambda: datetime.now(timezone.utc))

    excess_line_item = relationship("ExcessLineItem", back_populates="bids")
    bidder_company = relationship("Company", foreign_keys=[bidder_company_id])
    bidder_vendor_card = relationship("VendorCard", foreign_keys=[bidder_vendor_card_id])
    created_by_user = relationship("User", foreign_keys=[created_by])

    __table_args__ = (
        Index("ix_bids_line_item", "excess_line_item_id"),
        Index("ix_bids_company", "bidder_company_id"),
        Index("ix_bids_vendor_card", "bidder_vendor_card_id"),
        Index("ix_bids_status", "status"),
    )
