"""risk_flag.py — Structured risk flag model for deal intelligence.

Purpose: Tracks risk flags attached to buy plan lines and offers.
         Replaces ad-hoc JSON ai_flags with queryable, auditable records.
         Enables surfacing risk signals during offer review (not just in buy plans).

Called by: services/buyplan_service.py, routers/crm/offers.py, routers/crm/quotes.py
Depends on: models.base, models.buy_plan, models.offers
"""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import relationship

from ..constants import RiskFlagSeverity, RiskFlagType
from .base import Base

# Re-export enums so existing `from app.models.risk_flag import RiskFlagType` still works
__all__ = ["RiskFlagType", "RiskFlagSeverity", "RiskFlag"]


class RiskFlag(Base):
    """Structured risk flag attached to a buy plan line or source offer.

    Replaces the JSON ai_flags field on BuyPlan with queryable records. Risk flags can
    be raised by AI analysis, rule-based checks, or manual review.
    """

    __tablename__ = "risk_flags"

    id = Column(Integer, primary_key=True)

    # Link to buy plan line (optional — can exist without a buy plan)
    buy_plan_line_id = Column(Integer, ForeignKey("buy_plan_lines.id", ondelete="CASCADE"), nullable=True)
    # Link to source offer (optional — for surfacing in offer review)
    source_offer_id = Column(Integer, ForeignKey("offers.id", ondelete="CASCADE"), nullable=True)
    # Link to requisition (always set for filtering)
    requisition_id = Column(Integer, ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=True)

    type = Column(String(50), nullable=False)
    severity = Column(String(20), nullable=False, default=RiskFlagSeverity.INFO.value)
    message = Column(Text, nullable=False)

    # Who/what raised this flag
    source = Column(String(50), default="ai")  # ai, rule, manual

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Relationships
    buy_plan_line = relationship("BuyPlanLine", foreign_keys=[buy_plan_line_id])
    source_offer = relationship("Offer", foreign_keys=[source_offer_id])

    __table_args__ = (
        Index("ix_risk_flags_line", "buy_plan_line_id"),
        Index("ix_risk_flags_offer", "source_offer_id"),
        Index("ix_risk_flags_req", "requisition_id"),
        Index("ix_risk_flags_severity", "severity"),
        Index("ix_risk_flags_type", "type"),
    )
