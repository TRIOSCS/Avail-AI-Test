"""Centralized StrEnum constants for stringly-typed status fields.

Replaces scattered string literals with type-safe enums that are
drop-in compatible (StrEnum members compare equal to their string values).

Single source of truth — supersedes the older app/enums.py (str, Enum) style.

Called by: models, routers, services
Depends on: nothing (leaf module)
"""

from enum import StrEnum, nonmember

# ---------------------------------------------------------------------------
# File attachment limits (applies to ALL entities uniformly)
# ---------------------------------------------------------------------------

MAX_ATTACHMENT_BYTES: int = 10 * 1024 * 1024  # 10 MB

ALLOWED_ATTACHMENT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".pdf",
        ".xlsx",
        ".xls",
        ".csv",
        ".doc",
        ".docx",
        ".png",
        ".jpg",
        ".jpeg",
        ".txt",
        ".zip",
    }
)


class ProactiveMatchStatus(StrEnum):
    """Status lifecycle for ProactiveMatch records."""

    NEW = "new"
    SENT = "sent"
    FAILED = "failed"
    DISMISSED = "dismissed"
    CONVERTED = "converted"
    EXPIRED = "expired"


class ContactStatus(StrEnum):
    """Status lifecycle for outbound Contact records (RFQ emails, calls)."""

    SENT = "sent"
    FAILED = "failed"
    QUOTED = "quoted"
    DECLINED = "declined"
    RESPONDED = "responded"
    PENDING = "pending"
    OPENED = "opened"
    OOO = "ooo"
    BOUNCED = "bounced"
    RETRIED = "retried"


class OfferStatus(StrEnum):
    """Status lifecycle for Offer records."""

    PENDING_REVIEW = "pending_review"
    ACTIVE = "active"
    APPROVED = "approved"
    REJECTED = "rejected"
    SOLD = "sold"
    WON = "won"
    EXPIRED = "expired"


class AttributionStatus(StrEnum):
    """Attribution lifecycle for Offer records."""

    ACTIVE = "active"
    EXPIRED = "expired"
    CONVERTED = "converted"


class OfferCondition(StrEnum):
    """Offer-row condition vocabulary (lowercase; distinct from MaterialCondition).

    Drives the qualification capture spine. NOT the capitalized card/facet vocab.
    """

    NEW = "new"  # new, in original manufacturer packaging
    NEW_NO_PKG = "new_no_pkg"  # new, no original manufacturer packaging
    PULLS = "pulls"
    REFURB = "refurb"


class QualificationStatus(StrEnum):
    """Snapshot of how complete an offer's standardized qualification is."""

    UNSET = "unset"  # no condition chosen
    INCOMPLETE = "incomplete"  # an essential is missing (legacy/API only)
    ESSENTIALS = "essentials"  # essentials met, some recommended missing
    COMPLETE = "complete"  # essentials + recommended all present


class RequisitionStatus(StrEnum):
    """Status lifecycle for Requisition records.

    Pipeline (Sales Hub): OPEN -> RFQS_SENT -> OFFERS -> QUOTED -> WON/LOST. DRAFT
    precedes OPEN. HOTLIST is an off-pipeline *monitor* state: the salesperson watches a
    part/customer and the Proactive matcher surfaces an offer when stock appears.
    CANCELLED retained for existing rows. There is no archive/hide capability — a
    requisition ends in WON or LOST (each carrying a required outcome_reason).
    """

    DRAFT = "draft"
    OPEN = "open"  # entry stage; "open" automatically means sourcing
    RFQS_SENT = "rfqs_sent"
    OFFERS = "offers"
    QUOTED = "quoted"
    WON = "won"
    LOST = "lost"
    HOTLIST = "hotlist"  # monitor-only; surfaced by Proactive on a matching offer
    CANCELLED = "cancelled"

    # Terminal (done) — excluded from the default open list. Single source of truth.
    # `nonmember` keeps these off the member list (they're constants, not statuses).
    TERMINAL = nonmember(frozenset({"won", "lost", "cancelled"}))
    # Active pipeline stages shown by default in the Sales Hub list.
    OPEN_PIPELINE = nonmember(frozenset({"open", "rfqs_sent", "offers", "quoted"}))
    # Off-pipeline monitor states (Hotlist).
    MONITOR = nonmember(frozenset({"hotlist"}))


class SourcingStatus(StrEnum):
    """Status lifecycle for Requirement sourcing progress (per-part within a
    requisition)."""

    OPEN = "open"
    SOURCING = "sourcing"
    OFFERED = "offered"
    QUOTED = "quoted"
    WON = "won"
    LOST = "lost"
    ARCHIVED = "archived"


class ExcessListStatus(StrEnum):
    """Status lifecycle for ExcessList records.

    Resell (resell-brokerage) lifecycle: draft -> open -> collecting -> bid_out
    -> awarded -> closed/expired. The new members are chosen to map onto existing
    ``status_badge`` keys (open->sky, collecting/sourcing->amber, bid_out/quoted->
    violet, awarded/won->emerald). ACTIVE / BIDDING are the pre-Resell members,
    kept for backward-compat (additive reshape — a later cutover chunk retires them).
    """

    DRAFT = "draft"
    OPEN = "open"
    COLLECTING = "collecting"
    BID_OUT = "bid_out"
    AWARDED = "awarded"
    CLOSED = "closed"
    EXPIRED = "expired"
    # --- Legacy (kept for backward-compat; retired in the cutover chunk) ---
    ACTIVE = "active"
    BIDDING = "bidding"


class ExcessOfferStatus(StrEnum):
    """Status lifecycle for inbound ExcessOffer records (a broker's offer to buy).

    ``late`` flags an offer that landed after the list closed / bid went out — it is
    accepted and queued for review, never dropped (spec §Resolved-for-v1 #3).
    """

    OPEN = "open"
    WON = "won"
    LOST = "lost"
    EXPIRED = "expired"
    WITHDRAWN = "withdrawn"
    LATE = "late"


class ExcessOfferScope(StrEnum):
    """Whether an ExcessOffer binds individual lines or the whole list."""

    PER_LINE = "per_line"
    TAKE_ALL = "take_all"


class OfferLineMatchStatus(StrEnum):
    """Match result of an ExcessOfferLine against the posting's lines (part-number
    only).

    ``unmatched`` / ``ambiguous`` rows keep ``mpn_raw`` and queue for manual
    resolution — never dropped (a dropped offer is a lost deal).
    """

    MATCHED = "matched"
    UNMATCHED = "unmatched"
    AMBIGUOUS = "ambiguous"


class ExcessLineItemStatus(StrEnum):
    """Status lifecycle for ExcessLineItem records."""

    AVAILABLE = "available"
    BIDDING = "bidding"
    AWARDED = "awarded"
    WITHDRAWN = "withdrawn"


class CustomerBidStatus(StrEnum):
    """Status lifecycle for CustomerBid records (the outbound bid back to the seller).

    The owner assembles selected inbound offers into a customer-facing bid priced from
    the best-per-unit rollup, then sends it. ``draft`` while building, ``sent`` once the
    clean PDF goes out, ``accepted`` / ``rejected`` on the seller's reply.
    """

    DRAFT = "draft"
    SENT = "sent"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class ExcessOutreachChannel(StrEnum):
    """Channel a resell outreach went out on (ExcessOutreach.channel).

    The trader→buyer half of Resell: the medium used to offer excess to a buyer.
    Distinct from the CDM click-to-contact ``OutreachChannel`` (which carries WECHAT
    and drives the activity-panel buttons) — this set adds MARKETPLACE/OTHER for the
    broker-marketplace and manual-log paths and is what ``ExcessOutreach.channel``
    validates against.
    """

    EMAIL = "email"
    PHONE = "phone"
    TEAMS = "teams"
    MARKETPLACE = "marketplace"
    OTHER = "other"


class ExcessOutreachStatus(StrEnum):
    """Response lifecycle for a resell outreach (ExcessOutreach.status).

    sent -> opened -> responded -> bid (the buyer submitted an ExcessOffer) or
    declined; ``no_response`` is the terminal silence state used by the don't-forget
    nudge. Advanced by the reply adapter (see resell_outreach_service in Chunk B).
    """

    SENT = "sent"
    OPENED = "opened"
    RESPONDED = "responded"
    BID = "bid"
    DECLINED = "declined"
    NO_RESPONSE = "no_response"


class QuoteStatus(StrEnum):
    """Status lifecycle for Quote records."""

    DRAFT = "draft"
    SENT = "sent"
    WON = "won"
    LOST = "lost"
    REVISED = "revised"


class VendorResponseStatus(StrEnum):
    """Vendor response queue status."""

    NEW = "new"
    PARSED = "parsed"
    REVIEWED = "reviewed"
    REJECTED = "rejected"


class UserRole(StrEnum):
    """User role assignments."""

    BUYER = "buyer"
    SALES = "sales"
    TRADER = "trader"
    MANAGER = "manager"
    ADMIN = "admin"
    # Non-interactive service account (x-agent-key auth). Least privilege:
    # deliberately excluded from require_buyer's allowed set and never admin.
    AGENT = "agent"


# Roles scoped to requisitions they own (created_by). Single source of truth for the
# role-scoped access model: sales/trader act only on their own requisitions; buyer/
# manager/admin are unrestricted. Read by dependencies.require_requisition_access,
# get_req_for_user, get_quote_for_user, and the bulk/batch handlers.
RESTRICTED_ROLES = frozenset({UserRole.SALES, UserRole.TRADER})


# ---------------------------------------------------------------------------
# Access registry (user-management feature, Phase 1 foundation)
# ---------------------------------------------------------------------------


class AccessKey(StrEnum):
    """Per-feature access keys — the closed vocabulary of grantable access.

    Single source of truth for both nav-module visibility and capability gating. A
    user's effective access is: admin → everything; otherwise an explicit per-user
    override (User.access_overrides) wins, else the role default (ROLE_ACCESS_DEFAULTS).
    ops_verification is special-cased — it delegates to VerificationGroupMember (see
    dependencies.user_has_access).
    """

    # App-section (nav module) access
    REQUISITIONS = "requisitions"
    SIGHTINGS = "sightings"
    MATERIALS = "materials"
    SEARCH = "search"
    BUY_PLANS = "buy_plans"
    RESELL = "resell"
    CRM = "crm"
    PROACTIVE = "proactive"
    PROSPECTING = "prospecting"
    MY_DAY = "my_day"
    # Capability access
    SEND_RFQ = "send_rfq"
    APPROVE_OFFERS = "approve_offers"
    EXPORT_DATA = "export_data"
    MANAGE_CONNECTORS = "manage_connectors"
    OPS_VERIFICATION = "ops_verification"


# Partition of AccessKey into the two families above. Module keys gate nav-section
# visibility; capability keys gate discrete actions. Kept beside the enum so callers
# iterate the right subset (e.g. building the nav vs. the capabilities admin panel).
MODULE_ACCESS_KEYS = (
    AccessKey.REQUISITIONS,
    AccessKey.SIGHTINGS,
    AccessKey.MATERIALS,
    AccessKey.SEARCH,
    AccessKey.BUY_PLANS,
    AccessKey.RESELL,
    AccessKey.CRM,
    AccessKey.PROACTIVE,
    AccessKey.PROSPECTING,
    AccessKey.MY_DAY,
)
CAPABILITY_ACCESS_KEYS = (
    AccessKey.SEND_RFQ,
    AccessKey.APPROVE_OFFERS,
    AccessKey.EXPORT_DATA,
    AccessKey.MANAGE_CONNECTORS,
    AccessKey.OPS_VERIFICATION,
)


class UserAuditAction(StrEnum):
    """Closed vocabulary for UserAdminAudit.action — what an admin did to a user."""

    INVITE = "invite"
    ROLE_CHANGE = "role_change"
    ACTIVATE = "activate"
    DEACTIVATE = "deactivate"
    ACCESS_GRANT = "access_grant"
    ACCESS_REVOKE = "access_revoke"
    APPROVAL_GRANT = "approval_grant"  # granted buy-plan approval right
    APPROVAL_REVOKE = "approval_revoke"  # revoked buy-plan approval right


# Default access granted to every interactive (non-admin) role. This deliberately
# preserves TODAY'S behavior: the nav is fully visible to all interactive roles, and
# RFQ / approve-offers / export / manage-connectors are allowed for every buyer-tier
# role. ops_verification is INTENTIONALLY excluded — it is curated through the
# verification group (VerificationGroupMember), never via a blanket role default, so
# turning the access model on grants nobody new ops-verification rights.
_INTERACTIVE_DEFAULTS = frozenset(MODULE_ACCESS_KEYS) | {
    AccessKey.SEND_RFQ,
    AccessKey.APPROVE_OFFERS,
    AccessKey.EXPORT_DATA,
    AccessKey.MANAGE_CONNECTORS,
}

# Role → default access set. Defaults exactly preserve current behavior so that
# introducing the access layer is a no-op until an admin sets explicit overrides.
# ADMIN gets every key; AGENT (non-interactive service account) gets none.
ROLE_ACCESS_DEFAULTS: dict[UserRole, frozenset] = {
    UserRole.BUYER: _INTERACTIVE_DEFAULTS,
    UserRole.SALES: _INTERACTIVE_DEFAULTS,
    UserRole.TRADER: _INTERACTIVE_DEFAULTS,
    UserRole.MANAGER: _INTERACTIVE_DEFAULTS,
    UserRole.ADMIN: frozenset(AccessKey),  # admin has everything
    UserRole.AGENT: frozenset(),  # service account: no interactive access
}


class ProactiveOfferStatus(StrEnum):
    """Status lifecycle for ProactiveOffer records."""

    SENT = "sent"
    CONVERTED = "converted"
    EXPIRED = "expired"


class TicketStatus(StrEnum):
    """Status lifecycle for TroubleTicket records."""

    SUBMITTED = "submitted"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    WONT_FIX = "wont_fix"


class TicketSource(StrEnum):
    """Origin of a TroubleTicket."""

    REPORT_BUTTON = "report_button"
    TICKET_FORM = "ticket_form"


class BuyPlanStatus(StrEnum):
    """Buy plan header statuses."""

    DRAFT = "draft"
    PENDING = "pending"
    ACTIVE = "active"
    HALTED = "halted"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class SOVerificationStatus(StrEnum):
    """Sales Order verification by ops."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class BuyPlanLineStatus(StrEnum):
    """Per-line statuses tracking buyer execution."""

    AWAITING_PO = "awaiting_po"
    PENDING_VERIFY = "pending_verify"
    VERIFIED = "verified"
    ISSUE = "issue"
    CANCELLED = "cancelled"
    # Open claim pool — a cut PO was cancelled (vendor fell down) and the line is
    # back, unassigned, awaiting a NEW buyer/offer. See LineResourceReason.
    RESOURCING = "resourcing"


class LineIssueType(StrEnum):
    """Types of issues a buyer can flag on a line."""

    SOLD_OUT = "sold_out"
    PRICE_CHANGED = "price_changed"
    LEAD_TIME_CHANGED = "lead_time_changed"
    OTHER = "other"


class LineResourceReason(StrEnum):
    """Why a buyer is re-sourcing a line whose PO was cancelled.

    This flow is VENDOR-fall-down only (customer cancellations are handled elsewhere and
    never enter here), so every value here counts against the vendor's cancellation
    performance.
    """

    SOLD_ELSEWHERE = "sold_elsewhere"  # vendor sold the part to someone else
    CANNOT_DELIVER = "cannot_deliver"  # vendor can't fulfill the PO
    NO_STOCK = "no_stock"  # stock evaporated after the PO was cut
    PRICE_CHANGE = "price_change"  # vendor repriced / reneged
    OTHER = "other"


# Maps a re-source reason to the VendorPartUnavailability reason used when the
# canceled vendor is auto-marked unavailable for the part. Most fall-downs read
# as "vendor sold them"; explicit reasons map through where one exists.
RESOURCE_TO_UNAVAILABILITY_REASON: dict[str, str] = {
    LineResourceReason.SOLD_ELSEWHERE.value: "sold_elsewhere",
    LineResourceReason.CANNOT_DELIVER.value: "not_really_there",
    LineResourceReason.NO_STOCK.value: "not_really_there",
    LineResourceReason.PRICE_CHANGE.value: "sold_elsewhere",
    LineResourceReason.OTHER.value: "sold_elsewhere",
}


class POCancellationReason(StrEnum):
    """Why a cut PO was cancelled — the immutable vocabulary stored on
    POCancellation.reason_code.

    Mirrors LineResourceReason (the UI dropdown); kept as a separate enum so the durable
    record's vocabulary can evolve independently of the transient form.
    """

    SOLD_ELSEWHERE = "sold_elsewhere"
    CANNOT_DELIVER = "cannot_deliver"
    NO_STOCK = "no_stock"
    PRICE_CHANGE = "price_change"
    OTHER = "other"


class AIFlagSeverity(StrEnum):
    """Severity levels for AI-generated flags."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class RiskFlagType(StrEnum):
    """Types of risk flags that can be raised on a deal."""

    PRICE_INCREASE = "price_increase"
    LEAD_TIME_RISK = "lead_time_risk"
    VENDOR_RELIABILITY = "vendor_reliability"
    QTY_SHORTFALL = "qty_shortfall"
    GEO_RISK = "geo_risk"
    STALE_OFFER = "stale_offer"
    MARGIN_BELOW_THRESHOLD = "margin_below_threshold"
    SINGLE_SOURCE = "single_source"
    COUNTERFEIT_RISK = "counterfeit_risk"
    OTHER = "other"


class RiskFlagSeverity(StrEnum):
    """Severity levels for risk flags."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class ProspectAccountStatus(StrEnum):
    """Status lifecycle for ProspectAccount records in the prospect pool."""

    SUGGESTED = "suggested"
    CLAIMED = "claimed"
    DISMISSED = "dismissed"
    CONVERTED = "converted"


class CompanyDisposition(StrEnum):
    """Salesperson-set lifecycle disposition for a Company.

    NULL ⇒ active (mirrors tier's NULL ⇒ standard). "bucket" is the parking lot — a
    bucketed account is suppressed from the "needs a call" call-list (count + click-
    through) but stays findable/un-bucketable via the explicit Bucket facet. Never
    overloaded onto is_active.
    """

    ACTIVE = "active"
    BUCKET = "bucket"


class TaskStatus(StrEnum):
    """Status lifecycle for RequisitionTask records."""

    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class PendingBatchStatus(StrEnum):
    """Status lifecycle for PendingBatch (Anthropic Batch API) records."""

    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ApiSourceStatus(StrEnum):
    """ApiSource.status — managed by health_monitor.ping_source.

    Single source of truth for the api_sources.status string column.
    health_monitor.ping_source is the only writer of LIVE / ERROR. DISABLED is set when
    no connector is available for the source. DEGRADED is reserved for future
    ConnectorRateLimitError handling where the source should be auto-retry-after-window
    without exclusion from user searches.
    """

    PENDING = "pending"
    LIVE = "live"
    ERROR = "error"
    DEGRADED = "degraded"
    DISABLED = "disabled"


class SourceRunStatus(StrEnum):
    """Per-search-run status for source_stats[i] entries.

    Returned to the streaming search response so the per-source chip strip in the UI can
    render the right state (green / red / dim / pulsing).

    error_skipped means the source was excluded from this run because health_monitor
    previously flipped its ApiSource.status to ERROR; the operator sees a distinct chip
    with an actionable message.
    """

    OK = "ok"
    ERROR = "error"
    ERROR_SKIPPED = "error_skipped"
    SKIPPED = "skipped"
    DISABLED = "disabled"


class SpecCodeSource(StrEnum):
    """Provenance of an ``oem_spec_codes.source`` row — how the approved mapping entered
    the authoritative table.

    Single source of truth for the ``OemSpecCode.source`` string column
    (validated via ``@validates`` on the model). These are the *stored*
    provenance values, distinct from the in-memory ``ResolverSource`` Literal
    (``table``/``llm``/``none``) that describes a single ``resolve()`` outcome.

    - MANUAL: a human typed the mapping directly.
    - LLM_APPROVED: an LLM proposal that a human approved in the pending queue.
    - CSV_IMPORT: bulk-loaded from a spreadsheet.
    """

    MANUAL = "manual"
    LLM_APPROVED = "llm_approved"
    CSV_IMPORT = "csv_import"


class RfqAttachmentStatus(StrEnum):
    """Per-datasheet status in the RFQ attachment pipeline."""

    ATTACHED = "attached"
    MISSING = "missing"
    OVERSIZED = "oversized"
    FETCH_ERROR = "fetch_error"


BROWSER_WORKER_SOURCES = frozenset({"icsource", "netcomponents", "thebrokersite"})
"""api_sources rows backed by queue-driven browser workers, not request/response
connectors.

These have no entry in `_get_connector_for_source`, so `health_monitor.ping_source` would
flip them to DISABLED on every 15-min run. Skip them in `run_health_checks` so the seed
applied at startup (`seed_browser_worker_sources`) survives. Their actual health is
tracked via `IcsWorkerStatus`/`NcWorkerStatus`/`TbfWorkerStatus` heartbeats.
"""


class ActivityType(StrEnum):
    """Canonical activity_log.activity_type values.

    All <= 20 chars (column width).
    """

    RFQ_SENT = "rfq_sent"
    EMAIL_RECEIVED = "email_received"
    CALL_LOGGED = "call_logged"
    STATUS_CHANGED = "status_changed"
    OFFER_CREATED = "offer_created"
    OFFER_STATUS_CHANGED = "offer_status_changed"
    SIGHTING_ADDED = "sighting_added"
    SALES_NOTE = "sales_note"
    TASK_COMPLETED = "task_completed"
    TASK_REOPENED = "task_reopened"
    ASSIGNMENT_CHANGED = "assignment_changed"
    STRATEGIC_VENDOR_EXPIRING = "strategic_expiring"  # 18 chars — fits String(20)
    # Communication / manual-entry types
    EMAIL_SENT = "email_sent"
    NOTE = "note"
    CONTACT_NOTE = "contact_note"
    # Buy-plan lifecycle
    BUYPLAN_APPROVED = "buyplan_approved"
    BUYPLAN_REJECTED = "buyplan_rejected"
    BUYPLAN_PENDING = "buyplan_pending"
    BUYPLAN_COMPLETED = "buyplan_completed"
    # Offer / quote lifecycle
    OFFER_PENDING_REVIEW = "offer_pending_review"  # exactly 20 chars
    NEW_OFFER = "new_offer"
    COMPETITIVE_QUOTE = "competitive_quote"
    BID_RECEIVED = "bid_received"
    # Sourcing / ownership / part / comms signals
    OWNERSHIP_WARNING = "ownership_warning"
    PROACTIVE_MATCH = "proactive_match"
    PART_STATUS_CHANGE = "part_status_change"
    TEAMS_MESSAGE = "teams_message"
    WECHAT_MESSAGE = "wechat_message"
    MEETING = "meeting"
    # Vendor+part unavailability knowledge (vendor_unavailability service)
    VENDOR_UNAVAILABLE = "vendor_unavailable"  # 18 chars — fits String(20)
    VENDOR_AVAILABLE = "vendor_available"
    # Re-source: a cut PO was cancelled (vendor fall-down) and the deal needs backfill
    RESOURCE_REQUESTED = "resource_requested"  # 18 chars — fits String(20)
    # Approval lifecycle (approval engine — Task 5)
    APPROVAL_REQUESTED = "aprvl_requested"  # 15 chars
    APPROVAL_APPROVED = "aprvl_approved"  # 14 chars
    APPROVAL_REJECTED = "aprvl_rejected"  # 14 chars
    APPROVAL_DELEGATED = "aprvl_delegated"  # 15 chars
    APPROVAL_CANCELLED = "aprvl_cancelled"  # 15 chars


class CallOutcome(StrEnum):
    """Outcome values for a phone call, stamped into ActivityLog.details."""

    CONNECTED = "connected"
    LEFT_MESSAGE = "left_message"
    VOICEMAIL = "voicemail"
    NO_ANSWER = "no_answer"


# Outcomes that constitute a meaningful outreach touch (cadence clock advances).
# CONNECTED: live conversation confirmed. LEFT_MESSAGE: human voicemail recorded.
# VOICEMAIL / NO_ANSWER are not meaningful (no human contact made).
# NOTE: 8x8 CDR only emits CONNECTED and NO_ANSWER — adding LEFT_MESSAGE here
# does not change 8x8 behaviour; it makes manually-picked LEFT_MESSAGE consistent.
MEANINGFUL_CALL_OUTCOMES: frozenset[str] = frozenset({CallOutcome.CONNECTED, CallOutcome.LEFT_MESSAGE})


class Channel(StrEnum):
    """Canonical activity_log.channel values (the medium the activity came through)."""

    SYSTEM = "system"
    PHONE = "phone"
    MANUAL = "manual"
    EMAIL = "email"
    CHROME = "chrome"
    TEAMS = "teams"
    OUTLOOK = "outlook"
    CALENDAR = "calendar"
    AVAIL_SYSTEM = "avail_system"
    WECHAT = "wechat"


class OutreachChannel(StrEnum):
    """Click-to-contact outreach channels (CDM contact panel buttons)."""

    PHONE = "phone"
    EMAIL = "email"
    TEAMS = "teams"
    WECHAT = "wechat"


class ContactRole(StrEnum):
    """Canonical CRM site-contact buying-role taxonomy (Mike's vocabulary).

    Single source of truth for the role dropdown on the customer-tab contact card.
    `tuple(ContactRole)` drives both CANONICAL_ROLES (app/routers/htmx_views.py) and
    the `roles` Jinja2 global fallback (app/template_env.py). Stored in
    site_contacts.contact_role (String(50)); legacy DB values (buyer_po/specifier/
    ap_payer/logistics/exec/technical/decision_maker/operations) are NOT in this set —
    they render via the legacy display-label maps but can only be cleared, not re-saved.
    """

    BUYER = "buyer"
    MANAGER = "manager"
    ENGINEER = "engineer"
    PLANNER = "planner"
    OTHER = "other"


class EventType(StrEnum):
    """Canonical activity_log.event_type values (Communication-Intelligence kind)."""

    EMAIL = "email"
    CALL = "call"
    MESSAGE = "message"
    MEETING = "meeting"
    API_SOURCE_DOWN = "api_source_down"
    API_QUOTA_WARNING = "api_quota_warning"
    API_QUOTA_CRITICAL = "api_quota_critical"


class Direction(StrEnum):
    """Canonical stored activity_log.direction values.

    Writers may pass the input synonyms ``sent`` / ``received`` to log_email_activity /
    log_call_activity, which normalize them to these stored values. Genuinely-unknown
    direction is stored as NULL, never a sentinel string.
    """

    INBOUND = "inbound"
    OUTBOUND = "outbound"


class DigestEntityType(StrEnum):
    """Entity kinds an ActivityDigest can summarize."""

    REQUISITION = "requisition"
    COMPANY = "company"


class DigestStatusSignal(StrEnum):
    """Digest semantic state — drives the card's color."""

    ON_TRACK = "on_track"
    STALLED = "stalled"
    NEEDS_ATTENTION = "needs_attention"


class InboxSyncHealth(StrEnum):
    """Inbox-sync health for the Settings card and disconnected banner."""

    OK = "ok"
    WARNING = "warning"
    ERROR = "error"


class MaterialEnrichmentStatus(StrEnum):
    """Enrichment tier for MaterialCard.enrichment_status.

    Single source of truth for the seven valid enrichment tiers. Enforced at the ORM
    layer via @validates on MaterialCard.
    """

    UNENRICHED = "unenriched"
    VERIFIED = "verified"
    WEB_SOURCED = "web_sourced"
    AI_INFERRED = "ai_inferred"
    NOT_FOUND = "not_found"
    OEM_SOURCED = "oem_sourced"
    NOT_CATALOGUED = "not_catalogued"


class MaterialCondition(StrEnum):
    """Canonical stock-condition vocabulary for MaterialCard.condition.

    Single source of truth for the column's documented value set — the model comment
    (models/intelligence.py), the Condition global facet template, and the SP-Ingest
    condition canonicalizer (services/source_ingest/clean.py) all speak this vocabulary.
    Application-validated, no DB CHECK. NOTE: this is the card/facet vocabulary;
    ``app.utils.normalization.normalize_condition`` is a separate lowercase vocab
    (new/refurb/used) for search-result/offer rows — do not conflate the two.
    """

    NEW = "New"
    RECERTIFIED = "Recertified"
    REFURBISHED = "Refurbished"
    USED = "Used"
    PULLED = "Pulled"
    UNKNOWN = "Unknown"


class FruLinkKind(StrEnum):
    """Relationship kind for FruLink rows (IBM/Lenovo FRU crosswalk).

    Single source of truth for the valid fru_links.rel_kind values. Enforced via
    @validates on FruLink for ORM construction AND re-validated by the ingest's bulk
    upsert path (a Core insert, which bypasses ORM events).
    """

    IBM_11S = "ibm_11s"  # IBM 11S part number stamped on the part
    MFG_MODEL = "mfg_model"  # Manufacturer model / MPN (e.g. SSDSC2BB120G4I)
    OPTION = "option"  # IBM/Lenovo option number
    OPTION_PN = "option_pn"  # Option part number
    SOURCING_PN = "sourcing_pn"  # Additional sourcing / make-to-label numbers
    LENOVO_PN = "lenovo_pn"  # Lenovo / Idea part number
    LENOVO_PPN = "lenovo_ppn"  # Lenovo PPN (FRU-PPN BOM)
    TRAY = "tray"  # Carrier / tray part number
    TRAY_ALT = "tray_alt"  # Alternate carrier / tray
    BRACKET = "bracket"  # Mounting bracket
    BOARD = "board"  # Interposer / carrier board
    SCREWS = "screws"  # Mounting screws
    SHUTTLE = "shuttle"  # NetApp shuttle
    DONGLE = "dongle"  # NetApp dongle
    DRIVE_PN = "drive_pn"  # Bare drive part number (qual lists)
    ASSEMBLY = "assembly"  # Assembly part number


# fru_links.qual_status is otherwise free text from the workbook's qual column
# ("qlot approved", "qlot approved - Only EMEA", ...). CDC_PENDING is the single
# app-synthesized sentinel (CDC pending-qualification sheet); the FRU panels render
# it as the amber "CDC pending" pill — keep ingest and display on this constant.
CDC_PENDING = "cdc_pending"

# Provenance value in a Requirement.substitutes entry's optional "source" key:
# the substitute was system-derived from the FRU crosswalk by search_service's
# alias expansion (vs entered by a user). Written by
# search_service._persist_fru_aliases, preserved by parse_substitute_mpns, read
# by template_env's |fru_alias_mpns filter for the "via FRU crosswalk" tooltip.
FRU_ALIAS_SOURCE = "fru_crosswalk"


class UnavailabilityReason(StrEnum):
    """Why a (vendor, part) pair is durably unavailable.

    Single source of truth for VendorPartUnavailability.reason values AND their
    display labels (``.label``) — templates and services render labels through
    the property, never duplicate the strings.
    """

    BOUGHT_BY_US = "bought_by_us"
    SOLD_ELSEWHERE = "sold_elsewhere"
    BROKEN = "broken"
    NOT_REALLY_THERE = "not_really_there"
    DIFFERENT_PART = "different_part"
    OTHER = "other"

    @property
    def label(self) -> str:
        """Human display label for this reason."""
        return _UNAVAILABILITY_REASON_LABELS[self]


# Display labels for UnavailabilityReason, kept beside the enum. The .label property
# is the only reader — templates/services go through it, never duplicate these strings.
_UNAVAILABILITY_REASON_LABELS: dict[UnavailabilityReason, str] = {
    UnavailabilityReason.BOUGHT_BY_US: "We bought them",
    UnavailabilityReason.SOLD_ELSEWHERE: "Vendor sold them",
    UnavailabilityReason.BROKEN: "Broken / bad condition",
    UnavailabilityReason.NOT_REALLY_THERE: "Not really in stock",
    UnavailabilityReason.DIFFERENT_PART: "Different part number",
    UnavailabilityReason.OTHER: "Other",
}


class ReleaseTrigger(StrEnum):
    """How a VendorPartUnavailability record was released — the closed vocabulary for
    ``release_trigger``.

    Written ONLY by override O3 (buyer-routed vendor email) and the offer hook;
    enforced via @validates on the model. ``.label`` is the display fragment the
    advisory row hint renders ("released by <label>").
    """

    VENDOR_EMAIL = "vendor_email"
    OFFER_RECEIVED = "offer_received"

    @property
    def label(self) -> str:
        """Human display fragment for this trigger."""
        return _RELEASE_TRIGGER_LABELS[self]


_RELEASE_TRIGGER_LABELS: dict[ReleaseTrigger, str] = {
    ReleaseTrigger.VENDOR_EMAIL: "vendor email",
    ReleaseTrigger.OFFER_RECEIVED: "offer",
}


class OemCrosswalkStatus(StrEnum):
    """Status of an ``oem_crosswalk`` cache row (OEM web resolution, migration 101).

    Single source of truth for the only two valid states — a resolver trust-gate
    failure IS ``no_match`` (there is no separate "gate_failed" state). ``resolved``
    rows are permanent (never re-fetched); ``no_match`` rows block re-resolution for
    90 days from ``looked_up_at`` and are updated in place on retry. Enforced via
    @validates on OemCrosswalk.
    """

    RESOLVED = "resolved"
    NO_MATCH = "no_match"


class AlertKind(StrEnum):
    """alert_seen.alert_kind values — which cross-app alert a seen-row belongs to.

    FYI kinds (offer_confirmed, inbound_customer, inbound_vendor) clear on see — the
    badge count excludes seen rows. ACTION kinds (buyplan_action) clear on act — seen
    rows only suppress the one-time in-tab spotlight pulse, never the count.
    """

    OFFER_CONFIRMED = "offer_confirmed"
    INBOUND_CUSTOMER = "inbound_customer"
    INBOUND_VENDOR = "inbound_vendor"
    BUYPLAN_ACTION = "buyplan_action"
    TASKS_ACTION = "tasks_action"
    APPROVAL_ACTION = "approval_action"
    # Open re-sourcing pool — ACTION temperament, count from work-state (unclaimed
    # RESOURCING lines), seen-row only suppresses the in-tab spotlight pulse.
    BUYPLAN_RESOURCING = "buyplan_resourcing"


class SightingsSkipReason(StrEnum):
    """Advisory skip-reason for a vendor in the sightings RFQ modal / preview.

    Computed up-front so the compose and preview steps can show WHY a vendor will be
    skipped. The authoritative skip stays in send_batch_rfq (TOCTOU guard) — this enum
    is advisory only and never gates the actual send.

    Unavailable vendors are partitioned out *before* the per-entry skip_reason loop
    and placed in a separate ``unavailable_vendors`` list, so they never reach the
    previews list and this enum never carries an UNAVAILABLE value.

    READY          — vendor has a resolvable email and is not DNC (green / no badge).
    NO_EMAIL       — no resolvable VendorContact email (amber badge).
    DO_NOT_CONTACT — vendor contact email matches a do_not_contact SiteContact
                     (rose badge).
    """

    READY = "ready"
    NO_EMAIL = "no_email"
    DO_NOT_CONTACT = "do_not_contact"


# ---------------------------------------------------------------------------
# Approvals Engine + Quality Plan constants (Phase 1)
# ---------------------------------------------------------------------------


class ApprovalGateType(StrEnum):
    """Which approval gate a request belongs to.

    Single source of truth for the approval_requests.gate_type column. Each value names
    the workflow step that triggered the approval.
    """

    BUY_PLAN = "buy_plan"
    PREPAYMENT = "prepayment"
    QP_SALES = "qp_sales"
    PURCHASE_ORDER = "purchase_order"


class ApprovalSubjectType(StrEnum):
    """Which entity an ApprovalRequest is about (polymorphic subject).

    Single source of truth for the approval_requests.subject_type column. The
    (subject_type, subject_id) pair points back at the originating entity without a
    cross-table FK (mirrors MaterialCardAudit.material_card_id). BUY_PLAN (QP Phase C1)
    routes the live buy-plan gate through the engine; QUOTE and RESELL_OFFER follow in
    later phases when their fan-out lands.
    """

    QUALITY_PLAN = "quality_plan"
    PREPAYMENT = "prepayment"
    BUY_PLAN = "buy_plan"


class ApprovalRequestStatus(StrEnum):
    """Lifecycle state of an ApprovalRequest row.

    requested  — created, awaiting at least one recipient to act. approved   — all
    required recipients approved (or any, per rule). rejected   — at least one required
    recipient rejected. cancelled  — withdrawn by the requester before resolution.
    expired    — deadline passed without resolution.
    """

    REQUESTED = "requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class ApprovalRecipientStatus(StrEnum):
    """Per-recipient decision state within an ApprovalRequest.

    pending    — assigned, has not yet responded. approved   — this recipient approved.
    rejected   — this recipient rejected. reassigned — forwarded to another user; the
    original row is superseded.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REASSIGNED = "reassigned"


class ApprovalStepRule(StrEnum):
    """Quorum rule for a step in the approval chain.

    any — one approval from the recipient pool resolves the step. all — every recipient
    in the pool must approve.
    """

    ANY = "any"
    ALL = "all"


class PaymentMethod(StrEnum):
    """Payment method options for purchase orders and buy plans.

    Single source of truth for payment_method columns across models.
    """

    CC = "cc"
    PAYPAL = "paypal"
    WIRE = "wire"


class SourcingType(StrEnum):
    """Sourcing strategy classification for a buy plan or line item.

    spot       — one-time open-market purchase. contract   — negotiated long-term supply
    agreement. commodity  — standard commodity purchase. preferred  — preferred-vendor
    program purchase.
    """

    SPOT = "spot"
    CONTRACT = "contract"
    COMMODITY = "commodity"
    PREFERRED = "preferred"


class QualityPlanStatus(StrEnum):
    """Lifecycle state of a QualityPlan document.

    draft      — being authored, not yet submitted for review. in_review  — submitted;
    reviewer(s) have been assigned. approved   — all required reviewers approved the
    plan. rejected   — one or more reviewers rejected; requires revision.
    """

    DRAFT = "draft"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class QPOrderType(StrEnum):
    """Whether a QualityPlan is for a new order or a revision to an existing one.

    new      — first-time quality plan for this part / supplier pair. revision — updated
    plan superseding a previously approved version.
    """

    NEW = "new"
    REVISION = "revision"
