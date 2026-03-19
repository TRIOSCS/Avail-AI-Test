"""Pydantic schemas for Excess Inventory & Bid Collection.

Request/response models for the excess inventory and bid collection API.

Called by: routers/excess.py (Phase 2+)
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
    status: Literal["draft", "active", "bidding", "closed", "expired"] | None = None
    notes: str | None = None


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


class ExcessLineItemUpdate(BaseModel):
    part_number: str | None = None
    manufacturer: str | None = None
    quantity: int | None = Field(default=None, ge=1)
    date_code: str | None = None
    condition: str | None = None
    asking_price: float | None = Field(default=None, ge=0)
    status: Literal["available", "bidding", "awarded", "withdrawn"] | None = None
    notes: str | None = None


class ExcessLineItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    excess_list_id: int
    part_number: str
    manufacturer: str | None = None
    quantity: int
    date_code: str | None = None
    condition: str | None = None
    asking_price: float | None = None
    market_price: float | None = None
    demand_score: int | None = None
    status: str
    notes: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ExcessLineItemImportRow(BaseModel):
    """Schema for CSV/Excel import — one row per line item."""

    part_number: str
    manufacturer: str | None = None
    quantity: int = Field(default=1, ge=1)
    date_code: str | None = None
    condition: str | None = "New"
    asking_price: float | None = Field(default=None, ge=0)

    @field_validator("part_number")
    @classmethod
    def part_number_not_blank(cls, v: str) -> str:
        return _strip_not_blank(v, "part_number")


# ── Bid ──────────────────────────────────────────────────────────────


class BidCreate(BaseModel):
    excess_line_item_id: int
    unit_price: float = Field(ge=0)
    quantity_wanted: int = Field(ge=1)
    lead_time_days: int | None = Field(default=None, ge=0)
    bidder_company_id: int | None = None
    bidder_vendor_card_id: int | None = None
    bidder_contact_id: int | None = None
    source: Literal["manual", "email_parsed", "phone"] | None = "manual"
    notes: str | None = None


class BidUpdate(BaseModel):
    unit_price: float | None = Field(default=None, ge=0)
    quantity_wanted: int | None = Field(default=None, ge=1)
    status: Literal["pending", "accepted", "rejected", "expired", "withdrawn"] | None = None
    notes: str | None = None


class BidResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    excess_line_item_id: int
    bidder_company_id: int | None = None
    bidder_vendor_card_id: int | None = None
    bidder_contact_id: int | None = None
    unit_price: float
    quantity_wanted: int
    lead_time_days: int | None = None
    status: str
    source: str
    notes: str | None = None
    created_by: int
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ── BidSolicitation ──────────────────────────────────────────────────


class BidSolicitationCreate(BaseModel):
    excess_line_item_id: int
    contact_id: int


class BidSolicitationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    excess_line_item_id: int
    contact_id: int
    sent_by: int
    email_track_id: int | None = None
    status: str
    sent_at: datetime | None = None
    created_at: datetime | None = None
