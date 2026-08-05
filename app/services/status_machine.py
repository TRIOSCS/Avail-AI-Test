"""status_machine.py — State machine validation for domain entity status transitions.

Purpose: Prevents invalid status transitions on offers, quotes, and requirements
         (per-part sourcing status). Enforces valid state transitions and provides
         clear error messages when invalid transitions are attempted.

         Requisition transitions are enforced by requisition_state.transition
         (its ALLOWED_TRANSITIONS table); buy-plan transitions by
         buyplan_workflow/buyplan_state.transition (W3). Neither routes
         through this module.

Called by: services/offer_service.py (ONE offer transition site since W3),
           routers/htmx/quotes.py, routers/sightings.py, services/quote_send.py,
           services/po_cancellation_service.py, services/sourcing_auto_progress.py,
           services/requirement_status.py
Depends on: nothing (pure logic)
"""

from fastapi import HTTPException
from loguru import logger

from ..constants import (
    OfferStatus,
    QuoteStatus,
    SourcingStatus,
)

# ── Offer Status Transitions ────────────────────────────────────────────
# Valid transitions: from_status → {allowed to_statuses}
#
# INVARIANT: every path that transitions an offer INTO a live status
# (ACTIVE/APPROVED) from a non-live one (PENDING_REVIEW, EXPIRED) MUST call
# proactive_matching.trigger_rematch_on_offer_approval(db, offer) AFTER its
# commit. The batch scan only ever sees an offer once, at Offer.created_at, and
# excludes non-live statuses — so once the scan watermark passes a pending offer,
# only the targeted re-match can ever surface it to proactive matching. Since the
# W3 consolidation the ONE call site is services/offer_service.py approve_offer
# (which serves the JSON approve, the htmx review approve, and both promote
# doors).
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

# ── Sourcing Status Transitions (Requirement-level) ────────────────────
# SINGLE SOURCE OF TRUTH for per-part sourcing transition legality. Both
# validators route through this table: validate_transition("requirement", …)
# (below) and requirement_status.transition_requirement (which imports this as
# ALLOWED_TRANSITIONS). Do not fork it — keep the two in sync by reference.
#
# Reconciliation (2026-07): this table previously diverged from
# requirement_status.ALLOWED_TRANSITIONS. The permissive (superset) definition
# won because the authoritative event handlers require skip-ahead legality:
# on_offer_created advances open/sourcing → offered and on_quote_built advances
# open/sourcing/offered → quoted (offers/quotes can arrive on a part that never
# had an RFQ sent — e.g. inbound email mining). Under the old restrictive table
# those transitions were rejected and silently skipped, leaving a part that is
# on a confirmed offer or customer quote still displayed as "open" — a data
# integrity bug. Un-archive (archived → open) is likewise legal because a
# requirement is re-openable (unlike a terminal offer).
SOURCING_TRANSITIONS: dict[str, set[str]] = {
    SourcingStatus.OPEN: {
        SourcingStatus.SOURCING,
        SourcingStatus.OFFERED,
        SourcingStatus.QUOTED,
        SourcingStatus.WON,
        SourcingStatus.LOST,
        SourcingStatus.ARCHIVED,
    },
    SourcingStatus.SOURCING: {
        SourcingStatus.OFFERED,
        SourcingStatus.QUOTED,
        SourcingStatus.WON,
        SourcingStatus.LOST,
        SourcingStatus.OPEN,
        SourcingStatus.ARCHIVED,
    },
    SourcingStatus.OFFERED: {
        SourcingStatus.QUOTED,
        SourcingStatus.WON,
        SourcingStatus.LOST,
        SourcingStatus.SOURCING,
        SourcingStatus.ARCHIVED,
    },
    SourcingStatus.QUOTED: {
        SourcingStatus.WON,
        SourcingStatus.LOST,
        SourcingStatus.OFFERED,
        SourcingStatus.ARCHIVED,
    },
    SourcingStatus.WON: {SourcingStatus.LOST, SourcingStatus.ARCHIVED},
    SourcingStatus.LOST: {SourcingStatus.OPEN, SourcingStatus.SOURCING, SourcingStatus.ARCHIVED},
    SourcingStatus.ARCHIVED: {SourcingStatus.OPEN},  # re-openable (un-archive)
}


def require_valid_transition(entity_type: str, current_status: str, new_status: str) -> None:
    """Validate a status transition or raise HTTPException 409."""
    try:
        validate_transition(entity_type, current_status, new_status)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


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
