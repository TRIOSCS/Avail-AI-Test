"""Centralized StrEnum constants for stringly-typed status fields.

Replaces scattered string literals with type-safe enums that are
drop-in compatible (StrEnum members compare equal to their string values).

Called by: models/intelligence.py, services/proactive_matching.py,
           email_service.py, routers/proactive.py
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
