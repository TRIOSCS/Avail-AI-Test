"""
buy_plan.py — Buy Plan V4 Data Models (unified from V1 + V3)

Purpose: Structured buy plan tables with per-line status tracking, split lines,
         buyer assignment, PO confirmation, and ops verification.
         Replaces both the old V1 JSON line_items model and V3 intermediate model.

Description:
  - BuyPlan: header record linking quote → SO → customer PO, with dual
    approval tracks (manager spend approval + ops SO verification)
  - BuyPlanLine: one row per vendor/requirement purchase, supports splits
    (multiple lines sharing the same requirement_id)
  - VerificationGroupMember: simple user-to-ops-group mapping

Business Rules:
  - Lines can be SPLIT across vendors for the same requirement
  - AI selects vendors via weighted scoring (price/reliability/lead/geo/terms)
  - Manager approval required if total >= $5K or AI flags present
  - Ops verification runs in parallel with buy execution
  - First ops member to act wins (no double-verify)

Called by: services/buyplan_workflow.py, routers/crm/buy_plans.py
Depends on: models.base, models.quotes, models.sourcing, models.offers, models.auth
"""

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from .base import Base

# ── Enums ────────────────────────────────────────────────────────────


class BuyPlanStatus(str, enum.Enum):
    """Buy plan header statuses."""

    draft = "draft"
    pending = "pending"  # awaiting manager approval
    active = "active"  # approved, buy instructions sent
    halted = "halted"  # ops halted the deal
    completed = "completed"
    cancelled = "cancelled"


class SOVerificationStatus(str, enum.Enum):
    """Sales Order verification by ops."""

    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class BuyPlanLineStatus(str, enum.Enum):
    """Per-line statuses tracking buyer execution."""

    awaiting_po = "awaiting_po"  # buyer needs to cut PO
    pending_verify = "pending_verify"  # PO entered, awaiting ops verify
    verified = "verified"  # ops confirmed PO
    issue = "issue"  # buyer flagged a problem
    cancelled = "cancelled"


class LineIssueType(str, enum.Enum):
    """Types of issues a buyer can flag on a line."""

    sold_out = "sold_out"
    price_changed = "price_changed"
    lead_time_changed = "lead_time_changed"
    other = "other"


class AIFlagSeverity(str, enum.Enum):
    """Severity levels for AI-generated flags."""

    info = "info"
    warning = "warning"
    critical = "critical"


# ── Buy Plan (header) ──────────────────────────────────────────────


class BuyPlan(Base):
    """Buy plan with structured lines, dual approval tracks, and AI analysis.

    Unified V4 model replacing both V1 (JSON line_items) and V3 (intermediate).
    Table name kept as buy_plans_v3 for backward compatibility with existing data.
    """

    __tablename__ = "buy_plans_v3"

    id = Column(Integer, primary_key=True)

    # ── Quote / Deal linkage
    quote_id = Column(Integer, ForeignKey("quotes.id", ondelete="CASCADE"), nullable=False)
    requisition_id = Column(Integer, ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False)

    # ── Acctivate references
    sales_order_number = Column(String(100))
    customer_po_number = Column(String(100))

    # ── Status tracks
    status = Column(String(30), default=BuyPlanStatus.draft.value, nullable=False)
    so_status = Column(String(30), default=SOVerificationStatus.pending.value, nullable=False)

    # ── Financials (computed from lines)
    total_cost = Column(Numeric(12, 2))
    total_revenue = Column(Numeric(12, 2))
    total_margin_pct = Column(Numeric(5, 2))

    # ── AI analysis
    ai_summary = Column(Text)
    ai_flags = Column(JSON, default=list)  # list of {type, severity, line_id, message}

    # ── Approval
    auto_approved = Column(Boolean, default=False)
    approved_by_id = Column(Integer, ForeignKey("users.id"))
    approved_at = Column(DateTime)
    approval_notes = Column(Text)

    # ── SO verification
    so_verified_by_id = Column(Integer, ForeignKey("users.id"))
    so_verified_at = Column(DateTime)
    so_rejection_note = Column(Text)

    # ── Submission
    submitted_by_id = Column(Integer, ForeignKey("users.id"))
    submitted_at = Column(DateTime)
    salesperson_notes = Column(Text)

    # ── Completion
    completed_at = Column(DateTime)
    case_report = Column(Text)

    # ── Cancellation / halt
    cancelled_at = Column(DateTime)
    cancelled_by_id = Column(Integer, ForeignKey("users.id"))
    cancellation_reason = Column(Text)
    halted_by_id = Column(Integer, ForeignKey("users.id"))
    halted_at = Column(DateTime)

    # ── Stock sale flag
    is_stock_sale = Column(Boolean, default=False)

    # ── Token-based approval
    approval_token = Column(String(100), unique=True)
    token_expires_at = Column(DateTime)

    # ── Timestamps
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # ── Relationships
    quote = relationship("Quote", foreign_keys=[quote_id])
    requisition = relationship("Requisition", foreign_keys=[requisition_id])
    submitted_by = relationship("User", foreign_keys=[submitted_by_id])
    approved_by = relationship("User", foreign_keys=[approved_by_id])
    so_verified_by = relationship("User", foreign_keys=[so_verified_by_id])
    cancelled_by = relationship("User", foreign_keys=[cancelled_by_id])
    halted_by = relationship("User", foreign_keys=[halted_by_id])
    lines = relationship(
        "BuyPlanLine",
        back_populates="buy_plan",
        cascade="all, delete-orphan",
        order_by="BuyPlanLine.id",
    )

    __table_args__ = (
        Index("ix_bpv3_status", "status"),
        Index("ix_bpv3_so_status", "so_status"),
        Index("ix_bpv3_quote", "quote_id"),
        Index("ix_bpv3_requisition", "requisition_id"),
        Index("ix_bpv3_submitted_by", "submitted_by_id"),
        Index("ix_bpv3_status_created", "status", "created_at"),
        Index("ix_bpv3_token", "approval_token"),
    )


# ── Buy Plan Line ───────────────────────────────────────────────────


class BuyPlanLine(Base):
    """One purchase line — links requirement to offer with qty, buyer, and PO tracking.

    Multiple lines can share the same requirement_id (split lines).
    """

    __tablename__ = "buy_plan_lines"

    id = Column(Integer, primary_key=True)
    buy_plan_id = Column(Integer, ForeignKey("buy_plans_v3.id", ondelete="CASCADE"), nullable=False)

    # ── What to buy
    requirement_id = Column(Integer, ForeignKey("requirements.id", ondelete="SET NULL"))
    offer_id = Column(Integer, ForeignKey("offers.id", ondelete="SET NULL"))
    quantity = Column(Integer, nullable=False)

    # ── Pricing
    unit_cost = Column(Numeric(12, 4))
    unit_sell = Column(Numeric(12, 4))
    margin_pct = Column(Numeric(5, 2))

    # ── AI scoring
    ai_score = Column(Float)  # 0-100 weighted score for this offer

    # ── Buyer assignment
    buyer_id = Column(Integer, ForeignKey("users.id"))
    assignment_reason = Column(String(100))  # vendor_ownership, commodity, geo, workload

    # ── Status
    status = Column(String(30), default=BuyPlanLineStatus.awaiting_po.value, nullable=False)

    # ── PO confirmation
    po_number = Column(String(100))
    estimated_ship_date = Column(DateTime)
    po_confirmed_at = Column(DateTime)

    # ── PO verification
    po_verified_by_id = Column(Integer, ForeignKey("users.id"))
    po_verified_at = Column(DateTime)
    po_rejection_note = Column(Text)

    # ── Issue tracking
    issue_type = Column(String(30))
    issue_note = Column(Text)

    # ── Notes
    sales_note = Column(Text)
    manager_note = Column(Text)

    # ── Timestamps
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # ── Relationships
    buy_plan = relationship("BuyPlan", back_populates="lines")
    requirement = relationship("Requirement", foreign_keys=[requirement_id])
    offer = relationship("Offer", foreign_keys=[offer_id])
    buyer = relationship("User", foreign_keys=[buyer_id])
    po_verified_by = relationship("User", foreign_keys=[po_verified_by_id])

    __table_args__ = (
        Index("ix_bpl_buy_plan", "buy_plan_id"),
        Index("ix_bpl_requirement", "requirement_id"),
        Index("ix_bpl_status", "status"),
        Index("ix_bpl_buyer", "buyer_id"),
        Index("ix_bpl_offer", "offer_id"),
        Index("ix_bpl_plan_requirement", "buy_plan_id", "requirement_id"),
    )


# ── Verification Group ──────────────────────────────────────────────


class VerificationGroupMember(Base):
    """Maps users to the ops verification group.

    The verification group handles SO and PO verification.
    First member to act wins — no double-verification needed.
    """

    __tablename__ = "verification_group_members"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    is_active = Column(Boolean, default=True, nullable=False)
    added_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", foreign_keys=[user_id])

    __table_args__ = (Index("ix_vgm_active", "is_active"),)
