"""
v13_features.py — Pydantic schemas for v1.3 feature endpoints.

Covers phone call logging, strategic toggles, and activity attribution.

Called by: routers/v13_features.py
Depends on: pydantic
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, field_validator

# ── Activity Logging ────────────────────────────────────────────────


class PhoneCallLog(BaseModel):
    """Log an inbound or outbound phone call."""

    phone: str = ""
    direction: Literal["outbound", "inbound"] = "outbound"
    duration_seconds: int | None = None
    external_id: str | None = None
    contact_name: str | None = None


class CompanyCallLog(BaseModel):
    """Log a manual call against a company."""

    phone: str | None = None
    direction: Literal["outbound", "inbound"] = "outbound"
    duration_seconds: int | None = None
    contact_name: str | None = None
    notes: str | None = None


class CompanyNoteLog(BaseModel):
    """Log a manual note against a company."""

    contact_name: str | None = None
    notes: str


class VendorCallLog(BaseModel):
    """Log a manual call against a known vendor."""

    vendor_contact_id: int | None = None
    phone: str | None = None
    direction: Literal["outbound", "inbound"] = "outbound"
    duration_seconds: int | None = None
    contact_name: str | None = None
    notes: str | None = None
    requisition_id: int | None = None


class VendorNoteLog(BaseModel):
    """Log a manual note against a vendor."""

    vendor_contact_id: int | None = None
    contact_name: str | None = None
    notes: str
    requisition_id: int | None = None


# ── Unmatched Activity Queue ───────────────────────────────────────


class ActivityAttributeRequest(BaseModel):
    """Attribute an unmatched activity to a company or vendor."""

    entity_type: Literal["company", "vendor"]
    entity_id: int

    @field_validator("entity_id")
    @classmethod
    def must_be_positive(cls, v: int, info) -> int:
        if v <= 0:
            raise ValueError("entity_id must be positive")
        return v


# ── Customer Ownership ──────────────────────────────────────────────


class StrategicToggle(BaseModel):
    """Toggle a company's strategic account flag."""

    is_strategic: bool | None = None  # None = flip current value


# ── Webhook & Email Schemas ─────────────────────────────────────────


class GraphWebhookPayload(BaseModel, extra="allow"):
    """Microsoft Graph webhook notification payload."""

    value: list[dict] = []


class EmailClickLog(BaseModel):
    """Auto-log when a mailto: link is clicked."""

    email: str
    contact_name: str | None = None
