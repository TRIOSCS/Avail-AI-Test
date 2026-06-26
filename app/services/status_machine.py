"""status_machine.py — State machine validation for domain entity status transitions.

Purpose: Prevents invalid status transitions on offers, quotes, requisitions,
         and buy plans. Enforces valid state transitions and provides clear error
         messages when invalid transitions are attempted.

Called by: routers/crm/offers.py, routers/crm/quotes.py
Depends on: nothing (pure logic)
"""

from fastapi import HTTPException
from loguru import logger

from ..constants import (
    BuyPlanStatus,
    OfferStatus,
    QuoteStatus,
    RequisitionStatus,
    SourcingStatus,
)

# ── Offer Status Transitions ────────────────────────────────────────────
# Valid transitions: from_status → {allowed to_statuses}
OFFER_TRANSITIONS: dict[str, set[str]] = {
    OfferStatus.PENDING_REVIEW: {OfferStatus.ACTIVE, OfferStatus.APPROVED, OfferStatus.REJECTED, OfferStatus.SOLD},
    OfferStatus.ACTIVE: {OfferStatus.SOLD, OfferStatus.REJECTED, OfferStatus.WON, OfferStatus.EXPIRED},
    OfferStatus.APPROVED: {OfferStatus.SOLD, OfferStatus.REJECTED, OfferStatus.WON, OfferStatus.EXPIRED},
    OfferStatus.WON: {OfferStatus.SOLD},
    OfferStatus.REJECTED: set(),  # terminal
    OfferStatus.SOLD: set(),  # terminal
    OfferStatus.EXPIRED: {OfferStatus.ACTIVE},  # can be reactivated
}

# ── Quote Status Transitions ────────────────────────────────────────────
QUOTE_TRANSITIONS: dict[str, set[str]] = {
    QuoteStatus.DRAFT: {QuoteStatus.SENT, QuoteStatus.REVISED, QuoteStatus.WON, QuoteStatus.LOST},
    QuoteStatus.SENT: {QuoteStatus.DRAFT, QuoteStatus.REVISED, QuoteStatus.WON, QuoteStatus.LOST},
    QuoteStatus.REVISED: {QuoteStatus.SENT, QuoteStatus.WON, QuoteStatus.LOST},
    QuoteStatus.WON: {QuoteStatus.DRAFT, QuoteStatus.REVISED, QuoteStatus.SENT},  # can be re-opened
    QuoteStatus.LOST: {QuoteStatus.DRAFT, QuoteStatus.REVISED, QuoteStatus.SENT},  # can be re-opened
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
# Mirror of app/services/requisition_state.ALLOWED_TRANSITIONS — the Sales Hub
# pipeline. There is no archive/hide capability; a req ends in WON or LOST.
REQUISITION_TRANSITIONS: dict[str, set[str]] = {
    RequisitionStatus.DRAFT: {
        RequisitionStatus.OPEN,
        RequisitionStatus.HOTLIST,
    },
    RequisitionStatus.OPEN: {
        RequisitionStatus.RFQS_SENT,
        RequisitionStatus.OFFERS,
        RequisitionStatus.QUOTED,
        RequisitionStatus.WON,
        RequisitionStatus.LOST,
        RequisitionStatus.HOTLIST,
    },
    RequisitionStatus.RFQS_SENT: {
        RequisitionStatus.OPEN,
        RequisitionStatus.OFFERS,
        RequisitionStatus.QUOTED,
        RequisitionStatus.WON,
        RequisitionStatus.LOST,
        RequisitionStatus.HOTLIST,
    },
    RequisitionStatus.OFFERS: {
        RequisitionStatus.OPEN,
        RequisitionStatus.QUOTED,
        RequisitionStatus.WON,
        RequisitionStatus.LOST,
        RequisitionStatus.HOTLIST,
    },
    RequisitionStatus.QUOTED: {
        RequisitionStatus.OPEN,
        RequisitionStatus.OFFERS,
        RequisitionStatus.WON,
        RequisitionStatus.LOST,
        RequisitionStatus.HOTLIST,
    },
    RequisitionStatus.WON: {RequisitionStatus.OPEN},
    RequisitionStatus.LOST: {RequisitionStatus.OPEN, RequisitionStatus.HOTLIST},
    RequisitionStatus.HOTLIST: {
        RequisitionStatus.OPEN,
        RequisitionStatus.RFQS_SENT,
        RequisitionStatus.OFFERS,
        RequisitionStatus.QUOTED,
        RequisitionStatus.WON,
        RequisitionStatus.LOST,
    },
    RequisitionStatus.CANCELLED: {RequisitionStatus.OPEN},
}

# ── Sourcing Status Transitions (Requirement-level) ────────────────────
SOURCING_TRANSITIONS: dict[str, set[str]] = {
    SourcingStatus.OPEN: {SourcingStatus.SOURCING, SourcingStatus.ARCHIVED},
    SourcingStatus.SOURCING: {SourcingStatus.OFFERED, SourcingStatus.OPEN, SourcingStatus.ARCHIVED},
    SourcingStatus.OFFERED: {SourcingStatus.QUOTED, SourcingStatus.SOURCING, SourcingStatus.ARCHIVED},
    SourcingStatus.QUOTED: {SourcingStatus.WON, SourcingStatus.LOST, SourcingStatus.OFFERED, SourcingStatus.ARCHIVED},
    SourcingStatus.WON: {SourcingStatus.ARCHIVED},
    SourcingStatus.LOST: {SourcingStatus.OPEN, SourcingStatus.ARCHIVED},
    SourcingStatus.ARCHIVED: set(),  # terminal
}


def require_valid_transition(entity_type: str, current_status: str, new_status: str) -> None:
    """Validate a status transition or raise HTTPException 409."""
    try:
        validate_transition(entity_type, current_status, new_status)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


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
        "requirement": SOURCING_TRANSITIONS,
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
