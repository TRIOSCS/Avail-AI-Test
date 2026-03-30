"""Quote models.

Buy Plan V1 model removed — use BuyPlan from models.buy_plan.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import relationship, validates

from .base import Base


class Quote(Base):
    """Quote built by salesperson from selected offers."""

    __tablename__ = "quotes"
    id = Column(Integer, primary_key=True)
    requisition_id = Column(Integer, ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False)
    customer_site_id = Column(Integer, ForeignKey("customer_sites.id", ondelete="SET NULL"), nullable=True)

    quote_number = Column(String(50), nullable=False, unique=True)
    revision = Column(Integer, default=1)

    line_items = Column(JSON, nullable=False, default=list)

    subtotal = Column(Numeric(12, 2))
    total_cost = Column(Numeric(12, 2))
    total_margin_pct = Column(Numeric(5, 2))

    payment_terms = Column(String(100))
    shipping_terms = Column(String(100))
    validity_days = Column(Integer, default=7)
    notes = Column(Text)

    status = Column(String(20), default="draft")
    sent_at = Column(DateTime)
    followup_alert_sent_at = Column(DateTime(timezone=True), nullable=True)
    result = Column(String(20))
    result_reason = Column(String(255))
    result_notes = Column(Text)
    result_at = Column(DateTime)
    won_revenue = Column(Numeric(12, 2))

    created_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    requisition = relationship("Requisition", back_populates="quotes")
    customer_site = relationship("CustomerSite", foreign_keys=[customer_site_id])
    created_by = relationship("User", foreign_keys=[created_by_id])
    quote_lines = relationship("QuoteLine", back_populates="quote")

    @validates("status")
    def _validate_status(self, _key, value):
        from ..constants import QuoteStatus

        valid = {e.value for e in QuoteStatus}
        if value and value not in valid:
            raise ValueError(f"Invalid quote status: {value!r}. Valid: {valid}")
        return value

    __table_args__ = (
        Index("ix_quotes_req", "requisition_id"),
        Index("ix_quotes_site", "customer_site_id"),
        Index("ix_quotes_status", "status"),
        Index("ix_quotes_created_by", "created_by_id"),
    )


class QuoteLine(Base):
    """Structured line item in a quote — replaces JSON line_items for querying."""

    __tablename__ = "quote_lines"
    id = Column(Integer, primary_key=True)
    quote_id = Column(Integer, ForeignKey("quotes.id", ondelete="CASCADE"), nullable=False)
    material_card_id = Column(Integer, ForeignKey("material_cards.id", ondelete="SET NULL"))
    offer_id = Column(Integer, ForeignKey("offers.id", ondelete="SET NULL"))
    mpn = Column(String(255), nullable=False)
    manufacturer = Column(String(255))
    qty = Column(Integer)
    cost_price = Column(Numeric(12, 4))
    sell_price = Column(Numeric(12, 4))
    margin_pct = Column(Numeric(5, 2))
    currency = Column(String(10), default="USD")

    quote = relationship("Quote", back_populates="quote_lines")

    __table_args__ = (
        Index("ix_quote_lines_quote", "quote_id"),
        Index("ix_quote_lines_card", "material_card_id"),
        Index("ix_quote_lines_mpn", "mpn"),
        Index("ix_quote_lines_offer", "offer_id"),
    )


# V1 BuyPlan model removed. All buy plan functionality now in models/buy_plan.py (BuyPlan).
# The old `buy_plans` table still exists in the DB but is no longer mapped by SQLAlchemy.
# Migration 076 already moved all V1 data to buy_plans + buy_plan_lines.
