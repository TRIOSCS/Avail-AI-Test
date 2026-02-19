"""
schemas/ai.py — Pydantic models for AI-powered endpoints

Validates prospect contact finder, offer saving, and RFQ drafting requests.

Business Rules:
- Prospect finder requires entity_type + entity_id
- Save offers requires requisition_id and non-empty offers list
- RFQ draft requires vendor_name and non-empty parts list

Called by: routers/ai.py
Depends on: pydantic
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.utils.normalization import normalize_condition, normalize_mpn, normalize_packaging


class ProspectFinderRequest(BaseModel):
    entity_type: Literal["company", "site", "vendor"] = "company"
    entity_id: int | None = None
    title_keywords: str | None = None


class ProspectContactSave(BaseModel):
    """Optional body when saving a prospect contact."""

    notes: str | None = None


class DraftOfferItem(BaseModel):
    vendor_name: str = ""
    mpn: str = ""
    manufacturer: str | None = None
    qty_available: int | None = None
    unit_price: float | None = None
    currency: str = "USD"
    lead_time: str | None = None
    date_code: str | None = None
    condition: str | None = None
    packaging: str | None = None
    moq: int | None = None
    notes: str | None = None
    requirement_id: int | None = None

    @field_validator("mpn")
    @classmethod
    def normalize_mpn_field(cls, v: str) -> str:
        if not v:
            return v
        return normalize_mpn(v) or v

    @field_validator("condition")
    @classmethod
    def normalize_condition_field(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return normalize_condition(v) or v

    @field_validator("packaging")
    @classmethod
    def normalize_packaging_field(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return normalize_packaging(v) or v


class SaveDraftOffersRequest(BaseModel):
    response_id: int | None = None
    requisition_id: int
    offers: list[DraftOfferItem] = Field(min_length=1)

    @field_validator("requisition_id")
    @classmethod
    def req_id_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("requisition_id must be positive")
        return v


class RfqDraftRequest(BaseModel):
    vendor_name: str
    parts: list[str] = Field(min_length=1)

    @field_validator("vendor_name")
    @classmethod
    def name_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("vendor_name required")
        return v
