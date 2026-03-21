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

    ACTIVE = "active"
    APPROVED = "approved"
    REJECTED = "rejected"
    SOLD = "sold"
    WON = "won"


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
