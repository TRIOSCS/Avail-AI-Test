"""Pydantic schemas for the prospect accounts module."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ProspectAccountBase(BaseModel):
    """Shared fields for prospect accounts."""

    name: str
    domain: str
    website: str | None = None
    industry: str | None = None
    naics_code: str | None = None
    employee_count_range: str | None = None
    revenue_range: str | None = None
    hq_location: str | None = None
    region: str | None = None
    description: str | None = None
    parent_company_domain: str | None = None


class ProspectAccountCreate(ProspectAccountBase):
    """Used by discovery services to insert new prospects."""

    discovery_source: str
    discovery_batch_id: int | None = None
    company_id: int | None = None
    import_priority: str | None = None
    historical_context: dict[str, Any] = Field(default_factory=dict)
    enrichment_data: dict[str, Any] = Field(default_factory=dict)
    contacts_preview: list[dict[str, Any]] = Field(default_factory=list)


class ProspectAccountRead(ProspectAccountBase):
    """API response — full prospect record."""

    model_config = ConfigDict(from_attributes=True, extra="allow")

    id: int
    fit_score: int
    fit_reasoning: str | None = None
    readiness_score: int
    readiness_signals: dict[str, Any] = Field(default_factory=dict)
    discovery_source: str
    discovery_batch_id: int | None = None
    status: str
    import_priority: str | None = None
    historical_context: dict[str, Any] = Field(default_factory=dict)
    claimed_by: int | None = None
    claimed_at: datetime | None = None
    dismissed_by: int | None = None
    dismissed_at: datetime | None = None
    dismiss_reason: str | None = None
    company_id: int | None = None
    contacts_preview: list[dict[str, Any]] = Field(default_factory=list)
    similar_customers: list[dict[str, Any]] = Field(default_factory=list)
    enrichment_data: dict[str, Any] = Field(default_factory=dict)
    email_pattern: str | None = None
    ai_writeup: str | None = None
    last_enriched_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PoolStats(BaseModel):
    """Summary statistics for the prospect pool."""

    total_suggested: int
    total_claimed: int
    total_dismissed: int
    by_source: dict[str, int] = Field(default_factory=dict)
    avg_fit_score: float = 0.0
    avg_readiness_score: float = 0.0


class ProspectClaimRequest(BaseModel):
    """Request to claim a prospect."""

    notes: str | None = None


class ProspectDismissRequest(BaseModel):
    """Request to dismiss a prospect."""

    dismiss_reason: str


class ProspectFilters(BaseModel):
    """Query parameters for prospect list."""

    status: str | None = None
    min_fit_score: int | None = None
    min_readiness_score: int | None = None
    region: str | None = None
    industry: str | None = None
    has_intent_signals: bool | None = None
    discovery_source: str | None = None
    search: str | None = None
    sort_by: str = "fit_score"
    page: int = Field(default=1, ge=1)
    per_page: int = Field(default=20, ge=1, le=100)


class ProspectAddRequest(BaseModel):
    """Manual prospect submission by domain."""

    domain: str
    notes: str | None = None


class DiscoveryBatchRead(BaseModel):
    """Batch run summary for admin view."""

    model_config = ConfigDict(from_attributes=True, extra="allow")

    id: int
    batch_id: str
    source: str
    segment: str | None = None
    regions: list[str] = Field(default_factory=list)
    search_filters: dict[str, Any] = Field(default_factory=dict)
    status: str
    prospects_found: int
    prospects_new: int
    prospects_updated: int
    credits_used: int
    error_message: str | None = None
    started_at: datetime
    completed_at: datetime | None = None
    created_at: datetime | None = None
