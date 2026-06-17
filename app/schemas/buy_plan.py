"""schemas/buy_plan.py — Pydantic schemas for Buy Plan V4 (unified) API.

Request and response models for the structured buy plan system with
split lines, dual approval tracks, and AI-powered vendor selection.

Business Rules:
- SO # required on submit
- PO # + ship date required on PO confirmation
- Line edits support splits (multiple entries with same requirement_id)
- Manager approval supports line-level vendor overrides
- Offer comparison shows all feasible offers per requirement

Called by: routers/htmx_views.py
Depends on: pydantic, models/buy_plan.py (enum values)
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


def _require_and_strip_note(note: str | None, *, required: bool, message: str) -> str | None:
    """Enforce a conditionally-required note and strip surrounding whitespace.

    Raises ``ValueError`` with ``message`` when ``required`` is set but the note
    is missing or blank. A present note is returned stripped; ``None`` passes
    through unchanged.
    """
    if required and not (note and note.strip()):
        raise ValueError(message)
    if note:
        return note.strip()
    return note


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
        self.rejection_note = _require_and_strip_note(
            self.rejection_note,
            required=self.action in ("reject", "halt"),
            message="A note is required when rejecting or halting",
        )
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
        self.rejection_note = _require_and_strip_note(
            self.rejection_note,
            required=self.action == "reject",
            message="A note is required when rejecting a PO",
        )
        return self


class BuyPlanLineIssue(BaseModel):
    """Buyer flags an issue on a line."""

    issue_type: Literal["sold_out", "price_changed", "lead_time_changed", "other"]
    note: str | None = None

    @model_validator(mode="after")
    def note_required_for_other(self):
        self.note = _require_and_strip_note(
            self.note,
            required=self.issue_type == "other",
            message="A note is required for 'other' issue type",
        )
        return self


class VerificationGroupUpdate(BaseModel):
    """Add or remove a user from the ops verification group."""

    user_id: int
    action: Literal["add", "remove"]
