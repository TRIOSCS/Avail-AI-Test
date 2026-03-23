"""status_machine.py — State machine validation for domain entity status transitions.

Purpose: Prevents invalid status transitions on offers, quotes, requisitions,
         and buy plans. Enforces valid state transitions and provides clear error
         messages when invalid transitions are attempted.

Called by: routers/crm/offers.py, routers/crm/quotes.py, routers/crm/buy_plans.py
Depends on: nothing (pure logic)
"""

from loguru import logger

from ..constants import (
    BuyPlanStatus,
    OfferStatus,
    QuoteStatus,
    RequisitionStatus,
)

# ── Offer Status Transitions ────────────────────────────────────────────
# Valid transitions: from_status → {allowed to_statuses}
OFFER_TRANSITIONS: dict[str, set[str]] = {
    OfferStatus.PENDING_REVIEW: {OfferStatus.ACTIVE, OfferStatus.REJECTED},
    OfferStatus.ACTIVE: {OfferStatus.SOLD, OfferStatus.REJECTED, OfferStatus.WON, OfferStatus.EXPIRED},
    OfferStatus.WON: {OfferStatus.SOLD},
    OfferStatus.REJECTED: set(),  # terminal
    OfferStatus.SOLD: set(),  # terminal
    OfferStatus.EXPIRED: {OfferStatus.ACTIVE},  # can be reactivated
}

# ── Quote Status Transitions ────────────────────────────────────────────
QUOTE_TRANSITIONS: dict[str, set[str]] = {
    QuoteStatus.DRAFT: {QuoteStatus.SENT, QuoteStatus.REVISED, QuoteStatus.WON, QuoteStatus.LOST},
    QuoteStatus.SENT: {QuoteStatus.REVISED, QuoteStatus.WON, QuoteStatus.LOST},
    QuoteStatus.REVISED: {QuoteStatus.SENT, QuoteStatus.WON, QuoteStatus.LOST},
    QuoteStatus.WON: set(),  # terminal
    QuoteStatus.LOST: {QuoteStatus.DRAFT},  # can be re-opened
}

# ── Buy Plan Status Transitions ─────────────────────────────────────────
BUY_PLAN_TRANSITIONS: dict[str, set[str]] = {
    BuyPlanStatus.DRAFT: {BuyPlanStatus.PENDING, BuyPlanStatus.CANCELLED},
    BuyPlanStatus.PENDING: {BuyPlanStatus.ACTIVE, BuyPlanStatus.CANCELLED, BuyPlanStatus.DRAFT},
    BuyPlanStatus.ACTIVE: {BuyPlanStatus.COMPLETED, BuyPlanStatus.HALTED, BuyPlanStatus.CANCELLED},
    BuyPlanStatus.HALTED: {BuyPlanStatus.DRAFT, BuyPlanStatus.CANCELLED},
    BuyPlanStatus.COMPLETED: set(),  # terminal
    BuyPlanStatus.CANCELLED: {BuyPlanStatus.DRAFT},  # can be reset
}

# ── Requisition Status Transitions ──────────────────────────────────────
REQUISITION_TRANSITIONS: dict[str, set[str]] = {
    RequisitionStatus.DRAFT: {
        RequisitionStatus.ACTIVE,
        RequisitionStatus.SOURCING,
        RequisitionStatus.ARCHIVED,
        RequisitionStatus.CANCELLED,
    },
    RequisitionStatus.ACTIVE: {
        RequisitionStatus.SOURCING,
        RequisitionStatus.OFFERS,
        RequisitionStatus.QUOTING,
        RequisitionStatus.WON,
        RequisitionStatus.LOST,
        RequisitionStatus.ARCHIVED,
        RequisitionStatus.CANCELLED,
    },
    RequisitionStatus.SOURCING: {
        RequisitionStatus.ACTIVE,
        RequisitionStatus.OFFERS,
        RequisitionStatus.QUOTING,
        RequisitionStatus.WON,
        RequisitionStatus.LOST,
        RequisitionStatus.ARCHIVED,
        RequisitionStatus.CANCELLED,
    },
    RequisitionStatus.OFFERS: {
        RequisitionStatus.ACTIVE,
        RequisitionStatus.SOURCING,
        RequisitionStatus.QUOTING,
        RequisitionStatus.WON,
        RequisitionStatus.LOST,
        RequisitionStatus.ARCHIVED,
        RequisitionStatus.CANCELLED,
    },
    RequisitionStatus.QUOTING: {
        RequisitionStatus.ACTIVE,
        RequisitionStatus.SOURCING,
        RequisitionStatus.OFFERS,
        RequisitionStatus.WON,
        RequisitionStatus.LOST,
        RequisitionStatus.ARCHIVED,
        RequisitionStatus.CANCELLED,
    },
    RequisitionStatus.WON: {RequisitionStatus.ACTIVE, RequisitionStatus.ARCHIVED},
    RequisitionStatus.LOST: {RequisitionStatus.ACTIVE, RequisitionStatus.ARCHIVED},
    RequisitionStatus.ARCHIVED: {RequisitionStatus.ACTIVE, RequisitionStatus.DRAFT},
    RequisitionStatus.CANCELLED: {RequisitionStatus.ACTIVE, RequisitionStatus.DRAFT},
}


def validate_transition(
    entity_type: str,
    current_status: str | None,
    new_status: str,
    *,
    raise_on_invalid: bool = True,
) -> bool:
    """Validate a status transition for a given entity type.

    Returns True if valid, raises ValueError if invalid (when raise_on_invalid=True).
    Unknown current statuses are treated as allowing any transition (with a warning).
    """
    transition_map = {
        "offer": OFFER_TRANSITIONS,
        "quote": QUOTE_TRANSITIONS,
        "buy_plan": BUY_PLAN_TRANSITIONS,
        "requisition": REQUISITION_TRANSITIONS,
    }

    transitions = transition_map.get(entity_type)
    if not transitions:
        logger.warning("Unknown entity type for status validation: {}", entity_type)
        return True

    if current_status == new_status:
        return True  # no-op transition always valid

    if current_status is None or current_status not in transitions:
        logger.warning(
            "Unknown {} status '{}' — allowing transition to '{}'",
            entity_type,
            current_status,
            new_status,
        )
        return True

    allowed = transitions[current_status]
    if new_status not in allowed:
        msg = (
            f"Invalid {entity_type} status transition: '{current_status}' → '{new_status}'. "
            f"Allowed transitions from '{current_status}': {', '.join(sorted(allowed)) or 'none (terminal state)'}"
        )
        if raise_on_invalid:
            raise ValueError(msg)
        logger.warning(msg)
        return False

    return True
