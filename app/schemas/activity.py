"""Pydantic schemas for activity endpoints (click-to-call logging + timeline).

Called by: app/routers/activity.py, app/routers/v13_features/activity.py
Depends on: pydantic, app/constants (OutreachChannel)
"""

from datetime import datetime

from pydantic import BaseModel, Field

from ..constants import CallOutcome, OutreachChannel


class CallInitiatedRequest(BaseModel):
    """Request body for POST /api/activity/call-initiated."""

    phone_number: str
    vendor_card_id: int | None = None
    company_id: int | None = None
    customer_site_id: int | None = None
    requirement_id: int | None = None
    origin: str | None = None


class OutreachInitiatedRequest(BaseModel):
    """Request body for POST /api/activity/outreach-initiated.

    Logs a click-to-contact event (phone / email / Teams / WeChat) from the CDM account
    workspace contact panel. channel validates against the OutreachChannel StrEnum (the
    single source of truth — the service channel maps are keyed by the same enum), and
    the free-text fields carry max_length bounds matching the ActivityLog snapshot
    columns (contact_email/contact_name String(255)) so over-length input is a 422 at
    the boundary instead of a Postgres DataError 500.
    """

    channel: OutreachChannel
    contact_value: str = Field(max_length=255)  # phone number, email address, or WeChat ID
    company_id: int | None = None
    customer_site_id: int | None = None
    site_contact_id: int | None = None
    contact_name: str | None = Field(default=None, max_length=255)
    origin: str | None = Field(default=None, max_length=100)


class CallOutcomeRequest(BaseModel):
    """Request body for POST /api/activity/{activity_id}/call-outcome."""

    outcome: CallOutcome
    note: str | None = Field(default=None, max_length=500)


# ── Timeline filter / response schemas ────────────────────────────────


class ActivityLogRead(BaseModel):
    """Response model for a single activity log entry."""

    model_config = {"extra": "allow", "from_attributes": True}

    id: int
    user_id: int
    activity_type: str
    channel: str
    company_id: int | None = None
    vendor_card_id: int | None = None
    vendor_contact_id: int | None = None
    site_contact_id: int | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    contact_name: str | None = None
    subject: str | None = None
    notes: str | None = None
    duration_seconds: int | None = None
    direction: str | None = None
    event_type: str | None = None
    summary: str | None = None
    source_url: str | None = None
    created_at: datetime | None = None

    # Joined names (populated by service layer)
    user_name: str | None = None
    company_name: str | None = None
    vendor_name: str | None = None


class ActivityTimelineResponse(BaseModel):
    """Paginated timeline response."""

    items: list[ActivityLogRead]
    total: int
    limit: int
    offset: int
