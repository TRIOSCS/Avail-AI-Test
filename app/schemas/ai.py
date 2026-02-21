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

from datetime import date
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


# ── RFQ Email Parsing ──────────────────────────────────────────────────


class ParseEmailRequest(BaseModel):
    """Input for AI email parsing."""

    email_body: str = Field(min_length=1)
    email_subject: str = ""
    vendor_name: str = ""


class ParsedQuote(BaseModel):
    """Single extracted quote from a vendor email."""

    part_number: str | None = None
    manufacturer: str | None = None
    quantity_available: int | None = None
    unit_price: float | None = None
    currency: str = "USD"
    lead_time_days: int | None = None
    lead_time_text: str | None = None
    moq: int | None = None
    date_code: str | None = None
    condition: str | None = None
    packaging: str | None = None
    notes: str | None = None
    confidence: float = 0.5


class ParseEmailResponse(BaseModel):
    """Response from AI email parsing."""

    parsed: bool
    quotes: list[ParsedQuote] = []
    overall_confidence: float = 0.0
    email_type: str = "unclear"
    vendor_notes: str | None = None
    auto_apply: bool = False
    needs_review: bool = False


# ── Part Number Normalization ─────────────────────────────────────────


class NormalizePartsRequest(BaseModel):
    """Input for AI part number normalization."""

    parts: list[str] = Field(min_length=1, max_length=50)


class NormalizedPart(BaseModel):
    """Single normalized part number result."""

    original: str
    normalized: str
    manufacturer: str | None = None
    base_part: str | None = None
    package_code: str | None = None
    is_alias: bool = False
    confidence: float = 0.0


# ── RFQ Email Drafting ───────────────────────────────────────────────


class RfqDraftPart(BaseModel):
    """Single part in an RFQ draft request."""

    part_number: str
    manufacturer: str | None = None
    quantity: int
    target_price: float | None = None
    date_code_requirement: str | None = None
    condition_requirement: str | None = None
    delivery_deadline: date | None = None
    additional_notes: str | None = None


class RfqDraftEmailRequest(BaseModel):
    """Input for AI RFQ email draft generation."""

    vendor_name: str = Field(min_length=1)
    vendor_contact_name: str | None = None
    buyer_name: str = Field(min_length=1)
    parts: list[RfqDraftPart] = Field(min_length=1)


# ── Quote Comparison ─────────────────────────────────────────────────


class QuoteForAnalysis(BaseModel):
    """Single vendor quote to be compared."""

    vendor_name: str
    vendor_score: float | None = None
    unit_price: float | None = None
    currency: str = "USD"
    quantity_available: int | None = None
    lead_time_days: int | None = None
    date_code: str | None = None
    condition: str | None = None
    moq: int | None = None


class CompareQuotesRequest(BaseModel):
    """Input for AI quote comparison."""

    part_number: str = Field(min_length=1)
    quotes: list[QuoteForAnalysis] = Field(min_length=2)
    required_qty: int | None = None
