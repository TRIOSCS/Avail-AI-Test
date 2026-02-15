"""
schemas/rfq.py — Pydantic models for RFQ endpoints

Validates batch RFQ sends, phone call logging, RFQ preparation,
and follow-up email bodies.

Business Rules:
- Phone log requires requisition_id, vendor_name, vendor_phone
- Batch RFQ groups must be non-empty
- Follow-up body defaults to template if blank

Called by: routers/rfq.py
Depends on: pydantic
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class PhoneCallLog(BaseModel):
    """Log a phone contact with a vendor."""
    requisition_id: int
    vendor_name: str
    vendor_phone: str
    parts: list[str] = Field(default_factory=list)

    @field_validator("vendor_name", "vendor_phone")
    @classmethod
    def not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Field must not be blank")
        return v


class RfqVendorGroup(BaseModel):
    """A group of vendors to receive an RFQ for specific parts."""
    vendor_name: str
    email: str
    parts: list[str] = Field(default_factory=list)


class BatchRfqSend(BaseModel):
    """Batch RFQ send payload."""
    groups: list[RfqVendorGroup] = Field(default_factory=list)


class RfqPrepare(BaseModel):
    """Pre-send vendor preparation — check exhaustion + vendor cards."""
    vendors: list[str] = Field(default_factory=list)


class FollowUpEmail(BaseModel):
    """Follow-up email body — defaults handled in router if blank."""
    body: str = ""
