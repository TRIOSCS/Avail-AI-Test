"""Pydantic models for proactive offer endpoints."""

from pydantic import BaseModel


class DismissMatches(BaseModel):
    match_ids: list[int]


class SendProactive(BaseModel):
    match_ids: list[int]
    contact_ids: list[int]
    sell_prices: dict[str, float] = {}
    subject: str | None = None
    notes: str | None = None
