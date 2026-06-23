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
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import relationship, validates

from ..database import UTCDateTime
from .base import Base


class ExcessList(Base):
    """Customer excess/surplus inventory list — analogous to Requisition."""

    __tablename__ = "excess_lists"
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    customer_site_id = Column(Integer, ForeignKey("customer_sites.id", ondelete="SET NULL"), nullable=True)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    title = Column(String(255), nullable=False)
    status = Column(String(20), default="draft")  # draft, open, collecting, bid_out, awarded, closed, expired
    # Lock-on-post: revising a posted list bumps version (spec §Resolved-for-v1 #2).
    version = Column(Integer, nullable=False, default=1, server_default="1")
    source_filename = Column(String(255), nullable=True)
    notes = Column(Text, nullable=True)
    total_line_items = Column(Integer, default=0)
    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc), server_default=func.now())
    updated_at = Column(UTCDateTime, onupdate=lambda: datetime.now(timezone.utc), server_default=func.now())

    company = relationship("Company", foreign_keys=[company_id])
    customer_site = relationship("CustomerSite", foreign_keys=[customer_site_id])
    owner = relationship("User", foreign_keys=[owner_id])
    line_items = relationship("ExcessLineItem", back_populates="excess_list", cascade="all, delete-orphan")
    offers = relationship("ExcessOffer", back_populates="excess_list", cascade="all, delete-orphan")

    # --- Validators ---
    @validates("status")
    def _validate_status(self, _key, value):
        from ..constants import ExcessListStatus

        valid = {e.value for e in ExcessListStatus}
        if value and value not in valid:
            raise ValueError(f"Invalid ExcessList status: {value!r}")
        return value

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
    # Resolved on create (the Sighting mirror needs it; spec §Data-model).
    material_card_id = Column(Integer, ForeignKey("material_cards.id", ondelete="SET NULL"), nullable=True)
    description = Column(String(500), nullable=True)
    manufacturer = Column(String(255), nullable=True)
    quantity = Column(Integer, nullable=False)
    date_code = Column(String(50), nullable=True)
    condition = Column(String(50), default="New")
    asking_price = Column(Numeric(12, 4), nullable=True)
    # Best-price rollup across this line's collected offers — recomputed on offer
    # land/withdraw (spec §Offer-collection). best_offer_id is a plain int (NOT a hard
    # FK) to avoid a circular cascade with excess_offers.
    best_offer_unit_price = Column(Numeric(12, 4), nullable=True)
    best_offer_id = Column(Integer, nullable=True)
    offer_count = Column(Integer, nullable=False, default=0, server_default="0")
    demand_match_count = Column(Integer, default=0)
    status = Column(String(20), default="available")  # available, bidding, awarded, withdrawn
    notes = Column(Text, nullable=True)
    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc), server_default=func.now())
    updated_at = Column(UTCDateTime, onupdate=lambda: datetime.now(timezone.utc), server_default=func.now())

    excess_list = relationship("ExcessList", back_populates="line_items")
    bids = relationship("Bid", back_populates="excess_line_item", cascade="all, delete-orphan")
    solicitations = relationship("BidSolicitation", back_populates="excess_line_item", cascade="all, delete-orphan")

    # --- Validators ---
    @validates("quantity")
    def _validate_quantity(self, _key, value):
        if value is not None and value <= 0:
            raise ValueError("Quantity must be positive")
        return value

    __table_args__ = (
        Index("ix_excess_line_items_list", "excess_list_id"),
        Index("ix_excess_line_items_status", "status"),
        Index("ix_excess_line_items_pn_status", "part_number", "status"),
        Index("ix_excess_line_items_demand", "demand_match_count", "status"),
    )


class BidSolicitation(Base):
    """Outbound bid request sent to a potential buyer — analogous to RFQ tracking."""

    __tablename__ = "bid_solicitations"
    id = Column(Integer, primary_key=True)
    excess_line_item_id = Column(Integer, ForeignKey("excess_line_items.id", ondelete="CASCADE"), nullable=False)
    contact_id = Column(Integer, nullable=False)  # generic FK — no EmailTrack model exists
    sent_by = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    recipient_email = Column(String(255), nullable=True)
    recipient_name = Column(String(255), nullable=True)
    graph_message_id = Column(String(500), nullable=True)  # Graph API message ID for tracking
    subject = Column(String(500), nullable=True)
    body_preview = Column(String(500), nullable=True)  # First 500 chars of email body
    response_received_at = Column(UTCDateTime, nullable=True)
    parsed_bid_id = Column(
        Integer, ForeignKey("bids.id", ondelete="SET NULL", use_alter=True), nullable=True
    )  # auto-created bid
    status = Column(String(20), default="pending")  # pending, sent, responded, expired, failed
    sent_at = Column(UTCDateTime, nullable=True)
    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc), server_default=func.now())

    excess_line_item = relationship("ExcessLineItem", back_populates="solicitations")
    sent_by_user = relationship("User", foreign_keys=[sent_by])
    parsed_bid = relationship("Bid", foreign_keys=[parsed_bid_id])

    __table_args__ = (
        Index("ix_bid_solicitations_line_item", "excess_line_item_id"),
        Index("ix_bid_solicitations_contact", "contact_id"),
        Index("ix_bid_solicitations_graph_msg", "graph_message_id"),
        Index("ix_bidsol_status", "status"),
    )


class Bid(Base):
    """Incoming bid from a potential buyer — analogous to Offer."""

    __tablename__ = "bids"
    id = Column(Integer, primary_key=True)
    excess_line_item_id = Column(Integer, ForeignKey("excess_line_items.id", ondelete="CASCADE"), nullable=False)
    bidder_company_id = Column(Integer, ForeignKey("companies.id", ondelete="SET NULL"), nullable=True)
    bidder_vendor_card_id = Column(Integer, ForeignKey("vendor_cards.id", ondelete="SET NULL"), nullable=True)
    unit_price = Column(Numeric(12, 4), nullable=False)
    quantity_wanted = Column(Integer, nullable=False)
    lead_time_days = Column(Integer, nullable=True)
    status = Column(String(20), default="pending")  # pending, accepted, rejected, expired, withdrawn
    source = Column(String(20), default="manual")  # manual, email_parsed, phone
    notes = Column(Text, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc), server_default=func.now())
    updated_at = Column(UTCDateTime, onupdate=lambda: datetime.now(timezone.utc), server_default=func.now())

    excess_line_item = relationship("ExcessLineItem", back_populates="bids")
    bidder_company = relationship("Company", foreign_keys=[bidder_company_id])
    bidder_vendor_card = relationship("VendorCard", foreign_keys=[bidder_vendor_card_id])
    created_by_user = relationship("User", foreign_keys=[created_by])

    # --- Validators ---
    @validates("unit_price")
    def _validate_unit_price(self, _key, value):
        if value is not None and value < 0:
            raise ValueError(f"unit_price must be >= 0, got {value}")
        return value

    @validates("quantity_wanted")
    def _validate_quantity_wanted(self, _key, value):
        if value is not None and value <= 0:
            raise ValueError("quantity_wanted must be positive")
        return value

    __table_args__ = (
        Index("ix_bids_line_item", "excess_line_item_id"),
        Index("ix_bids_company", "bidder_company_id"),
        Index("ix_bids_vendor_card", "bidder_vendor_card_id"),
        Index("ix_bids_status", "status"),
    )


class ExcessOffer(Base):
    """Inbound offer from another broker to BUY a posted excess list.

    The Trading-module replacement for the per-line, money-required ``Bid``. An offer
    is either ``per_line`` (carries ``ExcessOfferLine`` rows, one per part the broker
    will buy) or ``take_all`` (binds the whole list, no line rows, optional lump
    ``take_all_total_price``). Matching is part-number only; ``unit_price`` is collected
    but nullable, then rolled up to the best-per-unit the trader plans the bid-back with.
    """

    __tablename__ = "excess_offers"
    id = Column(Integer, primary_key=True)
    excess_list_id = Column(Integer, ForeignKey("excess_lists.id", ondelete="CASCADE"), nullable=False)
    submitted_by = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    offerer_company_id = Column(Integer, ForeignKey("companies.id", ondelete="SET NULL"), nullable=True)
    offerer_vendor_card_id = Column(Integer, ForeignKey("vendor_cards.id", ondelete="SET NULL"), nullable=True)
    scope = Column(String(20), default="per_line")  # per_line, take_all
    take_all_total_price = Column(Numeric(12, 4), nullable=True)  # lump sum, take_all only
    valid_until = Column(UTCDateTime, nullable=True)
    status = Column(String(20), default="open")  # open, won, lost, expired, withdrawn, late
    notes = Column(Text, nullable=True)
    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc), server_default=func.now())
    updated_at = Column(UTCDateTime, onupdate=lambda: datetime.now(timezone.utc), server_default=func.now())

    excess_list = relationship("ExcessList", back_populates="offers")
    submitted_by_user = relationship("User", foreign_keys=[submitted_by])
    offerer_company = relationship("Company", foreign_keys=[offerer_company_id])
    offerer_vendor_card = relationship("VendorCard", foreign_keys=[offerer_vendor_card_id])
    lines = relationship("ExcessOfferLine", back_populates="offer", cascade="all, delete-orphan")

    # --- Validators ---
    @validates("scope")
    def _validate_scope(self, _key, value):
        from ..constants import ExcessOfferScope

        valid = {e.value for e in ExcessOfferScope}
        if value and value not in valid:
            raise ValueError(f"Invalid ExcessOffer scope: {value!r}")
        return value

    @validates("status")
    def _validate_status(self, _key, value):
        from ..constants import ExcessOfferStatus

        valid = {e.value for e in ExcessOfferStatus}
        if value and value not in valid:
            raise ValueError(f"Invalid ExcessOffer status: {value!r}")
        return value

    __table_args__ = (
        Index("ix_excess_offers_list", "excess_list_id"),
        Index("ix_excess_offers_status", "status"),
    )


class ExcessOfferLine(Base):
    """A single part line within a ``per_line`` ExcessOffer.

    ``excess_line_item_id`` is nullable: a row whose ``mpn_raw`` does not cleanly match
    a posted line is held in the unmatched queue (``match_status='unmatched'`` /
    ``'ambiguous'``) for manual resolution — never dropped. ``unit_price`` is nullable
    (a broker may bid "take-all, price TBD").
    """

    __tablename__ = "excess_offer_lines"
    id = Column(Integer, primary_key=True)
    offer_id = Column(Integer, ForeignKey("excess_offers.id", ondelete="CASCADE"), nullable=False)
    excess_line_item_id = Column(Integer, ForeignKey("excess_line_items.id", ondelete="SET NULL"), nullable=True)
    mpn_raw = Column(String(100), nullable=False)
    quantity = Column(Integer, nullable=False)
    unit_price = Column(Numeric(12, 4), nullable=True)
    lead_time_days = Column(Integer, nullable=True)
    terms_text = Column(Text, nullable=True)
    match_status = Column(String(20), default="unmatched")  # matched, unmatched, ambiguous

    offer = relationship("ExcessOffer", back_populates="lines")
    excess_line_item = relationship("ExcessLineItem", foreign_keys=[excess_line_item_id])

    # --- Validators ---
    @validates("quantity")
    def _validate_quantity(self, _key, value):
        if value is not None and value <= 0:
            raise ValueError("Quantity must be positive")
        return value

    @validates("match_status")
    def _validate_match_status(self, _key, value):
        from ..constants import OfferLineMatchStatus

        valid = {e.value for e in OfferLineMatchStatus}
        if value and value not in valid:
            raise ValueError(f"Invalid OfferLine match_status: {value!r}")
        return value

    __table_args__ = (
        Index("ix_excess_offer_lines_offer", "offer_id"),
        Index("ix_excess_offer_lines_line_item", "excess_line_item_id"),
    )
