"""Excess Inventory & Resell (resell-brokerage) models.

Data models for the Resell workspace: customers post excess/surplus inventory
(ExcessList / ExcessLineItem), brokers submit offers to buy (ExcessOffer /
ExcessOfferLine), and the trader assembles a clean bid back to the seller
(CustomerBid / CustomerBidLine). This is the reverse of sourcing: the customer
has parts to sell, Trio finds buyers.

Business Rules:
- ExcessList belongs to a Company (the seller) and is owned by a User (salesperson/trader)
- ExcessLineItems cascade-delete with their parent ExcessList
- ExcessOffers are inbound broker offers to buy (per_line or take_all)
- CustomerBids are the outbound clean bid back to the seller

Called by: routers/resell.py, services/excess_service.py
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
    # Posting window: open_at stamped on publish, close_at on close_list. Both nullable —
    # a draft has neither; close_at drives the "closes in Xd" urgency chip (spec §Data-model).
    open_at = Column(UTCDateTime, nullable=True)
    close_at = Column(UTCDateTime, nullable=True)
    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc), server_default=func.now())
    updated_at = Column(UTCDateTime, onupdate=lambda: datetime.now(timezone.utc), server_default=func.now())

    company = relationship("Company", foreign_keys=[company_id])
    customer_site = relationship("CustomerSite", foreign_keys=[customer_site_id])
    owner = relationship("User", foreign_keys=[owner_id])
    line_items = relationship("ExcessLineItem", back_populates="excess_list", cascade="all, delete-orphan")
    offers = relationship("ExcessOffer", back_populates="excess_list", cascade="all, delete-orphan")
    customer_bids = relationship("CustomerBid", back_populates="excess_list", cascade="all, delete-orphan")

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


class ExcessOffer(Base):
    """Inbound offer from another broker to BUY a posted excess list.

    The Resell-module replacement for the per-line, money-required ``Bid``. An offer
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


class CustomerBid(Base):
    """The outbound bid back — Trio's offer to BUY a customer's excess (the stock
    holder).

    The owner assembles selected inbound ExcessOffers into one customer-facing document,
    priced per line from the best-per-unit rollup (the trader may override each price).
    Exported as a CLEAN PDF (reuses the Quote report path) that NEVER carries broker /
    trader / source names — cleanliness is enforced at assembly (see
    ``bid_back_service.bid_back_export_context``), not just by template omission.
    """

    __tablename__ = "customer_bids"
    id = Column(Integer, primary_key=True)
    excess_list_id = Column(Integer, ForeignKey("excess_lists.id", ondelete="CASCADE"), nullable=False)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    status = Column(String(20), default="draft")  # draft, sent, accepted, rejected
    revision = Column(Integer, nullable=False, default=1, server_default="1")
    notes = Column(Text, nullable=True)
    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc), server_default=func.now())
    updated_at = Column(UTCDateTime, onupdate=lambda: datetime.now(timezone.utc), server_default=func.now())

    excess_list = relationship("ExcessList", back_populates="customer_bids")
    owner = relationship("User", foreign_keys=[owner_id])
    lines = relationship("CustomerBidLine", back_populates="customer_bid", cascade="all, delete-orphan")

    # --- Validators ---
    @validates("status")
    def _validate_status(self, _key, value):
        from ..constants import CustomerBidStatus

        valid = {e.value for e in CustomerBidStatus}
        if value and value not in valid:
            raise ValueError(f"Invalid CustomerBid status: {value!r}")
        return value

    __table_args__ = (
        Index("ix_customer_bids_list", "excess_list_id"),
        Index("ix_customer_bids_owner", "owner_id"),
        Index("ix_customer_bids_status", "status"),
    )


class CustomerBidLine(Base):
    """A single priced line within a CustomerBid.

    ``customer_unit_price`` is the trader's offer to the seller for this part — seeded
    from the line's ``best_offer_unit_price`` rollup, overridable per line. ``selected_offer_id``
    / ``selected_offer_line_id`` record WHICH inbound offer informed the price for the
    owner's internal audit — they are deliberately NOT exported (the customer never sees
    which broker bid what). Both nullable: a line may be priced manually with no backing
    offer.
    """

    __tablename__ = "customer_bid_lines"
    id = Column(Integer, primary_key=True)
    customer_bid_id = Column(Integer, ForeignKey("customer_bids.id", ondelete="CASCADE"), nullable=False)
    excess_line_item_id = Column(Integer, ForeignKey("excess_line_items.id", ondelete="SET NULL"), nullable=True)
    # Internal provenance — which inbound offer informed the price. NEVER exported.
    selected_offer_id = Column(Integer, ForeignKey("excess_offers.id", ondelete="SET NULL"), nullable=True)
    selected_offer_line_id = Column(Integer, ForeignKey("excess_offer_lines.id", ondelete="SET NULL"), nullable=True)
    customer_unit_price = Column(Numeric(12, 4), nullable=True)
    quantity = Column(Integer, nullable=False)

    customer_bid = relationship("CustomerBid", back_populates="lines")
    excess_line_item = relationship("ExcessLineItem", foreign_keys=[excess_line_item_id])

    # --- Validators ---
    @validates("quantity")
    def _validate_quantity(self, _key, value):
        if value is not None and value <= 0:
            raise ValueError("Quantity must be positive")
        return value

    __table_args__ = (
        Index("ix_customer_bid_lines_bid", "customer_bid_id"),
        Index("ix_customer_bid_lines_line_item", "excess_line_item_id"),
    )
