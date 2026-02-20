"""schemas/enrichment.py — Pydantic models for deep enrichment endpoints."""

from __future__ import annotations

from pydantic import BaseModel, Field


class BackfillRequest(BaseModel):
    entity_types: list[str] = Field(default=["vendor", "company"])
    max_items: int = Field(default=500, ge=1, le=2000)
    include_deep_email: bool = False
    lookback_days: int = Field(default=365, ge=1, le=730)


class QueueActionRequest(BaseModel):
    pass


class BulkApproveRequest(BaseModel):
    ids: list[int] = Field(..., min_length=1, max_length=500)


class EnrichmentQueueItem(BaseModel):
    id: int
    entity_type: str | None = None
    entity_name: str | None = None
    enrichment_type: str
    field_name: str
    current_value: str | None = None
    proposed_value: str
    confidence: float
    source: str
    status: str
    created_at: str | None = None


class EnrichmentJobSummary(BaseModel):
    id: int
    job_type: str
    status: str
    total_items: int
    processed_items: int
    enriched_items: int
    error_count: int
    progress_pct: float
    started_by: str | None = None
    started_at: str | None = None
    completed_at: str | None = None


class EnrichmentStats(BaseModel):
    queue_pending: int = 0
    queue_approved: int = 0
    queue_rejected: int = 0
    queue_auto_applied: int = 0
    vendors_enriched: int = 0
    vendors_total: int = 0
    companies_enriched: int = 0
    companies_total: int = 0
    active_jobs: int = 0
