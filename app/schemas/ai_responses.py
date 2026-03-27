"""Pydantic models for AI/Claude response validation.

Validates the raw dicts returned by Claude API calls before they flow
through the service layer. Each model matches the JSON schema sent to
Claude structured outputs or the expected shape from claude_json.

Called by: services/response_parser.py, services/ai_service.py
Depends on: pydantic
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# ── Vendor Response Parsing ──────────────────────────────────────────────


class ParsedPart(BaseModel):
    """A single part extracted from a vendor email reply.

    Fields match RESPONSE_PARSE_SCHEMA in response_parser.py.
    """

    model_config = ConfigDict(extra="allow")

    mpn: str = ""
    status: str = ""  # quoted | no_stock | follow_up
    qty_available: int | None = None
    unit_price: float | None = None
    currency: str = "USD"
    lead_time: str | None = None
    condition: str | None = None
    date_code: str | None = None
    moq: int | None = None
    packaging: str | None = None
    valid_days: int | None = None
    notes: str | None = None
    manufacturer: str | None = None


class VendorResponseParsed(BaseModel):
    """Structured output from parse_vendor_response.

    Fields match RESPONSE_PARSE_SCHEMA required + optional properties.
    """

    model_config = ConfigDict(extra="allow")

    overall_sentiment: str = "neutral"  # positive | negative | neutral | mixed
    overall_classification: str = ""  # quote_provided | no_stock | counter_offer | ...
    confidence: float = 0.0
    parts: list[ParsedPart] = Field(default_factory=list)
    vendor_notes: str | None = None


# ── Contact Enrichment ───────────────────────────────────────────────────


class EnrichedContact(BaseModel):
    """A contact found via Claude web search.

    Fields match the shape returned by enrich_contacts_websearch.
    """

    model_config = ConfigDict(extra="allow")

    full_name: str
    title: str | None = None
    email: str | None = None
    phone: str | None = None
    linkedin_url: str | None = None


class ContactSearchResult(BaseModel):
    """Raw response from Claude for contact enrichment.

    Matches the CONTACT_SEARCH_SCHEMA in ai_service.py.
    """

    model_config = ConfigDict(extra="allow")

    contacts: list[EnrichedContact] = Field(default_factory=list)


# ── Company Intelligence ─────────────────────────────────────────────────


class CompanyIntelligence(BaseModel):
    """Response from AI company intelligence.

    Fields match INTEL_SCHEMA in ai_service.py.
    """

    model_config = ConfigDict(extra="allow")

    summary: str = ""
    revenue: str | None = None
    employees: str | None = None
    products: str | None = None
    components_they_buy: list[str] = Field(default_factory=list)
    recent_news: list[str] = Field(default_factory=list)
    opportunity_signals: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
