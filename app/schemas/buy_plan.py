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


class BuyPlanLineOverride(BaseModel):
    """Manager override for a single line during approval."""

    line_id: int
    offer_id: int | None = None
    quantity: int | None = Field(default=None, gt=0)
    manager_note: str | None = None


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


class BuyPlanTokenApproval(BaseModel):
    sales_order_number: str
    notes: str | None = None


class BuyPlanTokenReject(BaseModel):
    reason: str = ""
