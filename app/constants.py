"""Centralized StrEnum constants for stringly-typed status fields.

Replaces scattered string literals with type-safe enums that are
drop-in compatible (StrEnum members compare equal to their string values).

Single source of truth — supersedes the older app/enums.py (str, Enum) style.

Called by: models, routers, services
Depends on: nothing (leaf module)
"""

from enum import StrEnum, nonmember


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


class RequisitionStatus(StrEnum):
    """Status lifecycle for Requisition records."""

    DRAFT = "draft"
    ACTIVE = "active"
    SOURCING = "sourcing"
    OFFERS = "offers"
    QUOTING = "quoting"
    QUOTED = "quoted"
    REOPENED = "reopened"
    WON = "won"
    LOST = "lost"
    ARCHIVED = "archived"
    CANCELLED = "cancelled"

    # Statuses considered "done" — excluded from re-archiving and shown under
    # the Archive filter. Single source of truth for terminal-status checks.
    # `nonmember` keeps this off the enum's member list (it's a constant, not a
    # status value). StrEnum members compare equal to their string values, so
    # `status.in_(RequisitionStatus.TERMINAL)` matches correctly.
    TERMINAL = nonmember(frozenset({"archived", "won", "lost", "cancelled"}))


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
    """Status lifecycle for ExcessList records."""

    DRAFT = "draft"
    ACTIVE = "active"
    BIDDING = "bidding"
    CLOSED = "closed"
    EXPIRED = "expired"


class ExcessLineItemStatus(StrEnum):
    """Status lifecycle for ExcessLineItem records."""

    AVAILABLE = "available"
    BIDDING = "bidding"
    AWARDED = "awarded"
    WITHDRAWN = "withdrawn"


class BidStatus(StrEnum):
    """Status lifecycle for Bid records."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"
    WITHDRAWN = "withdrawn"


class BidSolicitationStatus(StrEnum):
    """Status lifecycle for BidSolicitation records."""

    PENDING = "pending"
    SENT = "sent"
    RESPONDED = "responded"
    EXPIRED = "expired"
    FAILED = "failed"


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


class LineIssueType(StrEnum):
    """Types of issues a buyer can flag on a line."""

    SOLD_OUT = "sold_out"
    PRICE_CHANGED = "price_changed"
    LEAD_TIME_CHANGED = "lead_time_changed"
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


BROWSER_WORKER_SOURCES = frozenset({"icsource", "netcomponents"})
"""api_sources rows backed by queue-driven browser workers, not request/response
connectors.

These have no entry in `_get_connector_for_source`, so `health_monitor.ping_source` would
flip them to DISABLED on every 15-min run. Skip them in `run_health_checks` so the seed
applied at startup (`seed_browser_worker_sources`) survives. Their actual health is
tracked via `IcsWorkerStatus`/`NcWorkerStatus` heartbeats.
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
    REQ_ARCHIVED = "req_archived"
    REQ_UNARCHIVED = "req_unarchived"
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


class EventType(StrEnum):
    """Canonical activity_log.event_type values (Communication-Intelligence kind)."""

    EMAIL = "email"
    CALL = "call"
    MESSAGE = "message"
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
