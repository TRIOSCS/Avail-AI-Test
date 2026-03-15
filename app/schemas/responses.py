"""schemas/responses.py — Shared response models for OpenAPI documentation.

Provides base response wrappers (pagination, ok) and typed response models
for the top 30 endpoints. Used as response_model= on router decorators.

Called by: routers/*.py
Depends on: pydantic
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# ── Base Wrappers ───────────────────────────────────────────────────────


class PaginatedResponse(BaseModel):
    total: int = 0
    limit: int = 50
    offset: int = 0


class OkResponse(BaseModel):
    ok: bool = True


# ── Search / Sightings ─────────────────────────────────────────────────


class SightingItem(BaseModel, extra="allow"):
    """Single search-result row returned by search_requirement / quick_search.

    Includes unified scoring fields added in phase-4 task-5.
    """

    id: int | None = None
    vendor_name: str = ""
    mpn_matched: str = ""
    source_type: str = ""
    score: float = 0
    source_badge: str = ""
    confidence_pct: int = 0
    confidence_color: str = "red"
    reasoning: str | None = None


# ── Requisitions ────────────────────────────────────────────────────────


class RequisitionListItem(BaseModel, extra="allow"):
    id: int
    name: str
    status: str = ""
    customer_site_id: int | None = None
    requirement_count: int = 0
    created_at: str | None = None


class RequisitionListResponse(PaginatedResponse):
    requisitions: list[dict] = Field(default_factory=list)


class RequirementListItem(BaseModel, extra="allow"):
    id: int
    primary_mpn: str
    target_qty: int | None = None
    target_price: float | None = None
    substitutes: list[str] = Field(default_factory=list)
    sighting_count: int = 0


# ── Vendors ─────────────────────────────────────────────────────────────


class VendorListItem(BaseModel, extra="allow"):
    id: int
    display_name: str
    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    sighting_count: int = 0
    is_blacklisted: bool = False


class VendorListResponse(PaginatedResponse):
    vendors: list[dict] = Field(default_factory=list)


class VendorDetailResponse(BaseModel, extra="allow"):
    id: int
    display_name: str
    normalized_name: str = ""
    domain: str | None = None
    website: str | None = None
    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    sighting_count: int = 0
    is_blacklisted: bool = False
    engagement_score: float | None = None
    avg_rating: float | None = None
    review_count: int = 0
    reviews: list[dict] = Field(default_factory=list)


# ── Companies ───────────────────────────────────────────────────────────


class CompanyListItem(BaseModel, extra="allow"):
    id: int
    name: str
    site_count: int = 0
    sites: list[dict] = Field(default_factory=list)


# ── Offers ──────────────────────────────────────────────────────────────


class OfferGroupItem(BaseModel, extra="allow"):
    requirement_id: int
    mpn: str = ""
    offers: list[dict] = Field(default_factory=list)


class OfferListResponse(BaseModel, extra="allow"):
    has_new_offers: bool = False
    latest_offer_at: str | None = None
    groups: list[dict] = Field(default_factory=list)


# ── Quotes ──────────────────────────────────────────────────────────────


class QuoteDetailResponse(BaseModel, extra="allow"):
    id: int
    requisition_id: int
    quote_number: str = ""
    status: str = "draft"
    line_items: list[dict] = Field(default_factory=list)
    subtotal: float | None = None
    sent_at: str | None = None


class QuoteSummaryResponse(BaseModel, extra="allow"):
    """Lightweight quote-tab projection for inline requisition view.

    Always populated — never blank. Shows quote status, buy plan linkage, selected
    offers, and risk flags to drive CTAs.
    """

    requisition_id: int
    has_quote: bool = False
    has_buy_plan: bool = False
    selected_offer_count: int = 0
    total_offer_count: int = 0
    risk_flags: list[dict] = Field(default_factory=list)
    # Quote fields (present when has_quote=True)
    quote_id: int | None = None
    quote_number: str | None = None
    quote_status: str | None = None
    quote_revision: int | None = None
    line_count: int | None = None
    subtotal: float | None = None
    total_margin_pct: float | None = None
    quote_updated_at: str | None = None
    # Buy plan fields (present when has_buy_plan=True)
    buy_plan_id: int | None = None
    buy_plan_status: str | None = None
    buy_plan_line_count: int | None = None


# ── Buy Plans ───────────────────────────────────────────────────────────


class BuyPlanListItem(BaseModel, extra="allow"):
    id: int
    status: str = ""
    line_items: list[dict] = Field(default_factory=list)
    total_cost: float = 0
    total_revenue: float = 0


# ── Performance ─────────────────────────────────────────────────────────


class VendorScorecardListResponse(PaginatedResponse, extra="allow"):
    vendors: list[dict] = Field(default_factory=list)


class BuyerLeaderboardResponse(BaseModel, extra="allow"):
    month: str = ""
    entries: list[dict] = Field(default_factory=list)


# ── Sources ─────────────────────────────────────────────────────────────


class SourceListResponse(BaseModel, extra="allow"):
    sources: list[dict] = Field(default_factory=list)


# ── Enrichment ──────────────────────────────────────────────────────────


class EnrichmentQueueResponse(PaginatedResponse, extra="allow"):
    items: list[dict] = Field(default_factory=list)


# ── Vendor Sub-endpoints ────────────────────────────────────────────────


class VendorPartsSummaryResponse(BaseModel, extra="allow"):
    vendor_name: str = ""
    total: int = 0
    items: list[dict] = Field(default_factory=list)


class VendorEmailMetricsResponse(BaseModel, extra="allow"):
    vendor_name: str = ""
    total_rfqs_sent: int = 0
    total_replies: int = 0
    total_quotes: int = 0
    response_rate: int | None = None
    quote_rate: int | None = None
    avg_response_hours: float | None = None
