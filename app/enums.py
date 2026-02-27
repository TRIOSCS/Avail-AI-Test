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


class UserRole(str, enum.Enum):
    buyer = "buyer"
    sales = "sales"
    trader = "trader"
    manager = "manager"
    admin = "admin"
