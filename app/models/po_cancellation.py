"""POCancellation — one immutable row per cancelled vendor PO (vendor fall-down).

Append-only fact powering vendor-performance metrics: how OFTEN a vendor's cut POs
get cancelled (frequency) and how QUICKLY (days_to_cancel; a long wait before
cancelling wastes our time and is weighed harder). Written by
app/services/po_cancellation_service.py at re-source time; read by
app/services/vendor_score.py and app/services/vendor_scorecard.py.

A buy-plan line is re-bound to a NEW vendor/offer when it is re-sourced (and can be
re-sourced many times), so the cancellation fact MUST live outside the line. FKs are
SET NULL and the vendor key is denormalized so the fact outlives the line/plan/offer
and the vendor card — exactly the durability pattern of VendorPartUnavailability.

Called by: app/services/po_cancellation_service.py (record + metric refresh),
           app/services/vendor_score.py / vendor_scorecard.py (read aggregates)
Depends on: app/constants.POCancellationReason, Base, UTCDateTime, and the
            buy_plans_v3 / buy_plan_lines / requirements / offers / vendor_cards /
            users tables.
"""

from datetime import UTC, datetime

from sqlalchemy import Column, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import relationship, validates

from ..database import UTCDateTime
from .base import Base


class POCancellation(Base):
    __tablename__ = "po_cancellations"

    id = Column(Integer, primary_key=True)

    # ── Lineage (SET NULL — the fact outlives all of these) ──────────────────
    buy_plan_id = Column(Integer, ForeignKey("buy_plans_v3.id", ondelete="SET NULL"))
    buy_plan_line_id = Column(Integer, ForeignKey("buy_plan_lines.id", ondelete="SET NULL"))
    requirement_id = Column(Integer, ForeignKey("requirements.id", ondelete="SET NULL"))
    offer_id = Column(Integer, ForeignKey("offers.id", ondelete="SET NULL"))

    # ── Vendor identity (card + durable denormalized norm — survives card delete) ──
    vendor_card_id = Column(Integer, ForeignKey("vendor_cards.id", ondelete="SET NULL"))
    # normalize_vendor_name() of the canceled vendor; re-normalized via @validates.
    vendor_name_normalized = Column(String(255), nullable=False)
    # normalize_mpn_key() canonical key (same key space as offers/unavailability).
    normalized_mpn = Column(String(255), nullable=False)

    # ── The cancelled PO ─────────────────────────────────────────────────────
    po_number = Column(String(100), nullable=False)
    po_cut_at = Column(UTCDateTime)  # = line.po_confirmed_at at cancel time (may be NULL)
    cancelled_at = Column(
        UTCDateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    # (cancelled_at - po_cut_at).days, computed by the service; NULL if po_cut_at is NULL.
    days_to_cancel = Column(Integer)

    reason_code = Column(String(32), nullable=False)  # POCancellationReason value
    reason_text = Column(Text)  # buyer's free-text note

    cancelled_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    created_at = Column(
        UTCDateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    cancelled_by = relationship("User", foreign_keys=[cancelled_by_id])
    vendor_card = relationship("VendorCard", foreign_keys=[vendor_card_id])
    offer = relationship("Offer", foreign_keys=[offer_id])

    @validates("reason_code")
    def _validate_reason(self, _key, value):
        from ..constants import POCancellationReason

        return POCancellationReason(value).value  # raises ValueError on unknown

    @validates("vendor_name_normalized", "normalized_mpn")
    def _validate_normalized_keys(self, key, value):
        """Re-normalize through the canonical helpers — a row whose key isn't normalizer
        output would silently never match metric lookups, so make it unrepresentable
        (VendorPartUnavailability precedent)."""
        if key == "vendor_name_normalized":
            from ..vendor_utils import normalize_vendor_name

            normalized = normalize_vendor_name(value or "")
        else:
            from ..utils.normalization import normalize_mpn_key

            normalized = normalize_mpn_key(value)
        if not normalized:
            raise ValueError(f"{key}={value!r} normalizes to nothing — record would be unmatchable")
        return normalized

    __table_args__ = (
        Index("ix_po_cancel_vendor_card", "vendor_card_id"),
        Index("ix_po_cancel_vendor_norm", "vendor_name_normalized"),
        Index("ix_po_cancel_vendor_cut", "vendor_card_id", "cancelled_at"),
        Index("ix_po_cancel_line", "buy_plan_line_id"),
        Index("ix_po_cancel_requirement", "requirement_id"),
        Index("ix_po_cancel_mpn", "normalized_mpn"),
    )
