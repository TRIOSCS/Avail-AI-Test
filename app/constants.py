"""Centralized StrEnum constants for stringly-typed status fields.

Replaces scattered string literals with type-safe enums that are
drop-in compatible (StrEnum members compare equal to their string values).

Single source of truth — supersedes the older app/enums.py (str, Enum) style.

Called by: models, routers, services
Depends on: nothing (leaf module)
"""

from enum import StrEnum


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


class MatchMethod(StrEnum):
    """How an inbound email was matched to an outbound RFQ contact."""

    CONVERSATION_ID = "conversation_id"
    SUBJECT_TOKEN = "subject_token"
    SUBJECT_TOKEN_REQ_ONLY = "subject_token_req_only"
    EMAIL_EXACT = "email_exact"
    DOMAIN = "domain"
    UNMATCHED = "unmatched"


class OfferSource(StrEnum):
    """Origin of an Offer record."""

    EMAIL_PARSE = "email_parse"
    MANUAL = "manual"
    SEARCH = "search"
    HISTORICAL = "historical"
    VENDOR_AFFINITY = "vendor_affinity"
    AI_PARSED = "ai_parsed"
    AI_LOOKUP = "ai_lookup"


class ResponseClassification(StrEnum):
    """AI-derived classification of a vendor's email response."""

    QUOTE_PROVIDED = "quote_provided"
    NO_STOCK = "no_stock"
    OOO_BOUNCE = "ooo_bounce"
    COUNTER_OFFER = "counter_offer"
    CLARIFICATION_NEEDED = "clarification_needed"
    PARTIAL_AVAILABILITY = "partial_availability"
    FOLLOW_UP_PENDING = "follow_up_pending"


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


class ProactiveOfferStatus(StrEnum):
    """Status lifecycle for ProactiveOffer records."""

    SENT = "sent"
    CONVERTED = "converted"


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


class DiscoveryBatchStatus(StrEnum):
    """Status lifecycle for DiscoveryBatch (prospect discovery run) records."""

    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
