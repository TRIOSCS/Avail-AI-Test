"""Pydantic schemas for activity endpoints (click-to-call logging).

Called by: app/routers/activity.py
Depends on: pydantic
"""

from pydantic import BaseModel


class CallInitiatedRequest(BaseModel):
    """Request body for POST /api/activity/call-initiated."""

    phone_number: str
    vendor_card_id: int | None = None
    company_id: int | None = None
    customer_site_id: int | None = None
    requirement_id: int | None = None
    origin: str | None = None
