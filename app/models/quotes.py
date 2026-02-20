"""Quote and Buy Plan models."""

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
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


class Quote(Base):
    """Quote built by salesperson from selected offers."""

    __tablename__ = "quotes"
    id = Column(Integer, primary_key=True)
    requisition_id = Column(
        Integer, ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False
    )
    customer_site_id = Column(Integer, ForeignKey("customer_sites.id"), nullable=False)

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
    result = Column(String(20))
    result_reason = Column(String(255))
    result_notes = Column(Text)
    result_at = Column(DateTime)
    won_revenue = Column(Numeric(12, 2))

    created_by_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    requisition = relationship("Requisition", back_populates="quotes")
    customer_site = relationship("CustomerSite", foreign_keys=[customer_site_id])
    created_by = relationship("User", foreign_keys=[created_by_id])

    __table_args__ = (
        Index("ix_quotes_req", "requisition_id"),
        Index("ix_quotes_site", "customer_site_id"),
        Index("ix_quotes_status", "status"),
        Index("ix_quotes_created_by", "created_by_id"),
    )


class BuyPlan(Base):
    """Purchase plan submitted after a quote is won — requires manager approval."""

    __tablename__ = "buy_plans"
    id = Column(Integer, primary_key=True)
    requisition_id = Column(
        Integer, ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False
    )
    quote_id = Column(
        Integer, ForeignKey("quotes.id", ondelete="CASCADE"), nullable=False
    )

    status = Column(String(30), default="pending_approval")
    # pending_approval | approved | rejected | po_entered | po_confirmed | complete | cancelled

    line_items = Column(JSON, nullable=False, default=list)

    manager_notes = Column(Text)
    salesperson_notes = Column(Text)
    rejection_reason = Column(Text)
    sales_order_number = Column(String(100))

    submitted_by_id = Column(Integer, ForeignKey("users.id"))
    approved_by_id = Column(Integer, ForeignKey("users.id"))

    submitted_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    approved_at = Column(DateTime)
    rejected_at = Column(DateTime)
    completed_at = Column(DateTime)
    completed_by_id = Column(Integer, ForeignKey("users.id"))
    cancelled_at = Column(DateTime)
    cancelled_by_id = Column(Integer, ForeignKey("users.id"))
    cancellation_reason = Column(Text)

    approval_token = Column(String(100), unique=True)
    token_expires_at = Column(DateTime)
    is_stock_sale = Column(Boolean, default=False)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    requisition = relationship("Requisition", foreign_keys=[requisition_id])
    quote = relationship("Quote", foreign_keys=[quote_id])
    submitted_by = relationship("User", foreign_keys=[submitted_by_id])
    approved_by = relationship("User", foreign_keys=[approved_by_id])
    completed_by = relationship("User", foreign_keys=[completed_by_id])
    cancelled_by = relationship("User", foreign_keys=[cancelled_by_id])

    __table_args__ = (
        Index("ix_buyplans_req", "requisition_id"),
        Index("ix_buyplans_quote", "quote_id"),
        Index("ix_buyplans_status", "status"),
        Index("ix_buyplans_token", "approval_token"),
        Index("ix_buyplans_status_created", "status", "created_at"),
        Index("ix_buyplans_submitter_created", "submitted_by_id", "created_at"),
    )
