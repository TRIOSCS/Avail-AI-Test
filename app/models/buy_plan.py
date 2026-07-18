"""buy_plan.py — Buy Plan V4 Data Models (unified from V1 + V3)

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

Called by: services/buyplan_workflow.py, routers/htmx_views.py
Depends on: models.base, models.quotes, models.sourcing, models.offers, models.auth
"""

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import relationship, validates

from ..constants import (
    AIFlagSeverity,
    BuyPlanLineStatus,
    BuyPlanStatus,
    LineIssueType,
    PaymentMethod,
    SalesOrderType,
    SOVerificationStatus,
)
from ..database import UTCDateTime
from .base import Base

# Re-export enums so existing `from app.models.buy_plan import BuyPlanStatus` still works
__all__ = [
    "AIFlagSeverity",
    "BuyPlanLineStatus",
    "BuyPlanStatus",
    "LineIssueType",
    "SalesOrderType",
    "SOVerificationStatus",
    "BuyPlan",
    "BuyPlanAttachment",
    "BuyPlanLine",
    "VerificationGroupMember",
]

# ── Buy Plan (header) ──────────────────────────────────────────────


class BuyPlan(Base):
    """Buy plan with structured lines, dual approval tracks, and AI analysis.

    Unified V4 model replacing both V1 (JSON line_items) and V3 (intermediate). Table
    name kept as buy_plans_v3 for backward compatibility with existing data.
    """

    __tablename__ = "buy_plans_v3"

    id = Column(Integer, primary_key=True)

    # ── Quote / Deal linkage
    quote_id = Column(Integer, ForeignKey("quotes.id", ondelete="CASCADE"), nullable=True)
    requisition_id = Column(Integer, ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False)

    # ── Acctivate references
    sales_order_number = Column(String(100))
    customer_po_number = Column(String(100))

    # ── Status tracks
    status = Column(String(30), default=BuyPlanStatus.DRAFT.value, nullable=False)
    so_status = Column(String(30), default=SOVerificationStatus.PENDING.value, nullable=False)

    # ── Financials (computed from lines)
    total_cost = Column(Numeric(12, 2))
    total_revenue = Column(Numeric(12, 2))
    total_margin_pct = Column(Numeric(5, 2))

    # ── AI analysis
    ai_summary = Column(Text)
    ai_flags = Column(JSON, default=list)  # list of {type, severity, line_id, message}

    # ── Approval
    auto_approved = Column(Boolean, default=False)
    approved_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    approved_at = Column(UTCDateTime)
    approval_notes = Column(Text)

    # ── SO verification
    so_verified_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    so_verified_at = Column(UTCDateTime)
    so_rejection_note = Column(Text)

    # ── Submission
    submitted_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    submitted_at = Column(UTCDateTime)
    salesperson_notes = Column(Text)

    # ── Completion
    completed_at = Column(UTCDateTime)
    case_report = Column(Text)

    # Set once CPH has been recorded from this plan's lines (idempotency guard for
    # the buy-plan→customer_part_history feed and its backfill).
    purchase_history_recorded_at = Column(UTCDateTime)

    # ── Cancellation / halt
    cancelled_at = Column(UTCDateTime)
    cancelled_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    cancellation_reason = Column(Text)
    halted_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    halted_at = Column(UTCDateTime)

    # ── Stock sale flag
    is_stock_sale = Column(Boolean, default=False)

    # ── Order type (Approvals Workspace — migration 192). SalesOrderType vocabulary;
    # server_default 'new' so every pre-existing row reads as a New order (stock sales
    # backfilled to 'stock_sale' from is_stock_sale in the migration).
    order_type = Column(String(20), nullable=False, default=SalesOrderType.NEW.value, server_default="new")

    # ── Timestamps
    created_at = Column(UTCDateTime, default=lambda: datetime.now(UTC))
    updated_at = Column(
        UTCDateTime,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
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

    @validates("status")
    def _validate_status(self, _key, value):
        valid = {e.value for e in BuyPlanStatus}
        if value and value not in valid:
            raise ValueError(f"Invalid buy plan status: {value!r}. Valid: {valid}")
        return value

    @validates("order_type")
    def _validate_order_type(self, _key, value):
        valid = {e.value for e in SalesOrderType}
        if value and value not in valid:
            raise ValueError(f"Invalid order type: {value!r}. Valid: {valid}")
        return value

    __table_args__ = (
        Index("ix_bpv3_order_type", "order_type"),
        Index("ix_bpv3_status", "status"),
        Index("ix_bpv3_so_status", "so_status"),
        Index("ix_bpv3_quote", "quote_id"),
        Index("ix_bpv3_requisition", "requisition_id"),
        Index("ix_bpv3_submitted_by", "submitted_by_id"),
        Index("ix_bpv3_status_created", "status", "created_at"),
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
    buyer_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    assignment_reason = Column(String(100))  # vendor_ownership, commodity, geo, workload

    # ── Status
    status = Column(String(30), default=BuyPlanLineStatus.AWAITING_PO.value, nullable=False)

    # ── PO confirmation
    po_number = Column(String(100))
    estimated_ship_date = Column(UTCDateTime)
    po_confirmed_at = Column(UTCDateTime)

    # ── PO verification
    po_verified_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    po_verified_at = Column(UTCDateTime)
    po_rejection_note = Column(Text)

    # ── Issue tracking
    issue_type = Column(String(30))
    issue_note = Column(Text)

    # ── Notes
    sales_note = Column(Text)
    manager_note = Column(Text)

    # ── Payment method (Approvals Workspace — migration 192). PaymentMethod
    # vocabulary (PO lines accept all 5 incl. COD — see PO_LINE_PAYMENT_METHODS).
    # Nullable: recorded at confirm-PO time; legacy lines have none.
    payment_method = Column(String(20), nullable=True)

    # ── Receiving (Approvals Workspace — migration 192). Stamped by
    # mark_line_received (buyplan_workflow/buyplan_po.py, Phase 3) — a manual
    # "mark received" event; never changes plan status.
    received_at = Column(UTCDateTime, nullable=True)
    received_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    # ── Timestamps
    created_at = Column(UTCDateTime, default=lambda: datetime.now(UTC))
    updated_at = Column(
        UTCDateTime,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    # ── Nudge tracking
    last_nudge_at = Column(UTCDateTime, nullable=True)

    # ── Relationships
    buy_plan = relationship("BuyPlan", back_populates="lines")
    requirement = relationship("Requirement", foreign_keys=[requirement_id])
    offer = relationship("Offer", foreign_keys=[offer_id])
    buyer = relationship("User", foreign_keys=[buyer_id])
    po_verified_by = relationship("User", foreign_keys=[po_verified_by_id])
    received_by = relationship("User", foreign_keys=[received_by_id])

    @property
    def is_received(self) -> bool:
        """True once the goods on this line have been marked received (received_at
        stamped by mark_line_received).

        Drives the kanban RECEIVED lane.
        """
        return self.received_at is not None

    @property
    def has_cut_po(self) -> bool:
        """True once this line has left AWAITING_PO (a PO is cut / verified / flagged /
        cancelled).

        Vendor/qty/removal edits on such a line would corrupt live purchasing state, so
        the line-editing service (``app/services/buyplan_workflow/buyplan_lines.py``)
        refuses them once this is true — the header sell price can still be corrected
        (it never touches the PO). Single source of truth for both the service gate and
        the whole-plan-editor template's per-row "locked" state.
        """
        return bool(self.po_confirmed_at is not None or self.status != BuyPlanLineStatus.AWAITING_PO.value)

    @validates("status")
    def _validate_status(self, _key, value):
        valid = {e.value for e in BuyPlanLineStatus}
        if value and value not in valid:
            raise ValueError(f"Invalid buy plan line status: {value!r}. Valid: {valid}")
        return value

    @validates("issue_type")
    def _validate_issue_type(self, _key, value):
        valid = {e.value for e in LineIssueType}
        if value and value not in valid:
            raise ValueError(f"Invalid line issue type: {value!r}. Valid: {valid}")
        return value

    @validates("payment_method")
    def _validate_payment_method(self, _key, value):
        valid = {e.value for e in PaymentMethod}
        if value and value not in valid:
            raise ValueError(f"Invalid payment method: {value!r}. Valid: {valid}")
        return value

    __table_args__ = (
        Index("ix_bpl_buy_plan", "buy_plan_id"),
        Index("ix_bpl_requirement", "requirement_id"),
        Index("ix_bpl_status", "status"),
        Index("ix_bpl_buyer", "buyer_id"),
        Index("ix_bpl_offer", "offer_id"),
        Index("ix_bpl_plan_requirement", "buy_plan_id", "requirement_id"),
        Index("ix_bpl_nudge_status", "status", "last_nudge_at"),
    )


# ── Buy Plan Attachment ─────────────────────────────────────────────


class BuyPlanAttachment(Base):
    """File attachment on a buy plan, a buy-plan line, or a prepayment (stored in
    OneDrive or the company SharePoint library — mirrors CompanyAttachment).

    One table, three nullable subject FKs — EXACTLY ONE must be set (app-validated;
    call validate_subject() before insert — the attachment routes and
    attachment_service.store_and_attach writers own enforcement, there is no DB CHECK).
    All three FKs cascade: an attachment row has no meaning once its subject is gone.

    library_drive_id NULL  → OneDrive fallback row (user token, item in /me/drive)
    library_drive_id set   → company SharePoint library row (app token)

    Called by: app/services/attachment_service.py, approvals-workspace attachment
               routes (Phase 2.4)
    Depends on: BuyPlan, BuyPlanLine, Prepayment (models.quality_plan), User
    """

    __tablename__ = "buy_plan_attachments"

    id = Column(Integer, primary_key=True)

    # ── Subject (exactly one set — see validate_subject)
    buy_plan_id = Column(Integer, ForeignKey("buy_plans_v3.id", ondelete="CASCADE"), nullable=True)
    buy_plan_line_id = Column(Integer, ForeignKey("buy_plan_lines.id", ondelete="CASCADE"), nullable=True)
    prepayment_id = Column(Integer, ForeignKey("prepayments.id", ondelete="CASCADE"), nullable=True)

    file_name = Column(String(500), nullable=False)
    library_item_id = Column(String(500))
    library_drive_id = Column(String(200))
    library_web_url = Column(Text)
    thumbnail_url = Column(Text)
    content_type = Column(String(100))
    size_bytes = Column(Integer)
    uploaded_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    created_at = Column(UTCDateTime, default=lambda: datetime.now(UTC))

    buy_plan = relationship("BuyPlan", foreign_keys=[buy_plan_id])
    buy_plan_line = relationship("BuyPlanLine", foreign_keys=[buy_plan_line_id])
    prepayment = relationship("Prepayment", foreign_keys=[prepayment_id])
    uploaded_by = relationship("User", foreign_keys=[uploaded_by_id])

    def validate_subject(self) -> None:
        """Raise ValueError unless EXACTLY ONE subject FK is set.

        App-level stand-in for a DB CHECK constraint — every write path MUST call this
        before flush (single choke point: the Phase 2.4 attachment routes).
        """
        set_count = sum(1 for v in (self.buy_plan_id, self.buy_plan_line_id, self.prepayment_id) if v is not None)
        if set_count != 1:
            raise ValueError(
                f"BuyPlanAttachment requires exactly one subject FK set, got {set_count} "
                "(buy_plan_id / buy_plan_line_id / prepayment_id)"
            )

    __table_args__ = (
        Index("ix_bp_attachments_plan", "buy_plan_id"),
        Index("ix_bp_attachments_line", "buy_plan_line_id"),
        Index("ix_bp_attachments_prepayment", "prepayment_id"),
    )


# ── Verification Group ──────────────────────────────────────────────


class VerificationGroupMember(Base):
    """Maps users to the ops verification group.

    The verification group handles SO and PO verification. First member to act wins — no
    double-verification needed.
    """

    __tablename__ = "verification_group_members"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    is_active = Column(Boolean, default=True, nullable=False)
    added_at = Column(UTCDateTime, default=lambda: datetime.now(UTC))

    user = relationship("User", foreign_keys=[user_id])

    __table_args__ = (Index("ix_vgm_active", "is_active"),)
