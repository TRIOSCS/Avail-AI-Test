"""Pydantic models for proactive offer endpoints."""

from pydantic import BaseModel


class DismissMatches(BaseModel):
    match_ids: list[int]


class DraftProactive(BaseModel):
    match_ids: list[int]
    contact_ids: list[int] = []
    sell_prices: dict[str, float] = {}
    notes: str | None = None


class SendProactive(BaseModel):
    match_ids: list[int]
    contact_ids: list[int]
    sell_prices: dict[str, float] = {}
    subject: str | None = None
    notes: str | None = None
    email_html: str | None = None  # AI-drafted or user-edited HTML body


class PrepareProactive(BaseModel):
    match_ids: list[int]


class DoNotOfferItem(BaseModel):
    mpn: str
    company_id: int
    reason: str | None = None


class DoNotOfferRequest(BaseModel):
    items: list[DoNotOfferItem]
