"""schemas/buy_plan.py — Pydantic schemas for Buy Plan V4 (unified) API.

Request and response models for the structured buy plan system with
split lines, dual approval tracks, and AI-powered vendor selection.

Business Rules:
- SO # required on submit
- PO # + ship date required on PO confirmation
- Line edits support splits (multiple entries with same requirement_id)
- Manager approval supports line-level vendor overrides
- Offer comparison shows all feasible offers per requirement

Called by: routers/crm/buy_plans.py, routers/htmx_views.py
Depends on: pydantic, models/buy_plan.py (enum values)
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# ── Request Schemas ──────────────────────────────────────────────────


class BuyPlanLineEdit(BaseModel):
    """Salesperson edit for a single line (new, swap, or split)."""

    requirement_id: int
    offer_id: int
    quantity: int = Field(..., gt=0)
    sales_note: str | None = None


class BuyPlanSubmit(BaseModel):
    """Submit a buy plan from a won quote."""

    sales_order_number: str
    customer_po_number: str | None = None
    line_edits: list[BuyPlanLineEdit] | None = None
    salesperson_notes: str | None = None

    @field_validator("sales_order_number")
    @classmethod
    def so_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Acctivate Sales Order # is required")
        return v


class BuyPlanLineOverride(BaseModel):
    """Manager override for a single line during approval."""

    line_id: int
    offer_id: int | None = None
    quantity: int | None = Field(default=None, gt=0)
    manager_note: str | None = None


class BuyPlanApproval(BaseModel):
    """Manager approves or rejects the buy plan."""

    action: Literal["approve", "reject"]
    line_overrides: list[BuyPlanLineOverride] | None = None
    notes: str | None = None


class SOVerificationRequest(BaseModel):
    """Ops verifies the Sales Order setup in Acctivate."""

    action: Literal["approve", "reject", "halt"]
    rejection_note: str | None = None

    @model_validator(mode="after")
    def note_required_on_reject(self):
        if self.action in ("reject", "halt") and not (self.rejection_note and self.rejection_note.strip()):
            raise ValueError("A note is required when rejecting or halting")
        if self.rejection_note:
            self.rejection_note = self.rejection_note.strip()
        return self


class POConfirmation(BaseModel):
    """Buyer confirms PO was cut in Acctivate."""

    po_number: str
    estimated_ship_date: datetime

    @field_validator("po_number")
    @classmethod
    def po_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("PO number is required")
        return v


class POVerificationRequest(BaseModel):
    """Ops verifies a PO was properly entered."""

    action: Literal["approve", "reject"]
    rejection_note: str | None = None

    @model_validator(mode="after")
    def note_required_on_reject(self):
        if self.action == "reject" and not (self.rejection_note and self.rejection_note.strip()):
            raise ValueError("A note is required when rejecting a PO")
        if self.rejection_note:
            self.rejection_note = self.rejection_note.strip()
        return self


class BuyPlanLineIssue(BaseModel):
    """Buyer flags an issue on a line."""

    issue_type: Literal["sold_out", "price_changed", "lead_time_changed", "other"]
    note: str | None = None

    @model_validator(mode="after")
    def note_required_for_other(self):
        if self.issue_type == "other" and not (self.note and self.note.strip()):
            raise ValueError("A note is required for 'other' issue type")
        if self.note:
            self.note = self.note.strip()
        return self


class VerificationGroupUpdate(BaseModel):
    """Add or remove a user from the ops verification group."""

    user_id: int
    action: Literal["add", "remove"]


# ── Response Schemas ─────────────────────────────────────────────────


class BuyPlanLineResponse(BaseModel, extra="allow"):
    """Single line in a buy plan response."""

    id: int
    buy_plan_id: int
    requirement_id: int | None = None
    offer_id: int | None = None
    quantity: int
    unit_cost: float | None = None
    unit_sell: float | None = None
    margin_pct: float | None = None
    ai_score: float | None = None

    # Buyer
    buyer_id: int | None = None
    buyer_name: str | None = None
    assignment_reason: str | None = None

    # Status
    status: str = "awaiting_po"

    # PO tracking
    po_number: str | None = None
    estimated_ship_date: str | None = None
    po_confirmed_at: str | None = None
    po_verified_by_id: int | None = None
    po_verified_at: str | None = None
    po_rejection_note: str | None = None

    # Issue
    issue_type: str | None = None
    issue_note: str | None = None

    # Notes
    sales_note: str | None = None
    manager_note: str | None = None

    # Denormalized from requirement/offer for display
    mpn: str | None = None
    vendor_name: str | None = None
    manufacturer: str | None = None
    requirement_qty: int | None = None  # total qty needed (from requirement)
    lead_time: str | None = None
    condition: str | None = None


class AIFlag(BaseModel):
    """AI-generated flag on a buy plan or line."""

    type: str
    severity: str = "warning"
    line_id: int | None = None
    message: str


class BuyPlanResponse(BaseModel, extra="allow"):
    """Full buy plan with nested lines and AI analysis."""

    id: int
    quote_id: int
    requisition_id: int

    # References
    sales_order_number: str | None = None
    customer_po_number: str | None = None
    quote_number: str | None = None
    customer_name: str | None = None
    requisition_name: str | None = None

    # Statuses
    status: str = "draft"
    so_status: str = "pending"

    # Financials
    total_cost: float | None = None
    total_revenue: float | None = None
    total_margin_pct: float | None = None

    # AI
    ai_summary: str | None = None
    ai_flags: list[AIFlag] = Field(default_factory=list)

    # Approval
    auto_approved: bool = False
    approved_by_id: int | None = None
    approved_by_name: str | None = None
    approved_at: str | None = None
    approval_notes: str | None = None

    # SO verification
    so_verified_by_id: int | None = None
    so_verified_at: str | None = None
    so_rejection_note: str | None = None

    # Submission
    submitted_by_id: int | None = None
    submitted_by_name: str | None = None
    submitted_at: str | None = None
    salesperson_notes: str | None = None

    # Completion
    completed_at: str | None = None
    case_report: str | None = None

    # Stock sale
    is_stock_sale: bool = False

    # Lines
    lines: list[BuyPlanLineResponse] = Field(default_factory=list)
    line_count: int = 0
    vendor_count: int = 0

    # Timestamps
    created_at: str | None = None


class BuyPlanListItem(BaseModel, extra="allow"):
    """Summary item for queue views."""

    id: int
    quote_id: int
    requisition_id: int
    status: str = ""
    so_status: str = ""

    # Deal context
    sales_order_number: str | None = None
    customer_name: str | None = None
    quote_number: str | None = None

    # Financials
    total_cost: float | None = None
    total_revenue: float | None = None
    total_margin_pct: float | None = None

    # Summary
    line_count: int = 0
    vendor_count: int = 0
    ai_flag_count: int = 0

    # People
    submitted_by_name: str | None = None
    approved_by_name: str | None = None

    # Timing
    submitted_at: str | None = None
    approved_at: str | None = None
    created_at: str | None = None

    # Auto-approve indicator
    auto_approved: bool = False
    is_stock_sale: bool = False


class OfferComparisonItem(BaseModel, extra="allow"):
    """Single offer in a comparison view for a requirement."""

    offer_id: int
    vendor_name: str
    vendor_score: float | None = None
    unit_price: float | None = None
    qty_available: int | None = None
    lead_time: str | None = None
    condition: str | None = None
    date_code: str | None = None
    packaging: str | None = None
    ai_score: float | None = None
    is_selected: bool = False
    is_stale: bool = False
    created_at: str | None = None


class OfferComparisonResponse(BaseModel, extra="allow"):
    """All available offers for a requirement — used in manager and salesperson
    views."""

    requirement_id: int
    mpn: str | None = None
    target_qty: int | None = None
    selected_offer_ids: list[int] = Field(default_factory=list)
    offers: list[OfferComparisonItem] = Field(default_factory=list)


class VerificationGroupMemberResponse(BaseModel):
    """Member of the ops verification group."""

    id: int
    user_id: int
    user_name: str | None = None
    user_email: str | None = None
    is_active: bool = True
    added_at: str | None = None


class BuyPlanTokenApproval(BaseModel):
    sales_order_number: str
    notes: str | None = None


class BuyPlanTokenReject(BaseModel):
    reason: str = ""
