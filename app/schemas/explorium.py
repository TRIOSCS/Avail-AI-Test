"""Explorium/Vibe discovery request/response schemas.

Pydantic models for Explorium ICP discovery endpoints: segments listing,
company discovery by segment, and API status checks.

Called by: app/routers/explorium.py
Depends on: pydantic
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# -- Segments --


class ExploriumSegment(BaseModel, extra="allow"):
    """One ICP segment definition with its search criteria."""

    key: str = Field(..., description="Segment identifier (e.g. 'aerospace_defense')")
    name: str = Field(..., description="Human-readable segment name")
    linkedin_categories: list[str] = []
    naics_codes: list[str] = []
    intent_keywords: list[str] = []


class SegmentsResponse(BaseModel, extra="allow"):
    """List of all available ICP segments."""

    segments: list[ExploriumSegment] = []
    regions: dict[str, list[str]] = {}


# -- Discovery --


class DiscoverRequest(BaseModel):
    """Request to discover companies matching an ICP segment + region."""

    segment: str = Field(..., description="Segment key from SEGMENT_SEARCH_PARAMS")
    region: str = Field(default="US", description="Region key: US, EU, or Asia")


class DiscoveredCompany(BaseModel, extra="allow"):
    """A single company result from Explorium discovery."""

    name: str = ""
    domain: str = ""
    website: str | None = None
    industry: str | None = None
    naics_code: str | None = None
    employee_count_range: str | None = None
    revenue_range: str | None = None
    hq_location: str | None = None
    region: str | None = None
    description: str | None = None
    discovery_source: str = "explorium"
    segment_key: str | None = None
    intent: dict = {}
    hiring: dict = {}
    events: list[dict] = []


class DiscoverResponse(BaseModel, extra="allow"):
    """Response from Explorium company discovery."""

    segment: str
    region: str
    companies: list[DiscoveredCompany] = []
    total: int = 0


# -- Status --


class ExploriumStatus(BaseModel, extra="allow"):
    """Explorium API connectivity status."""

    configured: bool = False
    reachable: bool = False
    message: str = ""
