"""Pydantic schemas for Excess Inventory & Resell offers.

Request/response models for the Resell workspace: excess lists, line items,
bulk import, and inbound broker offers (ExcessOffer / ExcessOfferLine).

Called by: routers/resell.py
Depends on: pydantic
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _strip_not_blank(v: str, field_name: str) -> str:
    """Strip whitespace and reject blank strings."""
    v = v.strip()
    if not v:
        raise ValueError(f"{field_name} must not be blank")
    return v


# ── ExcessList ───────────────────────────────────────────────────────


class ExcessListCreate(BaseModel):
    title: str
    company_id: int
    customer_site_id: int | None = None
    notes: str | None = None

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, v: str) -> str:
        return _strip_not_blank(v, "title")


class ExcessListUpdate(BaseModel):
    title: str | None = None
    status: (
        Literal["draft", "active", "bidding", "closed", "expired", "open", "collecting", "bid_out", "awarded"] | None
    ) = None
    notes: str | None = None
    # Optional posting-window deadline (D1 "Offers close by"); future + tz-aware validation
    # lives in the service (_validate_draft_close_at), not the schema.
    close_at: datetime | None = None


class ExcessListResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    company_id: int
    customer_site_id: int | None = None
    owner_id: int
    title: str
    status: str
    source_filename: str | None = None
    notes: str | None = None
    total_line_items: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ── ExcessLineItem ───────────────────────────────────────────────────


class ExcessLineItemCreate(BaseModel):
    part_number: str
    manufacturer: str | None = None
    quantity: int = Field(ge=1)
    date_code: str | None = None
    condition: str | None = "New"
    asking_price: float | None = Field(default=None, ge=0)
    notes: str | None = None

    @field_validator("part_number")
    @classmethod
    def part_number_not_blank(cls, v: str) -> str:
        return _strip_not_blank(v, "part_number")


class ExcessLineItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    excess_list_id: int
    part_number: str
    normalized_part_number: str | None = None
    manufacturer: str | None = None
    quantity: int
    date_code: str | None = None
    condition: str | None = None
    asking_price: float | None = None
    demand_match_count: int = 0
    status: str
    notes: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ── Confirm Import ──────────────────────────────────────────────────


class ConfirmImportRow(BaseModel):
    """A single validated row for import confirmation."""

    part_number: str
    manufacturer: str | None = None
    quantity: int = Field(default=1, ge=1)
    date_code: str | None = None
    condition: str | None = "New"
    asking_price: float | None = Field(default=None, ge=0)


class ConfirmImportRequest(BaseModel):
    """Request body for confirming a bulk import."""

    rows: list[ConfirmImportRow] = Field(min_length=1)


# ── Stats ───────────────────────────────────────────────────────────


class ExcessStatsResponse(BaseModel):
    """Aggregate stats for the Resell workspace (offer counts, not bid counts)."""

    total_lists: int = 0
    total_line_items: int = 0
    open_offers: int = 0
    matched_items: int = 0
    total_offers: int = 0
    awarded_items: int = 0


# ── ExcessOffer (inbound broker offers — Resell module) ─────────────


class ExcessOfferLineCreate(BaseModel):
    """One part line within a per_line offer.

    unit_price optional (price-TBD allowed).
    """

    mpn_raw: str
    quantity: int = Field(ge=1)
    unit_price: float | None = Field(default=None, ge=0)
    lead_time_days: int | None = Field(default=None, ge=0)
    terms_text: str | None = None
    excess_line_item_id: int | None = None

    @field_validator("mpn_raw")
    @classmethod
    def mpn_not_blank(cls, v: str) -> str:
        return _strip_not_blank(v, "mpn_raw")


class ExcessOfferLineResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    offer_id: int
    excess_line_item_id: int | None = None
    mpn_raw: str
    quantity: int
    unit_price: float | None = None
    lead_time_days: int | None = None
    terms_text: str | None = None
    match_status: str


class ExcessOfferCreate(BaseModel):
    """Request body for submitting an inbound offer — excess_list_id from URL path.

    scope='per_line' carries `lines`; scope='take_all' carries an optional lump
    `take_all_total_price` and no lines.
    """

    scope: Literal["per_line", "take_all"] = "per_line"
    take_all_total_price: float | None = Field(default=None, ge=0)
    notes: str | None = None
    offerer_company_id: int | None = None
    offerer_vendor_card_id: int | None = None
    lines: list[ExcessOfferLineCreate] = Field(default_factory=list)


class ExcessOfferResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    excess_list_id: int
    submitted_by: int
    offerer_company_id: int | None = None
    offerer_vendor_card_id: int | None = None
    scope: Literal["per_line", "take_all"]
    take_all_total_price: float | None = None
    status: str
    notes: str | None = None
    lines: list[ExcessOfferLineResponse] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None
