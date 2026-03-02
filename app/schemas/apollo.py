"""Apollo sync request/response schemas.

Pydantic models for Apollo integration endpoints: discover, enrich,
sync, enroll, and credit tracking.

Called by: app/routers/apollo_sync.py
Depends on: pydantic
"""

from __future__ import annotations

from pydantic import BaseModel, Field


# -- Discovery --


class ApolloDiscoverRequest(BaseModel):
    domain: str = Field(..., description="Company domain to search")
    title_keywords: list[str] = Field(
        default=[
            "procurement",
            "purchasing",
            "buyer",
            "supply chain",
            "component engineer",
            "commodity manager",
            "sourcing",
        ],
        description="Job title keywords to filter by",
    )
    max_results: int = Field(default=10, ge=1, le=25)


class DiscoveredContact(BaseModel, extra="allow"):
    apollo_id: str | None = None
    full_name: str
    title: str | None = None
    seniority: str | None = None
    email_masked: str | None = None
    linkedin_url: str | None = None
    company_name: str | None = None


class ApolloDiscoverResponse(BaseModel, extra="allow"):
    domain: str
    contacts: list[DiscoveredContact] = []
    total_found: int = 0
    note: str | None = None


# -- Enrichment --


class ApolloEnrichRequest(BaseModel):
    apollo_ids: list[str] = Field(..., min_length=1, max_length=25)
    vendor_card_id: int = Field(
        ..., description="AvailAI vendor card to attach contacts to"
    )


class EnrichedContact(BaseModel, extra="allow"):
    apollo_id: str | None = None
    full_name: str
    title: str | None = None
    email: str | None = None
    email_status: str | None = None
    phone: str | None = None
    linkedin_url: str | None = None
    seniority: str | None = None
    is_verified: bool = False


class ApolloEnrichResponse(BaseModel, extra="allow"):
    enriched: int = 0
    verified: int = 0
    credits_used: int = 0
    credits_remaining: int = 0
    contacts: list[EnrichedContact] = []


# -- Sync --


class ApolloSyncResponse(BaseModel, extra="allow"):
    synced: int = 0
    skipped: int = 0
    errors: int = 0


# -- Sequence Enrollment --


class ApolloEnrollRequest(BaseModel):
    sequence_id: str = Field(..., description="Apollo sequence ID")
    contact_ids: list[str] = Field(
        ..., min_length=1, description="Apollo contact IDs"
    )
    email_account_id: str = Field(
        ..., description="Apollo email account ID for sending"
    )


class ApolloEnrollResponse(BaseModel, extra="allow"):
    enrolled: int = 0
    skipped_no_email: int = 0
    errors: int = 0


# -- Credits --


class ApolloCreditsResponse(BaseModel, extra="allow"):
    lead_credits_remaining: int = 0
    lead_credits_used: int = 0
    direct_dial_remaining: int = 0
    direct_dial_used: int = 0
    ai_credits_remaining: int = 0
    ai_credits_used: int = 0
