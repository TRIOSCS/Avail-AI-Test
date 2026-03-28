"""Pydantic schemas for the prospect accounts module."""

from typing import Any

from pydantic import BaseModel, Field


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
