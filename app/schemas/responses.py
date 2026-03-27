"""schemas/responses.py — Shared response models for OpenAPI documentation.

Provides base response wrappers (pagination, ok) and typed response models
for the top 30 endpoints. Used as response_model= on router decorators.

Called by: routers/*.py
Depends on: pydantic
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# ── Base Wrappers ───────────────────────────────────────────────────────


class PaginatedResponse(BaseModel):
    total: int = 0
    limit: int = 50
    offset: int = 0


class OkResponse(BaseModel):
    ok: bool = True


# ── Search / Sightings ─────────────────────────────────────────────────


class SightingItem(BaseModel):
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


class RequisitionListItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int
    name: str
    status: str = ""
    customer_site_id: int | None = None
    requirement_count: int = 0
    created_at: str | None = None


class RequisitionListResponse(PaginatedResponse):
    requisitions: list[RequisitionListItem] = Field(default_factory=list)


class RequirementListItem(BaseModel):
    id: int
    primary_mpn: str
    target_qty: int | None = None
    target_price: float | None = None
    substitutes: list[str] = Field(default_factory=list)
    sighting_count: int = 0


# ── Vendors ─────────────────────────────────────────────────────────────


class VendorListItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int
    display_name: str
    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    sighting_count: int = 0
    is_blacklisted: bool = False


class VendorListResponse(PaginatedResponse):
    vendors: list[VendorListItem] = Field(default_factory=list)


class VendorDetailResponse(BaseModel):
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
    # Extended fields from card_to_dict()
    linkedin_url: str | None = None
    legal_name: str | None = None
    industry: str | None = None
    employee_size: str | None = None
    hq_city: str | None = None
    hq_state: str | None = None
    hq_country: str | None = None
    last_enriched_at: str | None = None
    enrichment_source: str | None = None
    brands: list[dict] = Field(default_factory=list)
    unique_parts: int = 0
    vendor_score: float | None = None
    advancement_score: float | None = None
    is_new_vendor: bool = False
    total_outreach: int = 0
    total_responses: int = 0
    ghost_rate: float | None = None
    response_velocity_hours: float | None = None
    last_contact_at: str | None = None
    brand_tags: list[str] = Field(default_factory=list)
    commodity_tags: list[str] = Field(default_factory=list)
    material_tags_updated_at: str | None = None
    tags: list[dict] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None


# ── Companies ───────────────────────────────────────────────────────────


class CompanyListItem(BaseModel):
    id: int
    name: str
    site_count: int = 0
    sites: list[dict] = Field(default_factory=list)


# ── Offers ──────────────────────────────────────────────────────────────


class OfferGroupItem(BaseModel):
    requirement_id: int
    mpn: str = ""
    offers: list[dict] = Field(default_factory=list)


class OfferListResponse(BaseModel):
    has_new_offers: bool = False
    latest_offer_at: str | None = None
    groups: list[dict] = Field(default_factory=list)


# ── Quotes ──────────────────────────────────────────────────────────────


class QuoteDetailResponse(BaseModel):
    id: int
    requisition_id: int
    quote_number: str = ""
    status: str = "draft"
    line_items: list[dict] = Field(default_factory=list)
    subtotal: float | None = None
    sent_at: str | None = None
    # Extended fields from quote_to_dict()
    customer_site_id: int | None = None
    customer_name: str | None = None
    company_domain: str | None = None
    company_name_short: str | None = None
    contact_name: str | None = None
    contact_email: str | None = None
    site_contacts: list[dict] = Field(default_factory=list)
    revision: int | None = None
    total_cost: float | None = None
    total_margin_pct: float | None = None
    payment_terms: str | None = None
    shipping_terms: str | None = None
    validity_days: int | None = None
    notes: str | None = None
    result: str | None = None
    result_reason: str | None = None
    result_notes: str | None = None
    result_at: str | None = None
    won_revenue: float | None = None
    created_by: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    is_expired: bool = False
    days_until_expiry: int | None = None


class QuoteSummaryResponse(BaseModel):
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


class BuyPlanListItem(BaseModel):
    id: int
    status: str = ""
    line_items: list[dict] = Field(default_factory=list)
    total_cost: float = 0
    total_revenue: float = 0


# ── Performance ─────────────────────────────────────────────────────────


class VendorScorecardListResponse(PaginatedResponse):
    vendors: list[dict] = Field(default_factory=list)


class BuyerLeaderboardResponse(BaseModel):
    month: str = ""
    entries: list[dict] = Field(default_factory=list)


# ── Sources ─────────────────────────────────────────────────────────────


class SourceListResponse(BaseModel):
    sources: list[dict] = Field(default_factory=list)


# ── Enrichment ──────────────────────────────────────────────────────────


class EnrichmentQueueResponse(PaginatedResponse):
    items: list[dict] = Field(default_factory=list)


# ── Vendor Sub-endpoints ────────────────────────────────────────────────


class VendorPartsSummaryResponse(BaseModel):
    vendor_name: str = ""
    total: int = 0
    items: list[dict] = Field(default_factory=list)


class VendorEmailMetricsResponse(BaseModel):
    vendor_name: str = ""
    total_rfqs_sent: int = 0
    total_replies: int = 0
    total_quotes: int = 0
    response_rate: int | None = None
    quote_rate: int | None = None
    avg_response_hours: float | None = None
    last_contacted: str | None = None
    last_reply: str | None = None
    active_rfqs: int = 0


# ── AI Endpoints ───────────────────────────────────────────────────────


class SimpleOkResponse(BaseModel):
    """Generic success response for delete/toggle actions."""

    model_config = ConfigDict(extra="allow")

    ok: bool = True


class SimpleOkIdResponse(BaseModel):
    """Success response with an entity ID."""

    ok: bool = True
    id: int = 0

    model_config = ConfigDict(extra="allow")


class AiFindContactsResponse(BaseModel):
    """Response from AI contact finder."""

    contacts: list[dict] = Field(default_factory=list)
    total: int = 0
    saved_ids: list[int] = Field(default_factory=list)

    model_config = ConfigDict(extra="allow")


class AiParseEmailResponse(BaseModel):
    """Response from AI email parsing endpoint."""

    parsed: bool = False
    quotes: list[dict] = Field(default_factory=list)
    overall_confidence: float = 0.0
    email_type: str = "unclear"
    vendor_notes: str | None = None
    auto_apply: bool = False
    needs_review: bool = False
    reason: str = ""

    model_config = ConfigDict(extra="allow")


class AiNormalizePartsResponse(BaseModel):
    """Response from AI part normalization."""

    parts: list[dict] = Field(default_factory=list)
    count: int = 0

    model_config = ConfigDict(extra="allow")


class AiStandardizeResponse(BaseModel):
    """Response from AI description standardization."""

    description: str = ""

    model_config = ConfigDict(extra="allow")


class CompanyIntelResponse(BaseModel):
    """Response from company intelligence lookup."""

    available: bool = False
    intel: dict | None = None
    reason: str = ""

    model_config = ConfigDict(extra="allow")


class AiDraftRfqResponse(BaseModel):
    """Response from AI RFQ draft generation."""

    available: bool = False
    body: str = ""
    reason: str = ""

    model_config = ConfigDict(extra="allow")


class AiIntakeParseResponse(BaseModel):
    """Response from unified AI intake parser."""

    parsed: bool = False
    template: dict | None = None
    reason: str = ""

    model_config = ConfigDict(extra="allow")


class AiParseResponseResult(BaseModel):
    """Response from AI vendor response re-parsing."""

    parsed: bool = False
    classification: str | None = None
    confidence: float | None = None
    auto_apply: bool = False
    needs_review: bool = False
    parts: list[dict] = Field(default_factory=list)
    draft_offers: list[dict] = Field(default_factory=list)
    vendor_notes: str | None = None
    reason: str = ""

    model_config = ConfigDict(extra="allow")


# ── Sources Endpoints ──────────────────────────────────────────────────


class ApiTestResponse(BaseModel):
    """Response from source API connectivity test."""

    source: str = ""
    test_mpn: str = ""
    status: str = ""
    results_count: int = 0
    elapsed_ms: float = 0
    error: str | None = None
    sample: list[dict] = Field(default_factory=list)

    model_config = ConfigDict(extra="allow")


class ToggleResponse(BaseModel):
    """Response from source toggle/activate actions."""

    ok: bool = True
    status: str = ""

    model_config = ConfigDict(extra="allow")


class ToggleActiveResponse(BaseModel):
    """Response from source activate toggle."""

    ok: bool = True
    is_active: bool = False

    model_config = ConfigDict(extra="allow")


class HealthSummaryResponse(BaseModel):
    """Response from source health summary check."""

    has_errors: bool = False
    errored_sources: list[dict] = Field(default_factory=list)

    model_config = ConfigDict(extra="allow")


class SystemAlertsResponse(BaseModel):
    """Response from system alerts endpoint."""

    alerts: list[dict] = Field(default_factory=list)
    count: int = 0

    model_config = ConfigDict(extra="allow")


class InboxScanResponse(BaseModel):
    """Response from email inbox mining scan."""

    messages_scanned: int = 0
    vendors_found: int = 0
    offers_parsed: int = 0
    contacts_enriched: int = 0

    model_config = ConfigDict(extra="allow")


class EmailMiningStatusResponse(BaseModel):
    """Response from email mining status check."""

    enabled: bool = False
    last_scan: str | None = None
    total_scans: int = 0
    total_vendors_found: int = 0

    model_config = ConfigDict(extra="allow")


class OutboundScanResponse(BaseModel):
    """Response from outbound email mining scan."""

    messages_scanned: int = 0
    rfqs_detected: int = 0
    vendors_contacted: int = 0
    cards_updated: int = 0
    used_delta: bool = False

    model_config = ConfigDict(extra="allow")


class EngagementComputeResponse(BaseModel):
    """Response from engagement score recomputation."""

    updated: int = 0
    skipped: int = 0

    model_config = ConfigDict(extra="allow")


class VendorEngagementDetailResponse(BaseModel):
    """Response from vendor engagement detail endpoint."""

    vendor_id: int = 0
    vendor_name: str = ""
    vendor_score: float | None = None
    advancement_score: float | None = None
    is_new_vendor: bool = True
    engagement_score: float | None = None
    live_vendor_score: float | None = None
    live_is_new_vendor: bool = True
    raw_counts: dict = Field(default_factory=dict)
    computed_at: str | None = None

    model_config = ConfigDict(extra="allow")


class AttachmentParseResponse(BaseModel):
    """Response from response attachment parsing."""

    attachments_found: int = 0
    parseable: int = 0
    rows_parsed: int = 0
    sightings_created: int = 0

    model_config = ConfigDict(extra="allow")


# ── Command Center ─────────────────────────────────────────────────────


class CommandCenterResponse(BaseModel):
    """Response from command center actions endpoint."""

    stale_rfqs: list[dict] = Field(default_factory=list)
    pending_quotes: list[dict] = Field(default_factory=list)
    pending_reviews: list[dict] = Field(default_factory=list)
    today_responses: list[dict] = Field(default_factory=list)

    model_config = ConfigDict(extra="allow")
