"""Quote models.

Buy Plan V1 model removed — use BuyPlan from models.buy_plan.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Column,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import relationship, validates

from ..database import UTCDateTime
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
    sent_at = Column(UTCDateTime)
    # Microsoft Graph identifiers of the outbound quote email (captured at send time so
    # customer replies can be threaded back to this quote). Nullable: drafts and legacy
    # rows have no send, and Graph propagation can occasionally fail the Sent-Items lookup.
    graph_message_id = Column(String(255), nullable=True)
    graph_conversation_id = Column(String(255), nullable=True)
    followup_alert_sent_at = Column(UTCDateTime(timezone=True), nullable=True)
    result = Column(String(20))
    result_reason = Column(String(255))
    result_notes = Column(Text)
    result_at = Column(UTCDateTime)
    won_revenue = Column(Numeric(12, 2))

    created_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    # Revenue attribution: "proactive" = originated from proactive selling; NULL = manual/unknown.
    source = Column(String(50))
    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        UTCDateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    requisition = relationship("Requisition", back_populates="quotes")
    customer_site = relationship("CustomerSite", foreign_keys=[customer_site_id])
    created_by = relationship("User", foreign_keys=[created_by_id])
    # cascade + passive_deletes: quote_lines.quote_id is NOT NULL with DB
    # ondelete=CASCADE, so let the DB cascade the delete instead of the ORM
    # NULLing children first (which would violate NOT NULL → IntegrityError).
    quote_lines = relationship("QuoteLine", back_populates="quote", cascade="all, delete-orphan", passive_deletes=True)

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
    description = Column(String(500), nullable=True)
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


class QuoteRequisition(Base):
    """Join row linking a Quote to every requisition that contributes lines to it.

    A combined quote (OQ-02) spans lines from 2+ requisitions selected together in the
    list "Build Quote" flow. ``Quote.requisition_id`` still records the PRIMARY/anchor
    requisition (first selected, unchanged), while one ``QuoteRequisition`` row per
    contributing requisition (the primary included) makes the full membership queryable.
    Every quote — even a legacy single-req one — has at least its own self-row (added by
    migration 175's backfill), so all requisition-scoped read paths use ONE join helper
    (``services/quote_requisitions.py``) instead of the old ``Quote.requisition_id == req``
    filter, which would go blind to combined quotes on the non-primary requisitions.

    Written by: services/quote_requisitions.link_quote_to_requisitions.
    Read by: services/quote_requisitions helpers (requisition_ids_for_quote,
        quotes_for_requisition, requisitions_for_quote).
    """

    __tablename__ = "quote_requisitions"
    id = Column(Integer, primary_key=True)
    quote_id = Column(Integer, ForeignKey("quotes.id", ondelete="CASCADE"), nullable=False)
    requisition_id = Column(Integer, ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))

    quote = relationship("Quote", foreign_keys=[quote_id])
    requisition = relationship("Requisition", foreign_keys=[requisition_id])

    __table_args__ = (
        UniqueConstraint("quote_id", "requisition_id", name="uq_quote_requisition"),
        Index("ix_quote_requisitions_quote", "quote_id"),
        Index("ix_quote_requisitions_req", "requisition_id"),
    )


@event.listens_for(Quote, "after_insert")
def _quote_self_link(_mapper, connection, target) -> None:
    """Guarantee every new Quote gets its primary self-row in ``quote_requisitions``.

    This is the write-time twin of migration 175's backfill: it holds the invariant
    "every quote has ≥1 join row" for quotes created by ANY path (builder, revise,
    proactive, offers, CRM, or a raw ``Quote()`` in a test) — so a requisition-scoped read
    that goes through ``services/quote_requisitions.quotes_for_requisition`` never loses a
    quote just because its creation path predates the combined-quote wiring. Combined
    quotes add their OTHER contributing requisitions on top via
    ``link_quote_to_requisitions`` (idempotent — it skips this self-row).

    Uses the flush ``connection`` (not a Session) per the after_insert contract, and
    coalesces ``created_at`` so a NULL-timestamp legacy insert still stamps the join row.
    """
    if target.requisition_id is None:
        return
    connection.execute(
        QuoteRequisition.__table__.insert().values(  # type: ignore[attr-defined, unused-ignore]  # __table__ is a Table at runtime
            quote_id=target.id,
            requisition_id=target.requisition_id,
            created_at=target.created_at or datetime.now(timezone.utc),
        )
    )


# V1 BuyPlan model removed. All buy plan functionality now in models/buy_plan.py (BuyPlan).
# The old `buy_plans` table still exists in the DB but is no longer mapped by SQLAlchemy.
# Migration 076 already moved all V1 data to buy_plans + buy_plan_lines.
