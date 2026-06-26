"""quality_plan.py — QualityPlan and Prepayment ORM models.

Purpose: QualityPlan tracks inspection/QC documentation per buy-plan order.
         Prepayment captures upfront payment details (wire/CC/PayPal) that
         may require an approval gate before the PO is issued.

Called by: services/quality_plan.py, routers/quality_plan.py (Task 3+)
           Also referenced (no DB FK) by ApprovalRequest's polymorphic
           (subject_type, subject_id) pair — subject_type=ApprovalSubjectType.QUALITY_PLAN
           or .PREPAYMENT, subject_id holding the QualityPlan/Prepayment PK.
Depends on: models.base, app.constants (QualityPlanStatus, QPOrderType, PaymentMethod),
            models.buy_plan (BuyPlan), models.vendors (VendorCard)
"""

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import relationship

from ..constants import QPOrderType, QualityPlanStatus
from ..database import UTCDateTime
from .base import Base

__all__ = [
    "QualityPlan",
    "Prepayment",
]


# ── QualityPlan ────────────────────────────────────────────────────────────────


class QualityPlan(Base):
    """Quality plan document linked to a buy-plan order.

    One QualityPlan per buy-plan + vendor-card pair (new orders) or per revision cycle.
    status transitions: draft → in_review → approved/rejected. order_type distinguishes
    first-time plans ('new') from revisions ('revision').
    """

    __tablename__ = "quality_plans"

    id = Column(Integer, primary_key=True)

    buy_plan_id = Column(Integer, ForeignKey("buy_plans_v3.id", ondelete="CASCADE"), nullable=False)
    vendor_card_id = Column(Integer, ForeignKey("vendor_cards.id", ondelete="SET NULL"), nullable=True)

    status = Column(String(50), nullable=False, default=QualityPlanStatus.DRAFT)
    order_type = Column(String(20), nullable=False, default=QPOrderType.NEW)

    # Plan content
    inspection_level = Column(String(50), nullable=True)
    sampling_rate = Column(String(50), nullable=True)
    notes = Column(Text, nullable=True)

    # Audit
    created_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approved_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approved_at = Column(UTCDateTime, nullable=True)

    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(UTCDateTime, onupdate=lambda: datetime.now(timezone.utc))

    # ── Relationships
    buy_plan = relationship("BuyPlan", foreign_keys=[buy_plan_id])
    vendor_card = relationship("VendorCard", foreign_keys=[vendor_card_id])
    created_by = relationship("User", foreign_keys=[created_by_id])
    approved_by = relationship("User", foreign_keys=[approved_by_id])

    __table_args__ = (
        Index("ix_qp_buy_plan", "buy_plan_id"),
        Index("ix_qp_vendor_card", "vendor_card_id"),
        Index("ix_qp_status", "status"),
        Index("ix_qp_created_by", "created_by_id"),
    )


# ── Prepayment ─────────────────────────────────────────────────────────────────


class Prepayment(Base):
    """Upfront payment record for a buy-plan vendor purchase.

    Captures the total amount (inclusive of fees), payment method, and buyer remarks.
    May trigger an ApprovalRequest routed to users with can_approve_prepayments=True
    (filtered by prepayment_approval_limit if set). test_report_sent tracks whether the
    vendor has returned the test report that was promised as a condition of the
    prepayment.
    """

    __tablename__ = "prepayments"

    id = Column(Integer, primary_key=True)

    vendor_card_id = Column(Integer, ForeignKey("vendor_cards.id", ondelete="SET NULL"), nullable=True)
    buy_plan_id = Column(Integer, ForeignKey("buy_plans_v3.id", ondelete="CASCADE"), nullable=False)

    total_incl_fees = Column(Numeric(12, 2), nullable=False)
    currency = Column(String(10), nullable=False, default="USD")
    payment_method = Column(String(20), nullable=True)  # PaymentMethod

    test_report_sent = Column(Boolean, nullable=False, default=False)
    buyer_remarks = Column(Text, nullable=True)

    # Audit
    created_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(UTCDateTime, onupdate=lambda: datetime.now(timezone.utc))

    # ── Relationships
    vendor_card = relationship("VendorCard", foreign_keys=[vendor_card_id])
    buy_plan = relationship("BuyPlan", foreign_keys=[buy_plan_id])
    created_by = relationship("User", foreign_keys=[created_by_id])

    __table_args__ = (
        Index("ix_prepayment_vendor_card", "vendor_card_id"),
        Index("ix_prepayment_buy_plan", "buy_plan_id"),
        Index("ix_prepayment_created_by", "created_by_id"),
    )
