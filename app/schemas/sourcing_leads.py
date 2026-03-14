"""Sourcing lead schemas for buyer workflow and feedback endpoints.

Purpose:
- Validate lead status updates and feedback payloads from the sourcing UI.

Business Rules Enforced:
- Status values are constrained to the sourcing workflow state machine.
- Feedback is append-only event input; it does not mutate raw evidence rows.

Called by:
- app.routers.requisitions.requirements

Depends on:
- pydantic
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

LeadStatusLiteral = Literal["new", "contacted", "replied", "no_stock", "has_stock", "bad_lead", "do_not_contact"]


class LeadStatusUpdateIn(BaseModel):
    status: LeadStatusLiteral
    note: str | None = Field(default=None, max_length=2000)
    reason_code: str | None = Field(default=None, max_length=64)
    contact_method: str | None = Field(default=None, max_length=32)
    contact_attempt_count: int = Field(default=0, ge=0, le=999)


class LeadFeedbackIn(BaseModel):
    note: str | None = Field(default=None, max_length=2000)
    reason_code: str | None = Field(default=None, max_length=64)
    contact_method: str | None = Field(default=None, max_length=32)
    contact_attempt_count: int = Field(default=0, ge=0, le=999)


class LeadOut(BaseModel):
    id: int
    lead_id: str
    requisition_id: int
    requirement_id: int
    vendor_name: str
    part_number_requested: str
    part_number_matched: str
    confidence_score: float
    confidence_band: str
    vendor_safety_score: float | None = None
    vendor_safety_band: str | None = None
    vendor_safety_summary: str | None = None
    buyer_status: str
    evidence_count: int
    corroborated: bool
    reason_summary: str
    suggested_next_action: str | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)
