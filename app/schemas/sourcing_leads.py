"""Sourcing lead schemas for buyer workflow and feedback endpoints.

Purpose:
- Validate lead status updates and feedback payloads from the sourcing UI.
- Provide rich lead output for buyer-facing API responses.

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


class EvidenceOut(BaseModel):
    """Summary of a single evidence item supporting a lead."""
    id: int
    evidence_id: str
    signal_type: str
    source_type: str
    source_name: str
    source_reference: str | None = None
    part_number_observed: str | None = None
    vendor_name_observed: str | None = None
    observed_at: datetime | None = None
    freshness_age_days: float | None = None
    explanation: str | None = None
    source_reliability_band: str | None = None
    verification_state: str | None = None
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class FeedbackEventOut(BaseModel):
    """Summary of a buyer feedback event."""
    id: int
    status: str
    note: str | None = None
    reason_code: str | None = None
    contact_method: str | None = None
    contact_attempt_count: int = 0
    created_by_user_id: int | None = None
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class LeadOut(BaseModel):
    """Buyer-facing lead response with full attribution and scoring."""
    id: int
    lead_id: str
    requisition_id: int
    requirement_id: int

    # Vendor identity
    vendor_name: str
    vendor_name_normalized: str | None = None
    vendor_card_id: int | None = None

    # Part matching
    part_number_requested: str
    part_number_matched: str
    match_type: str | None = None

    # Source attribution
    primary_source_type: str | None = None
    primary_source_name: str | None = None
    source_reference: str | None = None
    source_first_seen_at: datetime | None = None
    source_last_seen_at: datetime | None = None

    # Confidence scoring
    confidence_score: float
    confidence_band: str
    freshness_score: float | None = None
    source_reliability_score: float | None = None
    contactability_score: float | None = None
    historical_success_score: float | None = None

    # Explainability
    reason_summary: str
    risk_flags: list[str] = []
    evidence_count: int
    corroborated: bool

    # Vendor safety
    vendor_safety_score: float | None = None
    vendor_safety_band: str | None = None
    vendor_safety_summary: str | None = None
    vendor_safety_flags: list[str] = []

    # Contact info
    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    contact_url: str | None = None
    location: str | None = None

    # Buyer workflow
    buyer_status: str
    suggested_next_action: str | None = None
    notes_for_buyer: str | None = None
    buyer_feedback_summary: str | None = None
    last_buyer_action_at: datetime | None = None

    # Timestamps
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class LeadDetailOut(LeadOut):
    """Extended lead response including evidence and feedback history."""
    evidence: list[EvidenceOut] = []
    feedback_events: list[FeedbackEventOut] = []
