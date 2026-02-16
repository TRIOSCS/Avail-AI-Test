"""
v13_features.py — Pydantic schemas for v1.3 feature endpoints.

Covers buyer profiles, phone call logging, strategic toggles,
and routing score/assignment requests.

Called by: routers/v13_features.py
Depends on: pydantic
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, field_validator


# ── Buyer Profiles ──────────────────────────────────────────────────


class BuyerProfileUpsert(BaseModel):
    """Create or update a buyer's expertise profile."""

    primary_commodity: str | None = None
    secondary_commodity: str | None = None
    primary_geography: str | None = None
    brand_specialties: list[str] | str | None = None
    brand_material_types: list[str] | str | None = None
    brand_usage_types: list[str] | str | None = None

    # Service layer already handles str→list conversion, so we pass through.


# ── Activity Logging ────────────────────────────────────────────────


class PhoneCallLog(BaseModel):
    """Log an inbound or outbound phone call."""

    phone: str = ""
    direction: Literal["outbound", "inbound"] = "outbound"
    duration_seconds: int | None = None
    external_id: str | None = None
    contact_name: str | None = None


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


# ── Customer Ownership ──────────────────────────────────────────────


class StrategicToggle(BaseModel):
    """Toggle a company's strategic account flag."""

    is_strategic: bool | None = None  # None = flip current value


# ── Routing ─────────────────────────────────────────────────────────


class RoutingPairRequest(BaseModel):
    """Identify a requirement+vendor pair for routing operations."""

    requirement_id: int
    vendor_card_id: int

    @field_validator("requirement_id", "vendor_card_id")
    @classmethod
    def must_be_positive(cls, v: int, info) -> int:
        if v <= 0:
            raise ValueError(f"{info.field_name} must be positive")
        return v
