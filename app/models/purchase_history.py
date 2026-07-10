"""Customer purchase history — tracks which customers bought which parts.

Populated by:
- Won offers (avail_offer)
- Won quotes (avail_quote_won)
- Salesforce imports (salesforce_import)
- Future: Acctivate PO imports (acctivate_po)

Used by the proactive matching engine to find customer matches
for newly available inventory.
"""

from datetime import UTC, datetime

from sqlalchemy import (
    Column,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from ..database import UTCDateTime
from .base import Base


class CustomerPartHistory(Base):
    """One record per (company, material_card, source) — upserted on each
    transaction."""

    __tablename__ = "customer_part_history"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    material_card_id = Column(Integer, ForeignKey("material_cards.id", ondelete="CASCADE"), nullable=False)
    mpn = Column(String(100), nullable=False)  # Denormalized for display
    source = Column(String(50), nullable=False)  # salesforce_import, avail_offer, avail_quote_won, acctivate_po

    last_purchased_at = Column(UTCDateTime)
    purchase_count = Column(Integer, default=1)
    last_unit_price = Column(Numeric(12, 4))
    avg_unit_price = Column(Numeric(12, 4))
    last_quantity = Column(Integer)
    total_quantity = Column(Integer, default=0)
    source_ref = Column(String(255))  # SF opportunity ID, AVAIL offer ID, PO number

    created_at = Column(UTCDateTime, default=lambda: datetime.now(UTC))
    updated_at = Column(
        UTCDateTime,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    company = relationship("Company", foreign_keys=[company_id])

    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "material_card_id",
            "source",
            name="uq_cph_company_card_source",
        ),
        Index("ix_cph_material_card_id", "material_card_id"),
        Index("ix_cph_company_id", "company_id"),
        Index("ix_cph_last_purchased_at", "last_purchased_at"),
    )
