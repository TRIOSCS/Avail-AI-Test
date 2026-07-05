"""quality_plan.py — QualityPlan, Prepayment, and QP section-child ORM models.

Purpose: QualityPlan tracks inspection/QC documentation per buy-plan order. It now
         carries the native Sales and Purchasing "Quality Questions" sections (C2b),
         replacing the Excel QP template field-for-field, plus the per-section
         reviewed-at/by stamps set by the buyer's lightweight Mark-Reviewed toggle
         (decision C — a per-section fold, NOT a separate approver gate).
         Prepayment captures upfront payment details (wire/CC/PayPal) that
         may require an approval gate before the PO is issued.
         QpSerialEntry rows track serial-number preapproval per QP (one row per
         serial submission). QpFruLookup pins FRU part numbers to a QP; the view
         live-joins FruLink by fru_norm to render the crosswalk.

Called by: services/quality_plan_service.py, routers/quality_plans.py
           Also referenced (no DB FK) by ApprovalRequest's polymorphic
           (subject_type, subject_id) pair — subject_type=ApprovalSubjectType.QUALITY_PLAN
           or .PREPAYMENT, subject_id holding the QualityPlan/Prepayment PK.
Depends on: models.base, app.constants (QualityPlanStatus, QPOrderType, PaymentMethod),
            models.buy_plan (BuyPlan, BuyPlanLine), models.vendors (VendorCard)
"""

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, Date, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from ..constants import PrepaymentStatus, QPOrderType, QualityPlanStatus
from ..database import UTCDateTime
from .base import Base

__all__ = [
    "QualityPlan",
    "Prepayment",
    "QpSerialEntry",
    "QpFruLookup",
]


# ── QualityPlan ────────────────────────────────────────────────────────────────


class QualityPlan(Base):
    """Quality plan document linked to a buy-plan order.

    One QualityPlan per buy-plan + vendor-card pair. status stays 'draft' — the
    submit/review lifecycle was never built; review folded into the per-section Mark-
    Reviewed stamps (sales/purchasing_section_reviewed_at). order_type is always 'new'
    (the 'revision' supersede-flow was never built).
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

    # ── § SALES "Quality Questions" (C2b) — gated by the QP_SALES approval.
    # All nullable; the completeness gate (validate_sales_section) enforces the
    # required subset at submit time. SO# is canonical on BuyPlan.sales_order_number
    # (retired from QP after SP-2 migration 164 Op C).
    sales_condition = Column(String(255), nullable=True)
    sales_quantity = Column(Integer, nullable=True)
    sales_fw_hw_rev = Column(Text, nullable=True)  # FW / HW / REV / Date & Week Codes
    sales_product_commodity = Column(String(255), nullable=True)
    sales_testing_required = Column(Boolean, nullable=True)
    sales_testing_option = Column(String(255), nullable=True)
    sales_testing_specifics = Column(Text, nullable=True)
    sales_test_location = Column(String(255), nullable=True)
    sales_serial_preapproval_required = Column(Boolean, nullable=True)
    sales_authorized_ship_early = Column(Boolean, nullable=True)
    sales_authorized_ship_partial = Column(Boolean, nullable=True)
    sales_routing_prescreening_whs = Column(String(255), nullable=True)
    sales_vendor_rating = Column(String(255), nullable=True)
    sales_third_party_pkg_ok = Column(Boolean, nullable=True)
    sales_pkg_requirements = Column(Text, nullable=True)
    sales_bom_matrix_links = Column(Text, nullable=True)  # BOM / Matrix Links / Acceptable SUBS / TSO
    sales_notes = Column(Text, nullable=True)

    # ── § PURCHASING "Quality Questions" (C2b) — gated by the PURCHASE_ORDER approval.
    purchasing_po_number = Column(String(255), nullable=True)
    purchasing_condition = Column(String(255), nullable=True)
    purchasing_fw_hw_rev = Column(Text, nullable=True)
    purchasing_product_commodity = Column(String(255), nullable=True)
    purchasing_testing_required = Column(Boolean, nullable=True)
    purchasing_testing_option = Column(String(255), nullable=True)
    purchasing_routing_prescreening_whs = Column(String(255), nullable=True)
    purchasing_packaging = Column(Text, nullable=True)
    purchasing_tpo_ship_complete = Column(Boolean, nullable=True)  # Will TPO ship complete?
    purchasing_tpo_notes = Column(Text, nullable=True)  # TPO Notes / Shipping Schedule

    # ── Section reviewed-at/by stamps (set by the buyer's Mark-Reviewed toggle — a
    # lightweight per-section fold via toggle_section_reviewed, NOT an approver gate;
    # decision C). reviewed_by_id records who last marked the section reviewed.
    sales_section_reviewed_at = Column(UTCDateTime, nullable=True)
    sales_section_reviewed_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    purchasing_section_reviewed_at = Column(UTCDateTime, nullable=True)
    purchasing_section_reviewed_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    # Audit
    created_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(UTCDateTime, onupdate=lambda: datetime.now(timezone.utc))

    # ── Relationships
    buy_plan = relationship("BuyPlan", foreign_keys=[buy_plan_id])
    vendor_card = relationship("VendorCard", foreign_keys=[vendor_card_id])
    created_by = relationship("User", foreign_keys=[created_by_id])
    sales_section_reviewed_by = relationship("User", foreign_keys=[sales_section_reviewed_by_id])
    purchasing_section_reviewed_by = relationship("User", foreign_keys=[purchasing_section_reviewed_by_id])
    serial_entries = relationship(
        "QpSerialEntry",
        back_populates="quality_plan",
        cascade="all, delete-orphan",
        order_by="QpSerialEntry.id",
    )
    fru_lookups = relationship(
        "QpFruLookup",
        back_populates="quality_plan",
        cascade="all, delete-orphan",
        order_by="QpFruLookup.id",
    )

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
    # The specific PO line this prepayment is against (migration 178). Nullable +
    # ondelete=SET NULL: a prepayment record outlives the line it prepaid (audit trail).
    buy_plan_line_id = Column(Integer, ForeignKey("buy_plan_lines.id", ondelete="SET NULL"), nullable=True)

    total_incl_fees = Column(Numeric(12, 2), nullable=False)
    currency = Column(String(10), nullable=False, default="USD")
    payment_method = Column(String(20), nullable=True)  # PaymentMethod

    # Payee snapshot (migration 178): the vendor name captured at request time from the
    # line's offer (Offer.vendor_name, NOT-NULL) or the vendor card's display_name, so the
    # approver / AP always see who is being paid even if the line/offer later changes.
    vendor_name = Column(String(255), nullable=True)

    test_report_sent = Column(Boolean, nullable=False, default=False)
    buyer_remarks = Column(Text, nullable=True)

    # ── Payment lifecycle (migration 179): requested → approved → paid, or void.
    # status is the source of truth; the paid/approved/void field groups are stamped at
    # each transition. pay_token is a single-use secrets.token_urlsafe(32) minted on
    # approve (the "OK TO WIRE" email link) and cleared on paid/void so a spent/dead link
    # can't act again.
    status = Column(
        String(20),
        nullable=False,
        default=PrepaymentStatus.REQUESTED.value,
        server_default=PrepaymentStatus.REQUESTED.value,
    )
    approved_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approved_at = Column(UTCDateTime, nullable=True)
    pay_token = Column(String(64), nullable=True)
    paid_at = Column(UTCDateTime, nullable=True)
    paid_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    paid_by_label = Column(String(120), nullable=True)
    paid_via = Column(String(20), nullable=True)  # accounting_email | in_app
    wire_reference = Column(String(120), nullable=True)
    paid_amount = Column(Numeric(12, 2), nullable=True)
    voided_at = Column(UTCDateTime, nullable=True)
    voided_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    void_reason = Column(String(255), nullable=True)

    # Audit
    created_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(UTCDateTime, onupdate=lambda: datetime.now(timezone.utc))

    # ── Relationships
    vendor_card = relationship("VendorCard", foreign_keys=[vendor_card_id])
    buy_plan = relationship("BuyPlan", foreign_keys=[buy_plan_id])
    buy_plan_line = relationship("BuyPlanLine", foreign_keys=[buy_plan_line_id])
    created_by = relationship("User", foreign_keys=[created_by_id])

    __table_args__ = (
        Index("ix_prepayment_vendor_card", "vendor_card_id"),
        Index("ix_prepayment_buy_plan", "buy_plan_id"),
        Index("ix_prepayment_buy_plan_line", "buy_plan_line_id"),
        Index("ix_prepayment_created_by", "created_by_id"),
        Index("ix_prepayment_status", "status"),
        Index("ix_prepayment_pay_token", "pay_token", unique=True),
    )


# ── QpSerialEntry ────────────────────────────────────────────────────────────────


class QpSerialEntry(Base):
    """One serial-preapproval tracking row on a QualityPlan's Serial section.

    Mirrors the QP template's serial table: a buyer submits a serial number to the
    customer for preapproval before shipment, then records the customer's decision.
    Deleting the parent QP cascades these rows away (FK ondelete CASCADE + ORM delete-
    orphan).
    """

    __tablename__ = "qp_serial_entries"

    id = Column(Integer, primary_key=True)
    qp_id = Column(Integer, ForeignKey("quality_plans.id", ondelete="CASCADE"), nullable=False)

    buyer_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    submitted_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    buyer_date = Column(Date, nullable=True)

    has_sn_prev_received = Column(Boolean, nullable=True)  # Has SN previously been received?
    purchase_order = Column(String(255), nullable=True)
    part_number = Column(String(255), nullable=True)
    serial_number = Column(String(255), nullable=True)
    seagate_sn = Column(String(255), nullable=True)  # Seagate SN (if applicable)
    tso = Column(String(255), nullable=True)
    customer_po = Column(String(255), nullable=True)
    submitted_to_customer_date = Column(Date, nullable=True)
    customer_approved = Column(Boolean, nullable=True)  # Did customer approve?
    customer_approved_date = Column(Date, nullable=True)
    ops_received = Column(Boolean, nullable=True)  # OPS received

    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))

    # ── Relationships
    quality_plan = relationship("QualityPlan", back_populates="serial_entries")
    buyer = relationship("User", foreign_keys=[buyer_id])
    submitted_by = relationship("User", foreign_keys=[submitted_by_id])

    __table_args__ = (Index("ix_qp_serial_qp", "qp_id"),)


# ── QpFruLookup ──────────────────────────────────────────────────────────────────


class QpFruLookup(Base):
    """A FRU part number pinned to a QualityPlan's FRU crosswalk section.

    Stores only the normalized FRU key (fru_norm); the view live-joins the shared
    FruLink crosswalk by fru_norm to render the related model/carrier/series rows.
    Unique per (qp_id, fru_norm) so a FRU can't be pinned twice. Cascades with the QP.
    """

    __tablename__ = "qp_fru_lookups"

    id = Column(Integer, primary_key=True)
    qp_id = Column(Integer, ForeignKey("quality_plans.id", ondelete="CASCADE"), nullable=False)
    fru_norm = Column(String(64), nullable=False)

    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))

    # ── Relationships
    quality_plan = relationship("QualityPlan", back_populates="fru_lookups")

    __table_args__ = (
        UniqueConstraint("qp_id", "fru_norm", name="uq_qp_fru_lookup"),
        Index("ix_qp_fru_qp", "qp_id"),
    )
