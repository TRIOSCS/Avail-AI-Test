"""Pydantic schemas for activity endpoints (click-to-call logging + timeline).

Called by: app/routers/activity.py, app/routers/v13_features/activity.py
Depends on: pydantic
"""

from datetime import datetime

from pydantic import BaseModel


class CallInitiatedRequest(BaseModel):
    """Request body for POST /api/activity/call-initiated."""

    phone_number: str
    vendor_card_id: int | None = None
    company_id: int | None = None
    customer_site_id: int | None = None
    requirement_id: int | None = None
    origin: str | None = None


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
