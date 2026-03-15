"""enums.py — Canonical status values for core domain models.

Replaces string literals scattered across routers/services with type-safe enums.
StrEnum so enum values compare equal to their string representation.

Called by: routers, services, requisition_state.py
Depends on: nothing
"""

import enum


class RequisitionStatus(str, enum.Enum):
    draft = "draft"
    active = "active"
    sourcing = "sourcing"
    offers = "offers"
    quoting = "quoting"
    quoted = "quoted"
    reopened = "reopened"
    won = "won"
    lost = "lost"
    archived = "archived"


class OfferStatus(str, enum.Enum):
    active = "active"
    rejected = "rejected"
    sold = "sold"
    won = "won"


class QuoteStatus(str, enum.Enum):
    draft = "draft"
    sent = "sent"
    won = "won"
    lost = "lost"
    revised = "revised"


class RequirementSourcingStatus(str, enum.Enum):
    """Per-part sourcing status within a requisition.

    Tracks where each individual part is in the sourcing → quoting pipeline. Parts
    within one requisition can be at different stages.
    """

    open = "open"  # Not yet being sourced
    sourcing = "sourcing"  # Buyer is actively contacting vendors
    offered = "offered"  # At least one confirmed offer exists
    quoted = "quoted"  # Part included in a customer quote
    won = "won"  # Customer accepted quote for this part
    lost = "lost"  # Customer declined / no stock found


class ContactStatus(str, enum.Enum):
    """RFQ outbound contact status."""

    sent = "sent"
    failed = "failed"
    opened = "opened"
    responded = "responded"
    quoted = "quoted"
    declined = "declined"
    ooo = "ooo"
    bounced = "bounced"
    retried = "retried"


class VendorResponseStatus(str, enum.Enum):
    """Vendor response queue status."""

    new = "new"
    reviewed = "reviewed"
    rejected = "rejected"


class UserRole(str, enum.Enum):
    buyer = "buyer"
    sales = "sales"
    trader = "trader"
    manager = "manager"
    admin = "admin"
