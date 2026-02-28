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


# ── Customer Enrichment Schemas ─────────────────────────────────────


class CustomerEnrichRequest(BaseModel):
    force: bool = False


class VerifyEmailRequest(BaseModel):
    email: str = Field(..., min_length=5)


class CustomerBackfillRequest(BaseModel):
    max_accounts: int = Field(default=50, ge=1, le=500)
    assigned_only: bool = False


class CreditUsageItem(BaseModel):
    provider: str
    month: str
    used: int
    limit: int
    remaining: int


class CustomerEnrichmentResult(BaseModel):
    ok: bool = False
    company_id: int | None = None
    contacts_added: int = 0
    contacts_verified: int = 0
    sources_used: list[str] = []
    status: str | None = None
    error: str | None = None
