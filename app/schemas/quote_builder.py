# app/schemas/quote_builder.py
"""schemas/quote_builder.py — Pydantic schemas for Quote Builder endpoints.

Validates the save payload from the Alpine.js quote builder modal.
Field names match what create_quote handler internally uses (cost_price,
sell_price, margin_pct) — NOT the legacy QuoteLineItem schema.

Called by: app.routers.quote_builder
Depends on: pydantic
"""

from pydantic import BaseModel, Field


class QuoteBuilderLine(BaseModel):
    """Single line item in a builder save payload."""

    requirement_id: int
    offer_id: int | None = None
    mpn: str
    manufacturer: str
    qty: int
    cost_price: float
    sell_price: float
    margin_pct: float
    lead_time: str | None = None
    date_code: str | None = None
    condition: str | None = None
    packaging: str | None = None
    moq: int | None = None
    material_card_id: int | None = None
    notes: str | None = None


class QuoteBuilderSaveRequest(BaseModel):
    """Full save payload from the quote builder modal."""

    lines: list[QuoteBuilderLine] = Field(..., min_length=1)
    payment_terms: str | None = None
    shipping_terms: str | None = None
    validity_days: int = 7
    notes: str | None = None
    quote_id: int | None = None  # Set when re-saving (triggers revision)
