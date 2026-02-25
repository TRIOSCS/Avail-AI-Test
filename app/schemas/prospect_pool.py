"""Pydantic models for the prospect pool (Suggested tab) endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class PoolAccountRead(BaseModel):
    """Company formatted for the Suggested tab card."""

    id: int
    name: str
    domain: str | None = None
    website: str | None = None
    industry: str | None = None
    phone: str | None = None
    hq_city: str | None = None
    hq_state: str | None = None
    hq_country: str | None = None
    import_priority: str | None = None
    sf_account_id: str | None = None


class PoolStats(BaseModel):
    total_available: int = 0
    priority_count: int = 0
    standard_count: int = 0
    claimed_this_month: int = 0


class PoolAccountList(BaseModel):
    items: list[PoolAccountRead]
    total: int
    page: int
    per_page: int
    pool_stats: PoolStats


class PoolDismissRequest(BaseModel):
    reason: Literal[
        "not_relevant",
        "competitor",
        "too_small",
        "too_large",
        "duplicate",
        "other",
    ]


class PoolFilters(BaseModel):
    """Query params for pool listing — constructed in the router."""

    import_priority: str | None = None
    industry: str | None = None
    search: str | None = None
    sort_by: str = "priority"
    page: int = Field(default=1, ge=1)
    per_page: int = Field(default=20, ge=1, le=100)
